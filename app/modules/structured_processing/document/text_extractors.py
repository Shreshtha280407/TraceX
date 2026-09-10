"""Shared helpers for text-bearing document extraction (PDF/DOCX/TXT).

Kept separate from the per-format modules because both the "is this text
meaningful or is it a scan?" heuristic and the overall text-length limit
apply identically regardless of which format produced the text.
"""

from __future__ import annotations

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.limits import MAX_TEXT_LENGTH
from app.modules.structured_processing.models import TextSegment

# A page/segment with fewer than this many non-whitespace characters is
# treated as having no meaningful embedded text (e.g. a scanned page with
# only a stray OCR artifact or watermark string baked into the PDF).
# ponytail: fixed heuristic threshold, not tuned against a real corpus —
# revisit if real-world scanned/native PDFs need a different cutoff.
MEANINGFUL_TEXT_MIN_CHARS = 20


def has_meaningful_text(text: str) -> bool:
    """Whether `text` is substantial enough to be real embedded content, not scan noise."""
    return len(text.strip()) >= MEANINGFUL_TEXT_MIN_CHARS


def enforce_text_limits(segments: list[TextSegment]) -> None:
    """Raise `input_limit_exceeded` if the combined extracted text is too large."""
    total = sum(len(segment.text) for segment in segments)
    if total > MAX_TEXT_LENGTH:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            f"extracted text exceeds the {MAX_TEXT_LENGTH}-byte limit",
        )
