"""Live PostgreSQL coverage for `app.modules.integrity.repository`/`service`.

Proof points 3, 4, 6, 7, 8, 9, 10, 11, 13 from the Phase 6 task spec. Proof
points 1, 2, 5, 12, 14, 15, 16 (in part) are pure and live in
`tests/unit/integrity/`; 17 lives alongside the producer it covers in
`tests/integration/evidence_lifecycle/`; 18 is a static migration-graph
check in `tests/unit/integrity/test_migration_head.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from app.contracts.common import SourceLocator
from app.contracts.evidence import SourceType
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission
from app.modules.integrity.repository import (
    IntegrityRepository,
    IntegrityValidationError,
    checkpoint_signatures_table,
    integrity_events_table,
    merkle_checkpoints_table,
    structured_observation_provenance_table,
)
from app.modules.integrity.service import IntegrityService
from app.modules.integrity.structured_provenance import build_structured_observation_provenance
from tests.fixtures.factories import make_observation

_NOW = datetime(2026, 9, 15, tzinfo=UTC)


def _submission(case_id: UUID, key: str, **overrides: object) -> IntegrityEventSubmission:
    fields: dict[str, object] = {
        "case_id": case_id,
        "event_kind": IntegrityEventKind.EVIDENCE_REGISTERED,
        "subject_type": "evidence",
        "subject_id": f"evidence-{key}",
        "canonical_metadata": {"sha256": "a" * 64, "note": key},
        "payload_schema_version": "v1",
        "source_created_at": _NOW,
        "idempotency_key": key,
    }
    fields.update(overrides)
    return IntegrityEventSubmission.model_validate(fields)


async def _seed_events(repository: IntegrityRepository, case_id: UUID, count: int) -> list[UUID]:
    ids = []
    for i in range(count):
        record = await repository.record_event(_submission(case_id, f"key-{i}"), now=_NOW)
        ids.append(record.integrity_event_id)
    return ids


async def test_exact_retry_is_idempotent(repository: IntegrityRepository, case_id: UUID) -> None:
    """Proof point 6."""
    first = await repository.record_event(_submission(case_id, "retry-key"), now=_NOW)
    second = await repository.record_event(_submission(case_id, "retry-key"), now=_NOW)
    assert first == second

    async with repository._engine.connect() as conn:  # noqa: SLF001 - test-only assertion
        count = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(integrity_events_table)
                .where(integrity_events_table.c.case_id == case_id)
            )
        ).scalar_one()
    assert count == 1


async def test_conflicting_idempotency_key_reuse_is_rejected(
    repository: IntegrityRepository, case_id: UUID
) -> None:
    """Proof point 7."""
    await repository.record_event(_submission(case_id, "conflict-key"), now=_NOW)
    with pytest.raises(IntegrityValidationError, match="idempotency key conflicts"):
        await repository.record_event(
            _submission(case_id, "conflict-key", canonical_metadata={"sha256": "b" * 64}),
            now=_NOW,
        )


async def test_event_sequence_determines_leaf_order_not_wall_clock(
    repository: IntegrityRepository, case_id: UUID
) -> None:
    """Proof point 4: sequence_number governs order, even against reversed timestamps."""
    later_wall_clock = _NOW
    earlier_wall_clock = _NOW - timedelta(days=1)
    first = await repository.record_event(_submission(case_id, "first"), now=later_wall_clock)
    second = await repository.record_event(_submission(case_id, "second"), now=earlier_wall_clock)
    assert first.sequence_number == 1
    assert second.sequence_number == 2
    assert first.created_at > second.created_at  # wall clock is reversed vs. sequence

    ordered = await repository.list_events_in_range(case_id, 1, 2)
    assert [e.integrity_event_id for e in ordered] == [
        first.integrity_event_id,
        second.integrity_event_id,
    ]


async def test_checkpoint_build_is_idempotent_for_an_exact_range(
    service: IntegrityService, case_id: UUID
) -> None:
    """Proof point 8."""
    repository = service._repository  # noqa: SLF001 - test-only access
    await _seed_events(repository, case_id, 3)
    first = await service.build_checkpoint(case_id=case_id, start_sequence=1, end_sequence=3)
    second = await service.build_checkpoint(case_id=case_id, start_sequence=1, end_sequence=3)
    assert first.replayed is False
    assert second.replayed is True
    assert first.checkpoint.checkpoint_id == second.checkpoint.checkpoint_id
    assert first.checkpoint.root_hash == second.checkpoint.root_hash


async def test_overlapping_checkpoint_range_is_rejected(
    service: IntegrityService, case_id: UUID
) -> None:
    """Proof point 9."""
    repository = service._repository  # noqa: SLF001 - test-only access
    await _seed_events(repository, case_id, 8)
    await service.build_checkpoint(case_id=case_id, start_sequence=1, end_sequence=5)
    with pytest.raises(IntegrityValidationError, match="overlaps"):
        await service.build_checkpoint(case_id=case_id, start_sequence=3, end_sequence=8)


async def test_checkpoints_never_cross_cases(service: IntegrityService, case_id: UUID) -> None:
    """Proof point 3: an identical range for a second case is unrelated, and
    a checkpoint is never visible to the wrong case_id."""
    repository = service._repository  # noqa: SLF001 - test-only access
    other_case_id = uuid4()
    await _seed_events(repository, case_id, 3)
    await _seed_events(repository, other_case_id, 3)
    mine = await service.build_checkpoint(case_id=case_id, start_sequence=1, end_sequence=3)
    theirs = await service.build_checkpoint(case_id=other_case_id, start_sequence=1, end_sequence=3)
    assert mine.checkpoint.checkpoint_id != theirs.checkpoint.checkpoint_id
    assert mine.checkpoint.root_hash != theirs.checkpoint.root_hash

    wrong_scope = await repository.get_checkpoint(
        mine.checkpoint.checkpoint_id, case_id=other_case_id
    )
    assert wrong_scope is None


async def test_database_rejects_update_on_integrity_event(
    service: IntegrityService, case_id: UUID
) -> None:
    """Proof point 10."""
    repository = service._repository  # noqa: SLF001 - test-only access
    events = await _seed_events(repository, case_id, 3)
    await service.build_checkpoint(case_id=case_id, start_sequence=1, end_sequence=3)

    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        async with repository._engine.begin() as conn:  # noqa: SLF001 - direct SQL boundary proof
            await conn.execute(
                sa.update(integrity_events_table)
                .where(integrity_events_table.c.integrity_event_id == events[0])
                .values(canonical_payload_sha256="f" * 64)
            )


async def test_database_rejects_delete_on_integrity_event(
    service: IntegrityService, case_id: UUID
) -> None:
    """Proof point 11."""
    repository = service._repository  # noqa: SLF001 - test-only access
    events = await _seed_events(repository, case_id, 3)
    await service.build_checkpoint(case_id=case_id, start_sequence=1, end_sequence=3)

    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        async with repository._engine.begin() as conn:  # noqa: SLF001 - direct SQL boundary proof
            await conn.execute(
                sa.delete(integrity_events_table).where(
                    integrity_events_table.c.integrity_event_id == events[1]
                )
            )


async def test_database_rejects_checkpoint_and_signature_mutation(
    service: IntegrityService, case_id: UUID
) -> None:
    """Proof point 13."""
    repository = service._repository  # noqa: SLF001 - test-only access
    await _seed_events(repository, case_id, 3)
    receipt = await service.build_checkpoint(case_id=case_id, start_sequence=1, end_sequence=3)

    statements = (
        sa.update(merkle_checkpoints_table)
        .where(merkle_checkpoints_table.c.checkpoint_id == receipt.checkpoint.checkpoint_id)
        .values(root_hash="e" * 64),
        sa.delete(merkle_checkpoints_table).where(
            merkle_checkpoints_table.c.checkpoint_id == receipt.checkpoint.checkpoint_id
        ),
        sa.update(checkpoint_signatures_table)
        .where(checkpoint_signatures_table.c.checkpoint_id == receipt.checkpoint.checkpoint_id)
        .values(key_id="changed"),
        sa.delete(checkpoint_signatures_table).where(
            checkpoint_signatures_table.c.checkpoint_id == receipt.checkpoint.checkpoint_id
        ),
    )
    for statement in statements:
        with pytest.raises(sa.exc.DBAPIError, match="append-only"):
            async with repository._engine.begin() as conn:  # noqa: SLF001 - direct SQL boundary proof
                await conn.execute(statement)


async def test_structured_provenance_is_case_scoped_idempotent_and_append_only(
    repository: IntegrityRepository, case_id: UUID
) -> None:
    """The durable safe projection has the same direct-SQL boundary as other leaves."""
    observation = make_observation(
        case_id=case_id,
        observation_type="cdr_call_record",
        attributes={
            "caller_number": "caller-synthetic",
            "callee_number": "callee-synthetic",
            "source_signal_quality": {"outcome": "accepted"},
        },
        source_locator=SourceLocator(row=2),
    )
    projection = build_structured_observation_provenance(
        observation=observation, evidence_sha256="a" * 64, source_type=SourceType.CDR
    )
    assert projection is not None
    first = await repository.record_structured_provenance(
        projection, source_created_at=_NOW, now=_NOW
    )
    second = await repository.record_structured_provenance(
        projection, source_created_at=_NOW, now=_NOW
    )
    assert first == second
    changed_observation = observation.model_copy(update={"source_locator": SourceLocator(row=3)})
    changed_projection = build_structured_observation_provenance(
        observation=changed_observation, evidence_sha256="a" * 64, source_type=SourceType.CDR
    )
    assert changed_projection is not None
    with pytest.raises(IntegrityValidationError, match="structured provenance record"):
        await repository.record_structured_provenance(
            changed_projection, source_created_at=_NOW, now=_NOW
        )
    assert await repository.list_structured_provenance(uuid4()) == []

    for statement in (
        sa.update(structured_observation_provenance_table)
        .where(structured_observation_provenance_table.c.provenance_id == first.provenance_id)
        .values(canonical_payload_sha256="b" * 64),
        sa.delete(structured_observation_provenance_table).where(
            structured_observation_provenance_table.c.provenance_id == first.provenance_id
        ),
    ):
        with pytest.raises(sa.exc.DBAPIError, match="append-only"):
            async with repository._engine.begin() as conn:  # noqa: SLF001 - direct SQL boundary proof
                await conn.execute(statement)


async def test_full_build_verify_export_round_trip_succeeds(
    service: IntegrityService, case_id: UUID
) -> None:
    """A positive-path sanity check tying hashing, signing, and storage together."""
    repository = service._repository  # noqa: SLF001 - test-only access
    await _seed_events(repository, case_id, 5)
    receipt = await service.build_checkpoint(case_id=case_id, start_sequence=1, end_sequence=5)

    result = await service.verify_checkpoint(receipt.checkpoint.checkpoint_id, case_id=case_id)
    assert result.ok is True
    assert result.reason is None

    bundle = await service.export_verification_bundle(
        receipt.checkpoint.checkpoint_id, case_id=case_id
    )
    assert len(bundle.leaves) == 5
    assert bundle.checkpoint.root_hash == receipt.checkpoint.root_hash
    assert bundle.signature.public_key_fingerprint == receipt.signature.public_key_fingerprint
    # No raw content, no private key: only IDs, hashes, counts, and public verification material.
    bundle_json = bundle.model_dump_json()
    for forbidden in ("private", "BEGIN ", "secret"):
        assert forbidden not in bundle_json
