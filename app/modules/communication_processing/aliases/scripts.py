"""Deterministic Unicode-block-based script detection.

Classifies the letter characters in a string into one of a fixed set of
scripts by Unicode code-point range only — no ML, no external library, no
corpus. Supports Latin, Devanagari, Gurmukhi explicitly (this task's
minimum), plus `MIXED` (letters from more than one recognized/unrecognized
script present) and `UNKNOWN` (no letters from any recognized script —
pure digits/punctuation, or an unsupported script).
"""

from __future__ import annotations

from enum import StrEnum

# Devanagari block: U+0900-U+097F. Gurmukhi block: U+0A00-U+0A7F.
_DEVANAGARI_RANGE = (0x0900, 0x097F)
_GURMUKHI_RANGE = (0x0A00, 0x0A7F)
# Basic Latin letters + Latin-1 Supplement letters + Latin Extended-A/B.
_LATIN_RANGES = ((0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F))


class Script(StrEnum):
    """A detected script, or a sentinel for mixed/unrecognized text."""

    LATIN = "latin"
    DEVANAGARI = "devanagari"
    GURMUKHI = "gurmukhi"
    MIXED = "mixed"
    UNKNOWN = "unknown"


def _char_script(ch: str) -> Script | None:
    """Classify one letter, or `None` if it's not in a recognized script's range."""
    codepoint = ord(ch)
    if _DEVANAGARI_RANGE[0] <= codepoint <= _DEVANAGARI_RANGE[1]:
        return Script.DEVANAGARI
    if _GURMUKHI_RANGE[0] <= codepoint <= _GURMUKHI_RANGE[1]:
        return Script.GURMUKHI
    if any(low <= codepoint <= high for low, high in _LATIN_RANGES):
        return Script.LATIN
    return None


def detect_script(text: str) -> Script:
    """Detect the script of `text` from its letter characters only.

    Digits, punctuation, and whitespace carry no script information and
    are ignored. Returns `UNKNOWN` for text with no recognized-script
    letters at all (including text made only of digits/punctuation, or of
    an unsupported script), and `MIXED` when letters from more than one
    distinct script (recognized, unrecognized, or both) are present.
    """
    recognized: set[Script] = set()
    has_unrecognized_letter = False

    for ch in text:
        if not ch.isalpha():
            continue
        script = _char_script(ch)
        if script is not None:
            recognized.add(script)
        else:
            has_unrecognized_letter = True

    if not recognized:
        return Script.UNKNOWN
    if len(recognized) == 1 and not has_unrecognized_letter:
        return next(iter(recognized))
    return Script.MIXED
