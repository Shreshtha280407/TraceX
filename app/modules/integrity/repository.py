"""PostgreSQL durable boundary for Phase 6 integrity events and checkpoints.

Mirrors `app.modules.graph.integration_repository`'s idempotent-submit /
fingerprint-comparison pattern. `integrity_events` rows are treated as
immutable and append-only by every code path here: there is no update or
delete method. Verification instead *recomputes* each leaf hash from the
current row and compares it against the value sealed into a checkpoint's
signed root -- if a row were ever altered outside this module (direct DB
edit), that recomputation naturally detects it (see `service.py`).

Per-case sequence numbers are allocated from `integrity_sequence_counters`
via `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, inside the same
transaction as the event insert. This is the one existing precedent this
schema doesn't reuse directly: `worker_progress_events_ordinal_seq` (see
`c1c9c1c9d9f1_observation_batch_ingestion.py`) is a single global Postgres
`SEQUENCE`, not scoped per case, so it can't give per-case contiguous
ordering on its own. A replayed (already-recorded) event never advances the
counter, so genuinely new events always get a gap-free 1..N sequence per
case -- required for checkpoint range contiguity.

Checkpoint ranges cannot overlap within a case: enforced at the database
level by a GiST exclusion constraint (see the migration), not only in
application code, so a race between two checkpoint-builders can't slip an
overlapping range past a check-then-insert gap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.canonical import canonical_sha256
from app.core.config import Settings
from app.core.ids import deterministic_uuid
from app.modules.integrity.modality_provenance import ModalityObservationIntegrityProvenanceV1
from app.modules.integrity.models import (
    CheckpointBuildReceipt,
    CheckpointSignatureRecord,
    IntegrityEventRecord,
    IntegrityEventSubmission,
    MerkleCheckpointRecord,
    ModalityObservationProvenanceRecord,
    StructuredObservationProvenanceRecord,
)
from app.modules.integrity.signing import SignedRoot
from app.modules.integrity.structured_provenance import (
    StructuredObservationIntegrityProvenanceV1,
)

metadata = sa.MetaData()

integrity_sequence_counters_table = sa.Table(
    "integrity_sequence_counters",
    metadata,
    sa.Column("case_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("last_sequence", sa.BigInteger(), nullable=False),
)

integrity_events_table = sa.Table(
    "integrity_events",
    metadata,
    sa.Column("integrity_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("event_kind", sa.Text(), nullable=False),
    sa.Column("subject_type", sa.Text(), nullable=False),
    sa.Column("subject_id", sa.Text(), nullable=False),
    sa.Column("canonical_payload_sha256", sa.Text(), nullable=False),
    sa.Column("payload_schema_version", sa.Text(), nullable=False),
    sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("sequence_number", sa.BigInteger(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

merkle_checkpoints_table = sa.Table(
    "merkle_checkpoints",
    metadata,
    sa.Column("checkpoint_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("start_sequence", sa.BigInteger(), nullable=False),
    sa.Column("end_sequence", sa.BigInteger(), nullable=False),
    sa.Column("leaf_count", sa.Integer(), nullable=False),
    sa.Column("root_hash", sa.Text(), nullable=False),
    sa.Column("tree_format_version", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

checkpoint_signatures_table = sa.Table(
    "checkpoint_signatures",
    metadata,
    sa.Column("signature_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column(
        "checkpoint_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("merkle_checkpoints.checkpoint_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("key_id", sa.Text(), nullable=False),
    sa.Column("algorithm", sa.Text(), nullable=False),
    sa.Column("signature_encoding", sa.Text(), nullable=False),
    sa.Column("signature", sa.Text(), nullable=False),
    sa.Column("public_key_b64", sa.Text(), nullable=False),
    sa.Column("public_key_fingerprint", sa.Text(), nullable=False),
    sa.Column("signed_root_hash", sa.Text(), nullable=False),
    sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
)

structured_observation_provenance_table = sa.Table(
    "structured_observation_integrity_provenance",
    metadata,
    sa.Column("provenance_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("source_family", sa.Text(), nullable=False),
    sa.Column("schema_version", sa.Text(), nullable=False),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("canonical_payload_sha256", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

modality_observation_provenance_table = sa.Table(
    "modality_observation_integrity_provenance",
    metadata,
    sa.Column("provenance_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("provenance_kind", sa.Text(), nullable=False),
    sa.Column("source_family", sa.Text(), nullable=False),
    sa.Column("schema_version", sa.Text(), nullable=False),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("canonical_payload_sha256", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


class IntegrityValidationError(ValueError):
    """Raised when a submission or checkpoint range is rejected as unsafe/inconsistent."""


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.postgres_dsn))


def _event_from_row(row: sa.RowMapping) -> IntegrityEventRecord:
    return IntegrityEventRecord.model_validate(dict(row))


def _checkpoint_from_row(row: sa.RowMapping) -> MerkleCheckpointRecord:
    return MerkleCheckpointRecord.model_validate(dict(row))


def _signature_from_row(row: sa.RowMapping) -> CheckpointSignatureRecord:
    return CheckpointSignatureRecord.model_validate(dict(row))


def _structured_provenance_from_row(row: sa.RowMapping) -> StructuredObservationProvenanceRecord:
    return StructuredObservationProvenanceRecord.model_validate(dict(row))


def _modality_provenance_from_row(row: sa.RowMapping) -> ModalityObservationProvenanceRecord:
    return ModalityObservationProvenanceRecord.model_validate(dict(row))


def _event_fingerprint(
    case_id: UUID, submission: IntegrityEventSubmission, payload_hash: str
) -> str:
    return canonical_sha256(
        {
            "case_id": str(case_id),
            "event_kind": submission.event_kind.value,
            "subject_type": submission.subject_type,
            "subject_id": submission.subject_id,
            "canonical_payload_sha256": payload_hash,
            "payload_schema_version": submission.payload_schema_version,
            "source_created_at": submission.source_created_at,
            "idempotency_key": submission.idempotency_key,
        }
    )


def _persisted_event_fingerprint(record: IntegrityEventRecord) -> str:
    return canonical_sha256(
        {
            "case_id": str(record.case_id),
            "event_kind": record.event_kind.value,
            "subject_type": record.subject_type,
            "subject_id": record.subject_id,
            "canonical_payload_sha256": record.canonical_payload_sha256,
            "payload_schema_version": record.payload_schema_version,
            "source_created_at": record.source_created_at,
            "idempotency_key": record.idempotency_key,
        }
    )


class IntegrityRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def record_event(
        self, submission: IntegrityEventSubmission, *, now: datetime | None = None
    ) -> IntegrityEventRecord:
        """Persist one integrity event. Exact retries replay; conflicts raise.

        `canonical_metadata` is hashed by the caller (`service.py`) before
        reaching here -- this method never sees or stores raw metadata.
        """
        now = now or datetime.now(UTC)
        payload_hash = canonical_sha256(submission.canonical_metadata)
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(integrity_events_table).where(
                            integrity_events_table.c.case_id == submission.case_id,
                            integrity_events_table.c.idempotency_key == submission.idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                record = _event_from_row(existing)
                if _persisted_event_fingerprint(record) != _event_fingerprint(
                    submission.case_id, submission, payload_hash
                ):
                    raise IntegrityValidationError(
                        "idempotency key conflicts with an existing integrity event"
                    )
                return record

            counter_row = await conn.execute(
                sa.text(
                    "INSERT INTO integrity_sequence_counters (case_id, last_sequence) "
                    "VALUES (:case_id, 1) "
                    "ON CONFLICT (case_id) DO UPDATE SET "
                    "last_sequence = integrity_sequence_counters.last_sequence + 1 "
                    "RETURNING last_sequence"
                ),
                {"case_id": submission.case_id},
            )
            sequence_number = counter_row.scalar_one()

            integrity_event_id = deterministic_uuid(
                "phase_6_integrity_event", str(submission.case_id), submission.idempotency_key
            )
            record = IntegrityEventRecord(
                integrity_event_id=integrity_event_id,
                case_id=submission.case_id,
                event_kind=submission.event_kind,
                subject_type=submission.subject_type,
                subject_id=submission.subject_id,
                canonical_payload_sha256=payload_hash,
                payload_schema_version=submission.payload_schema_version,
                source_created_at=submission.source_created_at,
                idempotency_key=submission.idempotency_key,
                sequence_number=sequence_number,
                created_at=now,
            )
            await conn.execute(
                sa.insert(integrity_events_table).values(
                    integrity_event_id=record.integrity_event_id,
                    case_id=record.case_id,
                    event_kind=record.event_kind.value,
                    subject_type=record.subject_type,
                    subject_id=record.subject_id,
                    canonical_payload_sha256=record.canonical_payload_sha256,
                    payload_schema_version=record.payload_schema_version,
                    source_created_at=record.source_created_at,
                    idempotency_key=record.idempotency_key,
                    sequence_number=record.sequence_number,
                    created_at=record.created_at,
                )
            )
            return record

    async def record_structured_provenance(
        self,
        projection: StructuredObservationIntegrityProvenanceV1,
        *,
        source_created_at: datetime,
        now: datetime | None = None,
    ) -> StructuredObservationProvenanceRecord:
        """Persist one safe structured projection; exact retries replay, conflicts fail.

        The payload hash is calculated from the exact projection that will be
        used for its integrity leaf.  A same-case retry with changed safe
        metadata therefore cannot overwrite the first accepted commitment.
        """
        now = now or datetime.now(UTC)
        payload = projection.canonical_metadata()
        payload_hash = projection.canonical_payload_sha256
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(structured_observation_provenance_table).where(
                            structured_observation_provenance_table.c.case_id == projection.case_id,
                            structured_observation_provenance_table.c.idempotency_key
                            == projection.idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                record = _structured_provenance_from_row(existing)
                if (
                    record.observation_id != projection.observation_id
                    or record.canonical_payload_sha256 != payload_hash
                    or record.canonical_payload != payload
                ):
                    raise IntegrityValidationError(
                        "idempotency key conflicts with an existing structured provenance record"
                    )
                return record

            provenance_id = deterministic_uuid(
                "phase_6_structured_observation_provenance",
                str(projection.case_id),
                str(projection.observation_id),
                projection.schema_version,
            )
            record = StructuredObservationProvenanceRecord(
                provenance_id=provenance_id,
                case_id=projection.case_id,
                evidence_id=projection.evidence_id,
                observation_id=projection.observation_id,
                source_family=projection.source_family.value,
                schema_version=projection.schema_version,
                canonical_payload=payload,
                canonical_payload_sha256=payload_hash,
                idempotency_key=projection.idempotency_key,
                source_created_at=source_created_at,
                created_at=now,
            )
            await conn.execute(
                sa.insert(structured_observation_provenance_table).values(
                    provenance_id=record.provenance_id,
                    case_id=record.case_id,
                    evidence_id=record.evidence_id,
                    observation_id=record.observation_id,
                    source_family=record.source_family,
                    schema_version=record.schema_version,
                    canonical_payload=record.canonical_payload,
                    canonical_payload_sha256=record.canonical_payload_sha256,
                    idempotency_key=record.idempotency_key,
                    source_created_at=record.source_created_at,
                    created_at=record.created_at,
                )
            )
            return record

    async def list_structured_provenance(
        self, case_id: UUID, *, limit: int = 500
    ) -> list[StructuredObservationProvenanceRecord]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(structured_observation_provenance_table)
                        .where(structured_observation_provenance_table.c.case_id == case_id)
                        .order_by(structured_observation_provenance_table.c.created_at.asc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [_structured_provenance_from_row(row) for row in rows]

    async def record_modality_provenance(
        self,
        projection: ModalityObservationIntegrityProvenanceV1,
        *,
        source_created_at: datetime,
        now: datetime | None = None,
    ) -> ModalityObservationProvenanceRecord:
        """Persist one immutable visual/communication projection.

        Exact retries return the first record.  A changed safe projection
        under the deterministic retry key is a conflict, never an update.
        """
        now = now or datetime.now(UTC)
        payload = projection.canonical_metadata()
        payload_hash = projection.canonical_payload_sha256
        provenance_kind = (
            "visual" if projection.schema_version == "visual_provenance.v1" else "communication"
        )
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(modality_observation_provenance_table).where(
                            modality_observation_provenance_table.c.case_id == projection.case_id,
                            modality_observation_provenance_table.c.idempotency_key
                            == projection.idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                record = _modality_provenance_from_row(existing)
                if (
                    record.observation_id != projection.observation_id
                    or record.canonical_payload_sha256 != payload_hash
                    or record.canonical_payload != payload
                ):
                    raise IntegrityValidationError(
                        "idempotency key conflicts with an existing modality provenance record"
                    )
                return record

            provenance_id = deterministic_uuid(
                "phase_6_modality_observation_provenance",
                provenance_kind,
                str(projection.case_id),
                str(projection.observation_id),
                projection.schema_version,
            )
            record = ModalityObservationProvenanceRecord(
                provenance_id=provenance_id,
                case_id=projection.case_id,
                evidence_id=projection.evidence_id,
                observation_id=projection.observation_id,
                provenance_kind=provenance_kind,
                source_family=projection.source_family.value,
                schema_version=projection.schema_version,
                canonical_payload=payload,
                canonical_payload_sha256=payload_hash,
                idempotency_key=projection.idempotency_key,
                source_created_at=source_created_at,
                created_at=now,
            )
            await conn.execute(
                sa.insert(modality_observation_provenance_table).values(
                    provenance_id=record.provenance_id,
                    case_id=record.case_id,
                    evidence_id=record.evidence_id,
                    observation_id=record.observation_id,
                    provenance_kind=record.provenance_kind,
                    source_family=record.source_family,
                    schema_version=record.schema_version,
                    canonical_payload=record.canonical_payload,
                    canonical_payload_sha256=record.canonical_payload_sha256,
                    idempotency_key=record.idempotency_key,
                    source_created_at=record.source_created_at,
                    created_at=record.created_at,
                )
            )
            return record

    async def list_modality_provenance(
        self, case_id: UUID, *, limit: int = 500
    ) -> list[ModalityObservationProvenanceRecord]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(modality_observation_provenance_table)
                        .where(modality_observation_provenance_table.c.case_id == case_id)
                        .order_by(modality_observation_provenance_table.c.created_at.asc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [_modality_provenance_from_row(row) for row in rows]

    async def list_events_in_range(
        self, case_id: UUID, start_sequence: int, end_sequence: int
    ) -> list[IntegrityEventRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(integrity_events_table)
                        .where(
                            integrity_events_table.c.case_id == case_id,
                            integrity_events_table.c.sequence_number >= start_sequence,
                            integrity_events_table.c.sequence_number <= end_sequence,
                        )
                        .order_by(integrity_events_table.c.sequence_number.asc())
                    )
                )
                .mappings()
                .all()
            )
            return [_event_from_row(row) for row in rows]

    async def create_checkpoint(
        self,
        *,
        case_id: UUID,
        start_sequence: int,
        end_sequence: int,
        leaf_count: int,
        root_hash: str,
        tree_format_version: str,
        signed: SignedRoot,
        now: datetime | None = None,
    ) -> CheckpointBuildReceipt:
        """Persist a checkpoint and its signature atomically. Idempotent per exact range."""
        now = now or datetime.now(UTC)
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(merkle_checkpoints_table).where(
                            merkle_checkpoints_table.c.case_id == case_id,
                            merkle_checkpoints_table.c.start_sequence == start_sequence,
                            merkle_checkpoints_table.c.end_sequence == end_sequence,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                checkpoint = _checkpoint_from_row(existing)
                if checkpoint.root_hash != root_hash or checkpoint.leaf_count != leaf_count:
                    raise IntegrityValidationError(
                        "checkpoint range conflicts with a previously sealed checkpoint"
                    )
                signature_row = (
                    (
                        await conn.execute(
                            sa.select(checkpoint_signatures_table).where(
                                checkpoint_signatures_table.c.checkpoint_id
                                == checkpoint.checkpoint_id
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return CheckpointBuildReceipt(
                    checkpoint=checkpoint,
                    signature=_signature_from_row(signature_row),
                    replayed=True,
                )

            checkpoint_id = deterministic_uuid(
                "phase_6_merkle_checkpoint", str(case_id), str(start_sequence), str(end_sequence)
            )
            checkpoint = MerkleCheckpointRecord(
                checkpoint_id=checkpoint_id,
                case_id=case_id,
                start_sequence=start_sequence,
                end_sequence=end_sequence,
                leaf_count=leaf_count,
                root_hash=root_hash,
                tree_format_version=tree_format_version,
                created_at=now,
            )
            try:
                await conn.execute(
                    sa.insert(merkle_checkpoints_table).values(
                        checkpoint_id=checkpoint.checkpoint_id,
                        case_id=checkpoint.case_id,
                        start_sequence=checkpoint.start_sequence,
                        end_sequence=checkpoint.end_sequence,
                        leaf_count=checkpoint.leaf_count,
                        root_hash=checkpoint.root_hash,
                        tree_format_version=checkpoint.tree_format_version,
                        created_at=checkpoint.created_at,
                    )
                )
            except sa.exc.IntegrityError as exc:
                raise IntegrityValidationError(
                    "checkpoint range overlaps an existing checkpoint for this case"
                ) from exc

            signature_id = deterministic_uuid("phase_6_checkpoint_signature", str(checkpoint_id))
            signature = CheckpointSignatureRecord(
                signature_id=signature_id,
                checkpoint_id=checkpoint_id,
                case_id=case_id,
                key_id=signed.key_id,
                algorithm=signed.algorithm,
                signature_encoding=signed.signature_encoding,
                signature=signed.signature_b64,
                public_key_b64=signed.public_key_b64,
                public_key_fingerprint=signed.public_key_fingerprint,
                signed_root_hash=root_hash,
                signed_at=now,
            )
            await conn.execute(
                sa.insert(checkpoint_signatures_table).values(
                    signature_id=signature.signature_id,
                    checkpoint_id=signature.checkpoint_id,
                    case_id=signature.case_id,
                    key_id=signature.key_id,
                    algorithm=signature.algorithm,
                    signature_encoding=signature.signature_encoding,
                    signature=signature.signature,
                    public_key_b64=signature.public_key_b64,
                    public_key_fingerprint=signature.public_key_fingerprint,
                    signed_root_hash=signature.signed_root_hash,
                    signed_at=signature.signed_at,
                )
            )
            return CheckpointBuildReceipt(
                checkpoint=checkpoint, signature=signature, replayed=False
            )

    async def get_checkpoint(
        self, checkpoint_id: UUID, *, case_id: UUID
    ) -> MerkleCheckpointRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(merkle_checkpoints_table).where(
                            merkle_checkpoints_table.c.checkpoint_id == checkpoint_id,
                            merkle_checkpoints_table.c.case_id == case_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _checkpoint_from_row(row) if row else None

    async def list_checkpoints(
        self, case_id: UUID, *, limit: int, offset: int
    ) -> list[MerkleCheckpointRecord]:
        """Return a bounded, case-scoped checkpoint page; never integrity leaves."""
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(merkle_checkpoints_table)
                        .where(merkle_checkpoints_table.c.case_id == case_id)
                        .order_by(merkle_checkpoints_table.c.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .mappings()
                .all()
            )
        return [_checkpoint_from_row(row) for row in rows]

    async def get_event_by_idempotency_key(
        self, case_id: UUID, idempotency_key: str
    ) -> IntegrityEventRecord | None:
        """Internal reconciliation lookup, structurally scoped to one case."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(integrity_events_table).where(
                            integrity_events_table.c.case_id == case_id,
                            integrity_events_table.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _event_from_row(row) if row else None

    async def get_signature(self, checkpoint_id: UUID) -> CheckpointSignatureRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(checkpoint_signatures_table).where(
                            checkpoint_signatures_table.c.checkpoint_id == checkpoint_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _signature_from_row(row) if row else None
