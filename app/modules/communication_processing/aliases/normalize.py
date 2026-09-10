"""Conservative, deterministic Unicode/whitespace normalization for alias text.

Every function here is a pure, total transformation with no external
lookups, models, or corpora — the same input always produces the same
output. Nothing here merges, resolves, or asserts that two normalized
strings refer to the same real-world person; see `docs/architecture/
multilingual-alias-candidates-v1.md`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

# NFC (Canonical Composition) is the sole normalization form used
# throughout this module. Chosen over NFD/NFKC/NFKD: NFC preserves the
# canonical precomposed form (matching how most real-world text — and
# most Devanagari/Gurmukhi input methods — already produce text) without
# the *compatibility* folding NFKC/NFKD apply, which can collapse
# meaningfully distinct characters (e.g. ligatures, width variants) into a
# shared target — too aggressive for a conservative comparison utility.
UNICODE_NORMALIZATION_FORM: Literal["NFC"] = "NFC"

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_unicode(text: str) -> str:
    """Apply NFC normalization. Deterministic, stable, and idempotent."""
    return unicodedata.normalize(UNICODE_NORMALIZATION_FORM, text)


def normalize_whitespace(text: str) -> str:
    """Collapse internal whitespace runs to a single space and strip the ends."""
    return _WHITESPACE_RUN.sub(" ", text).strip()


def comparison_key(text: str) -> str:
    """A stable, case-folded, whitespace-normalized comparison key.

    Used only for *exact* comparison after normalization (the
    `exact_normalized` alias-candidate category) — never a fuzzy or
    edit-distance match.
    """
    return normalize_whitespace(normalize_unicode(text)).casefold()


def safe_tokenize(text: str) -> list[str]:
    """Split NFC-normalized text into whitespace-delimited tokens.

    Deliberately simple and Unicode-whitespace-aware (`str.split()` with
    no argument splits on any Unicode whitespace run and discards empty
    tokens) — no locale-specific word segmentation, no external tokenizer.
    """
    return normalize_unicode(text).split()
