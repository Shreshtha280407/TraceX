"""Per-page PDF text-layer trust classification.

Every embedded-text PDF page is classified into exactly one of four
outcomes before any downstream extraction runs against it:

- `TEXT_TRUSTED`: the embedded text layer is substantial and looks like
  real, readable text. Extraction runs directly against it; OCR never
  runs for this page.
- `SCANNED_NO_TEXT`: no meaningful embedded text at all (a scanned/
  image-only page, or a genuinely blank one). A candidate for OCR.
- `UNTRUSTWORTHY_TEXT_LAYER`: *some* embedded text exists, but it fails
  one or more quality checks below (a common symptom of a bad PDF
  producer's font-to-Unicode mapping, or a partially-OCR'd source that
  was re-saved with a broken text layer). Also a candidate for OCR —
  never blindly trusted just because `len(text) > 0`.
- `CORRUPT`: this specific page's content stream could not be read at all
  (`pypdf` raised while extracting it). Reported as a safe, explicit
  outcome for that one page — the rest of the document is still
  processed; a corrupt page never silently vanishes from the result and
  never aborts the whole document by itself (see `pdf.py`).

None of these checks are tuned against a large real-world corpus (see
`docs/qa/known-limitations.md`) — they are fixed, documented heuristics
applied consistently, not a trained classifier.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from app.core.canonical import canonical_sha256
from app.modules.structured_processing.document.text_extractors import (
    MEANINGFUL_TEXT_MIN_CHARS,
)

#: Below this fraction of "printable" (non-control, non-replacement)
#: characters, a non-empty text layer is judged corrupted rather than real
#: readable content (a common symptom of a broken font/Unicode mapping).
MIN_PRINTABLE_RATIO = 0.85

#: Below this fraction of "plausible" whitespace-separated tokens (each
#: containing at least one letter or digit), a text layer is judged too
#: fragmented/garbled to trust (e.g. mostly single stray symbols).
MIN_TOKEN_QUALITY_RATIO = 0.6

#: Unicode's own "no glyph available" sentinel. Any occurrence at all in an
#: embedded text layer is a strong, unambiguous corruption signal.
_REPLACEMENT_CHAR = "�"

PAGE_TRUST_CONFIG: dict[str, object] = {
    "meaningful_text_min_chars": MEANINGFUL_TEXT_MIN_CHARS,
    "min_printable_ratio": MIN_PRINTABLE_RATIO,
    "min_token_quality_ratio": MIN_TOKEN_QUALITY_RATIO,
}


def page_trust_config_hash() -> str:
    return canonical_sha256(PAGE_TRUST_CONFIG)


class PageTrustLevel(StrEnum):
    """The routing outcome for one PDF page's embedded text layer."""

    TEXT_TRUSTED = "text_trusted"
    SCANNED_NO_TEXT = "scanned_no_text"
    UNTRUSTWORTHY_TEXT_LAYER = "untrustworthy_text_layer"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class PageTrustAssessment:
    """The classification outcome plus the safe, non-content quality indicators behind it."""

    level: PageTrustLevel
    non_whitespace_chars: int
    printable_ratio: float | None
    token_quality_ratio: float | None
    reason: str


def classify_page_trust(text: str) -> PageTrustAssessment:
    """Classify one page's already-extracted embedded text.

    Never called for a page whose extraction itself raised — that page is
    `CORRUPT` and is assessed by the caller (`pdf.py`) without calling this
    function at all.
    """
    non_whitespace = len(text.strip())
    if non_whitespace < MEANINGFUL_TEXT_MIN_CHARS:
        return PageTrustAssessment(
            level=PageTrustLevel.SCANNED_NO_TEXT,
            non_whitespace_chars=non_whitespace,
            printable_ratio=None,
            token_quality_ratio=None,
            reason=(
                f"only {non_whitespace} non-whitespace characters (< {MEANINGFUL_TEXT_MIN_CHARS})"
            ),
        )

    printable_ratio = _printable_ratio(text)
    if printable_ratio < MIN_PRINTABLE_RATIO:
        return PageTrustAssessment(
            level=PageTrustLevel.UNTRUSTWORTHY_TEXT_LAYER,
            non_whitespace_chars=non_whitespace,
            printable_ratio=printable_ratio,
            token_quality_ratio=None,
            reason=f"printable-character ratio {printable_ratio:.2f} < {MIN_PRINTABLE_RATIO}",
        )

    token_quality_ratio = _token_quality_ratio(text)
    if token_quality_ratio < MIN_TOKEN_QUALITY_RATIO:
        return PageTrustAssessment(
            level=PageTrustLevel.UNTRUSTWORTHY_TEXT_LAYER,
            non_whitespace_chars=non_whitespace,
            printable_ratio=printable_ratio,
            token_quality_ratio=token_quality_ratio,
            reason=f"token-quality ratio {token_quality_ratio:.2f} < {MIN_TOKEN_QUALITY_RATIO}",
        )

    return PageTrustAssessment(
        level=PageTrustLevel.TEXT_TRUSTED,
        non_whitespace_chars=non_whitespace,
        printable_ratio=printable_ratio,
        token_quality_ratio=token_quality_ratio,
        reason="passed all text-layer trust checks",
    )


def _printable_ratio(text: str) -> float:
    """Fraction of characters that are real printable content, not control/replacement chars.

    Whitespace counts as printable (it's normal, expected content); the
    Unicode replacement character and other C0/C1 control characters
    (other than whitespace) do not.
    """
    if not text:
        return 1.0
    printable = sum(
        1
        for ch in text
        if ch != _REPLACEMENT_CHAR and (ch.isspace() or unicodedata.category(ch) != "Cc")
    )
    return printable / len(text)


def _token_quality_ratio(text: str) -> float:
    """Fraction of whitespace-separated tokens containing at least one letter or digit."""
    tokens = text.split()
    if not tokens:
        return 0.0
    plausible = sum(1 for token in tokens if any(ch.isalnum() for ch in token))
    return plausible / len(tokens)


__all__ = [
    "MEANINGFUL_TEXT_MIN_CHARS",
    "MIN_PRINTABLE_RATIO",
    "MIN_TOKEN_QUALITY_RATIO",
    "PAGE_TRUST_CONFIG",
    "PageTrustAssessment",
    "PageTrustLevel",
    "classify_page_trust",
    "page_trust_config_hash",
]
