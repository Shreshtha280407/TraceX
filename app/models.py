"""Operational control-plane records. Analytical facts are intentionally out of Phase 1."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_subject: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(256))
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    scoring_mode: Mapped[str] = mapped_column(String(32), default="unsupervised", server_default="unsupervised")
    candidate_domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaseMembership(Base):
    __tablename__ = "case_memberships"
    __table_args__ = (UniqueConstraint("case_id", "user_id", name="uq_case_membership"),)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidenceSource(Base):
    __tablename__ = "evidence_sources"
    __table_args__ = (UniqueConstraint("case_id", "sha256", name="uq_case_source_sha256"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(512))
    source_format: Mapped[str] = mapped_column(String(16))
    storage_relative_path: Mapped[str] = mapped_column(String(1024), unique=True)
    parser_revision: Mapped[str] = mapped_column(String(64), default="phase1-source-v1")
    acquisition_note: Mapped[str] = mapped_column(Text, default="authorised upload")
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (UniqueConstraint("case_id", "idempotency_key", name="uq_case_import_idempotency"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("evidence_sources.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    bytes_read: Mapped[int] = mapped_column(Integer, default=0)
    rows_seen: Mapped[int] = mapped_column(Integer, default=0)
    rows_accepted: Mapped[int] = mapped_column(Integer, default=0)
    rows_quarantined: Mapped[int] = mapped_column(Integer, default=0)
    # Cheap up-front record count, used only to turn rows_seen into a real
    # percentage. Null when the format can't be counted without a full parse.
    total_records: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ImportCheckpoint(Base):
    __tablename__ = "import_checkpoints"
    __table_args__ = (UniqueConstraint("job_id", "logical_record", name="uq_job_checkpoint_record"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True)
    logical_record: Mapped[int] = mapped_column(Integer)
    bytes_read: Mapped[int] = mapped_column(Integer)
    rows_seen: Mapped[int] = mapped_column(Integer)
    rows_accepted: Mapped[int] = mapped_column(Integer)
    rows_quarantined: Mapped[int] = mapped_column(Integer)
    parser_revision: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalysisStage(Base):
    __tablename__ = "analysis_stages"
    __table_args__ = (UniqueConstraint("job_id", "name", "attempt", name="uq_job_stage_attempt"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class FindingConfidence(Base):
    """Write-once explanation provenance; never alters a finding's evidence hash."""
    __tablename__ = "finding_confidence"
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    provenance: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AddressActivity(Base):
    """Provisional address participation, committed with the fragment receipts."""
    __tablename__ = "address_activity"
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), primary_key=True)
    address: Mapped[str] = mapped_column(String(512), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    transactions: Mapped[int] = mapped_column(Integer)
    participations: Mapped[int] = mapped_column(Integer)
    last_batch: Mapped[int] = mapped_column(Integer)


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("case_id", "version", name="uq_case_snapshot_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="provisional")
    provisional: Mapped[bool] = mapped_column(Boolean, default=True)
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FragmentReceipt(Base):
    __tablename__ = "fragment_receipts"
    __table_args__ = (
        UniqueConstraint("job_id", "logical_batch", "record_type", name="uq_job_fragment_batch_type"),
        UniqueConstraint("storage_relative_path", name="uq_fragment_path"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("evidence_sources.id", ondelete="CASCADE"), index=True)
    logical_batch: Mapped[int] = mapped_column(Integer)
    record_type: Mapped[str] = mapped_column(String(32))
    record_count: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    storage_relative_path: Mapped[str] = mapped_column(String(1024))
    parser_revision: Mapped[str] = mapped_column(String(64))
    source_record_start: Mapped[int] = mapped_column(Integer)
    source_record_end: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphSnapshot(Base):
    __tablename__ = "graph_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_id", name="uq_graph_snapshot_source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    storage_relative_path: Mapped[str] = mapped_column(String(1024), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    node_count: Mapped[int] = mapped_column(Integer)
    edge_count: Mapped[int] = mapped_column(Integer)
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SyntheticReviewSeed(Base):
    """Explicitly synthetic evaluation context; never an attribution record."""

    __tablename__ = "synthetic_review_seeds"
    __table_args__ = (UniqueConstraint("snapshot_id", "seed_entity_ref", "seed_reason", name="uq_snapshot_seed"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    seed_entity_ref: Mapped[str] = mapped_column(String(512), index=True)
    seed_reason: Mapped[str] = mapped_column(Text)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeatureRecord(Base):
    """Frozen, case/snapshot-scoped address-time-window feature row."""

    __tablename__ = "feature_records"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "entity_ref", "window_start", "window_end", name="uq_snapshot_feature_window"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    graph_snapshot_id: Mapped[str] = mapped_column(ForeignKey("graph_snapshots.id", ondelete="CASCADE"), index=True)
    entity_ref: Mapped[str] = mapped_column(String(512), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    feature_schema_version: Mapped[str] = mapped_column(String(64))
    feature_vector: Mapped[dict] = mapped_column(JSON, default=dict)
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FindingRecord(Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "entity_ref",
            "window_start",
            "window_end",
            "rule_id",
            name="uq_snapshot_finding_rule",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    graph_snapshot_id: Mapped[str] = mapped_column(ForeignKey("graph_snapshots.id", ondelete="CASCADE"), index=True)
    entity_ref: Mapped[str] = mapped_column(String(512), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rule_id: Mapped[str] = mapped_column(String(64))
    rule_version: Mapped[str] = mapped_column(String(64))
    claim: Mapped[str] = mapped_column(Text)
    finding_version: Mapped[int] = mapped_column(Integer, default=1)
    raw_score: Mapped[float] = mapped_column()
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    feature_vector: Mapped[dict] = mapped_column(JSON, default=dict)
    feature_vector_hash: Mapped[str] = mapped_column(String(64))
    explanations: Mapped[list] = mapped_column(JSON, default=list)
    benign_alternatives: Mapped[list] = mapped_column(JSON, default=list)
    opposing_evidence: Mapped[list] = mapped_column(JSON, default=list)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReviewDecisionRecord(Base):
    __tablename__ = "review_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"), index=True)
    finding_version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    disposition: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    counterevidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    prior_review_id: Mapped[str | None] = mapped_column(ForeignKey("review_decisions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaseEvent(Base):
    __tablename__ = "case_events"
    __table_args__ = (UniqueConstraint("case_id", "sequence", name="uq_case_event_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("import_jobs.id"), nullable=True, index=True)
    stage: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("case_events.id", ondelete="CASCADE"), unique=True)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditRecord(Base):
    __tablename__ = "audit_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=True, index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(128))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalyticsSnapshot(Base):
    """Entity clusters, Geo-IP enrichment, network correlation, flow arrays and
    graph embeddings for one completed snapshot (app.engine.analytics).

    Stored beside the immutable graph file rather than inside it, so the graph
    snapshot's content hash is unchanged by this derived layer.
    """

    __tablename__ = "analytics_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_id", name="uq_analytics_snapshot_source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    graph_snapshot_id: Mapped[str] = mapped_column(ForeignKey("graph_snapshots.id", ondelete="CASCADE"), index=True)
    storage_relative_path: Mapped[str] = mapped_column(String(1024), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    analytics_version: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalyticsRevision(Base):
    """Append-only recomputation; the original analytics receipt stays immutable."""

    __tablename__ = "analytics_revisions"
    __table_args__ = (UniqueConstraint("snapshot_id", "revision", name="uq_analytics_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    graph_snapshot_id: Mapped[str] = mapped_column(ForeignKey("graph_snapshots.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    storage_relative_path: Mapped[str] = mapped_column(String(1024), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    analytics_version: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalysisRequest(Base):
    """Durable refresh intent that survives failed/reclaimed worker attempts."""

    __tablename__ = "analysis_requests"
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    refresh_analytics: Mapped[bool] = mapped_column(Boolean, default=False)
    fulfilled: Mapped[bool] = mapped_column(Boolean, default=False)


class RiskSeed(Base):
    """An analyst-asserted starting point for risk propagation (e.g. a wallet
    named in a ransomware report). A seed is an input assumption with a stated
    reason and provenance, never a conclusion TraceX drew itself."""

    __tablename__ = "risk_seeds"
    __table_args__ = (UniqueConstraint("case_id", "wallet_ref", name="uq_case_risk_seed"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    wallet_ref: Mapped[str] = mapped_column(String(512), index=True)
    label: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(default=1.0)
    source: Mapped[str] = mapped_column(String(64), default="analyst")
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskRun(Base):
    """One propagation of the case's seeds over one analytics snapshot."""

    __tablename__ = "risk_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    method_version: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    seeds: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    scores: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeatureStoreRecord(Base):
    """One snapshot's address-window feature rows, stored as a Parquet file.

    These are write-once analytical rows -- ~8 per transaction, so ~25 million
    for a 3-million-row import -- which is why they live in a compressed
    columnar file in the evidence vault (app.engine.feature_store) rather than
    as JSON rows in the control-plane database.
    """

    __tablename__ = "feature_stores"
    __table_args__ = (UniqueConstraint("snapshot_id", name="uq_feature_store_snapshot"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"), index=True)
    graph_snapshot_id: Mapped[str] = mapped_column(ForeignKey("graph_snapshots.id", ondelete="CASCADE"), index=True)
    storage_relative_path: Mapped[str] = mapped_column(String(1024), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    feature_schema_version: Mapped[str] = mapped_column(String(64))
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
