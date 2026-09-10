"""Shared types and helpers for social/chat export parsing.

`ChatMessageRecord` is the common intermediate shape every platform parser
(`whatsapp.py`, `telegram.py`, `instagram.py`, `json_records.py`) produces
before conversion to a `RawMention`/`ObservationV1` — so `worker.py` and
`linking/deterministic.py` never need to know which platform a message
came from.

Timezone policy (enforced here, used by every parser): a timestamp is
normalized to UTC (`timestamp_utc`) *only* when its source is unambiguous
by construction — an explicit UTC offset/`Z` suffix, or a Unix epoch value
(which is UTC by definition, there is nothing to guess). A naive/local
timestamp with no such signal is never guessed at: `timestamp_raw` is
always preserved, `timestamp_utc` stays `None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import JsonValue

from app.contracts.common import SourceLocator
from app.modules.communication_processing.limits import MAX_HANDLE_LENGTH, MAX_MESSAGE_TEXT_LENGTH
from app.modules.communication_processing.models import RawMention
from app.modules.communication_processing.provenance import CONFIDENCE_STRUCTURED_COMPLETE

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


def utc_from_iso_with_explicit_offset(raw: str) -> datetime | None:
    """Parse an ISO-8601 string to UTC only if it carries an explicit offset/`Z`.

    Returns `None` (never a guess) for a naive ISO string with no offset.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def chat_message_to_mention(record: ChatMessageRecord) -> RawMention:
    """Convert one `ChatMessageRecord` into a `chat_message` mention.

    Attributes are a small, allow-listed set — never the whole export or
    conversation. `confidence=1.00`: a complete message record read
    directly from a validated export, per this module's documented
    confidence policy.
    """
    attributes: dict[str, JsonValue] = {
        "platform": record.platform,
        "conversation_id": record.conversation_id,
        "message_id": record.message_id,
        "sender": record.sender,
        "participants": list(record.participants),
        "timestamp_raw": record.timestamp_raw,
        "reply_to": record.reply_to,
        "text": truncate_text(record.text),
        "text_present": record.text is not None,
    }
    return RawMention(
        observation_type=OBSERVATION_TYPE,
        text=f"{record.platform}_message",
        locator=record.locator,
        confidence=CONFIDENCE_STRUCTURED_COMPLETE,
        attributes=attributes,
        event_time=record.timestamp_utc,
    )
