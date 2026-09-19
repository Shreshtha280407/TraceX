"""Gate B OCR diagnostic: PSM x binarization sweep against the real FIR fixtures.

    uv run python scripts/diagnose_ocr_gate_b.py

Read-only diagnostic script, not a test: prints a full matrix (fixture x
PSM x binarize) of raw OCR text, extracted mention values, and recall, so
an operator can compare Tesseract behavior across machines/versions
without needing Claude Code available on that machine.

Reuses only existing production code and existing test fixture builders --
`DocumentPageOcrEngine`, `render_pdf_pages`, `extract_fir_mentions`,
`normalize_text`, `build_scanned_pdf_page`, `build_mixed_pdf` -- never a
separate/hand-rolled OCR path, and never a fabricated observation. Touches
no database, no network, and no repository file; every fixture is built
in-memory from the same synthetic, non-sensitive lines the real test suite
already uses (see `tests/unit/structured_processing/
test_ocr_field_match_precision.py` and `test_worker_document_batches.py`).
"""

from __future__ import annotations

from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.document.normalization import normalize_text
from app.modules.structured_processing.document.ocr import DocumentPageOcrEngine, OcrConfig
from app.modules.structured_processing.document.pdf import render_pdf_pages
from app.modules.structured_processing.models import TextSegment
from tests.fixtures.structured_processing.builders import build_mixed_pdf, build_scanned_pdf_page

PSM_MODES = (3, 4, 6, 11, 12)
BINARIZE_OPTIONS = (False, True)

FIXTURES = {
    "91/2026": {
        "pdf_bytes": build_scanned_pdf_page(
            ["FIR No: 91/2026", "Police Station: Colaba", "Phone: 9876543210", "Amount: Rs. 25000"]
        ),
        "expected": {"91/2026", "9876543210", "25000"},
    },
    "20/2026": {
        "pdf_bytes": build_mixed_pdf([["FIR No: 20/2026", "Police Station: Colaba"]]),
        "expected": {"20/2026"},
    },
    "30/2026": {
        "pdf_bytes": build_mixed_pdf([["FIR No: 30/2026 filed today"]]),
        "expected": {"30/2026"},
    },
}


def main() -> None:
    for fixture_name, spec in FIXTURES.items():
        expected: set[str] = spec["expected"]  # type: ignore[assignment]
        print(f"\n{'=' * 90}\nFIXTURE {fixture_name}  expected={sorted(expected)}\n{'=' * 90}")
        images = render_pdf_pages(spec["pdf_bytes"], [1], dpi=300)  # type: ignore[arg-type]
        for psm in PSM_MODES:
            for binarize in BINARIZE_OPTIONS:
                try:
                    engine = DocumentPageOcrEngine(
                        config=OcrConfig(page_segmentation_mode=psm, binarize=binarize)
                    )
                    result = engine.recognize_page(images[1], page_number=1)
                    normalized = normalize_text(result.joined_text)
                    mentions = extract_fir_mentions(
                        [TextSegment(page=1, text=normalized.normalized)]
                    )
                    extracted = sorted({m.text for m in mentions})
                except Exception as exc:  # noqa: BLE001 - diagnostic: report, never abort the sweep
                    print(
                        f"fixture={fixture_name} PSM={psm:2} binarize={binarize!s:5} "
                        f"-> ERROR: {type(exc).__name__}: {exc}"
                    )
                    continue

                recall_str = "n/a"
                if fixture_name == "91/2026":
                    recall = len(set(extracted) & expected) / len(expected)
                    recall_str = f"{recall:.2f}"

                print(
                    f"fixture={fixture_name} PSM={psm:2} binarize={binarize!s:5} "
                    f"recall={recall_str} extracted={extracted} expected={sorted(expected)}"
                )
                print(f"    raw_text={result.joined_text!r}")


if __name__ == "__main__":
    main()
