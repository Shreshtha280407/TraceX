"""`WorkerJobRecord.to_contract()` -- gap-closure regression coverage.

`worker_jobs.idempotency_key` (a DB-level dedup key) and `WorkerJobV1.
idempotency_key` (a frozen contract field with its own strict format
validator) are different concepts that `to_contract()` used to conflate,
by echoing the raw stored column straight into the contract field. A
`reprocess_evidence`-created job's DB key carries an extra
`:reprocess:{caller_key}` suffix (so a retry never collides with the
original upload's job) that the contract's own validator rejects outright
-- previously a real `ValidationError` (surfaced as an HTTP 500) the
moment such a job was actually claimed, not caught by any existing test
because none of them exercised a real claim of a real reprocessed job.
"""

from __future__ import annotations

from tests.fixtures.evidence_lifecycle.factories import make_job_record


def test_to_contract_derives_the_canonical_idempotency_key_for_a_normal_job() -> None:
    job = make_job_record()
    contract = job.to_contract()
    assert contract.idempotency_key == (
        f"{job.case_id}:{job.evidence_id}:{job.processor_name}:{job.processor_version}"
    )


def test_to_contract_never_raises_for_a_reprocess_style_suffixed_idempotency_key() -> None:
    job = make_job_record(
        idempotency_key="ignored-db-only-value:reprocess:caller-supplied-key",
    )
    contract = job.to_contract()
    assert contract.idempotency_key == (
        f"{job.case_id}:{job.evidence_id}:{job.processor_name}:{job.processor_version}"
    )
