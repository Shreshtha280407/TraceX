"""Scenarios 1-4, 10-11: document micro-batch orchestration (`run_document_job_with_batches`).

Uses a real PDF (hand-assembled, see `tests/fixtures/structured_processing/
builders.py`), real `pypdf` text extraction, real `pypdfium2` rendering,
and real local Tesseract OCR -- self-skips OCR-dependent assertions only if
the `tesseract` binary genuinely isn't on `PATH` in the environment running
the suite (never fabricates a pass), mirroring every other real-OCR test in
this repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pytesseract import TesseractNotFoundError, get_tesseract_version

from app.contracts.evidence import SourceType
from app.contracts.observation_batch import (
    BatchAcceptanceStatus,
    ObservationBatchReceiptV1,
    ObservationBatchSubmissionV1,
)
from app.contracts.worker import WorkerJobV1, WorkerStatus
from app.modules.structured_processing.document.classifier import ContentKind
from app.modules.structured_processing.errors import ErrorCode
from app.modules.structured_processing.structured.profiles import FIR_REPORT_TEXT_V1
from app.modules.structured_processing.worker import run_document_job_with_batches
from tests.fixtures.structured_processing.builders import build_mixed_pdf


def _tesseract_available() -> bool:
    try:
        get_tesseract_version()
        return True
    except (TesseractNotFoundError, OSError):
        return False


@dataclass
class _RecordingClient:
    """Duck-types the `submit_batch` slice of `WorkerApiClient`."""

    batches: list[ObservationBatchSubmissionV1] = field(default_factory=list)

    def submit_batch(
        self, *, job_id: UUID, claim_token: str, submission: ObservationBatchSubmissionV1
    ) -> ObservationBatchReceiptV1:
        self.batches.append(submission)
        return ObservationBatchReceiptV1(
            job_id=job_id,
            batch_id=submission.batch_id,
            status=BatchAcceptanceStatus.ACCEPTED,
            accepted_observation_count=len(submission.observations),
            progress=submission.progress,
            request_id=None,
        )


def _make_job(source_type: SourceType = SourceType.DOCUMENT) -> WorkerJobV1:
    case_id, evidence_id = uuid4(), uuid4()
    return WorkerJobV1(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=source_type,
        processor_name=FIR_REPORT_TEXT_V1.name,
        processor_version=FIR_REPORT_TEXT_V1.version,
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:{FIR_REPORT_TEXT_V1.name}:1.0.0",
        input_object_uri="local://fir.pdf",
        requested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_text_bearing_page_bypasses_ocr_entirely() -> None:
    """Scenario 1: a trusted embedded-text page never invokes OCR."""
    pdf_bytes = build_mixed_pdf(
        ["FIR No: 10/2026\nPolice Station: Colaba\nDate: 01/01/2026 substantial text here"]
    )
    client = _RecordingClient()
    job = _make_job()

    result = run_document_job_with_batches(
        client=client,
        job=job,
        claim_token="tok",
        kind=ContentKind.PDF,
        profile=FIR_REPORT_TEXT_V1,
        data=pdf_bytes,
    )

    assert result.status == WorkerStatus.SUCCEEDED
    assert len(client.batches) == 1
    step_names = {t.step_name for t in client.batches[0].transformations}
    assert "pdf_page_ocr" not in step_names
    assert "pdf_embedded_text_extraction" in step_names
    assert any(o.observation_type == "fir_reference" for o in client.batches[0].observations)


@pytest.mark.skipif(not _tesseract_available(), reason="tesseract binary not available")
def test_scanned_page_invokes_real_local_ocr_fallback() -> None:
    """Scenario 2: a scanned/no-text page runs real local OCR."""
    pdf_bytes = build_mixed_pdf([["FIR No: 20/2026", "Police Station: Colaba"]])
    client = _RecordingClient()
    job = _make_job()

    result = run_document_job_with_batches(
        client=client,
        job=job,
        claim_token="tok",
        kind=ContentKind.PDF,
        profile=FIR_REPORT_TEXT_V1,
        data=pdf_bytes,
    )

    assert result.status == WorkerStatus.SUCCEEDED
    assert len(client.batches) == 1
    transformations = client.batches[0].transformations
    ocr_step = next(t for t in transformations if t.step_name == "pdf_page_ocr")
    assert ocr_step.safe_metadata["region_count"] >= 1
    observations = client.batches[0].observations
    assert any(o.observation_type == "fir_reference" for o in observations)


@pytest.mark.skipif(not _tesseract_available(), reason="tesseract binary not available")
def test_ocr_observation_has_valid_page_and_normalized_bbox_provenance() -> None:
    """Scenario 4: an OCR-derived observation's locator carries page + a valid normalized bbox."""
    pdf_bytes = build_mixed_pdf([["FIR No: 30/2026 filed today"]])
    client = _RecordingClient()
    job = _make_job()

    run_document_job_with_batches(
        client=client,
        job=job,
        claim_token="tok",
        kind=ContentKind.PDF,
        profile=FIR_REPORT_TEXT_V1,
        data=pdf_bytes,
    )

    fir_obs = next(
        o for o in client.batches[0].observations if o.observation_type == "fir_reference"
    )
    locator = fir_obs.source_locator
    assert locator.page == 1
    assert locator.bbox_xyxy_normalized is not None
    bbox = locator.bbox_xyxy_normalized
    assert 0.0 <= bbox.x_min < bbox.x_max <= 1.0
    assert 0.0 <= bbox.y_min < bbox.y_max <= 1.0


@pytest.mark.skipif(not _tesseract_available(), reason="tesseract binary not available")
def test_mixed_pdf_uses_text_extraction_for_trusted_pages_and_ocr_only_for_required_pages() -> None:
    """Scenario 3: trusted pages use embedded text; only the scanned page runs OCR."""
    pdf_bytes = build_mixed_pdf(
        [
            "FIR No: 40/2026 reported at the station with substantial detail here.",
            ["Police Station: Colaba", "Officer: Suresh"],
        ]
    )
    client = _RecordingClient()
    job = _make_job()

    result = run_document_job_with_batches(
        client=client,
        job=job,
        claim_token="tok",
        kind=ContentKind.PDF,
        profile=FIR_REPORT_TEXT_V1,
        data=pdf_bytes,
    )

    assert result.status == WorkerStatus.SUCCEEDED
    assert len(client.batches) == 2

    page1_steps = {t.step_name for t in client.batches[0].transformations}
    page2_steps = {t.step_name for t in client.batches[1].transformations}
    assert "pdf_embedded_text_extraction" in page1_steps and "pdf_page_ocr" not in page1_steps
    assert "pdf_page_ocr" in page2_steps and "pdf_embedded_text_extraction" not in page2_steps


def test_corrupt_page_is_reported_safely_without_aborting_the_whole_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 10: a page whose extraction raises is reported, not silently dropped."""
    import pypdf

    pdf_bytes = build_mixed_pdf(
        [
            "FIR No: 50/2026 reported at the station with substantial detail here.",
            "Second page also has substantial embedded text for this test scenario.",
        ]
    )

    real_extract_text = pypdf.PageObject.extract_text
    call_count = {"n": 0}

    def _flaky_extract_text(self: object, *args: object, **kwargs: object) -> str:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("simulated corrupt content stream")
        return real_extract_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pypdf.PageObject, "extract_text", _flaky_extract_text)

    client = _RecordingClient()
    job = _make_job()
    result = run_document_job_with_batches(
        client=client,
        job=job,
        claim_token="tok",
        kind=ContentKind.PDF,
        profile=FIR_REPORT_TEXT_V1,
        data=pdf_bytes,
    )

    # The corrupt page is named in the checkpoint, not silently absent, and
    # the other (valid) page's batch was still submitted.
    assert result.status == WorkerStatus.DEFERRED
    assert result.checkpoint is not None
    assert "2" in result.checkpoint  # corrupt_pages names page 2
    assert len(client.batches) == 1
    assert client.batches[0].observations  # page 1 was still processed


def test_document_batch_result_status_matches_error_code_conventions() -> None:
    """A genuinely invalid PDF fails safely with a documented error code, not a crash."""
    client = _RecordingClient()
    job = _make_job()
    result = run_document_job_with_batches(
        client=client,
        job=job,
        claim_token="tok",
        kind=ContentKind.PDF,
        profile=FIR_REPORT_TEXT_V1,
        data=b"not a pdf at all",
    )
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.INVALID_PDF
    assert not client.batches
