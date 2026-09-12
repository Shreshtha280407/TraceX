"""Native (embedded-text) PDF extraction via `pypdf`, plus page rendering for OCR.

Every page is classified by `page_trust.classify_page_trust` before it is
trusted: `segments` contains embedded text for `TEXT_TRUSTED` pages only —
a page with *some* embedded text that fails the trust checks (a garbled
font/Unicode mapping, for example) is never silently trusted just because
`len(text) > 0`. `ocr_required_pages` lists every page that is a candidate
for OCR (`SCANNED_NO_TEXT` or `UNTRUSTWORTHY_TEXT_LAYER`); see
`ocr_routing.py` for how this feeds into the overall `WorkerResultV1`/batch
flow, and `ocr.py` for the OCR engine itself. A page whose content stream
cannot be read at all is `CORRUPT` — reported explicitly in
`corrupt_pages`, never silently dropped and never aborting the rest of the
document by itself (only a document-level failure — encrypted, or entirely
unparseable — raises).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.modules.structured_processing.document.page_trust import (
    PageTrustAssessment,
    PageTrustLevel,
    classify_page_trust,
)
from app.modules.structured_processing.document.text_extractors import enforce_text_limits
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.limits import MAX_PDF_PAGES
from app.modules.structured_processing.models import TextSegment


@dataclass(frozen=True)
class PdfExtractionResult:
    """Per-page extraction outcome for one PDF."""

    segments: list[TextSegment]  # one per TEXT_TRUSTED page
    ocr_required_pages: list[int]  # 1-based page numbers needing OCR (scanned or untrustworthy)
    total_pages: int
    page_assessments: dict[int, PageTrustAssessment] = field(default_factory=dict)
    corrupt_pages: list[int] = field(default_factory=list)


def extract_pdf(data: bytes) -> PdfExtractionResult:
    """Extract and trust-classify embedded text, page by page.

    Raises `encrypted_pdf_unsupported` for any password-protected PDF (no
    password is ever attempted, including an empty one), `invalid_pdf` for
    a PDF that can't be parsed at the document level at all, and
    `input_limit_exceeded` for a PDF over `MAX_PDF_PAGES`. A single page's
    own extraction failure does *not* raise — it is recorded as `CORRUPT`
    for that page and the rest of the document is still processed.
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
    corrupt_pages: list[int] = []
    page_assessments: dict[int, PageTrustAssessment] = {}

    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - pypdf can raise a variety of parse errors per-page
            corrupt_pages.append(page_number)
            page_assessments[page_number] = PageTrustAssessment(
                level=PageTrustLevel.CORRUPT,
                non_whitespace_chars=0,
                printable_ratio=None,
                token_quality_ratio=None,
                reason="page content stream could not be read",
            )
            continue

        assessment = classify_page_trust(text)
        page_assessments[page_number] = assessment

        if assessment.level is PageTrustLevel.TEXT_TRUSTED:
            segments.append(TextSegment(page=page_number, text=text))
        else:
            ocr_required_pages.append(page_number)

    enforce_text_limits(segments)
    return PdfExtractionResult(
        segments=segments,
        ocr_required_pages=ocr_required_pages,
        total_pages=total_pages,
        page_assessments=page_assessments,
        corrupt_pages=corrupt_pages,
    )


def render_pdf_pages(
    data: bytes, page_numbers: Sequence[int], *, dpi: int = 300
) -> dict[int, object]:
    """Render specific 1-based page numbers of a PDF to raster images (PIL `Image`s), for OCR.

    Uses `pypdfium2` (a self-contained PDF renderer with no external
    system binary of its own) rather than `pdftoppm`/`poppler-utils`, so
    this repository's OCR fallback needs no extra system package beyond
    `tesseract-ocr` itself. Raises `invalid_pdf` if a requested page fails
    to render (distinct from a *text*-extraction failure — `pdf.py`'s
    per-page `CORRUPT` classification is about `pypdf`'s text layer, not
    `pypdfium2`'s rasterizer; a page that fails to render at all is a
    document-level problem, since OCR has no fallback for it either).
    """
    scale = dpi / 72.0
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise ProcessingError(ErrorCode.INVALID_PDF, "PDF could not be rendered for OCR") from exc

    images: dict[int, object] = {}
    try:
        for page_number in page_numbers:
            try:
                page = document[page_number - 1]
                bitmap = page.render(scale=scale)
                images[page_number] = bitmap.to_pil()
            except (pdfium.PdfiumError, IndexError) as exc:
                raise ProcessingError(
                    ErrorCode.INVALID_PDF, f"page {page_number} could not be rendered for OCR"
                ) from exc
    finally:
        document.close()
    return images


__all__ = ["PdfExtractionResult", "extract_pdf", "render_pdf_pages"]
