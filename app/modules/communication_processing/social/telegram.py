"""Telegram Desktop JSON export parsing ("Export chat history" -> `result.json`).

Documented accepted shape:

```json
{
  "name": "Chat Name",
  "id": 123456789,
  "messages": [
    {
      "id": 1001,
      "type": "message",
      "date": "2026-01-01T10:00:00",
      "date_unixtime": "1767261600",
      "from": "Alice",
      "from_id": "user123",
      "text": "Hello",
      "reply_to_message_id": 1000
    }
  ]
}
```

`"type": "service"` entries (group name changes, pins, ...) are skipped —
not an error, just not a message. Only plain-string `"text"` is supported;
Telegram's rich-text entity-array form is treated as `text_present=False`
rather than rejecting the whole export (documented limitation).

Telegram's `"date"` field is local/naive with no timezone — never
converted to UTC. `"date_unixtime"` is a Unix epoch value and therefore
unambiguous UTC *by construction*; this is used as the authoritative
`timestamp_utc` source specifically because it carries no ambiguity to
guess at, not because `"date"` was reinterpreted.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.contracts.common import SourceLocator
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_CHAT_EXPORT_BYTES,
    MAX_CHAT_MESSAGES,
)
from app.modules.communication_processing.social.common import (
    ChatMessageRecord,
    utc_from_unix_seconds,
)


def _parse_date_unixtime(value: object) -> datetime | None:
    """`date_unixtime` may be a numeric string or a number; either is unambiguous UTC."""
    if isinstance(value, str) and value.isdigit():
        return utc_from_unix_seconds(float(value))
    if isinstance(value, int | float):
        return utc_from_unix_seconds(float(value))
    return None


def parse_telegram_export(data: bytes) -> list[ChatMessageRecord]:
    """Parse a Telegram JSON export into `ChatMessageRecord`s.

    Raises `input_limit_exceeded` for oversized input or too many message
    entries, and `malformed_chat_export` for anything not matching the
    documented shape.
    """
    if len(data) > MAX_CHAT_EXPORT_BYTES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"export exceeds the {MAX_CHAT_EXPORT_BYTES}-byte limit"
        )

    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(ErrorCode.MALFORMED_CHAT_EXPORT, "export is not valid JSON") from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("messages"), list):
        raise ProcessingError(
            ErrorCode.MALFORMED_CHAT_EXPORT, "expected a top-level object with a 'messages' array"
        )

    conversation_id = str(parsed["id"]) if "id" in parsed else None
    messages: list[Any] = parsed["messages"]
    if len(messages) > MAX_CHAT_MESSAGES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"export exceeds the {MAX_CHAT_MESSAGES}-message limit"
        )

    records: list[ChatMessageRecord] = []
    for index, entry in enumerate(messages):
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue

        text = entry.get("text")
        text = text if isinstance(text, str) and text else None

        timestamp_utc = _parse_date_unixtime(entry.get("date_unixtime"))

        reply_to = entry.get("reply_to_message_id")
        message_id = entry.get("id")

        records.append(
            ChatMessageRecord(
                platform="telegram",
                conversation_id=conversation_id,
                message_id=str(message_id) if message_id is not None else None,
                sender=entry.get("from") if isinstance(entry.get("from"), str) else None,
                participants=(),
                timestamp_raw=entry.get("date") if isinstance(entry.get("date"), str) else None,
                timestamp_utc=timestamp_utc,
                text=text,
                reply_to=str(reply_to) if reply_to is not None else None,
                locator=SourceLocator(json_path=f"$.messages[{index}]"),
            )
        )

    if not records:
        raise ProcessingError(
            ErrorCode.MALFORMED_CHAT_EXPORT, "no message-type entries found in export"
        )

    return records
