"""Native (embedded-text) PDF extraction via `pypdf`.

Pages are processed independently. A page with no meaningful embedded text
is never assumed to be blank — it's a candidate for OCR (later-phase work)
and is reported back as such via `ocr_required_pages`, never silently
dropped and never given fabricated text. See `ocr_routing.py` for how this
feeds into the overall `WorkerResultV1` status.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.modules.structured_processing.document.text_extractors import (
    enforce_text_limits,
    has_meaningful_text,
)
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.limits import MAX_PDF_PAGES
from app.modules.structured_processing.models import TextSegment


@dataclass(frozen=True)
class PdfExtractionResult:
    """Per-page extraction outcome for one PDF."""

    segments: list[TextSegment]  # one per page with meaningful embedded text
    ocr_required_pages: list[int]  # 1-based page numbers with no meaningful embedded text
    total_pages: int


def extract_pdf(data: bytes) -> PdfExtractionResult:
    """Extract embedded text per page.

    Raises `encrypted_pdf_unsupported` for any password-protected PDF (no
    password is ever attempted, including an empty one — that would still
    be password guessing), `invalid_pdf` for a PDF that can't be parsed at
    all, and `input_limit_exceeded` for a PDF over `MAX_PDF_PAGES`.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ProcessingError(
                ErrorCode.ENCRYPTED_PDF_UNSUPPORTED,
                "PDF is password-protected; encrypted PDFs are not processed in this phase",
            )
        total_pages = len(reader.pages)
    except ProcessingError:
        raise
    except PdfReadError as exc:
        raise ProcessingError(ErrorCode.INVALID_PDF, "PDF could not be parsed") from exc

    if total_pages == 0:
        raise ProcessingError(ErrorCode.INVALID_PDF, "PDF contains no pages")
    if total_pages > MAX_PDF_PAGES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"PDF exceeds the {MAX_PDF_PAGES}-page limit"
        )

    segments: list[TextSegment] = []
    ocr_required_pages: list[int] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pypdf can raise a variety of parse errors per-page
            raise ProcessingError(
                ErrorCode.INVALID_PDF, f"page {page_number} could not be read"
            ) from exc

        if has_meaningful_text(text):
            segments.append(TextSegment(page=page_number, text=text))
        else:
            ocr_required_pages.append(page_number)

    enforce_text_limits(segments)
    return PdfExtractionResult(
        segments=segments, ocr_required_pages=ocr_required_pages, total_pages=total_pages
    )
