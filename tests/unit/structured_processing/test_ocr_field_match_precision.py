"""Scenario 11: OCR + identifier field-match precision, measured on a labelled synthetic fixture.

Builds a real scanned-page fixture with a known, exact set of expected
identifiers (an FIR number, a phone number, a police station name, an
amount), runs the real OCR -> normalization -> regex pipeline this
project's own worker uses, and computes actual precision/recall against
that ground truth -- an honest, reproducible measurement (see
`docs/qa/test-results.md` for the recorded values from an actual run),
never a fabricated or assumed accuracy claim. Self-skips (never fabricates
a pass) if the `tesseract` binary isn't available.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pytesseract import TesseractNotFoundError, get_tesseract_version

from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.document.normalization import normalize_text
from app.modules.structured_processing.document.ocr import DocumentPageOcrEngine
from app.modules.structured_processing.document.pdf import extract_pdf, render_pdf_pages
from app.modules.structured_processing.models import TextSegment
from tests.fixtures.structured_processing.builders import build_scanned_pdf_page


def _tesseract_available() -> bool:
    try:
        get_tesseract_version()
        return True
    except (TesseractNotFoundError, OSError):
        return False


@dataclass(frozen=True)
class _LabelledFixture:
    lines: list[str]
    expected_values: frozenset[str]


_FIXTURE = _LabelledFixture(
    lines=[
        "FIR No: 91/2026",
        "Police Station: Colaba",
        "Phone: 9876543210",
        "Amount: Rs. 25000",
    ],
    expected_values=frozenset({"91/2026", "9876543210", "25000"}),
)


@pytest.mark.skipif(not _tesseract_available(), reason="tesseract binary not available")
def test_ocr_and_regex_field_match_precision_on_a_labelled_fixture() -> None:
    pdf_bytes = build_scanned_pdf_page(_FIXTURE.lines)
    extraction = extract_pdf(pdf_bytes)
    assert extraction.ocr_required_pages == [1]

    images = render_pdf_pages(pdf_bytes, [1], dpi=300)
    engine = DocumentPageOcrEngine()
    ocr_result = engine.recognize_page(images[1], page_number=1)

    normalized = normalize_text(ocr_result.joined_text)
    mentions = extract_fir_mentions([TextSegment(page=1, text=normalized.normalized)])
    extracted_values = {m.text for m in mentions}

    true_positives = extracted_values & _FIXTURE.expected_values
    precision = len(true_positives) / len(extracted_values) if extracted_values else 0.0
    recall = len(true_positives) / len(_FIXTURE.expected_values)

    print(  # noqa: T201 - deliberately recorded for docs/qa/test-results.md
        f"OCR field-match measurement: extracted={sorted(extracted_values)} "
        f"expected={sorted(_FIXTURE.expected_values)} "
        f"precision={precision:.2f} recall={recall:.2f} "
        f"ocr_average_confidence={ocr_result.average_confidence}"
    )

    # A real, honest threshold for clean, synthetic, high-contrast rendered
    # text -- not a claim about real-world scanned-document accuracy (see
    # docs/architecture/document-structured-processing.md's "Honest
    # accuracy note").
    assert recall >= 0.66, f"expected at least 2/3 of known identifiers recovered, got {recall}"
    assert precision >= 0.5, (
        f"expected at least half of extracted values to be correct, got {precision}"
    )
