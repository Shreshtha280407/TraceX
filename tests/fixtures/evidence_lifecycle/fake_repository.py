"""In-memory duck-typed stand-in for `EvidenceLifecycleRepository`.

Implements the same async method signatures, including the real migration's
uniqueness constraints (`(case_id, upload_idempotency_key)` and
`worker_jobs.idempotency_key`), by raising `sqlalchemy.exc.IntegrityError`
just like a live PostgreSQL insert would -- so `service.py`'s
conflict-detection path is exercisable without a real database. See
`tests/integration/evidence_lifecycle/` for tests against a live one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy.exc

from app.contracts.evidence import EvidenceClassification, EvidenceProcessingStatus, SourceType
from app.contracts.worker import WorkerStatus
from app.core.canonical import canonical_sha256
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


@dataclass
class FakeGraphProjectionJob:
    """Shape-only stand-in for a `graph_projection_jobs` row.

    Just enough fields to let `evidence_lifecycle`'s own tests assert that
    one row is durably enqueued per accepted observation -- the real
    claim/lease/retry lifecycle over this table is `app/modules/graph/`'s
    responsibility, tested against its own fixtures.
    """

    projection_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_id: UUID
    status: str
    attempt: int
    max_attempts: int


class FakeEvidenceLifecycleRepository:
    def __init__(self) -> None:
        self.evidence: dict[UUID, EvidenceRecord] = {}
        self.jobs: dict[UUID, WorkerJobRecord] = {}
        self.results: dict[UUID, WorkerResultRecord] = {}
        self.observations: dict[UUID, ObservationRecord] = {}
        self.graph_projection_jobs: dict[UUID, FakeGraphProjectionJob] = {}
        self.observation_batches: dict[UUID, ObservationBatchRecord] = {}
        self.transformations: dict[UUID, ObservationTransformationRecord] = {}
        self.progress_events: dict[UUID, WorkerProgressEventRecord] = {}
        self.media_manifests: dict[UUID, MediaManifestRecord] = {}
        self.media_chunks: dict[UUID, MediaChunkRecord] = {}
        self.media_checkpoints: dict[UUID, MediaCheckpointRecord] = {}
        self.media_artifacts: dict[UUID, dict[str, object]] = {}
        self._next_progress_ordinal = 1

    async def close(self) -> None:
        pass

    async def create_media_manifest(self, manifest: ChunkManifest) -> bool:
        if manifest.manifest_id in self.media_manifests:
            return False
        self.media_manifests[manifest.manifest_id] = MediaManifestRecord(
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
        for item in manifest.chunks:
            chunk_id = chunk_identity(manifest, item.index)
            self.media_chunks[chunk_id] = MediaChunkRecord(
                chunk_id=chunk_id,
                manifest_id=manifest.manifest_id,
                case_id=manifest.case_id,
                evidence_id=manifest.evidence_id,
                job_id=manifest.job_id,
                chunk_index=item.index,
                canonical_boundary=item.boundary.model_dump(mode="json"),
                status="pending",
                publication_hash=None,
                observation_batch_id=None,
                completed_at=None,
                created_at=manifest.created_at,
            )
        return True

    async def get_media_manifest(self, manifest_id: UUID) -> MediaManifestRecord | None:
        return self.media_manifests.get(manifest_id)

    async def get_media_chunk(self, chunk_id: UUID) -> MediaChunkRecord | None:
        return self.media_chunks.get(chunk_id)

    async def get_latest_media_checkpoint(
        self, case_id: UUID, job_id: UUID, manifest_id: UUID
    ) -> MediaCheckpointRecord | None:
        values = [
            item
            for item in self.media_checkpoints.values()
            if item.case_id == case_id and item.job_id == job_id and item.manifest_id == manifest_id
        ]
        return max(values, key=lambda item: item.created_at) if values else None

    async def create_evidence_with_job(
        self, evidence: EvidenceRecord, job: WorkerJobRecord
    ) -> None:
        if evidence.upload_idempotency_key is not None and any(
            e.case_id == evidence.case_id
            and e.upload_idempotency_key == evidence.upload_idempotency_key
            for e in self.evidence.values()
        ):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate upload_idempotency_key", {}, Exception("unique violation")
            )
        if any(j.idempotency_key == job.idempotency_key for j in self.jobs.values()):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate worker_jobs.idempotency_key", {}, Exception("unique violation")
            )
        self.evidence[evidence.evidence_id] = evidence
        self.jobs[job.job_id] = job

    async def get_job_by_idempotency_key(
        self, case_id: UUID, idempotency_key: str
    ) -> WorkerJobRecord | None:
        return next(
            (
                j
                for j in self.jobs.values()
                if j.case_id == case_id and j.idempotency_key == idempotency_key
            ),
            None,
        )

    async def create_job(self, job: WorkerJobRecord) -> None:
        if any(j.idempotency_key == job.idempotency_key for j in self.jobs.values()):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate worker_jobs.idempotency_key", {}, Exception("unique violation")
            )
        self.jobs[job.job_id] = job

    async def get_evidence(self, case_id: UUID, evidence_id: UUID) -> EvidenceRecord | None:
        record = self.evidence.get(evidence_id)
        return record if record is not None and record.case_id == case_id else None

    async def list_evidence(
        self, case_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[EvidenceRecord]:
        records = sorted(
            (e for e in self.evidence.values() if e.case_id == case_id),
            key=lambda e: e.created_at,
            reverse=True,
        )
        return records[offset : offset + limit]

    async def search_evidence(
        self,
        case_id: UUID,
        *,
        query: str | None = None,
        source_type: str | None = None,
        processing_status: str | None = None,
        classification: str | None = None,
        uploaded_by: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvidenceRecord]:
        """In-memory counterpart of the repository's case-scoped library search."""
        records = [record for record in self.evidence.values() if record.case_id == case_id]
        if source_type is not None:
            records = [record for record in records if record.source_type.value == source_type]
        if processing_status is not None:
            records = [
                record for record in records if record.processing_status.value == processing_status
            ]
        if classification is not None:
            records = [
                record for record in records if record.classification.value == classification
            ]
        if uploaded_by is not None:
            records = [record for record in records if record.uploaded_by == uploaded_by]
        if query:
            needle = query.lower()
            matching_evidence_ids = {
                observation.evidence_id
                for observation in self.observations.values()
                if observation.case_id == case_id
                and needle in str(observation.canonical_payload).lower()
            }
            records = [
                record
                for record in records
                if needle in record.original_filename.lower()
                or needle in str(record.evidence_id).lower()
                or needle in record.source_type.value
                or record.evidence_id in matching_evidence_ids
            ]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[offset : offset + limit]

    async def update_evidence_classification(
        self, case_id: UUID, evidence_id: UUID, *, classification: str, now: datetime
    ) -> EvidenceRecord | None:
        record = await self.get_evidence(case_id, evidence_id)
        if record is None:
            return None
        updated = record.model_copy(
            update={"classification": EvidenceClassification(classification), "updated_at": now}
        )
        self.evidence[evidence_id] = updated
        return updated

    async def update_evidence_route_with_job(
        self,
        *,
        evidence: EvidenceRecord,
        source_type: str,
        parser_profile: str,
        job: WorkerJobRecord,
    ) -> EvidenceRecord:
        updated = evidence.model_copy(
            update={
                "source_type": SourceType(source_type),
                "parser_profile": parser_profile,
                "processing_status": EvidenceProcessingStatus.QUEUED,
                "updated_at": job.requested_at,
            }
        )
        self.evidence[evidence.evidence_id] = updated
        self.jobs[job.job_id] = job
        return updated

    async def get_evidence_by_idempotency_key(
        self, case_id: UUID, upload_idempotency_key: str
    ) -> EvidenceRecord | None:
        return next(
            (
                e
                for e in self.evidence.values()
                if e.case_id == case_id and e.upload_idempotency_key == upload_idempotency_key
            ),
            None,
        )

    async def get_job(self, case_id: UUID, job_id: UUID) -> WorkerJobRecord | None:
        job = self.jobs.get(job_id)
        return job if job is not None and job.case_id == case_id else None

    async def get_job_by_evidence(self, case_id: UUID, evidence_id: UUID) -> WorkerJobRecord | None:
        candidates = [
            job
            for job in self.jobs.values()
            if job.case_id == case_id and job.evidence_id == evidence_id
        ]
        return max(candidates, key=lambda job: job.requested_at) if candidates else None

    async def list_observation_payloads_for_evidence(
        self, case_id: UUID, evidence_id: UUID, *, limit: int = 8
    ) -> list[dict[str, object]]:
        records = sorted(
            (
                observation
                for observation in self.observations.values()
                if observation.case_id == case_id and observation.evidence_id == evidence_id
            ),
            key=lambda observation: observation.created_at,
        )
        return [dict(record.canonical_payload) for record in records[:limit]]

    async def mark_job_dispatched(self, job_id: UUID, dispatched_at: datetime) -> None:
        job = self.jobs.get(job_id)
        if job is not None:
            self.jobs[job_id] = job.model_copy(
                update={"dispatched_at": dispatched_at, "updated_at": dispatched_at}
            )

    async def get_job_by_id(self, job_id: UUID) -> WorkerJobRecord | None:
        return self.jobs.get(job_id)

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
        eligible = sorted(
            (
                job
                for job in self.jobs.values()
                if job.processor_name == processor_name
                and job.processor_version == processor_version
                and (
                    job.status is WorkerStatus.QUEUED
                    or (
                        job.status is WorkerStatus.RUNNING
                        and job.lease_expires_at is not None
                        and job.lease_expires_at < now
                        and job.attempt < job.max_attempts
                    )
                )
            ),
            key=lambda job: job.requested_at,
        )
        if not eligible:
            return None
        candidate = eligible[0]
        was_reclaim = candidate.status is WorkerStatus.RUNNING
        new_attempt = candidate.attempt + 1 if was_reclaim else candidate.attempt
        claimed = candidate.model_copy(
            update={
                "status": WorkerStatus.RUNNING,
                "attempt": new_attempt,
                "claimed_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "claimed_by": processor_name,
                "claimed_by_worker_id": claimed_by_worker_id,
                "claim_token_hash": claim_token_hash,
                "updated_at": now,
            }
        )
        self.jobs[claimed.job_id] = claimed
        return claimed, was_reclaim

    async def renew_lease(
        self, job_id: UUID, *, now: datetime, lease_seconds: int, max_lease_seconds: int
    ) -> datetime | None:
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.status is not WorkerStatus.RUNNING
            or job.lease_expires_at is None
            or job.lease_expires_at < now
        ):
            return None
        candidate_expires_at = now + timedelta(seconds=lease_seconds)
        ceiling = (job.claimed_at or now) + timedelta(seconds=max_lease_seconds)
        lease_expires_at = min(candidate_expires_at, ceiling)
        self.jobs[job_id] = job.model_copy(
            update={"lease_expires_at": lease_expires_at, "updated_at": now}
        )
        return lease_expires_at

    async def get_retry_exhausted_jobs(self, *, now: datetime) -> list[WorkerJobRecord]:
        return [
            job
            for job in self.jobs.values()
            if job.status is WorkerStatus.RUNNING
            and job.lease_expires_at is not None
            and job.lease_expires_at < now
            and job.attempt >= job.max_attempts
        ]

    # --- worker result -------------------------------------------------------

    async def get_result_for_job(self, job_id: UUID) -> WorkerResultRecord | None:
        return next((r for r in self.results.values() if r.job_id == job_id), None)

    async def list_observations_for_result(self, result_id: UUID) -> list[ObservationRecord]:
        return [o for o in self.observations.values() if o.result_id == result_id]

    async def count_observations_for_job(self, job_id: UUID) -> int:
        return sum(1 for o in self.observations.values() if o.job_id == job_id)

    async def submit_result(
        self,
        *,
        job_id: UUID,
        expected_claim_token_hash: str,
        result: WorkerResultRecord,
        observations: list[ObservationRecord],
        graph_projection_max_attempts: int,
    ) -> None:
        if any(r.job_id == result.job_id for r in self.results.values()):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate worker_results.job_id", {}, Exception("unique violation")
            )
        job = self.jobs.get(job_id)
        if job is None or job.claim_token_hash != expected_claim_token_hash:
            return
        self.results[result.result_id] = result
        for observation in observations:
            self.observations[observation.observation_id] = observation
            self.graph_projection_jobs[observation.observation_id] = FakeGraphProjectionJob(
                projection_id=uuid4(),
                case_id=observation.case_id,
                evidence_id=observation.evidence_id,
                observation_id=observation.observation_id,
                status="queued",
                attempt=0,
                max_attempts=graph_projection_max_attempts,
            )
        self.jobs[job_id] = job.model_copy(
            update={
                "status": result.status,
                "last_error_code": result.error_code,
                "last_error_message": result.error_message,
                "last_error_retryable": result.error_retryable,
                "updated_at": result.updated_at,
            }
        )

    # --- observation batches (Phase 3) --------------------------------------

    async def get_batch_by_job_and_batch_id(
        self, job_id: UUID, batch_id: str
    ) -> ObservationBatchRecord | None:
        return next(
            (
                b
                for b in self.observation_batches.values()
                if b.job_id == job_id and b.batch_id == batch_id
            ),
            None,
        )

    async def get_batch_by_job_and_idempotency_key(
        self, job_id: UUID, idempotency_key: str
    ) -> ObservationBatchRecord | None:
        return next(
            (
                b
                for b in self.observation_batches.values()
                if b.job_id == job_id and b.idempotency_key == idempotency_key
            ),
            None,
        )

    async def list_observations_for_batch(
        self, observation_batch_id: UUID
    ) -> list[ObservationRecord]:
        return [
            o for o in self.observations.values() if o.observation_batch_id == observation_batch_id
        ]

    async def list_transformations_for_batch(
        self, observation_batch_id: UUID
    ) -> list[ObservationTransformationRecord]:
        return sorted(
            (
                t
                for t in self.transformations.values()
                if t.observation_batch_id == observation_batch_id
            ),
            key=lambda t: t.ordinal,
        )

    async def get_latest_progress_event(self, job_id: UUID) -> WorkerProgressEventRecord | None:
        candidates = sorted(
            (p for p in self.progress_events.values() if p.job_id == job_id),
            key=lambda p: p.ordinal,
        )
        return candidates[-1] if candidates else None

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
        if any(
            b.job_id == batch.job_id and b.batch_id == batch.batch_id
            for b in self.observation_batches.values()
        ):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate observation_batches.(job_id, batch_id)",
                {},
                Exception("unique violation"),
            )
        if any(
            b.job_id == batch.job_id and b.idempotency_key == batch.idempotency_key
            for b in self.observation_batches.values()
        ):
            raise sqlalchemy.exc.IntegrityError(
                "duplicate observation_batches.(job_id, idempotency_key)",
                {},
                Exception("unique violation"),
            )
        for observation in observations:
            if observation.observation_id in self.observations:
                raise sqlalchemy.exc.IntegrityError(
                    "duplicate worker_observations.observation_id",
                    {},
                    Exception("unique violation"),
                )

        self.observation_batches[batch.observation_batch_id] = batch
        for observation in observations:
            self.observations[observation.observation_id] = observation
            self.graph_projection_jobs[observation.observation_id] = FakeGraphProjectionJob(
                projection_id=uuid4(),
                case_id=observation.case_id,
                evidence_id=observation.evidence_id,
                observation_id=observation.observation_id,
                status="queued",
                attempt=0,
                max_attempts=graph_projection_max_attempts,
            )
        for transformation in transformations:
            self.transformations[transformation.transformation_id] = transformation

        if media_publication is not None:
            chunk = self.media_chunks.get(media_publication.chunk_id)
            if chunk is None or chunk.status != "pending":
                raise sqlalchemy.exc.IntegrityError(
                    "invalid media chunk", {}, Exception("unique violation")
                )
            for artifact in media_publication.artifacts:
                if artifact.artifact_id in self.media_artifacts:
                    raise sqlalchemy.exc.IntegrityError(
                        "duplicate media artifact", {}, Exception("unique violation")
                    )
                self.media_artifacts[artifact.artifact_id] = artifact.model_dump(mode="json")
            self.media_chunks[chunk.chunk_id] = chunk.model_copy(
                update={
                    "status": "completed",
                    "publication_hash": canonical_sha256(media_publication),
                    "observation_batch_id": batch.observation_batch_id,
                    "completed_at": media_publication.completed_at,
                }
            )
            completed = sorted(
                (
                    item
                    for item in self.media_chunks.values()
                    if item.manifest_id == media_publication.manifest_id
                    and item.status == "completed"
                ),
                key=lambda item: item.chunk_index,
            )
            manifest = self.media_manifests[media_publication.manifest_id]
            self.media_checkpoints[media_publication.checkpoint_id] = MediaCheckpointRecord(
                checkpoint_id=media_publication.checkpoint_id,
                manifest_id=manifest.manifest_id,
                case_id=batch.case_id,
                evidence_id=batch.evidence_id,
                job_id=batch.job_id,
                manifest_hash=media_publication.manifest_hash,
                processor_version=manifest.processor_version,
                configuration_hash=manifest.configuration_hash,
                completed_chunk_ids=[item.chunk_id for item in completed],
                observation_batch_ids=[
                    item.observation_batch_id for item in completed if item.observation_batch_id
                ],
                artifact_ids=list(self.media_artifacts),
                created_at=media_publication.completed_at,
            )

        if progress_event is None:
            return None
        persisted = progress_event.model_copy(update={"ordinal": self._next_progress_ordinal})
        self._next_progress_ordinal += 1
        self.progress_events[persisted.progress_event_id] = persisted
        return persisted
