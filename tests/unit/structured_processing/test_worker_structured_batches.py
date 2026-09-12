"""Scenarios 13-14, 19, 21-27: CDR/finance chunked micro-batch orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.contracts.evidence import SourceType
from app.contracts.observation_batch import (
    BatchAcceptanceStatus,
    ObservationBatchReceiptV1,
    ObservationBatchSubmissionV1,
)
from app.contracts.worker import WorkerJobV1, WorkerStatus
from app.core.config import Settings
from app.modules.structured_processing.client import RenewAck
from app.modules.structured_processing.document.classifier import ContentKind
from app.modules.structured_processing.errors import ErrorCode
from app.modules.structured_processing.structured.profiles import (
    CDR_GENERIC_V1,
    FINANCIAL_TRANSACTION_GENERIC_V1,
)
from app.modules.structured_processing.worker import run_structured_batches_job


@dataclass
class _RecordingClient:
    batches: list[ObservationBatchSubmissionV1] = field(default_factory=list)
    renew_calls: int = 0

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

    def renew_lease(self, job_id: UUID, *, claim_token: str) -> RenewAck:
        self.renew_calls += 1
        return RenewAck(job_id=job_id, lease_expires_at=datetime.now(UTC))


def _make_job(processor_name: str, processor_version: str, source_type: SourceType) -> WorkerJobV1:
    case_id, evidence_id = uuid4(), uuid4()
    return WorkerJobV1(
        job_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=source_type,
        processor_name=processor_name,
        processor_version=processor_version,
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:{processor_name}:{processor_version}",
        input_object_uri="local://data.csv",
        requested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _cdr_csv(row_count: int) -> bytes:
    data = b"caller_number,timestamp\n"
    for i in range(row_count):
        data += f"98765432{i:02d},2026-01-01 10:{i % 60:02d}:00\n".encode()
    return data


def test_large_cdr_fixture_is_processed_in_bounded_micro_batches_with_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 19: a large fixture is chunked into multiple bounded batches, each with progress."""
    monkeypatch.setattr(Settings, "structured_batch_size", 10, raising=False)
    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=10))

    client = _RecordingClient()
    job = _make_job(CDR_GENERIC_V1.name, CDR_GENERIC_V1.version, SourceType.CDR)
    data = _cdr_csv(25)

    result = run_structured_batches_job(
        client=client,
        job=job,
        claim_token="tok",
        profile=CDR_GENERIC_V1,
        kind=ContentKind.CSV,
        data=data,
    )

    assert result.status == WorkerStatus.SUCCEEDED
    assert len(client.batches) == 3  # 10 + 10 + 5
    for batch in client.batches:
        assert batch.progress is not None
        assert batch.progress.stage == "normalizing"
    total_observations = sum(len(b.observations) for b in client.batches)
    assert total_observations == 25


def test_lease_is_renewed_periodically_during_a_long_running_batch_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=1))

    client = _RecordingClient()
    job = _make_job(CDR_GENERIC_V1.name, CDR_GENERIC_V1.version, SourceType.CDR)
    data = _cdr_csv(12)  # 12 batches at batch_size=1 -> renews at least twice (every 5)

    run_structured_batches_job(
        client=client,
        job=job,
        claim_token="tok",
        profile=CDR_GENERIC_V1,
        kind=ContentKind.CSV,
        data=data,
    )

    assert client.renew_calls >= 2


def test_malformed_rows_are_reported_under_the_partial_success_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 25 (also 18): malformed rows are reported safely without failing valid ones."""
    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=500))

    client = _RecordingClient()
    job = _make_job(
        FINANCIAL_TRANSACTION_GENERIC_V1.name,
        FINANCIAL_TRANSACTION_GENERIC_V1.version,
        SourceType.FINANCIAL,
    )
    data = b"amount,currency\n500,INR\n600,INR\nnot-a-number,INR\n"  # 2 valid, 1 malformed

    result = run_structured_batches_job(
        client=client,
        job=job,
        claim_token="tok",
        profile=FINANCIAL_TRANSACTION_GENERIC_V1,
        kind=ContentKind.CSV,
        data=data,
    )

    assert result.status == WorkerStatus.SUCCEEDED
    assert result.checkpoint is not None
    assert '"malformed_row_count": 1' in result.checkpoint
    assert '"valid_row_count": 2' in result.checkpoint


def test_all_rows_malformed_fails_the_job_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=500))

    client = _RecordingClient()
    job = _make_job(
        FINANCIAL_TRANSACTION_GENERIC_V1.name,
        FINANCIAL_TRANSACTION_GENERIC_V1.version,
        SourceType.FINANCIAL,
    )
    data = b"amount,currency\nnot-a-number,INR\n"

    result = run_structured_batches_job(
        client=client,
        job=job,
        claim_token="tok",
        profile=FINANCIAL_TRANSACTION_GENERIC_V1,
        kind=ContentKind.CSV,
        data=data,
    )

    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.PARTIAL_ROW_FAILURES


def test_ambiguous_schema_is_rejected_before_any_batch_is_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=500))

    client = _RecordingClient()
    job = _make_job(CDR_GENERIC_V1.name, CDR_GENERIC_V1.version, SourceType.CDR)
    data = b"unrelated_column,another_column\nfoo,bar\n"

    result = run_structured_batches_job(
        client=client,
        job=job,
        claim_token="tok",
        profile=CDR_GENERIC_V1,
        kind=ContentKind.CSV,
        data=data,
    )

    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.AMBIGUOUS_SCHEMA
    assert not client.batches


def test_identical_source_produces_stable_deterministic_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 26: identical source/config produces stable observations and idempotent batches."""
    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=500))

    client_a = _RecordingClient()
    client_b = _RecordingClient()
    job = _make_job(CDR_GENERIC_V1.name, CDR_GENERIC_V1.version, SourceType.CDR)
    data = _cdr_csv(5)

    run_structured_batches_job(
        client=client_a,
        job=job,
        claim_token="tok",
        profile=CDR_GENERIC_V1,
        kind=ContentKind.CSV,
        data=data,
    )
    run_structured_batches_job(
        client=client_b,
        job=job,
        claim_token="tok",
        profile=CDR_GENERIC_V1,
        kind=ContentKind.CSV,
        data=data,
    )

    assert client_a.batches[0].batch_id == client_b.batches[0].batch_id
    assert client_a.batches[0].idempotency_key == client_b.batches[0].idempotency_key
    ids_a = [o.observation_id for o in client_a.batches[0].observations]
    ids_b = [o.observation_id for o in client_b.batches[0].observations]
    assert ids_a == ids_b


def test_xlsx_cdr_source_preserves_sheet_and_row_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 13: XLSX CDR preserves sheet/row/cell provenance through the batch orchestration."""
    import io

    from openpyxl import Workbook

    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=500))

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "CallLog"
    sheet.append(["caller_number", "timestamp"])
    sheet.append(["9876543210", "2026-01-01 10:00:00"])
    buf = io.BytesIO()
    workbook.save(buf)

    client = _RecordingClient()
    job = _make_job(CDR_GENERIC_V1.name, CDR_GENERIC_V1.version, SourceType.CDR)
    result = run_structured_batches_job(
        client=client,
        job=job,
        claim_token="tok",
        profile=CDR_GENERIC_V1,
        kind=ContentKind.XLSX,
        data=buf.getvalue(),
    )

    assert result.status == WorkerStatus.SUCCEEDED
    record_obs = next(
        o for o in client.batches[0].observations if o.observation_type == "cdr_call_record"
    )
    assert record_obs.source_locator.sheet == "CallLog"
    assert record_obs.source_locator.row == 2


def test_json_finance_source_preserves_json_path_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 23: JSON finance input preserves JSON-path provenance."""
    import json as json_module

    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=500))

    data = json_module.dumps([{"amount": "500", "currency": "INR"}]).encode()

    client = _RecordingClient()
    job = _make_job(
        FINANCIAL_TRANSACTION_GENERIC_V1.name,
        FINANCIAL_TRANSACTION_GENERIC_V1.version,
        SourceType.FINANCIAL,
    )
    result = run_structured_batches_job(
        client=client,
        job=job,
        claim_token="tok",
        profile=FINANCIAL_TRANSACTION_GENERIC_V1,
        kind=ContentKind.JSON,
        data=data,
    )

    assert result.status == WorkerStatus.SUCCEEDED
    record_obs = next(
        o
        for o in client.batches[0].observations
        if o.observation_type == "financial_transaction_record"
    )
    assert record_obs.source_locator.json_path == "$[0]"


def test_finance_result_never_infers_identity_guilt_or_a_graph_relationship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 27: no finance result infers identity, guilt, or a graph relationship."""
    import app.modules.structured_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(structured_batch_size=500))

    client = _RecordingClient()
    job = _make_job(
        FINANCIAL_TRANSACTION_GENERIC_V1.name,
        FINANCIAL_TRANSACTION_GENERIC_V1.version,
        SourceType.FINANCIAL,
    )
    data = b"amount,currency,sender_account,receiver_account\n500,INR,111,222\n"

    run_structured_batches_job(
        client=client,
        job=job,
        claim_token="tok",
        profile=FINANCIAL_TRANSACTION_GENERIC_V1,
        kind=ContentKind.CSV,
        data=data,
    )

    for observation in client.batches[0].observations:
        assert observation.observation_type in {
            "financial_transaction_record",
            "amount_mention",
            "financial_account_mention",
        }
        assert observation.extraction_confidence <= 1.0  # never a guilt/certainty score
