"""Worker dispatch across every content kind: scenarios 13, 16, and general routing."""

from __future__ import annotations

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerResultV1, WorkerStatus
from app.modules.structured_processing.models import StaticBytesResolver
from app.modules.structured_processing.worker import process_job
from tests.fixtures.structured_processing.builders import (
    build_docx,
    build_encrypted_pdf,
    build_minimal_pdf,
    build_xlsx,
)
from tests.fixtures.structured_processing.factory import make_evidence_and_job


def _run(
    content_type: str,
    filename: str,
    processor_name: str,
    data: bytes,
    *,
    source_type: SourceType = SourceType.DOCUMENT,
) -> WorkerResultV1:
    evidence, job = make_evidence_and_job(
        content_type=content_type,
        filename=filename,
        processor_name=processor_name,
        source_type=source_type,
    )
    result = process_job(job, evidence, StaticBytesResolver(payload=data))
    assert isinstance(result, WorkerResultV1)
    return result


def test_txt_job_succeeds() -> None:
    result = _run(
        "text/plain", "notes.txt", "fir_report_text_v1", b"FIR No: 1/2026, phone 9876543210"
    )
    assert result.status == WorkerStatus.SUCCEEDED
    assert result.observations


def test_pdf_job_with_native_text_succeeds() -> None:
    pdf_bytes = build_minimal_pdf(["FIR No: 2/2026 native text page"])
    result = _run("application/pdf", "fir.pdf", "fir_report_text_v1", pdf_bytes)
    assert result.status == WorkerStatus.SUCCEEDED


def test_pdf_job_fully_scanned_defers() -> None:
    pdf_bytes = build_minimal_pdf([None])
    result = _run("application/pdf", "fir.pdf", "fir_report_text_v1", pdf_bytes)
    assert result.status == WorkerStatus.DEFERRED
    assert result.observations == []
    assert result.checkpoint is not None
    assert result.error is None


def test_encrypted_pdf_job_fails() -> None:
    pdf_bytes = build_encrypted_pdf()
    result = _run("application/pdf", "fir.pdf", "fir_report_text_v1", pdf_bytes)
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "encrypted_pdf_unsupported"


def test_docx_job_succeeds() -> None:
    docx_bytes = build_docx(["FIR No: 3/2026 in a docx"])
    result = _run(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "fir.docx",
        "fir_report_text_v1",
        docx_bytes,
    )
    assert result.status == WorkerStatus.SUCCEEDED


def test_csv_cdr_job_succeeds() -> None:
    data = b"caller_number,timestamp\n9876543210,2026-01-01 10:00:00\n"
    result = _run("text/csv", "cdr.csv", "cdr_generic_v1", data, source_type=SourceType.CDR)
    assert result.status == WorkerStatus.SUCCEEDED
    assert any(o.observation_type == "cdr_call_record" for o in result.observations)


def test_csv_financial_job_succeeds() -> None:
    data = b"amount,currency\n500,INR\n"
    result = _run(
        "text/csv",
        "txns.csv",
        "financial_transaction_generic_v1",
        data,
        source_type=SourceType.FINANCIAL,
    )
    assert result.status == WorkerStatus.SUCCEEDED


def test_xlsx_generic_tabular_job_succeeds() -> None:
    data = build_xlsx(headers=["a", "b"], rows=[["1", "2"]])
    result = _run(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "data.xlsx",
        "generic_tabular_v1",
        data,
    )
    assert result.status == WorkerStatus.SUCCEEDED
    assert result.observations[0].observation_type == "tabular_record"


def test_json_generic_job_succeeds() -> None:
    data = b'{"a": 1, "b": {"c": "x"}}'
    result = _run("application/json", "data.json", "generic_json_v1", data)
    assert result.status == WorkerStatus.SUCCEEDED
    paths = {o.source_locator.json_path for o in result.observations}
    assert "$.a" in paths
    assert "$.b.c" in paths


def test_unsupported_content_type_fails() -> None:
    result = _run("application/zip", "archive.zip", "generic_json_v1", b"whatever")
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "unsupported_content_type"


def test_profile_content_type_mismatch_fails() -> None:
    """A CDR job pointed at a PDF evidence record must be rejected, not silently reinterpreted."""
    result = _run("application/pdf", "fir.pdf", "cdr_generic_v1", build_minimal_pdf(["text"]))
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "unsupported_parser_profile"


def test_unknown_processor_name_fails() -> None:
    result = _run("text/plain", "notes.txt", "not_a_real_profile", b"hello")
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "unsupported_parser_profile"


def test_same_job_produces_same_observation_ids_across_runs() -> None:
    evidence, job = make_evidence_and_job(
        content_type="text/plain", filename="notes.txt", processor_name="fir_report_text_v1"
    )
    data = b"FIR No: 9/2026, phone 9876543210"
    resolver = StaticBytesResolver(payload=data)
    first = process_job(job, evidence, resolver)
    second = process_job(job, evidence, resolver)
    assert [o.observation_id for o in first.observations] == [
        o.observation_id for o in second.observations
    ]


def test_every_worker_result_round_trips_through_json() -> None:
    result = _run(
        "text/plain", "notes.txt", "fir_report_text_v1", b"FIR No: 4/2026, phone 9876543210"
    )
    reloaded = WorkerResultV1.model_validate_json(result.model_dump_json())
    assert reloaded == result
