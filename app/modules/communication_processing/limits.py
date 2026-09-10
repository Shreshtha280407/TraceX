"""Documented, enforced input-safety limits for audio/social/alias/link processing.

Mirrors `app.modules.structured_processing.limits`: fixed conservative
constants, not configurable per-request, existing solely to stop a
malformed or hostile input from exhausting memory/CPU before it reaches a
parser.
"""

from __future__ import annotations

# --- Audio ---
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MiB per audio file
MAX_AUDIO_DURATION_SECONDS = 3600.0  # 1 hour

# --- Transcript segments ---
MAX_TRANSCRIPT_SEGMENTS = 20_000
MAX_TRANSCRIPT_SEGMENT_TEXT_LENGTH = 5_000
MAX_TRANSCRIPT_SEGMENT_DURATION_MS = 10 * 60 * 1000  # 10 minutes: a single "segment" longer than
# this is almost certainly a data error, not a real transcript turn.

# --- Diarization segments ---
MAX_DIARIZATION_SEGMENTS = 20_000
MAX_SPEAKER_LABEL_LENGTH = 100

# --- Social/chat exports ---
MAX_CHAT_EXPORT_BYTES = 20 * 1024 * 1024  # 20 MiB per export
MAX_CHAT_LINES = 200_000
MAX_CHAT_MESSAGES = 50_000
MAX_CHAT_JSON_DEPTH = 32
MAX_CHAT_PARTICIPANTS = 2_000
MAX_MESSAGE_TEXT_LENGTH = 4_000
MAX_HANDLE_LENGTH = 300
MAX_TIMESTAMP_PARSE_ATTEMPTS = 5  # documented formats tried, in order, per timestamp value

# --- Alias / transliteration ---
MAX_ALIAS_TEXT_LENGTH = 300
MAX_ALIAS_TOKENS = 32

# --- Communication-link candidates ---
MAX_LINK_CANDIDATES_PER_CALL = 10_000
