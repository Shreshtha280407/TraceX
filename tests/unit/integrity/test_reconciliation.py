from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission
from app.modules.integrity.reconciliation import IntegrityReconciliationService


class _Repository:
    def __init__(self) -> None:
        self.events: dict[tuple[object, str], object] = {}

    async def get_event_by_idempotency_key(self, case_id, idempotency_key):
        return self.events.get((case_id, idempotency_key))


class _Service:
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository

    async def record_integrity_event(self, submission):
        self.repository.events[(submission.case_id, submission.idempotency_key)] = submission
        return submission


async def test_reconciliation_records_one_missing_event_and_is_idempotent() -> None:
    case_id, other_case_id = uuid4(), uuid4()
    repository = _Repository()
    reconciliation = IntegrityReconciliationService(_Service(repository), repository)  # type: ignore[arg-type]
    submission = IntegrityEventSubmission(
        case_id=case_id,
        event_kind=IntegrityEventKind.EVIDENCE_REGISTERED,
        subject_type="evidence",
        subject_id="synthetic-evidence",
        canonical_metadata={"sha256": "a" * 64},
        payload_schema_version="evidence_registered.v1",
        source_created_at=datetime(2026, 9, 15, tzinfo=UTC),
        idempotency_key="synthetic-evidence",
    )

    async def submissions(requested_case_id, *, limit):
        assert requested_case_id == case_id
        assert limit == 500
        return [submission]

    reconciliation._durable_submissions = submissions  # type: ignore[method-assign]
    first = await reconciliation.reconcile_case(case_id)
    second = await reconciliation.reconcile_case(case_id)
    assert (first.missing, first.recorded, first.replayed) == (1, 1, 0)
    assert (second.missing, second.recorded, second.replayed) == (0, 0, 1)
    assert (other_case_id, submission.idempotency_key) not in repository.events
