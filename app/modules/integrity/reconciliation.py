"""Operator-invoked, idempotent repair of missing integrity events.

The primary write remains successful if its subsequent integrity recording
fails. This service scans only durable source-owned records and reconstructs
the same safe submission used at the producer seam; it never accepts or
invents source content.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from app.modules.evidence_lifecycle.repository import evidence_records_table
from app.modules.graph.integration_repository import correlation_records_table
from app.modules.integrity.modality_provenance import (
    CommunicationObservationIntegrityProvenanceV1,
    VisualObservationIntegrityProvenanceV1,
)
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission
from app.modules.integrity.repository import (
    IntegrityRepository,
    modality_observation_provenance_table,
    structured_observation_provenance_table,
)
from app.modules.integrity.service import IntegrityService
from app.modules.integrity.structured_provenance import (
    StructuredObservationIntegrityProvenanceV1,
)

EVIDENCE_REGISTERED_SCHEMA_VERSION = "evidence_registered.v1"
CORRELATION_COMPLETED_SCHEMA_VERSION = "correlation_completed.v1"


@dataclass(frozen=True)
class ReconciliationReceipt:
    case_id: UUID
    scanned: int
    missing: int
    recorded: int
    replayed: int


class IntegrityReconciliationService:
    """A deliberately bounded, case-scoped repair seam for durable evidence writes.

    Later modality/review producers can add source adapters here. This Part 2
    implementation handles evidence/correlation producers plus persisted
    structured and modality projections.  It never reconstructs a
    projection from a raw observation payload: only the immutable safe
    projection itself is replayed.
    """

    def __init__(
        self, integrity_service: IntegrityService, repository: IntegrityRepository
    ) -> None:
        self._integrity_service = integrity_service
        self._repository = repository

    async def reconcile_case(self, case_id: UUID, *, limit: int = 500) -> ReconciliationReceipt:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        submissions = await self._durable_submissions(case_id, limit=limit)
        missing = recorded = replayed = 0
        for submission in submissions:
            existing = await self._repository.get_event_by_idempotency_key(
                case_id, submission.idempotency_key
            )
            if existing is not None:
                replayed += 1
                continue
            missing += 1
            await self._integrity_service.record_integrity_event(submission)
            recorded += 1
        return ReconciliationReceipt(
            case_id=case_id,
            scanned=len(submissions),
            missing=missing,
            recorded=recorded,
            replayed=replayed,
        )

    async def _durable_submissions(
        self, case_id: UUID, *, limit: int
    ) -> list[IntegrityEventSubmission]:
        async with self._repository._engine.connect() as conn:  # noqa: SLF001 - shared DB seam
            evidence_rows = (
                (
                    await conn.execute(
                        sa.select(evidence_records_table)
                        .where(evidence_records_table.c.case_id == case_id)
                        .order_by(evidence_records_table.c.created_at.asc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            remaining = limit - len(evidence_rows)
            correlation_rows: Sequence[sa.RowMapping] = ()
            if remaining:
                correlation_rows = (
                    (
                        await conn.execute(
                            sa.select(correlation_records_table)
                            .where(correlation_records_table.c.case_id == case_id)
                            .order_by(correlation_records_table.c.created_at.asc())
                            .limit(remaining)
                        )
                    )
                    .mappings()
                    .all()
                )
            remaining -= len(correlation_rows)
            structured_rows: Sequence[sa.RowMapping] = ()
            if remaining:
                structured_rows = (
                    (
                        await conn.execute(
                            sa.select(structured_observation_provenance_table)
                            .where(structured_observation_provenance_table.c.case_id == case_id)
                            .order_by(structured_observation_provenance_table.c.created_at.asc())
                            .limit(remaining)
                        )
                    )
                    .mappings()
                    .all()
                )
            remaining -= len(structured_rows)
            modality_rows: Sequence[sa.RowMapping] = ()
            if remaining:
                modality_rows = (
                    (
                        await conn.execute(
                            sa.select(modality_observation_provenance_table)
                            .where(modality_observation_provenance_table.c.case_id == case_id)
                            .order_by(modality_observation_provenance_table.c.created_at.asc())
                            .limit(remaining)
                        )
                    )
                    .mappings()
                    .all()
                )
        evidence_submissions = [
            IntegrityEventSubmission(
                case_id=case_id,
                event_kind=IntegrityEventKind.EVIDENCE_REGISTERED,
                subject_type="evidence",
                subject_id=str(row["evidence_id"]),
                canonical_metadata={
                    "evidence_id": str(row["evidence_id"]),
                    "source_type": row["source_type"],
                    "content_type": row["content_type"],
                    "sha256": row["sha256"],
                    "classification": row["classification"],
                    "uploaded_by": str(row["uploaded_by"]),
                    "parser_profile": row["parser_profile"],
                },
                payload_schema_version=EVIDENCE_REGISTERED_SCHEMA_VERSION,
                source_created_at=row["uploaded_at"],
                idempotency_key=str(row["evidence_id"]),
            )
            for row in evidence_rows
        ]
        correlation_submissions = [
            IntegrityEventSubmission(
                case_id=case_id,
                event_kind=IntegrityEventKind.CORRELATION_COMPLETED,
                subject_type="correlation",
                subject_id=str(row["correlation_id"]),
                canonical_metadata={
                    "correlation_id": str(row["correlation_id"]),
                    "correlation_type": row["correlation_type"],
                    "status": row["status"],
                    "supporting_observation_ids": row["supporting_observation_ids"],
                    "contradictory_observation_ids": row["contradictory_observation_ids"],
                    "mapping_version": row["mapping_version"],
                    "config_version": row["config_version"],
                },
                payload_schema_version=CORRELATION_COMPLETED_SCHEMA_VERSION,
                source_created_at=row["created_at"],
                idempotency_key=row["idempotency_key"],
            )
            for row in correlation_rows
        ]
        structured_submissions = [
            StructuredObservationIntegrityProvenanceV1.model_validate(
                row["canonical_payload"]
            ).to_integrity_submission(source_created_at=row["source_created_at"])
            for row in structured_rows
        ]
        modality_submissions = [
            _modality_submission(row["canonical_payload"], row["source_created_at"])
            for row in modality_rows
        ]
        return (
            evidence_submissions
            + correlation_submissions
            + structured_submissions
            + modality_submissions
        )


def _modality_submission(payload: object, source_created_at: object) -> IntegrityEventSubmission:
    """Replay only a stored typed projection; never inspect a raw observation row."""
    if not isinstance(payload, dict):  # pragma: no cover - database invariant
        raise ValueError("persisted modality provenance payload is invalid")
    if payload.get("schema_version") == "visual_provenance.v1":
        return VisualObservationIntegrityProvenanceV1.model_validate(
            payload
        ).to_integrity_submission(source_created_at=_valid_timestamp(source_created_at))
    if payload.get("schema_version") == "communication_provenance.v1":
        return CommunicationObservationIntegrityProvenanceV1.model_validate(
            payload
        ).to_integrity_submission(source_created_at=_valid_timestamp(source_created_at))

    # pragma: no cover - migration/schema invariant
    raise ValueError("persisted modality provenance schema is unsupported")


def _valid_timestamp(value: object) -> datetime:
    """Narrow a database timestamp without accepting a fabricated value."""
    if not isinstance(value, datetime):  # pragma: no cover - database invariant
        raise ValueError("persisted modality provenance timestamp is invalid")
    return value
