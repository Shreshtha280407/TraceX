"""Typed async PostgreSQL repository for the evidence-lifecycle module.

Mirrors `app.modules.access_control.repository`'s conventions exactly:
hand-written `sqlalchemy.Table` objects (this module does not import the
migration, and `migrations/env.py` keeps `target_metadata = None` -- see
the note at the top of
`migrations/versions/<rev>_evidence_lifecycle_foundation.py`), every
statement built with SQLAlchemy Core's expression language, values always
bound parameters. Keep the two in sync when either changes.

Every query here is scoped by `case_id` at the WHERE-clause level, not only
enforced by the caller -- case isolation is structural, not a convention an
API route could accidentally skip.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.contracts.evidence import EvidenceProcessingStatus
from app.contracts.worker import WorkerStatus
from app.core.canonical import canonical_sha256
from app.core.config import Settings
from app.modules.evidence_lifecycle.media_orchestration import (
    ChunkManifest,
    MediaChunkPublication,
    chunk_identity,
)
from app.modules.evidence_lifecycle.models import (
    EvidenceRecord,
    MediaCheckpointRecord,
    MediaChunkRecord,
    MediaManifestRecord,
    ObservationBatchRecord,
    ObservationRecord,
    ObservationTransformationRecord,
    WorkerJobRecord,
    WorkerProgressEventRecord,
    WorkerResultRecord,
)


def _dump_for_insert(model: BaseModel, enum_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """`model.model_dump(mode="python")`, with named enum fields reduced to their plain `.value`.

    See `app.modules.access_control.repository._dump_for_insert` for why.
    """
    values = model.model_dump(mode="python")
    for field in enum_fields:
        values[field] = values[field].value
    return values


metadata = sa.MetaData()

evidence_records_table = sa.Table(
    "evidence_records",
    metadata,
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("source_type", sa.Text(), nullable=False),
    sa.Column("original_filename", sa.Text(), nullable=False),
    sa.Column("content_type", sa.Text(), nullable=False),
    sa.Column("object_uri", sa.Text(), nullable=False),
    sa.Column("sha256", sa.Text(), nullable=False),
    sa.Column("classification", sa.Text(), nullable=False),
    sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("parser_profile", sa.Text(), nullable=True),
    sa.Column("processing_status", sa.Text(), nullable=False),
    sa.Column("upload_idempotency_key", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

worker_jobs_table = sa.Table(
    "worker_jobs",
    metadata,
    sa.Column("job_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("source_type", sa.Text(), nullable=False),
    sa.Column("processor_name", sa.Text(), nullable=False),
    sa.Column("processor_version", sa.Text(), nullable=False),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("max_attempts", sa.Integer(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("input_object_uri", sa.Text(), nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("claimed_by", sa.Text(), nullable=True),
    sa.Column("claimed_by_worker_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("claim_token_hash", sa.Text(), nullable=True),
    sa.Column("last_error_code", sa.Text(), nullable=True),
    sa.Column("last_error_message", sa.Text(), nullable=True),
    sa.Column("last_error_retryable", sa.Boolean(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

worker_results_table = sa.Table(
    "worker_results",
    metadata,
    sa.Column("result_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("derived_artifacts", postgresql.JSONB(), nullable=False),
    sa.Column("checkpoint", sa.Text(), nullable=True),
    sa.Column("error_code", sa.Text(), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.Column("error_retryable", sa.Boolean(), nullable=True),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("payload_hash", sa.Text(), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

worker_observations_table = sa.Table(
    "worker_observations",
    metadata,
    sa.Column("observation_id", postgresql.UUID(as_uuid=True), primary_key=True),
    # Exactly one of `result_id` (Phase 2.1 terminal `WorkerResultV1`) /
    # `observation_batch_id` (Phase 3 partial-batch submission) is ever set --
    # see `ObservationRecord`'s docstring and `docs/architecture/
    # phase-3-decisions.md`.
    sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("observation_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("observation_type", sa.Text(), nullable=False),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

#: One immutable receipt per accepted partial `ObservationBatchSubmissionV1` --
#: see `ObservationBatchRecord`'s docstring.
observation_batches_table = sa.Table(
    "observation_batches",
    metadata,
    sa.Column("observation_batch_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("batch_id", sa.Text(), nullable=False),
    sa.Column("batch_sequence", sa.Integer(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("is_final_batch", sa.Boolean(), nullable=False),
    sa.Column("observation_count", sa.Integer(), nullable=False),
    sa.Column("payload_hash", sa.Text(), nullable=False),
    sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

#: One durable row per `TransformationProvenanceV1` accepted in a batch.
observation_transformations_table = sa.Table(
    "observation_transformations",
    metadata,
    sa.Column("transformation_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("observation_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("step_name", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

#: An ordered, append-only progress-event history -- `ordinal` is a
#: server-assigned, globally monotonic tiebreaker (see the migration).
worker_progress_events_table = sa.Table(
    "worker_progress_events",
    metadata,
    sa.Column("progress_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("ordinal", sa.BigInteger(), nullable=False),
    sa.Column("observation_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("stage", sa.Text(), nullable=False),
    sa.Column("units_total", sa.Integer(), nullable=True),
    sa.Column("units_completed", sa.Integer(), nullable=False),
    sa.Column("observations_emitted", sa.Integer(), nullable=False),
    sa.Column("batch_sequence", sa.Integer(), nullable=False),
    sa.Column("message_code", sa.Text(), nullable=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

#: A durable projection queue/outbox row: "this accepted observation needs
#: (re)projecting into Neo4j." Defined here because this module owns the
#: transaction that inserts it (`submit_result` below, atomic with
#: `worker_results`/`worker_observations`); `app/modules/graph/
#: outbox_repository.py` imports this exact `Table` object to claim/update
#: rows -- a one-directional import, since `app.modules.graph` is a
#: downstream consumer of this module's accepted results (like every other
#: extraction module, just over a direct PostgreSQL connection instead of
#: the internal worker HTTP API, per its own explicit "backend-owned
#: internal service" design -- see
#: `docs/architecture/graph-projection.md`). This module itself only ever
#: inserts the initial `queued` row here; it never reads or transitions
#: one, and it never imports `app.modules.graph` back (see
#: `tests/security/evidence_lifecycle/test_evidence_lifecycle_boundaries.py`).
graph_projection_jobs_table = sa.Table(
    "graph_projection_jobs",
    metadata,
    sa.Column("projection_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("max_attempts", sa.Integer(), nullable=False),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_error_code", sa.Text(), nullable=True),
    sa.Column("last_error_message", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
)

# Phase 4 media orchestration records.  These hold only canonical metadata
# and opaque object references; source/derived bytes remain in object storage.
media_chunk_manifests_table = sa.Table(
    "media_chunk_manifests",
    metadata,
    sa.Column("manifest_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("version", sa.Text(), nullable=False),
    sa.Column("manifest_hash", sa.Text(), nullable=False),
    sa.Column("processor_name", sa.Text(), nullable=False),
    sa.Column("processor_version", sa.Text(), nullable=False),
    sa.Column("configuration_hash", sa.Text(), nullable=False),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
)

media_chunks_table = sa.Table(
    "media_chunks",
    metadata,
    sa.Column("chunk_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("chunk_index", sa.Integer(), nullable=False),
    sa.Column("canonical_boundary", postgresql.JSONB(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("publication_hash", sa.Text(), nullable=True),
    sa.Column("observation_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

media_derived_artifacts_table = sa.Table(
    "media_derived_artifacts",
    metadata,
    sa.Column("artifact_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("canonical_payload", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

media_chunk_observations_table = sa.Table(
    "media_chunk_observations",
    metadata,
    sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
)

media_checkpoints_table = sa.Table(
    "media_checkpoints",
    metadata,
    sa.Column("checkpoint_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("manifest_hash", sa.Text(), nullable=False),
    sa.Column("processor_version", sa.Text(), nullable=False),
    sa.Column("configuration_hash", sa.Text(), nullable=False),
    sa.Column("completed_chunk_ids", postgresql.JSONB(), nullable=False),
    sa.Column("observation_batch_ids", postgresql.JSONB(), nullable=False),
    sa.Column("artifact_ids", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

#: The one status this module ever writes -- every later transition
#: (`running`/`succeeded`/`failed`/`deferred`) is `app/modules/graph/`'s.
_GRAPH_PROJECTION_STATUS_QUEUED = "queued"

#: `evidence_records.processing_status`'s terminal value for each terminal
#: `WorkerResultV1.status` a job can be submitted with (`docs/architecture/
#: worker-job-lifecycle.md`: "deferred"/"cancelled" are equally terminal
#: outcomes, never auto-retried by anything in this repository) --
#: `EvidenceProcessingStatus` itself has no separate deferred/cancelled
#: state, so both map to `failed`: the evidence did not finish processing
#: and nothing will retry it on its own.
_RESULT_STATUS_TO_EVIDENCE_STATUS: dict[WorkerStatus, EvidenceProcessingStatus] = {
    WorkerStatus.SUCCEEDED: EvidenceProcessingStatus.PROCESSED,
    WorkerStatus.FAILED: EvidenceProcessingStatus.FAILED,
    WorkerStatus.DEFERRED: EvidenceProcessingStatus.FAILED,
    WorkerStatus.CANCELLED: EvidenceProcessingStatus.FAILED,
}


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async PostgreSQL engine from application configuration."""
    return create_async_engine(str(settings.postgres_dsn))


def _evidence_from_row(row: sa.RowMapping) -> EvidenceRecord:
    return EvidenceRecord.model_validate(dict(row))


def _job_from_row(row: sa.RowMapping) -> WorkerJobRecord:
    return WorkerJobRecord.model_validate(dict(row))


def _result_from_row(row: sa.RowMapping) -> WorkerResultRecord:
    return WorkerResultRecord.model_validate(dict(row))


def _observation_from_row(row: sa.RowMapping) -> ObservationRecord:
    return ObservationRecord.model_validate(dict(row))


def _observation_batch_from_row(row: sa.RowMapping) -> ObservationBatchRecord:
    return ObservationBatchRecord.model_validate(dict(row))


def _transformation_from_row(row: sa.RowMapping) -> ObservationTransformationRecord:
    return ObservationTransformationRecord.model_validate(dict(row))


def _progress_event_from_row(row: sa.RowMapping) -> WorkerProgressEventRecord:
    return WorkerProgressEventRecord.model_validate(dict(row))


def _media_manifest_from_row(row: sa.RowMapping) -> MediaManifestRecord:
    return MediaManifestRecord.model_validate(dict(row))


def _media_chunk_from_row(row: sa.RowMapping) -> MediaChunkRecord:
    return MediaChunkRecord.model_validate(dict(row))


def _media_checkpoint_from_row(row: sa.RowMapping) -> MediaCheckpointRecord:
    return MediaCheckpointRecord.model_validate(dict(row))


class EvidenceLifecycleRepository:
    """Typed async persistence for evidence metadata and durable worker jobs.

    Accepts an injected `AsyncEngine` (see `create_engine`) so tests can
    point it at a real ephemeral test database; there is no in-memory fake
    for this repository -- the integration tests self-skip instead when
    PostgreSQL isn't reachable (see `tests/integration/evidence_lifecycle/`).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    # --- media orchestration (Phase 4) -----------------------------------

    async def create_media_manifest(self, manifest: ChunkManifest) -> bool:
        """Create an immutable manifest and all ordered chunks in one transaction.

        `False` means the deterministic manifest identity already exists; the
        service compares its canonical hash before treating that as a replay.
        """
        manifest_record = MediaManifestRecord(
            manifest_id=manifest.manifest_id,
            case_id=manifest.case_id,
            evidence_id=manifest.evidence_id,
            job_id=manifest.job_id,
            version=manifest.version,
            manifest_hash=manifest.manifest_hash,
            processor_name=manifest.processor_name,
            processor_version=manifest.processor_version,
            configuration_hash=manifest.configuration_hash,
            canonical_payload=manifest.model_dump(mode="json"),
            created_at=manifest.created_at,
        )
        values = _dump_for_insert(manifest_record)
        chunk_values = [
            {
                "chunk_id": chunk_identity(manifest, item.index),
                "manifest_id": manifest.manifest_id,
                "case_id": manifest.case_id,
                "evidence_id": manifest.evidence_id,
                "job_id": manifest.job_id,
                "chunk_index": item.index,
                "canonical_boundary": item.boundary.model_dump(mode="json"),
                "status": "pending",
                "publication_hash": None,
                "observation_batch_id": None,
                "completed_at": None,
                "created_at": manifest.created_at,
            }
            for item in manifest.chunks
        ]
        try:
            async with self._engine.begin() as conn:
                await conn.execute(sa.insert(media_chunk_manifests_table).values(**values))
                await conn.execute(sa.insert(media_chunks_table), chunk_values)
        except sa.exc.IntegrityError:
            return False
        return True

    async def get_media_manifest(self, manifest_id: UUID) -> MediaManifestRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(media_chunk_manifests_table).where(
                            media_chunk_manifests_table.c.manifest_id == manifest_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _media_manifest_from_row(row) if row is not None else None

    async def get_media_chunk(self, chunk_id: UUID) -> MediaChunkRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(media_chunks_table).where(
                            media_chunks_table.c.chunk_id == chunk_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _media_chunk_from_row(row) if row is not None else None

    async def get_latest_media_checkpoint(
        self, case_id: UUID, job_id: UUID, manifest_id: UUID
    ) -> MediaCheckpointRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(media_checkpoints_table)
                        .where(
                            media_checkpoints_table.c.case_id == case_id,
                            media_checkpoints_table.c.job_id == job_id,
                            media_checkpoints_table.c.manifest_id == manifest_id,
                        )
                        .order_by(media_checkpoints_table.c.created_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        return _media_checkpoint_from_row(row) if row is not None else None

    # --- evidence + job (single atomic write path) -----------------------

    async def create_evidence_with_job(
        self, evidence: EvidenceRecord, job: WorkerJobRecord
    ) -> None:
        """Insert the evidence row and its job row in one transaction.

        Atomic on purpose: an evidence row must never exist without its
        corresponding job row (or vice versa). A unique-constraint
        violation (concurrent duplicate `Idempotency-Key`, or a duplicate
        `worker_jobs.idempotency_key`) raises `sqlalchemy.exc.IntegrityError`
        and rolls the whole transaction back -- `service.py` handles that.
        """
        evidence_values = _dump_for_insert(
            evidence, ("source_type", "classification", "processing_status")
        )
        job_values = _dump_for_insert(job, ("source_type", "status"))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(evidence_records_table).values(**evidence_values))
            await conn.execute(sa.insert(worker_jobs_table).values(**job_values))

    async def get_job_by_idempotency_key(
        self, case_id: UUID, idempotency_key: str
    ) -> WorkerJobRecord | None:
        """Gap-Closure WP-4 (G7): the lookup half of `create_job`'s idempotent
        insert -- mirrors `get_evidence_by_idempotency_key`'s exact shape."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table).where(
                            worker_jobs_table.c.case_id == case_id,
                            worker_jobs_table.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _job_from_row(row) if row is not None else None

    async def create_job(self, job: WorkerJobRecord) -> None:
        """Gap-Closure WP-4 (G7): insert one standalone job row for
        already-existing evidence (`POST .../evidence/{id}/reprocess`) --
        unlike `create_evidence_with_job`, no evidence row is written (it
        already exists), but its `processing_status` is reset to `queued`
        in this same transaction: reprocessing a `processed`/`failed`
        evidence item is exactly the event that makes its old terminal
        status stale.
        Raises `sqlalchemy.exc.IntegrityError` on a duplicate
        `idempotency_key`; the caller (`service.reprocess_evidence`)
        resolves that via `get_job_by_idempotency_key`, exactly like
        `upload_evidence`'s own replay-or-conflict handling.
        """
        job_values = _dump_for_insert(job, ("source_type", "status"))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(worker_jobs_table).values(**job_values))
            await conn.execute(
                sa.update(evidence_records_table)
                .where(evidence_records_table.c.evidence_id == job.evidence_id)
                .values(
                    processing_status=EvidenceProcessingStatus.QUEUED.value,
                    updated_at=job.requested_at,
                )
            )

    # --- evidence (read) ---------------------------------------------------

    async def get_evidence(self, case_id: UUID, evidence_id: UUID) -> EvidenceRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(evidence_records_table).where(
                            evidence_records_table.c.case_id == case_id,
                            evidence_records_table.c.evidence_id == evidence_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _evidence_from_row(row) if row is not None else None

    async def list_evidence(
        self, case_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[EvidenceRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(evidence_records_table)
                        .where(evidence_records_table.c.case_id == case_id)
                        .order_by(evidence_records_table.c.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .mappings()
                .all()
            )
        return [_evidence_from_row(row) for row in rows]

    async def get_evidence_by_idempotency_key(
        self, case_id: UUID, upload_idempotency_key: str
    ) -> EvidenceRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(evidence_records_table).where(
                            evidence_records_table.c.case_id == case_id,
                            evidence_records_table.c.upload_idempotency_key
                            == upload_idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _evidence_from_row(row) if row is not None else None

    # --- jobs (read + dispatch bookkeeping) ---------------------------------

    async def get_job(self, case_id: UUID, job_id: UUID) -> WorkerJobRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table).where(
                            worker_jobs_table.c.case_id == case_id,
                            worker_jobs_table.c.job_id == job_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _job_from_row(row) if row is not None else None

    async def get_job_by_evidence(self, case_id: UUID, evidence_id: UUID) -> WorkerJobRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table).where(
                            worker_jobs_table.c.case_id == case_id,
                            worker_jobs_table.c.evidence_id == evidence_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _job_from_row(row) if row is not None else None

    async def mark_job_dispatched(self, job_id: UUID, dispatched_at: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(worker_jobs_table)
                .where(worker_jobs_table.c.job_id == job_id)
                .values(dispatched_at=dispatched_at, updated_at=dispatched_at)
            )

    async def get_job_by_id(self, job_id: UUID) -> WorkerJobRecord | None:
        """Unscoped-by-case lookup for the internal worker endpoints.

        A worker authenticates by holding a valid claim token for a
        specific `job_id`, not by case membership -- see
        `docs/architecture/worker-job-lifecycle.md`'s "Worker identity"
        section. Never exposed to a case-scoped user-facing caller.
        """
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table).where(worker_jobs_table.c.job_id == job_id)
                    )
                )
                .mappings()
                .first()
            )
        return _job_from_row(row) if row is not None else None

    # --- job claim (worker lifecycle) --------------------------------------

    async def claim_job(
        self,
        *,
        processor_name: str,
        processor_version: str,
        now: datetime,
        lease_seconds: int,
        claim_token_hash: str,
        claimed_by_worker_id: UUID | None = None,
    ) -> tuple[WorkerJobRecord, bool] | None:
        """Atomically claim one eligible job for `processor_name`/`processor_version`.

        Eligible means either genuinely `queued`, or `running` with an
        expired lease (a prior claim that was never resolved). `FOR UPDATE
        SKIP LOCKED` makes two concurrent callers structurally unable to
        claim the same row: the loser's `SELECT` simply skips the winner's
        locked row and finds a different eligible job (or none).

        `claimed_by_worker_id` (the authenticated worker's verified
        identity, when known) is written unconditionally on every
        successful claim -- including a reclaim, which is exactly how
        ownership legitimately transfers to a new worker after a lease
        expires (see `docs/architecture/worker-identity-and-security.md`).

        Returns `(claimed_job, was_reclaim)`, or `None` if nothing eligible
        exists right now. `was_reclaim` is `True` only when an expired
        lease was recovered (`attempt` is incremented in that case, never
        on a job's first-ever claim).
        """
        async with self._engine.begin() as conn:
            candidate = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table)
                        .where(
                            worker_jobs_table.c.processor_name == processor_name,
                            worker_jobs_table.c.processor_version == processor_version,
                            sa.or_(
                                # A job's first-ever claim is always allowed
                                # regardless of `max_attempts` -- `attempt`
                                # is already `1` at creation (it is not
                                # incremented on a first claim, see
                                # `docs/architecture/worker-job-lifecycle.md`'s
                                # "Attempt semantics"), so this is not a
                                # retry at all, even when `max_attempts == 1`.
                                worker_jobs_table.c.status == WorkerStatus.QUEUED.value,
                                sa.and_(
                                    worker_jobs_table.c.status == WorkerStatus.RUNNING.value,
                                    worker_jobs_table.c.lease_expires_at < now,
                                    # A reclaim increments `attempt` by one --
                                    # only allowed while there is room for
                                    # one more. A job that has already used
                                    # its last allowed attempt is never
                                    # reclaimed here -- see
                                    # `get_retry_exhausted_jobs`, which is
                                    # how such a row is actually transitioned
                                    # to `failed`.
                                    worker_jobs_table.c.attempt < worker_jobs_table.c.max_attempts,
                                ),
                            ),
                        )
                        .order_by(worker_jobs_table.c.requested_at.asc())
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .first()
            )
            if candidate is None:
                return None

            was_reclaim = candidate["status"] == WorkerStatus.RUNNING.value
            new_attempt = candidate["attempt"] + 1 if was_reclaim else candidate["attempt"]
            lease_expires_at = now + timedelta(seconds=lease_seconds)

            await conn.execute(
                sa.update(worker_jobs_table)
                .where(worker_jobs_table.c.job_id == candidate["job_id"])
                .values(
                    status=WorkerStatus.RUNNING.value,
                    attempt=new_attempt,
                    claimed_at=now,
                    lease_expires_at=lease_expires_at,
                    claimed_by=processor_name,
                    claimed_by_worker_id=claimed_by_worker_id,
                    claim_token_hash=claim_token_hash,
                    updated_at=now,
                )
            )
            # Gap-closure: `evidence_records.processing_status` previously
            # never left `queued` -- a claim (first-ever or a reclaim after
            # an expired lease) is the real "processing started" signal, so
            # it belongs here in the same transaction as the job's own
            # `running` transition.
            await conn.execute(
                sa.update(evidence_records_table)
                .where(evidence_records_table.c.evidence_id == candidate["evidence_id"])
                .values(processing_status=EvidenceProcessingStatus.PROCESSING.value, updated_at=now)
            )
            updated = dict(candidate)
            updated.update(
                status=WorkerStatus.RUNNING.value,
                attempt=new_attempt,
                claimed_at=now,
                lease_expires_at=lease_expires_at,
                claimed_by=processor_name,
                claimed_by_worker_id=claimed_by_worker_id,
                claim_token_hash=claim_token_hash,
                updated_at=now,
            )
            return WorkerJobRecord.model_validate(updated), was_reclaim

    async def renew_lease(
        self, job_id: UUID, *, now: datetime, lease_seconds: int, max_lease_seconds: int
    ) -> datetime | None:
        """Atomically extend a currently-`running`, unexpired-lease job's lease.

        Returns the new `lease_expires_at`, or `None` if the job is not
        `running` or its lease had already expired at `now` -- by the time
        this is called, `service.renew_claim` has already verified the
        caller's claim token and worker identity; this is a defensive,
        atomic re-check so a lease can never be extended past its own
        expiry (a legitimate reclaim by another worker, racing a slow
        renewal, must always win -- never both hold a "valid" lease at
        once).

        The new lease is also capped, via `LEAST(...)` evaluated in the same
        `UPDATE`, at `claimed_at + max_lease_seconds` -- this attempt's own
        absolute lifetime ceiling (`claimed_at` resets on every fresh claim/
        reclaim, so the ceiling is per-attempt, not per-job). No separate
        rejection path is needed for "renewed past the ceiling too many
        times": once real time passes the ceiling, the capped value stops
        advancing, `lease_expires_at` falls behind `now` on its own, and the
        `lease_expires_at >= now` re-check above starts failing naturally --
        exactly as if the worker had simply stopped renewing.
        """
        candidate_expires_at = now + timedelta(seconds=lease_seconds)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(worker_jobs_table)
                .where(
                    worker_jobs_table.c.job_id == job_id,
                    worker_jobs_table.c.status == WorkerStatus.RUNNING.value,
                    worker_jobs_table.c.lease_expires_at >= now,
                )
                .values(
                    lease_expires_at=sa.func.least(
                        candidate_expires_at,
                        worker_jobs_table.c.claimed_at + timedelta(seconds=max_lease_seconds),
                    ),
                    updated_at=now,
                )
                .returning(worker_jobs_table.c.lease_expires_at)
            )
            row = result.first()
        return row[0] if row is not None else None

    async def get_retry_exhausted_jobs(self, *, now: datetime) -> list[WorkerJobRecord]:
        """Every currently-`running`, lease-expired job with no attempts remaining.

        Read-only and deliberately never mutates or locks anything itself:
        `service.claim_job` transitions each returned job to `failed` by
        calling the existing `submit_result` with a synthetic terminal
        `WorkerResultV1` (`error.code="retry_exhausted"`) -- reusing its
        already-correct idempotency/conflict handling rather than a second,
        parallel "mark terminal" write path. Two concurrent callers seeing
        the same row here is therefore self-healing: the loser's
        `submit_result` call raises `sqlalchemy.exc.IntegrityError` on
        `worker_results.job_id`'s unique constraint (the same race
        `create_evidence_with_job`'s idempotency-key path already handles),
        which the caller swallows -- the winner already made it terminal.
        """
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(worker_jobs_table).where(
                            worker_jobs_table.c.status == WorkerStatus.RUNNING.value,
                            worker_jobs_table.c.lease_expires_at < now,
                            worker_jobs_table.c.attempt >= worker_jobs_table.c.max_attempts,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_job_from_row(row) for row in rows]

    # --- worker result (single atomic write path) ---------------------------

    async def get_result_for_job(self, job_id: UUID) -> WorkerResultRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_results_table).where(
                            worker_results_table.c.job_id == job_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _result_from_row(row) if row is not None else None

    async def list_observations_for_result(self, result_id: UUID) -> list[ObservationRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(worker_observations_table).where(
                            worker_observations_table.c.result_id == result_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_observation_from_row(row) for row in rows]

    async def count_observations_for_job(self, job_id: UUID) -> int:
        async with self._engine.connect() as conn:
            count = await conn.scalar(
                sa.select(sa.func.count())
                .select_from(worker_observations_table)
                .where(worker_observations_table.c.job_id == job_id)
            )
        return count or 0

    async def submit_result(
        self,
        *,
        job_id: UUID,
        expected_claim_token_hash: str,
        result: WorkerResultRecord,
        observations: list[ObservationRecord],
        graph_projection_max_attempts: int,
    ) -> None:
        """Insert the result + its observations and mark the job terminal, in one transaction.

        Guarded by `job_id` + the caller's already-verified claim token
        hash on the `UPDATE ... WHERE` clause as defense in depth; the
        primary concurrency-safety mechanism is `worker_results.job_id`'s
        unique constraint -- a losing concurrent submission raises
        `sqlalchemy.exc.IntegrityError` here, exactly like
        `create_evidence_with_job`'s idempotency-key race, and `service.py`
        handles it the same way (re-read, compare, replay-or-conflict).

        Also durably enqueues one `graph_projection_jobs` row per accepted
        observation, in this same transaction: a canonical observation is
        never accepted without a corresponding projection job, or vice
        versa -- see `docs/architecture/graph-projection.md`. Empty when
        `observations` is empty (a non-`succeeded` result), same as
        `worker_observations` above.
        """
        result_values = _dump_for_insert(result, ("status",))
        observation_values = [_dump_for_insert(o) for o in observations]
        projection_job_values = [
            {
                "projection_id": uuid4(),
                "case_id": observation.case_id,
                "evidence_id": observation.evidence_id,
                "observation_id": observation.observation_id,
                "status": _GRAPH_PROJECTION_STATUS_QUEUED,
                "attempt": 0,
                "max_attempts": graph_projection_max_attempts,
                "lease_expires_at": None,
                "last_error_code": None,
                "last_error_message": None,
                "created_at": observation.created_at,
                "updated_at": observation.created_at,
                "completed_at": None,
            }
            for observation in observations
        ]
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(worker_results_table).values(**result_values))
            if observation_values:
                await conn.execute(sa.insert(worker_observations_table), observation_values)
                await conn.execute(sa.insert(graph_projection_jobs_table), projection_job_values)
            job_update = await conn.execute(
                sa.update(worker_jobs_table)
                .where(
                    worker_jobs_table.c.job_id == job_id,
                    worker_jobs_table.c.claim_token_hash == expected_claim_token_hash,
                )
                .values(
                    status=result.status.value,
                    last_error_code=result.error_code,
                    last_error_message=result.error_message,
                    last_error_retryable=result.error_retryable,
                    updated_at=result.updated_at,
                )
            )
            # Gap-closure: mirrors the `claim_job` -> `processing` transition
            # above -- a terminal result is the real "processing finished"
            # signal for `evidence_records.processing_status`. Guarded on
            # the job update actually matching a row (mirrors that update's
            # own claim-token defense in depth): a stale/invalid token here
            # means the job's own status was never touched either, and the
            # evidence's status must not diverge from it.
            if job_update.rowcount > 0:
                await conn.execute(
                    sa.update(evidence_records_table)
                    .where(evidence_records_table.c.evidence_id == result.evidence_id)
                    .values(
                        processing_status=_RESULT_STATUS_TO_EVIDENCE_STATUS[result.status].value,
                        updated_at=result.updated_at,
                    )
                )

    # --- observation batches (Phase 3: partial micro-batch submission) ------

    async def get_batch_by_job_and_batch_id(
        self, job_id: UUID, batch_id: str
    ) -> ObservationBatchRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(observation_batches_table).where(
                            observation_batches_table.c.job_id == job_id,
                            observation_batches_table.c.batch_id == batch_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _observation_batch_from_row(row) if row is not None else None

    async def get_batch_by_job_and_idempotency_key(
        self, job_id: UUID, idempotency_key: str
    ) -> ObservationBatchRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(observation_batches_table).where(
                            observation_batches_table.c.job_id == job_id,
                            observation_batches_table.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _observation_batch_from_row(row) if row is not None else None

    async def list_observations_for_batch(
        self, observation_batch_id: UUID
    ) -> list[ObservationRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(worker_observations_table).where(
                            worker_observations_table.c.observation_batch_id == observation_batch_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_observation_from_row(row) for row in rows]

    async def list_transformations_for_batch(
        self, observation_batch_id: UUID
    ) -> list[ObservationTransformationRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(observation_transformations_table)
                        .where(
                            observation_transformations_table.c.observation_batch_id
                            == observation_batch_id
                        )
                        .order_by(observation_transformations_table.c.ordinal.asc())
                    )
                )
                .mappings()
                .all()
            )
        return [_transformation_from_row(row) for row in rows]

    async def get_latest_progress_event(self, job_id: UUID) -> WorkerProgressEventRecord | None:
        """The most recently accepted progress event for `job_id`, or `None`.

        Ordered by `ordinal` (server-assigned, globally monotonic), not
        `created_at` -- a race-free "which came last" even for two events
        inserted within the same millisecond.
        """
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_progress_events_table)
                        .where(worker_progress_events_table.c.job_id == job_id)
                        .order_by(worker_progress_events_table.c.ordinal.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        return _progress_event_from_row(row) if row is not None else None

    async def submit_observation_batch(
        self,
        *,
        batch: ObservationBatchRecord,
        observations: list[ObservationRecord],
        transformations: list[ObservationTransformationRecord],
        progress_event: WorkerProgressEventRecord | None,
        graph_projection_max_attempts: int,
        media_publication: MediaChunkPublication | None = None,
    ) -> WorkerProgressEventRecord | None:
        """Insert the batch receipt + its observations/transformations/progress event
        + one durable graph-projection job per newly accepted observation, all in
        one transaction.

        Mirrors `submit_result`'s identical shape and concurrency-safety
        reasoning: a duplicate `(job_id, batch_id)` or `(job_id,
        idempotency_key)` raises `sqlalchemy.exc.IntegrityError` via
        `observation_batches`'s own unique constraints; a duplicate
        `observation_id` reused from a completely different batch/result
        raises the same `IntegrityError` via `worker_observations
        .observation_id`'s primary key. `service.py` handles both the same
        way `submit_result` already does: re-read, compare, replay-or-conflict
        -- see `docs/architecture/phase-3-decisions.md`.

        Returns the persisted progress event with its real, server-assigned
        `ordinal` (`progress_event`'s own `ordinal` value is a placeholder,
        never written), or `None` if this batch carried no progress update.
        """
        batch_values = _dump_for_insert(batch)
        observation_values = [_dump_for_insert(o) for o in observations]
        transformation_values = [_dump_for_insert(t, ("status",)) for t in transformations]
        projection_job_values = [
            {
                "projection_id": uuid4(),
                "case_id": observation.case_id,
                "evidence_id": observation.evidence_id,
                "observation_id": observation.observation_id,
                "status": _GRAPH_PROJECTION_STATUS_QUEUED,
                "attempt": 0,
                "max_attempts": graph_projection_max_attempts,
                "lease_expires_at": None,
                "last_error_code": None,
                "last_error_message": None,
                "created_at": observation.created_at,
                "updated_at": observation.created_at,
                "completed_at": None,
            }
            for observation in observations
        ]
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(observation_batches_table).values(**batch_values))
            if observation_values:
                await conn.execute(sa.insert(worker_observations_table), observation_values)
                await conn.execute(sa.insert(graph_projection_jobs_table), projection_job_values)
            if transformation_values:
                await conn.execute(
                    sa.insert(observation_transformations_table), transformation_values
                )
            persisted_progress_event: WorkerProgressEventRecord | None = None
            if progress_event is not None:
                progress_values = _dump_for_insert(progress_event)
                # Server-assigned via `nextval()` (see the migration) --
                # never a client-supplied value.
                del progress_values["ordinal"]
                insert_result = await conn.execute(
                    sa.insert(worker_progress_events_table)
                    .values(**progress_values)
                    .returning(worker_progress_events_table.c.ordinal)
                )
                real_ordinal = insert_result.scalar_one()
                persisted_progress_event = progress_event.model_copy(
                    update={"ordinal": real_ordinal}
                )
            if media_publication is not None:
                publication_hash = canonical_sha256(media_publication)
                chunk = (
                    (
                        await conn.execute(
                            sa.select(media_chunks_table)
                            .where(media_chunks_table.c.chunk_id == media_publication.chunk_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    chunk is None
                    or chunk["manifest_id"] != media_publication.manifest_id
                    or chunk["case_id"] != batch.case_id
                    or chunk["evidence_id"] != batch.evidence_id
                    or chunk["job_id"] != batch.job_id
                    or chunk["chunk_index"] != media_publication.chunk_index
                    or chunk["status"] != "pending"
                ):
                    raise sa.exc.IntegrityError(
                        "invalid or already-completed media chunk", {}, Exception("constraint")
                    )
                artifact_values = [
                    {
                        "artifact_id": artifact.artifact_id,
                        "case_id": batch.case_id,
                        "evidence_id": batch.evidence_id,
                        "job_id": batch.job_id,
                        "manifest_id": media_publication.manifest_id,
                        "chunk_id": media_publication.chunk_id,
                        "idempotency_key": artifact.idempotency_key,
                        "canonical_payload": artifact.model_dump(mode="json"),
                        "created_at": media_publication.completed_at,
                    }
                    for artifact in media_publication.artifacts
                ]
                if artifact_values:
                    await conn.execute(sa.insert(media_derived_artifacts_table), artifact_values)
                if observation_values:
                    await conn.execute(
                        sa.insert(media_chunk_observations_table),
                        [
                            {
                                "chunk_id": media_publication.chunk_id,
                                "observation_id": value["observation_id"],
                            }
                            for value in observation_values
                        ],
                    )
                await conn.execute(
                    sa.update(media_chunks_table)
                    .where(media_chunks_table.c.chunk_id == media_publication.chunk_id)
                    .values(
                        status="completed",
                        publication_hash=publication_hash,
                        observation_batch_id=batch.observation_batch_id,
                        completed_at=media_publication.completed_at,
                    )
                )
                completed_rows = (
                    (
                        await conn.execute(
                            sa.select(
                                media_chunks_table.c.chunk_id,
                                media_chunks_table.c.observation_batch_id,
                            )
                            .where(
                                media_chunks_table.c.manifest_id == media_publication.manifest_id,
                                media_chunks_table.c.status == "completed",
                            )
                            .order_by(media_chunks_table.c.chunk_index)
                        )
                    )
                    .mappings()
                    .all()
                )
                chunk_count = (
                    await conn.execute(
                        sa.select(sa.func.count())
                        .select_from(media_chunks_table)
                        .where(media_chunks_table.c.manifest_id == media_publication.manifest_id)
                    )
                ).scalar_one()
                artifact_rows = (
                    (
                        await conn.execute(
                            sa.select(media_derived_artifacts_table.c.artifact_id)
                            .where(
                                media_derived_artifacts_table.c.manifest_id
                                == media_publication.manifest_id
                            )
                            .order_by(media_derived_artifacts_table.c.artifact_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                manifest = (
                    (
                        await conn.execute(
                            sa.select(media_chunk_manifests_table).where(
                                media_chunk_manifests_table.c.manifest_id
                                == media_publication.manifest_id
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                await conn.execute(
                    sa.insert(media_checkpoints_table).values(
                        checkpoint_id=media_publication.checkpoint_id,
                        manifest_id=media_publication.manifest_id,
                        case_id=batch.case_id,
                        evidence_id=batch.evidence_id,
                        job_id=batch.job_id,
                        manifest_hash=media_publication.manifest_hash,
                        processor_version=manifest["processor_version"],
                        configuration_hash=manifest["configuration_hash"],
                        completed_chunk_ids=[str(row["chunk_id"]) for row in completed_rows],
                        observation_batch_ids=[
                            str(row["observation_batch_id"])
                            for row in completed_rows
                            if row["observation_batch_id"] is not None
                        ],
                        artifact_ids=[str(item) for item in artifact_rows],
                        created_at=media_publication.completed_at,
                    )
                )
                if len(completed_rows) == chunk_count:
                    await conn.execute(
                        sa.update(media_chunk_manifests_table)
                        .where(
                            media_chunk_manifests_table.c.manifest_id
                            == media_publication.manifest_id
                        )
                        .values(completed_at=media_publication.completed_at)
                    )
        return persisted_progress_event
