"""PostgreSQL durable boundary for Phase 5 correlation propositions.

The insert path writes the proposition and its graph-update event in the
same transaction.  Neo4j is intentionally not contacted from this module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.contracts.observation import ObservationV1
from app.core.canonical import canonical_sha256
from app.core.config import Settings
from app.core.ids import deterministic_uuid
from app.modules.evidence_lifecycle.repository import worker_observations_table
from app.modules.graph.integration_models import (
    CORRELATION_EVENT_TYPE,
    INTEGRATION_SCHEMA_VERSION,
    CandidateLinkRecord,
    CorrelationProjectionContext,
    CorrelationRecord,
    CorrelationSubmission,
    CorrelationSubmissionReceipt,
    EvidencePathSnapshot,
    FeatureSnapshotRecord,
    GraphUpdateEventRecord,
    GraphUpdateEventStatus,
)

metadata = sa.MetaData()

correlation_records_table = sa.Table(
    "correlation_records",
    metadata,
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("correlation_type", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("supporting_observation_ids", postgresql.JSONB(), nullable=False),
    sa.Column("contradictory_observation_ids", postgresql.JSONB(), nullable=False),
    sa.Column("evidence_paths", postgresql.JSONB(), nullable=False),
    sa.Column("mapping_version", sa.Text(), nullable=False),
    sa.Column("config_version", sa.Text(), nullable=False),
    sa.Column("hypothesis_reference", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
candidate_links_table = sa.Table(
    "correlation_candidate_links",
    metadata,
    sa.Column("candidate_link_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("left_observation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("right_observation_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("reason_reference", sa.Text(), nullable=True),
    sa.Column("evidence_paths", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
feature_snapshots_table = sa.Table(
    "correlation_feature_snapshots",
    metadata,
    sa.Column("feature_snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("snapshot_version", sa.Text(), nullable=False),
    sa.Column("config_hash", sa.Text(), nullable=False),
    sa.Column("values", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
graph_update_events_table = sa.Table(
    "graph_update_events",
    metadata,
    sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("event_type", sa.Text(), nullable=False),
    sa.Column("schema_version", sa.Text(), nullable=False),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("aggregate_type", sa.Text(), nullable=False),
    sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("payload_reference", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("mapping_version", sa.Text(), nullable=False),
    sa.Column("config_version", sa.Text(), nullable=False),
    sa.Column("provenance_observation_ids", postgresql.JSONB(), nullable=False),
    sa.Column("projection_key", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("attempt", sa.Integer(), nullable=False),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_error_code", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
)


class IntegrationValidationError(ValueError):
    """Safe domain failure: references were absent, mismatched, or cross-case."""


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.postgres_dsn))


def _json(model: object) -> object:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model


def _correlation(row: sa.RowMapping | dict[str, object]) -> CorrelationRecord:
    values = dict(row)
    for name in ("supporting_observation_ids", "contradictory_observation_ids"):
        values[name] = tuple(UUID(value) for value in values[name])
    values["evidence_paths"] = tuple(
        EvidencePathSnapshot.model_validate(v) for v in values["evidence_paths"]
    )
    return CorrelationRecord.model_validate(values)


def _candidate(row: sa.RowMapping | dict[str, object]) -> CandidateLinkRecord:
    values = dict(row)
    values["evidence_paths"] = tuple(
        EvidencePathSnapshot.model_validate(v) for v in values["evidence_paths"]
    )
    return CandidateLinkRecord.model_validate(values)


def _event(row: sa.RowMapping | dict[str, object]) -> GraphUpdateEventRecord:
    values = dict(row)
    values["provenance_observation_ids"] = tuple(
        UUID(value) for value in values["provenance_observation_ids"]
    )
    return GraphUpdateEventRecord.model_validate(values)


def _feature(row: sa.RowMapping | dict[str, object]) -> FeatureSnapshotRecord:
    return FeatureSnapshotRecord.model_validate(dict(row))


def _path(observation: ObservationV1) -> EvidencePathSnapshot:
    return EvidencePathSnapshot(
        evidence_id=observation.evidence_id,
        observation_id=observation.observation_id,
        source_locator=observation.source_locator,
        extractor=observation.extractor,
        observation_created_at=observation.created_at,
        event_time=observation.event_time,
        time_window_start=observation.time_window.start if observation.time_window else None,
        time_window_end=observation.time_window.end if observation.time_window else None,
    )


def _submission_fingerprint(case_id: UUID, submission: CorrelationSubmission) -> str:
    """All semantically immutable fields a replay must reproduce exactly."""
    return canonical_sha256(
        {
            "case_id": str(case_id),
            "idempotency_key": submission.idempotency_key,
            "correlation_type": submission.correlation_type,
            "status": submission.status.value,
            "supporting_observation_ids": [str(v) for v in submission.supporting_observation_ids],
            "contradictory_observation_ids": [
                str(v) for v in submission.contradictory_observation_ids
            ],
            "mapping_version": submission.mapping_version,
            "config_version": submission.config_version,
            "hypothesis_reference": submission.hypothesis_reference,
            "candidate_links": sorted(
                [candidate.model_dump(mode="json") for candidate in submission.candidate_links],
                key=lambda candidate: candidate["idempotency_key"],
            ),
            "feature_snapshot": (
                submission.feature_snapshot.model_dump(mode="json")
                if submission.feature_snapshot is not None
                else None
            ),
        }
    )


class GraphCorrelationIntegrationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def submit(
        self, *, case_id: UUID, submission: CorrelationSubmission, now: datetime | None = None
    ) -> CorrelationSubmissionReceipt:
        """Persist a proposition and its replayable event atomically.

        Observation IDs are reloaded from canonical PostgreSQL rows inside
        the transaction.  This derives, rather than trusts, evidence paths
        and rejects cross-case or missing references before any write.
        """
        now = now or datetime.now(UTC)
        correlation_id = deterministic_uuid(
            "phase_5_correlation", str(case_id), submission.idempotency_key
        )
        event_id = deterministic_uuid(
            "phase_5_graph_update", str(case_id), CORRELATION_EVENT_TYPE, submission.idempotency_key
        )
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        sa.select(correlation_records_table).where(
                            correlation_records_table.c.case_id == case_id,
                            correlation_records_table.c.idempotency_key
                            == submission.idempotency_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                correlation = _correlation(existing)
                candidate_rows = (
                    (
                        await conn.execute(
                            sa.select(candidate_links_table)
                            .where(
                                candidate_links_table.c.correlation_id == correlation.correlation_id
                            )
                            .order_by(candidate_links_table.c.idempotency_key)
                        )
                    )
                    .mappings()
                    .all()
                )
                feature_row = (
                    (
                        await conn.execute(
                            sa.select(feature_snapshots_table).where(
                                feature_snapshots_table.c.correlation_id
                                == correlation.correlation_id
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                persisted_fingerprint = canonical_sha256(
                    {
                        "case_id": str(case_id),
                        "idempotency_key": correlation.idempotency_key,
                        "correlation_type": correlation.correlation_type,
                        "status": correlation.status.value,
                        "supporting_observation_ids": [
                            str(v) for v in correlation.supporting_observation_ids
                        ],
                        "contradictory_observation_ids": [
                            str(v) for v in correlation.contradictory_observation_ids
                        ],
                        "mapping_version": correlation.mapping_version,
                        "config_version": correlation.config_version,
                        "hypothesis_reference": correlation.hypothesis_reference,
                        "candidate_links": [
                            {
                                "idempotency_key": candidate.idempotency_key,
                                "left_observation_id": str(candidate.left_observation_id),
                                "right_observation_id": str(candidate.right_observation_id),
                                "status": candidate.status.value,
                                "reason_reference": candidate.reason_reference,
                            }
                            for candidate in (_candidate(row) for row in candidate_rows)
                        ],
                        "feature_snapshot": (
                            {
                                "snapshot_version": _feature(feature_row).snapshot_version,
                                "config_hash": _feature(feature_row).config_hash,
                                "values": _feature(feature_row).values,
                            }
                            if feature_row is not None
                            else None
                        ),
                    }
                )
                if persisted_fingerprint != _submission_fingerprint(case_id, submission):
                    raise IntegrationValidationError(
                        "idempotency key conflicts with an existing correlation"
                    )
                event_row = (
                    (
                        await conn.execute(
                            sa.select(graph_update_events_table).where(
                                graph_update_events_table.c.aggregate_id
                                == correlation.correlation_id
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                return CorrelationSubmissionReceipt(
                    correlation=correlation, event=_event(event_row), replayed=True
                )

            referenced_ids = set(submission.supporting_observation_ids) | set(
                submission.contradictory_observation_ids
            )
            for candidate in submission.candidate_links:
                referenced_ids.update(
                    (candidate.left_observation_id, candidate.right_observation_id)
                )
            rows = (
                (
                    await conn.execute(
                        sa.select(worker_observations_table.c.canonical_payload).where(
                            worker_observations_table.c.case_id == case_id,
                            worker_observations_table.c.observation_id.in_(referenced_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
            observations = {
                ObservationV1.model_validate(
                    row["canonical_payload"]
                ).observation_id: ObservationV1.model_validate(row["canonical_payload"])
                for row in rows
            }
            if set(observations) != referenced_ids:
                raise IntegrationValidationError(
                    "one or more observation references are missing or outside this case"
                )
            paths = tuple(
                _path(observations[observation_id])
                for observation_id in sorted(referenced_ids, key=str)
            )
            correlation_values = {
                "correlation_id": correlation_id,
                "case_id": case_id,
                "idempotency_key": submission.idempotency_key,
                "correlation_type": submission.correlation_type,
                "status": submission.status.value,
                "supporting_observation_ids": [
                    str(v) for v in submission.supporting_observation_ids
                ],
                "contradictory_observation_ids": [
                    str(v) for v in submission.contradictory_observation_ids
                ],
                "evidence_paths": [_json(path) for path in paths],
                "mapping_version": submission.mapping_version,
                "config_version": submission.config_version,
                "hypothesis_reference": submission.hypothesis_reference,
                "created_at": now,
                "updated_at": now,
            }
            await conn.execute(sa.insert(correlation_records_table).values(**correlation_values))
            for candidate in submission.candidate_links:
                candidate_id = deterministic_uuid(
                    "phase_5_candidate_link", str(correlation_id), candidate.idempotency_key
                )
                candidate_paths = tuple(
                    _path(observations[ref])
                    for ref in (candidate.left_observation_id, candidate.right_observation_id)
                )
                await conn.execute(
                    sa.insert(candidate_links_table).values(
                        candidate_link_id=candidate_id,
                        correlation_id=correlation_id,
                        case_id=case_id,
                        idempotency_key=candidate.idempotency_key,
                        left_observation_id=candidate.left_observation_id,
                        right_observation_id=candidate.right_observation_id,
                        status=candidate.status.value,
                        reason_reference=candidate.reason_reference,
                        evidence_paths=[_json(path) for path in candidate_paths],
                        created_at=now,
                    )
                )
            if submission.feature_snapshot is not None:
                snapshot = submission.feature_snapshot
                await conn.execute(
                    sa.insert(feature_snapshots_table).values(
                        feature_snapshot_id=deterministic_uuid(
                            "phase_5_feature_snapshot",
                            str(correlation_id),
                            snapshot.snapshot_version,
                            snapshot.config_hash,
                        ),
                        correlation_id=correlation_id,
                        case_id=case_id,
                        snapshot_version=snapshot.snapshot_version,
                        config_hash=snapshot.config_hash,
                        values=snapshot.values,
                        created_at=now,
                    )
                )
            projection_key = canonical_sha256(
                {
                    "event_type": CORRELATION_EVENT_TYPE,
                    "case_id": str(case_id),
                    "correlation_id": str(correlation_id),
                    "mapping_version": submission.mapping_version,
                    "config_version": submission.config_version,
                }
            )
            event_values = {
                "event_id": event_id,
                "event_type": CORRELATION_EVENT_TYPE,
                "schema_version": INTEGRATION_SCHEMA_VERSION,
                "case_id": case_id,
                "aggregate_type": "correlation",
                "aggregate_id": correlation_id,
                "payload_reference": correlation_id,
                "mapping_version": submission.mapping_version,
                "config_version": submission.config_version,
                "provenance_observation_ids": [str(v) for v in sorted(referenced_ids, key=str)],
                "projection_key": projection_key,
                "idempotency_key": submission.idempotency_key,
                "status": GraphUpdateEventStatus.QUEUED.value,
                "attempt": 0,
                "lease_expires_at": None,
                "last_error_code": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            await conn.execute(sa.insert(graph_update_events_table).values(**event_values))
            return CorrelationSubmissionReceipt(
                correlation=_correlation(correlation_values),
                event=_event(event_values),
                replayed=False,
            )

    async def get_correlation(
        self, case_id: UUID, correlation_id: UUID
    ) -> CorrelationRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(correlation_records_table).where(
                            correlation_records_table.c.case_id == case_id,
                            correlation_records_table.c.correlation_id == correlation_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _correlation(row) if row else None

    async def list_correlations(self, case_id: UUID) -> list[CorrelationRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(correlation_records_table)
                        .where(correlation_records_table.c.case_id == case_id)
                        .order_by(correlation_records_table.c.created_at.desc())
                    )
                )
                .mappings()
                .all()
            )
        return [_correlation(row) for row in rows]

    async def get_candidate_link(
        self, case_id: UUID, candidate_link_id: UUID
    ) -> CandidateLinkRecord | None:
        """Case-scoped single-candidate lookup, for Shreshtha's Phase 6 Part 5
        review workflow (`review_service.py`) -- mirrors `get_correlation`'s
        exact shape and scoping."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(candidate_links_table).where(
                            candidate_links_table.c.case_id == case_id,
                            candidate_links_table.c.candidate_link_id == candidate_link_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _candidate(row) if row else None

    async def list_candidates(self, case_id: UUID) -> list[CandidateLinkRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(candidate_links_table)
                        .where(candidate_links_table.c.case_id == case_id)
                        .order_by(candidate_links_table.c.created_at.desc())
                    )
                )
                .mappings()
                .all()
            )
        return [_candidate(row) for row in rows]

    async def get_event(self, case_id: UUID, event_id: UUID) -> GraphUpdateEventRecord | None:
        """Load an outbox event only through its known case scope.

        This internal worker helper is intentionally case-scoped too, so a
        future caller cannot accidentally turn a globally valid event UUID
        into a cross-case lookup primitive.
        """
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(graph_update_events_table).where(
                            graph_update_events_table.c.case_id == case_id,
                            graph_update_events_table.c.event_id == event_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _event(row) if row else None

    async def get_event_for_correlation(
        self, case_id: UUID, correlation_id: UUID
    ) -> GraphUpdateEventRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(graph_update_events_table).where(
                            graph_update_events_table.c.case_id == case_id,
                            graph_update_events_table.c.aggregate_id == correlation_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _event(row) if row else None

    async def claim_events(
        self, *, now: datetime, lease_seconds: int, batch_size: int
    ) -> list[GraphUpdateEventRecord]:
        async with self._engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(graph_update_events_table)
                        .where(
                            sa.or_(
                                graph_update_events_table.c.status.in_(
                                    (
                                        GraphUpdateEventStatus.QUEUED.value,
                                        GraphUpdateEventStatus.DEFERRED.value,
                                    )
                                ),
                                sa.and_(
                                    graph_update_events_table.c.status
                                    == GraphUpdateEventStatus.RUNNING.value,
                                    graph_update_events_table.c.lease_expires_at < now,
                                ),
                            ),
                        )
                        .order_by(graph_update_events_table.c.created_at)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            lease = now + timedelta(seconds=lease_seconds)
            claimed = []
            for row in rows:
                await conn.execute(
                    sa.update(graph_update_events_table)
                    .where(graph_update_events_table.c.event_id == row["event_id"])
                    .values(
                        status=GraphUpdateEventStatus.RUNNING.value,
                        attempt=row["attempt"] + 1,
                        lease_expires_at=lease,
                        updated_at=now,
                    )
                )
                values = dict(row)
                values.update(
                    status=GraphUpdateEventStatus.RUNNING.value,
                    attempt=row["attempt"] + 1,
                    lease_expires_at=lease,
                    updated_at=now,
                )
                claimed.append(_event(values))
            return claimed

    async def projection_context(
        self, event: GraphUpdateEventRecord
    ) -> CorrelationProjectionContext | None:
        correlation = await self.get_correlation(event.case_id, event.payload_reference)
        if correlation is None:
            return None
        candidates = tuple(
            candidate
            for candidate in await self.list_candidates(event.case_id)
            if candidate.correlation_id == correlation.correlation_id
        )
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(feature_snapshots_table).where(
                            feature_snapshots_table.c.correlation_id == correlation.correlation_id,
                            feature_snapshots_table.c.case_id == event.case_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return CorrelationProjectionContext(
            event=event,
            correlation=correlation,
            candidates=candidates,
            feature_snapshot=_feature(row) if row else None,
        )

    async def mark_event_succeeded(self, event_id: UUID, now: datetime) -> None:
        await self._transition(
            event_id, now, GraphUpdateEventStatus.SUCCEEDED, None, completed=True
        )

    async def mark_event_retryable(
        self, event_id: UUID, now: datetime, error_code: str, *, deferred: bool = False
    ) -> None:
        await self._transition(
            event_id,
            now,
            GraphUpdateEventStatus.DEFERRED if deferred else GraphUpdateEventStatus.QUEUED,
            error_code,
            completed=False,
        )

    async def mark_event_failed(self, event_id: UUID, now: datetime, error_code: str) -> None:
        await self._transition(
            event_id, now, GraphUpdateEventStatus.FAILED, error_code, completed=True
        )

    async def _transition(
        self,
        event_id: UUID,
        now: datetime,
        status: GraphUpdateEventStatus,
        error_code: str | None,
        *,
        completed: bool,
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(graph_update_events_table)
                .where(graph_update_events_table.c.event_id == event_id)
                .values(
                    status=status.value,
                    lease_expires_at=None,
                    last_error_code=error_code,
                    updated_at=now,
                    completed_at=now if completed else None,
                )
            )
