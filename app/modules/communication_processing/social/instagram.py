"""Instagram message JSON export parsing (Meta "Download Your Information" shape).

Documented accepted shape:

```json
{
  "thread_path": "inbox/alice_and_bob_123",
  "participants": [{"name": "alice"}, {"name": "bob"}],
  "messages": [
    {"sender_name": "alice", "timestamp_ms": 1767261600000, "content": "Hello there"}
  ]
}
```

`timestamp_ms` is a Unix epoch value in milliseconds and therefore
unambiguous UTC by construction — used directly, nothing guessed. This
basic export shape carries no native message ID or reply-reference field,
so `message_id`/`reply_to` are always `None`; `json_path` alone provides
non-empty source provenance for every emitted observation.
"""

from __future__ import annotations

import json
from typing import Any

from app.contracts.common import SourceLocator
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.limits import (
    MAX_CHAT_EXPORT_BYTES,
    MAX_CHAT_MESSAGES,
    MAX_CHAT_PARTICIPANTS,
)
from app.modules.communication_processing.social.common import (
    ChatMessageRecord,
    utc_from_unix_seconds,
)


def parse_instagram_export(data: bytes) -> list[ChatMessageRecord]:
    """Parse an Instagram message JSON export into `ChatMessageRecord`s.

    Raises `input_limit_exceeded` for oversized input, too many
    participants, or too many message entries, and `malformed_chat_export`
    for anything not matching the documented shape.
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

    conversation_id = (
        parsed.get("thread_path") if isinstance(parsed.get("thread_path"), str) else None
    )

    raw_participants = parsed.get("participants")
    participants: tuple[str, ...] = ()
    if isinstance(raw_participants, list):
        if len(raw_participants) > MAX_CHAT_PARTICIPANTS:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                f"export exceeds the {MAX_CHAT_PARTICIPANTS}-participant limit",
            )
        participants = tuple(
            p["name"]
            for p in raw_participants
            if isinstance(p, dict) and isinstance(p.get("name"), str)
        )

    messages: list[Any] = parsed["messages"]
    if len(messages) > MAX_CHAT_MESSAGES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"export exceeds the {MAX_CHAT_MESSAGES}-message limit"
        )

    records: list[ChatMessageRecord] = []
    for index, entry in enumerate(messages):
        if not isinstance(entry, dict):
            continue

        content = entry.get("content")
        content = content if isinstance(content, str) and content else None

        timestamp_ms = entry.get("timestamp_ms")
        timestamp_utc = (
            utc_from_unix_seconds(timestamp_ms / 1000)
            if isinstance(timestamp_ms, int | float)
            else None
        )

        sender = entry.get("sender_name")

        records.append(
            ChatMessageRecord(
                platform="instagram",
                conversation_id=conversation_id,
                message_id=None,
                sender=sender if isinstance(sender, str) else None,
                participants=participants,
                timestamp_raw=str(timestamp_ms) if timestamp_ms is not None else None,
                timestamp_utc=timestamp_utc,
                text=content,
                reply_to=None,
                locator=SourceLocator(json_path=f"$.messages[{index}]"),
            )
        )

    if not records:
        raise ProcessingError(ErrorCode.MALFORMED_CHAT_EXPORT, "no message entries found in export")

    return records
