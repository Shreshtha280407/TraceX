"""Generic social-intelligence JSON record export parsing.

Documented accepted shape:

```json
{
  "platform": "generic",
  "records": [
    {
      "message_id": "m1",
      "conversation_id": "c1",
      "sender": "alice",
      "participants": ["alice", "bob"],
      "timestamp": "2026-01-01T10:00:00Z",
      "text": "hi",
      "reply_to": null
    }
  ]
}
```

`"timestamp"` is converted to UTC directly when it is a valid ISO-8601
string carrying an explicit offset or `Z` suffix; otherwise, if it is a
naive ISO-8601 string, it is interpreted in
`Settings.communication_default_timezone` (see `social/common.py`'s
documented timezone policy). A string that isn't valid ISO-8601 at all
leaves `timestamp_utc` as `None`; `timestamp_raw` always preserves the
original value regardless.
"""

from __future__ import annotations

import json
from typing import Any

from app.contracts.common import SourceLocator
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_CHAT_EXPORT_BYTES,
    MAX_CHAT_JSON_DEPTH,
    MAX_CHAT_MESSAGES,
    MAX_CHAT_PARTICIPANTS,
)
from app.modules.communication_processing.social.common import (
    ChatMessageRecord,
    resolve_naive_or_explicit_iso,
)


def _check_depth(node: Any, depth: int) -> None:
    if depth > MAX_CHAT_JSON_DEPTH:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            f"JSON exceeds the {MAX_CHAT_JSON_DEPTH}-level nesting limit",
        )
    if isinstance(node, dict):
        for value in node.values():
            _check_depth(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _check_depth(value, depth + 1)


def parse_generic_json_export(data: bytes) -> list[ChatMessageRecord]:
    """Parse a generic social-intelligence JSON record export into `ChatMessageRecord`s.

    Raises `input_limit_exceeded` for oversized/over-nested/too-many-
    record input, and `malformed_chat_export` for anything not matching
    the documented shape.
    """
    if len(data) > MAX_CHAT_EXPORT_BYTES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"export exceeds the {MAX_CHAT_EXPORT_BYTES}-byte limit"
        )

    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessingError(ErrorCode.MALFORMED_CHAT_EXPORT, "export is not valid JSON") from exc

    _check_depth(parsed, 0)

    if not isinstance(parsed, dict) or not isinstance(parsed.get("records"), list):
        raise ProcessingError(
            ErrorCode.MALFORMED_CHAT_EXPORT, "expected a top-level object with a 'records' array"
        )

    platform_value = parsed.get("platform")
    platform = platform_value if isinstance(platform_value, str) else "generic_json"
    records: list[Any] = parsed["records"]
    if len(records) > MAX_CHAT_MESSAGES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"export exceeds the {MAX_CHAT_MESSAGES}-record limit"
        )

    result: list[ChatMessageRecord] = []
    for index, entry in enumerate(records):
        if not isinstance(entry, dict):
            continue

        participants_raw = entry.get("participants")
        participants: tuple[str, ...] = ()
        if isinstance(participants_raw, list):
            if len(participants_raw) > MAX_CHAT_PARTICIPANTS:
                raise ProcessingError(
                    ErrorCode.INPUT_LIMIT_EXCEEDED,
                    f"record {index} exceeds the {MAX_CHAT_PARTICIPANTS}-participant limit",
                )
            participants = tuple(p for p in participants_raw if isinstance(p, str))

        timestamp_raw = entry.get("timestamp")
        timestamp_raw = timestamp_raw if isinstance(timestamp_raw, str) else None
        resolved_timestamp = (
            resolve_naive_or_explicit_iso(timestamp_raw) if timestamp_raw is not None else None
        )
        timestamp_utc, timestamp_source_timezone = resolved_timestamp or (None, None)

        text = entry.get("text")
        message_id = entry.get("message_id")
        reply_to = entry.get("reply_to")

        result.append(
            ChatMessageRecord(
                platform=platform,
                conversation_id=entry.get("conversation_id")
                if isinstance(entry.get("conversation_id"), str)
                else None,
                message_id=str(message_id) if message_id is not None else None,
                sender=entry.get("sender") if isinstance(entry.get("sender"), str) else None,
                participants=participants,
                timestamp_raw=timestamp_raw,
                timestamp_utc=timestamp_utc,
                timestamp_source_timezone=timestamp_source_timezone,
                text=text if isinstance(text, str) and text else None,
                reply_to=str(reply_to) if reply_to is not None else None,
                locator=SourceLocator(json_path=f"$.records[{index}]"),
            )
        )

    if not result:
        raise ProcessingError(ErrorCode.MALFORMED_CHAT_EXPORT, "no record entries found in export")

    return result
