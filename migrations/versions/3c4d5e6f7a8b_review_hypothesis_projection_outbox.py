"""review_hypothesis_projection_events: durable replay outbox for G13

Closes gap register G13. Mirrors `graph_projection_jobs`' shape (status/
attempt/lease/last_error), reusing the same `GraphProjectionJobStatus`
values, but scoped generically to `(subject_type, subject_id)` so it can
represent both a candidate review decision and a hypothesis action without
a second near-duplicate table per subject kind. Intentionally a SEPARATE
table from `graph_update_events` (Phase 5's correlation outbox) and
`graph_projection_jobs` (observation projection) -- see
`docs/decisions/ADR-022-review-hypothesis-projection-replay.md` for why a
new, minimal outbox was built rather than genericizing either existing one.

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c4d5e6f7a8b"
down_revision: str | None = "2b3c4d5e6f7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = ("queued", "running", "succeeded", "failed", "deferred")
_SUBJECT_TYPES = ("candidate_review_decision", "hypothesis_created", "hypothesis_review_decision")


def upgrade() -> None:
    op.create_table(
        "review_hypothesis_projection_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('" + "','".join(_STATUSES) + "')",
            name="ck_review_hypothesis_projection_events_status",
        ),
        sa.CheckConstraint(
            "subject_type IN ('" + "','".join(_SUBJECT_TYPES) + "')",
            name="ck_review_hypothesis_projection_events_subject_type",
        ),
        sa.UniqueConstraint(
            "case_id",
            "subject_type",
            "subject_id",
            name="uq_review_hypothesis_projection_events_subject",
        ),
    )
    op.create_index(
        "ix_review_hypothesis_projection_events_status",
        "review_hypothesis_projection_events",
        ["status"],
    )
    op.create_index(
        "ix_review_hypothesis_projection_events_case_id",
        "review_hypothesis_projection_events",
        ["case_id"],
    )


def downgrade() -> None:
    op.drop_table("review_hypothesis_projection_events")
