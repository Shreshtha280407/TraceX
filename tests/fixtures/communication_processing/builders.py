"""Synthetic, non-sensitive fixture builders for communication-processing tests."""

from __future__ import annotations

import io
import json
import struct
import wave


def build_wav_bytes(
    *,
    duration_seconds: float = 1.0,
    sample_rate: int = 8000,
    channels: int = 1,
    sample_width: int = 2,
    amplitude: int = 0,
) -> bytes:
    """A real, valid, silent PCM WAV file of the given duration."""
    frame_count = round(duration_seconds * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        if amplitude and sample_width == 2:
            frame = struct.pack("<h", amplitude) * channels
            writer.writeframes(frame * frame_count)
        else:
            writer.writeframes(b"\x00" * (frame_count * channels * sample_width))
    return buf.getvalue()


def truncate_wav_bytes(data: bytes, *, keep_bytes: int) -> bytes:
    """Cut a valid WAV short, keeping its header intact but its declared
    frame count no longer backed by enough actual bytes."""
    return data[:keep_bytes]


def build_fake_mp3_bytes() -> bytes:
    return b"ID3" + b"\x00" * 64


def build_fake_m4a_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64


def build_fake_ogg_bytes() -> bytes:
    return b"OggS" + b"\x00" * 64


def build_whatsapp_export(lines: list[str]) -> bytes:
    """Join pre-formatted WhatsApp export lines (caller supplies the exact
    `DD/MM/YY, HH:MM - Sender: text` shape where relevant)."""
    return "\n".join(lines).encode("utf-8")


def build_telegram_export(
    *,
    chat_id: int = 1001,
    messages: list[dict[str, object]] | None = None,
) -> bytes:
    payload = {
        "name": "Synthetic Test Chat",
        "type": "personal_chat",
        "id": chat_id,
        "messages": messages if messages is not None else [],
    }
    return json.dumps(payload).encode("utf-8")


def build_instagram_export(
    *,
    thread_path: str = "inbox/synthetic_thread",
    participants: list[str] | None = None,
    messages: list[dict[str, object]] | None = None,
) -> bytes:
    payload = {
        "thread_path": thread_path,
        "participants": [{"name": name} for name in (participants or ["alice", "bob"])],
        "messages": messages if messages is not None else [],
    }
    return json.dumps(payload).encode("utf-8")


def build_generic_json_export(
    *,
    platform: str = "generic",
    records: list[dict[str, object]] | None = None,
) -> bytes:
    payload = {"platform": platform, "records": records if records is not None else []}
    return json.dumps(payload).encode("utf-8")
