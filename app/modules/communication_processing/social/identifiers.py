"""Deterministic regex-based extraction of mentioned identifiers from chat message text.

Mirrors `app.modules.structured_processing.document.fir_report`'s
regex-only extraction approach (own copy, not imported -- this module's
established independently-buildable-module convention): no NLP, no LLMs,
no fuzzy matching. Every pattern requires a structurally-distinctive
format (an Indian mobile number, an email address, an `http(s)://`/`www.`
URL, an `@handle` mention); a value with no such signal is never guessed
at.

Each match becomes exactly one `RawMention` of observation type
`phone_number`, `email_address`, `url`, or `username_or_handle` (the
concrete kinds of "mentioned identifier" this module's contract brief
approves -- see `docs/architecture/communication-processing.md`). Every
such mention is an **extracted claim about text that appears in one
message** -- never a verified identity, never a graph edge, and never
merged with any entity. It intentionally reuses that message's own
`ChatMessageRecord.locator` (the message *is* the accurate source
location; there is nothing more precise inside one message's text to
independently anchor a locator to). `worker.py::_observations_for`
disambiguates any resulting `(observation_type, locator)` collisions
(e.g. two phone numbers quoted in one message) with a positional
discriminator so every observation still gets a distinct, stable
`observation_id`.

Because each pattern below runs independently over the same message
text, two different observation types may legitimately reference
overlapping or identical substrings (e.g. an email address embedded in a
matched URL) -- this module never attempts cross-pattern overlap
resolution, exactly like `fir_report.py`'s own precedent; each pattern's
output stands on its own.

Documented, known limitations (see docs/qa/known-limitations.md): Indian
mobile numbers only (no landline/international formats, same limitation
as `fir_report.py`); a handle is any bare `@name` token, with no
platform-specific username-format validation; a URL match is a
whitespace-delimited span starting with `http(s)://` or `www.`, not a
strict RFC 3986 parse.
"""

from __future__ import annotations

import re

from app.modules.communication_processing.limits import (
    MAX_IDENTIFIER_MATCHES_PER_MESSAGE,
    MAX_IDENTIFIER_TEXT_LENGTH,
)
from app.modules.communication_processing.models import RawMention
from app.modules.communication_processing.provenance import CONFIDENCE_REGEX_EXACT_MATCH
from app.modules.communication_processing.social.common import ChatMessageRecord

# Indian mobile numbers only: 10 digits starting 6-9, optional +91/0 prefix
# -- same documented limitation as `structured_processing.document.fir_report`.
_PHONE_NUMBER = re.compile(r"(?:\+91[-\s]?|0)?([6-9]\d{9})\b")
_EMAIL_ADDRESS = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_URL = re.compile(r"\b((?:https?://|www\.)[^\s<>\"']+)", re.IGNORECASE)
#: Trailing punctuation that's almost always sentence structure, not part of
#: the URL itself (e.g. "check http://example.com." at a sentence's end).
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}\"'"
# A negative lookbehind excludes the "@" already consumed by an email
# address's local-part/domain boundary (preceded by a word character).
_USERNAME_OR_HANDLE = re.compile(r"(?<![\w.@])(@\w{2,32})\b")

# (observation_type, pattern, match_kind)
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("phone_number", _PHONE_NUMBER, "indian_mobile"),
    ("email_address", _EMAIL_ADDRESS, "email"),
    ("url", _URL, "http_or_www_url"),
    ("username_or_handle", _USERNAME_OR_HANDLE, "at_mention_handle"),
)


def extract_mentioned_identifiers(record: ChatMessageRecord) -> list[RawMention]:
    """Run every deterministic identifier pattern over one message's text.

    Returns `[]` for a message with no text (e.g. an attachment-only
    message) -- never an error; this is a normal, safe outcome. Bounded by
    `MAX_IDENTIFIER_MATCHES_PER_MESSAGE` per observation type so a
    pathological message (e.g. one crafted to repeat a matchable token
    thousands of times) can't inflate one message into an unbounded
    number of observations.
    """
    if not record.text:
        return []

    mentions: list[RawMention] = []
    for observation_type, pattern, match_kind in _PATTERNS:
        match_count = 0
        for match in pattern.finditer(record.text):
            value = match.group(1).strip()[:MAX_IDENTIFIER_TEXT_LENGTH]
            if observation_type == "url":
                value = value.rstrip(_URL_TRAILING_PUNCTUATION)
            if not value:
                continue
            match_count += 1
            if match_count > MAX_IDENTIFIER_MATCHES_PER_MESSAGE:
                break
            mentions.append(
                RawMention(
                    observation_type=observation_type,
                    text=value,
                    locator=record.locator,
                    confidence=CONFIDENCE_REGEX_EXACT_MATCH,
                    attributes={
                        "match_kind": match_kind,
                        "platform": record.platform,
                        "conversation_id": record.conversation_id,
                        "message_id": record.message_id,
                    },
                    event_time=record.timestamp_utc,
                )
            )
    return mentions


__all__ = ["extract_mentioned_identifiers"]
