"""Scenarios 15, 28-31: multi-batch orchestration, provenance scoping, lease renewal, terminal result."""  # noqa: E501

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.contracts.evidence import SourceType
from app.contracts.observation_batch import (
    BatchAcceptanceStatus,
    ObservationBatchReceiptV1,
    ObservationBatchSubmissionV1,
)
from app.contracts.worker import WorkerStatus
from app.core.config import Settings
from app.modules.communication_processing.client import RenewAck
from app.modules.communication_processing.models import SocialExportInput
from app.modules.communication_processing.worker import (
    WHATSAPP_EXPORT_V1,
    run_communication_job_with_batches,
)
from tests.fixtures.communication_processing.builders import build_whatsapp_export
from tests.fixtures.communication_processing.factory import make_job


@dataclass
class _RecordingClient:
    batches: list[ObservationBatchSubmissionV1] = field(default_factory=list)
    renew_calls: int = 0
    result_calls: int = 0

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


def _whatsapp_export_with_messages(count: int) -> bytes:
    lines = [f"01/01/26, 10:{i:02d} - Alice: message number {i}" for i in range(count)]
    return build_whatsapp_export(lines)


def test_large_export_is_processed_in_bounded_micro_batches_with_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 28-29: multiple batches, each with monotonically-increasing progress."""
    import app.modules.communication_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(communication_batch_size=5))

    client = _RecordingClient()
    job = make_job(processor_name=WHATSAPP_EXPORT_V1.name, source_type=SourceType.WHATSAPP_CHAT)
    payload = SocialExportInput(platform="whatsapp", data=_whatsapp_export_with_messages(12))

    result = run_communication_job_with_batches(
        client=client, job=job, claim_token="tok", input_payload=payload
    )

    assert result.status is WorkerStatus.SUCCEEDED
    assert len(client.batches) == 3  # 5 + 5 + 2
    total_observations = sum(len(b.observations) for b in client.batches)
    assert total_observations == 12

    completed = [b.progress.units_completed for b in client.batches if b.progress is not None]
    assert completed == sorted(completed)
    assert completed[-1] == 12


def test_lease_is_renewed_periodically_during_a_long_running_batch_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 30: renew_lease fires via the existing client pattern for a long job."""
    import app.modules.communication_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(communication_batch_size=1))

    client = _RecordingClient()
    job = make_job(processor_name=WHATSAPP_EXPORT_V1.name, source_type=SourceType.WHATSAPP_CHAT)
    payload = SocialExportInput(platform="whatsapp", data=_whatsapp_export_with_messages(12))

    run_communication_job_with_batches(
        client=client, job=job, claim_token="tok", input_payload=payload
    )

    assert len(client.batches) == 12
    assert client.renew_calls >= 2  # renews every 5 batches -> at least twice over 12


def test_transformation_provenance_references_only_its_own_batch_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 28: a batch's transformation never references another batch's observation IDs."""
    import app.modules.communication_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(communication_batch_size=5))

    client = _RecordingClient()
    job = make_job(processor_name=WHATSAPP_EXPORT_V1.name, source_type=SourceType.WHATSAPP_CHAT)
    payload = SocialExportInput(platform="whatsapp", data=_whatsapp_export_with_messages(12))

    run_communication_job_with_batches(
        client=client, job=job, claim_token="tok", input_payload=payload
    )

    for batch in client.batches:
        own_ids = {o.observation_id for o in batch.observations}
        for transformation in batch.transformations:
            assert set(transformation.output_observation_ids) <= own_ids


def test_exactly_one_terminal_result_with_empty_observations_after_multi_batch_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 31: exactly one terminal WorkerResultV1, observations=[] -- already delivered."""
    import app.modules.communication_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(communication_batch_size=5))

    client = _RecordingClient()
    job = make_job(processor_name=WHATSAPP_EXPORT_V1.name, source_type=SourceType.WHATSAPP_CHAT)
    payload = SocialExportInput(platform="whatsapp", data=_whatsapp_export_with_messages(12))

    result = run_communication_job_with_batches(
        client=client, job=job, claim_token="tok", input_payload=payload
    )

    assert result.status is WorkerStatus.SUCCEEDED
    assert result.observations == []
    assert len(client.batches) > 1  # genuinely exercised the multi-batch path


def test_replaying_the_same_job_produces_identical_batch_and_observation_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 15: a replayed run never produces duplicate-but-differently-identified batches."""
    import app.modules.communication_processing.worker as worker_module

    monkeypatch.setattr(worker_module, "get_settings", lambda: Settings(communication_batch_size=5))

    job = make_job(processor_name=WHATSAPP_EXPORT_V1.name, source_type=SourceType.WHATSAPP_CHAT)
    payload = SocialExportInput(platform="whatsapp", data=_whatsapp_export_with_messages(12))

    client_a = _RecordingClient()
    client_b = _RecordingClient()
    run_communication_job_with_batches(
        client=client_a, job=job, claim_token="tok", input_payload=payload
    )
    run_communication_job_with_batches(
        client=client_b, job=job, claim_token="tok", input_payload=payload
    )

    ids_a = [b.batch_id for b in client_a.batches]
    ids_b = [b.batch_id for b in client_b.batches]
    assert ids_a == ids_b

    obs_ids_a = [[str(o.observation_id) for o in b.observations] for b in client_a.batches]
    obs_ids_b = [[str(o.observation_id) for o in b.observations] for b in client_b.batches]
    assert obs_ids_a == obs_ids_b
