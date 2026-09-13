"""phase 5 graph correlation integration foundation

Revision ID: b5f8d7c2a1e0
Revises: d3f1a6c9b8e2
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b5f8d7c2a1e0"
down_revision = "d3f1a6c9b8e2"
branch_labels = None
depends_on = None


def _check(column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=f"ck_phase5_{column}")


def upgrade() -> None:
    op.create_table(
        "correlation_records",
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
        sa.UniqueConstraint(
            "case_id", "idempotency_key", name="uq_correlation_records_case_idempotency"
        ),
        _check("status", ("candidate", "needs_review", "rejected")),
    )
    op.create_index(
        "ix_correlation_records_case_created", "correlation_records", ["case_id", "created_at"]
    )
    op.create_table(
        "correlation_candidate_links",
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
        sa.UniqueConstraint(
            "correlation_id", "idempotency_key", name="uq_candidate_links_correlation_idempotency"
        ),
        _check("status", ("candidate", "needs_review", "rejected")),
    )
    op.create_index(
        "ix_candidate_links_case_created", "correlation_candidate_links", ["case_id", "created_at"]
    )
    op.create_table(
        "correlation_feature_snapshots",
        sa.Column("feature_snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_version", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("values", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "graph_update_events",
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
        sa.UniqueConstraint(
            "case_id",
            "event_type",
            "idempotency_key",
            name="uq_graph_update_events_case_type_idempotency",
        ),
        _check("status", ("queued", "running", "succeeded", "failed", "deferred")),
    )
    op.create_index(
        "ix_graph_update_events_status_lease", "graph_update_events", ["status", "lease_expires_at"]
    )
    op.create_index(
        "ix_graph_update_events_case_created", "graph_update_events", ["case_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_graph_update_events_case_created", table_name="graph_update_events")
    op.drop_index("ix_graph_update_events_status_lease", table_name="graph_update_events")
    op.drop_table("graph_update_events")
    op.drop_table("correlation_feature_snapshots")
    op.drop_index("ix_candidate_links_case_created", table_name="correlation_candidate_links")
    op.drop_table("correlation_candidate_links")
    op.drop_index("ix_correlation_records_case_created", table_name="correlation_records")
    op.drop_table("correlation_records")
