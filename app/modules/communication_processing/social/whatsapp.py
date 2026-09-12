"""WhatsApp plain-text export parsing.

Supports exactly the standard WhatsApp "Export chat" plain-text shape:

```
DD/MM/YY, HH:MM - Sender Name: Message text
DD/MM/YY, HH:MM - Messages and calls are end-to-end encrypted.
```

A line matching the leading `DD/MM/YY, HH:MM -` prefix starts a new
message; a `Name: ` prefix after the dash identifies the sender (absent
for WhatsApp's own system notices). Any line *not* matching that prefix is
a continuation of the previous message's text (WhatsApp wraps multi-line
messages this way), appended verbatim.

WhatsApp's exported timestamp carries no timezone information at all — it
is always the exporting device's local time. Per this module's documented
timezone policy, a recognizable timestamp is interpreted in
`Settings.communication_default_timezone` (see `social/common.py`) to
produce `timestamp_utc`, with `timestamp_source_timezone` recording that
resolved zone name; `timestamp_raw` always preserves the original,
untouched string. An unrecognized date/time shape leaves `timestamp_utc`
as `None` rather than guessing. No reply-reference marker exists in this
plain-text format, so `reply_to` is always `None`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.contracts.common import SourceLocator
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_CHAT_EXPORT_BYTES,
    MAX_CHAT_LINES,
    MAX_CHAT_MESSAGES,
)
from app.modules.communication_processing.social.common import (
    ChatMessageRecord,
    default_timezone_name,
    utc_from_naive_local,
)

_LINE_PATTERN = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4}), (?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(?P<rest>.*)$"
)
_SENDER_PATTERN = re.compile(r"^(?P<sender>[^:\n]{1,100}?):\s(?P<text>.*)$")

#: Tried in order against the `"DD/MM/YY[YY], H:MM[:SS]"` shape `_LINE_PATTERN`
#: extracts -- a value matching none of these is a documented extraction
#: limit, not a guess.
_TIMESTAMP_FORMATS = (
    "%d/%m/%y, %H:%M:%S",
    "%d/%m/%y, %H:%M",
    "%d/%m/%Y, %H:%M:%S",
    "%d/%m/%Y, %H:%M",
)


def _parse_whatsapp_timestamp(raw: str) -> tuple[datetime, str] | None:
    """Resolve a WhatsApp timestamp string to `(timestamp_utc, timestamp_source_timezone)`.

    Always the configured default timezone -- WhatsApp's plain-text export
    never carries any timezone signal of its own -- or `None` if `raw`
    doesn't match any documented format.
    """
    for fmt in _TIMESTAMP_FORMATS:
        try:
            naive = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return utc_from_naive_local(naive), default_timezone_name()
    return None


@dataclass
class _PendingMessage:
    timestamp_raw: str
    sender: str | None
    text: str
    start_offset: int
    end_offset: int


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_whatsapp_export(
    data: bytes, *, conversation_id: str | None = None
) -> list[ChatMessageRecord]:
    """Parse a WhatsApp plain-text export into `ChatMessageRecord`s.

    Raises `input_limit_exceeded` for oversized input (bytes, lines, or
    message count) and `malformed_chat_export` if the export contains not
    a single recognizable timestamped line.
    """
    if len(data) > MAX_CHAT_EXPORT_BYTES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"export exceeds the {MAX_CHAT_EXPORT_BYTES}-byte limit"
        )

    text = _decode(data)
    lines = text.splitlines(keepends=True)
    if len(lines) > MAX_CHAT_LINES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"export exceeds the {MAX_CHAT_LINES}-line limit"
        )

    pending: list[_PendingMessage] = []
    current: _PendingMessage | None = None
    offset = 0

    for line in lines:
        stripped_line = line.rstrip("\r\n")
        match = _LINE_PATTERN.match(stripped_line)
        if match is not None:
            if current is not None:
                current.end_offset = offset
                pending.append(current)
                if len(pending) > MAX_CHAT_MESSAGES:
                    raise ProcessingError(
                        ErrorCode.INPUT_LIMIT_EXCEEDED,
                        f"export exceeds the {MAX_CHAT_MESSAGES}-message limit",
                    )
            rest = match.group("rest")
            sender_match = _SENDER_PATTERN.match(rest)
            sender, message_text = (
                (sender_match.group("sender"), sender_match.group("text"))
                if sender_match is not None
                else (None, rest)
            )
            current = _PendingMessage(
                timestamp_raw=f"{match.group('date')}, {match.group('time')}",
                sender=sender,
                text=message_text,
                start_offset=offset,
                end_offset=offset,
            )
        elif current is not None:
            current.text = f"{current.text}\n{stripped_line}"
        offset += len(line)

    if current is not None:
        current.end_offset = offset
        pending.append(current)

    if not pending:
        raise ProcessingError(
            ErrorCode.MALFORMED_CHAT_EXPORT, "no recognizable WhatsApp message line found"
        )

    records: list[ChatMessageRecord] = []
    for message in pending:
        resolved = _parse_whatsapp_timestamp(message.timestamp_raw)
        timestamp_utc, timestamp_source_timezone = (
            resolved if resolved is not None else (None, None)
        )
        records.append(
            ChatMessageRecord(
                platform="whatsapp",
                conversation_id=conversation_id,
                message_id=None,
                sender=message.sender,
                participants=(),
                timestamp_raw=message.timestamp_raw,
                timestamp_utc=timestamp_utc,
                timestamp_source_timezone=timestamp_source_timezone,
                text=message.text,
                reply_to=None,
                locator=SourceLocator(span_start=message.start_offset, span_end=message.end_offset),
            )
        )
    return records
