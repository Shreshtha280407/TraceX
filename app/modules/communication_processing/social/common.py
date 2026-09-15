"""Shared types and helpers for social/chat export parsing.

`ChatMessageRecord` is the common intermediate shape every platform parser
(`whatsapp.py`, `telegram.py`, `instagram.py`, `json_records.py`) produces
before conversion to a `RawMention`/`ObservationV1` — so `worker.py` and
`linking/deterministic.py` never need to know which platform a message
came from.

Timezone policy (enforced here, used by every parser): a timestamp is
converted to UTC (`timestamp_utc`) using, in order:

1. An explicit, unambiguous signal carried by the source itself — a UTC
   offset/`Z` suffix on an ISO-8601 string, or a Unix epoch value (which is
   UTC by definition) — never guessed, `timestamp_source_timezone` stays
   `None` since there is no assumption to record.
2. Otherwise, `Settings.communication_default_timezone` (default
   `"Asia/Kolkata"`) — a fixed, documented default applied to an
   otherwise-naive local timestamp, mirroring
   `structured_processing.structured.cdr`'s identical policy for CDR/
   finance records. The resolved zone name is recorded in
   `timestamp_source_timezone` precisely so a default-derived UTC value is
   never indistinguishable from a genuinely explicit one.

`timestamp_raw` always preserves the untouched original value regardless
of which path was used. A value that cannot be parsed under any
documented format leaves both `timestamp_utc` and
`timestamp_source_timezone` as `None` — never a silent host-machine-local
guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from app.contracts.common import SourceLocator
from app.core.config import get_settings
from app.modules.communication_processing.aliases.transliteration import (
    TransliterationCandidate,
    generate_candidates_for_tokens,
)
from app.modules.communication_processing.limits import (
    MAX_ALIAS_TOKENS,
    MAX_HANDLE_LENGTH,
    MAX_MESSAGE_TEXT_LENGTH,
)
from app.modules.communication_processing.models import RawMention
from app.modules.communication_processing.provenance import CONFIDENCE_STRUCTURED_COMPLETE
from app.modules.communication_processing.signal_validation import validate_chat_signal

OBSERVATION_TYPE = "chat_message"


@dataclass(frozen=True)
class ChatMessageRecord:
    """One parsed message, in the common shape every platform parser produces.

    A `ChatMessageRecord` is a parsed fact about one message — never itself
    an `EntityV1`, `EventV1`, verified identity, graph relationship, guilt
    claim, or hypothesis.
    """

    platform: str
    conversation_id: str | None
    message_id: str | None
    sender: str | None
    participants: tuple[str, ...]
    timestamp_raw: str | None
    timestamp_utc: datetime | None
    timestamp_source_timezone: str | None
    text: str | None
    reply_to: str | None
    locator: SourceLocator


def truncate_text(text: str | None, *, limit: int = MAX_MESSAGE_TEXT_LENGTH) -> str | None:
    """Bound message text length — an observation never holds an entire export/conversation."""
    if text is None:
        return None
    return text[:limit]


def bounded_handle(value: str | None, *, limit: int = MAX_HANDLE_LENGTH) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped[:limit] if stripped else None


def utc_from_unix_seconds(value: float) -> datetime:
    """A Unix epoch value is unambiguous UTC by construction — nothing to guess."""
    return datetime.fromtimestamp(value, tz=UTC)


def default_timezone_name() -> str:
    """The configured fallback zone for a source timestamp with no explicit signal."""
    return get_settings().communication_default_timezone


def utc_from_naive_local(naive: datetime) -> datetime:
    """Interpret a naive local datetime in the configured default timezone.

    Callers use this only once a timestamp has been confirmed to carry no
    explicit offset/`Z`/epoch signal of its own — see this module's
    documented timezone policy.
    """
    return naive.replace(tzinfo=ZoneInfo(default_timezone_name())).astimezone(UTC)


def resolve_naive_or_explicit_iso(raw: str) -> tuple[datetime, str | None] | None:
    """Resolve one ISO-8601 timestamp string to `(timestamp_utc, timestamp_source_timezone)`.

    An explicit offset/`Z` suffix is unambiguous and used directly --
    `timestamp_source_timezone` is `None`. A naive string is converted
    using the configured default timezone, whose name is then returned as
    `timestamp_source_timezone` so the two cases stay distinguishable.
    Returns `None` (never a guess) if `raw` isn't a valid ISO-8601 string
    at all.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC), None
    return utc_from_naive_local(parsed), default_timezone_name()


def _sender_transliteration_candidates(sender: str) -> list[JsonValue]:
    """Per-word transliteration candidates for a (possibly multi-word) sender name.

    `generate_candidates`/`aliases/transliteration.py`'s algorithm operates
    on one token at a time -- a bare space is not itself a mappable
    character, so a whole multi-word name run through it directly would
    always come back `not_generated`. This splits on whitespace first
    (bounded to `MAX_ALIAS_TOKENS`, truncating rather than failing the
    whole message over a pathological sender field) and returns one
    candidate per word, in original order -- never a synthesized "joined"
    full-name candidate, since that would be a new derived value this
    module's own tested contract doesn't cover.
    """
    tokens = sender.split()[:MAX_ALIAS_TOKENS]
    return [_candidate_attribute(candidate) for candidate in generate_candidates_for_tokens(tokens)]


def _candidate_attribute(candidate: TransliterationCandidate) -> dict[str, JsonValue]:
    """Serialize one `TransliterationCandidate` as a bounded observation attribute.

    Per this module's documented transliteration policy (see
    `aliases/transliteration.py` and
    `docs/architecture/communication-processing.md`): `candidates` are
    review-only normalized/transliterated *representations* of
    `original_text`, never an interchangeable identity, and are never
    attached to `EntityV1.aliases` automatically.
    """
    return {
        "original_text": candidate.original_text,
        "script": candidate.script.value,
        "candidates": list(candidate.candidates),
        "method": candidate.method,
        "method_version": candidate.method_version,
        "category": candidate.category,
        "reason": candidate.reason,
    }


def chat_message_to_mention(record: ChatMessageRecord) -> RawMention:
    """Convert one `ChatMessageRecord` into a `chat_message` mention.

    Attributes are a small, allow-listed set — never the whole export or
    conversation. `confidence=1.00`: a complete message record read
    directly from a validated export, per this module's documented
    confidence policy.

    `sender` is bounded to `MAX_HANDLE_LENGTH` before use (some platform
    parsers read it from an otherwise-unbounded JSON string field) — both
    for the raw attribute value and as input to `generate_candidates`,
    which otherwise rejects text over its own, smaller length limit.
    `sender_transliteration_candidates` is `None` when there's no sender
    at all (e.g. a WhatsApp system notice); the candidates it does carry
    are extraction aids only, per section 7.2's documented policy -- never
    identity resolution, and never auto-attached to any entity's aliases.
    """
    sender = bounded_handle(record.sender)
    participants: list[JsonValue] = [
        participant
        for value in record.participants
        if (participant := bounded_handle(value)) is not None
    ]
    message_text = truncate_text(record.text)
    validation = validate_chat_signal(record)
    attributes: dict[str, JsonValue] = {
        "platform": record.platform,
        "conversation_id": record.conversation_id,
        "message_id": record.message_id,
        "sender": sender,
        "sender_transliteration_candidates": (
            _sender_transliteration_candidates(sender) if sender is not None else None
        ),
        "participants": participants,
        "timestamp_raw": record.timestamp_raw,
        "timestamp_source_timezone": record.timestamp_source_timezone,
        "reply_to": record.reply_to,
        "text_present": record.text is not None,
        # A source record remains reviewable through the protected evidence
        # system; graph-facing canonical attributes carry only a commitment
        # and length, never message/export content.
        "message_text_sha256": (
            sha256(message_text.encode("utf-8")).hexdigest() if message_text is not None else None
        ),
        "message_text_length": len(message_text or ""),
        "communication_signal_validation": validation.attribute_value(),
    }
    return RawMention(
        observation_type=OBSERVATION_TYPE,
        text=f"{record.platform}_message",
        locator=record.locator,
        confidence=CONFIDENCE_STRUCTURED_COMPLETE,
        attributes=attributes,
        event_time=record.timestamp_utc,
    )
