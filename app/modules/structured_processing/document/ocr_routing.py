"""Scanned/image-only page routing decision for PDFs.

Turns a per-page `PdfExtractionResult` into a routing decision: fully
processed, fully deferred to OCR (a real OCR engine is later-phase work —
see CLAUDE.md non-goals), or partially deferred (some pages had usable
embedded text, others need OCR). The checkpoint is a small, deterministic
JSON string — never the page image or any fabricated text — recording
exactly which pages still need OCR, so a later-phase OCR worker knows
precisely what's left to do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.modules.structured_processing.document.pdf import PdfExtractionResult
from app.modules.structured_processing.errors import ErrorCode


@dataclass(frozen=True)
class OcrRoutingDecision:
    """Whether (and how much of) this PDF needs OCR before it's fully processed."""

    requires_ocr: bool
    fully_requires_ocr: bool  # every page needs OCR; no embedded text anywhere
    deferred_pages: tuple[int, ...]
    checkpoint: str | None  # deterministic JSON; None when no OCR is needed


def route_pdf(result: PdfExtractionResult) -> OcrRoutingDecision:
    """Decide OCR routing for a PDF from its per-page extraction result."""
    if not result.ocr_required_pages:
        return OcrRoutingDecision(
            requires_ocr=False, fully_requires_ocr=False, deferred_pages=(), checkpoint=None
        )

    fully = len(result.ocr_required_pages) == result.total_pages
    checkpoint = json.dumps(
        {
            "reason": ErrorCode.DOCUMENT_REQUIRES_OCR,
            "deferred_pages": list(result.ocr_required_pages),
            "total_pages": result.total_pages,
        },
        sort_keys=True,
    )
    return OcrRoutingDecision(
        requires_ocr=True,
        fully_requires_ocr=fully,
        deferred_pages=tuple(result.ocr_required_pages),
        checkpoint=checkpoint,
    )
