"""phase 6 part 5 candidate review decisions and evidence-backed hypotheses

Revision ID: a3b4c5d6e7f8
Revises: e26f7a8b9c0d
Create Date: 2026-09-15

Adds three additive, Shreshtha-owned tables for the Phase 6 Part 5 review
and hypothesis workflow. `candidate_review_decisions` and
`hypothesis_actions` are append-only audit trails, protected by the same
`tracex_reject_integrity_record_mutation` trigger function Nipun's Phase 6
Part 2 migration created for `integrity_events`/`merkle_checkpoints`/
`checkpoint_signatures`. `hypotheses` is a normal mutable row (its status
changes exactly once, on the one allowed review decision) and is
deliberately NOT append-only, mirroring `correlation_records`.

Does not modify `correlation_records`, `correlation_candidate_links`, or
any other existing Phase 5/6 table or trigger.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3b4c5d6e7f8"
down_revision = "e26f7a8b9c0d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidate_review_decisions",
        sa.Column("candidate_review_decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_link_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("rationale_commitment_sha256", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('accepted_by_reviewer', 'rejected_by_reviewer')",
            name="ck_candidate_review_decisions_decision",
        ),
        sa.UniqueConstraint(
            "case_id",
            "candidate_link_id",
            name="uq_candidate_review_decisions_case_candidate",
        ),
    )
    op.create_index(
        "ix_candidate_review_decisions_case_created",
        "candidate_review_decisions",
        ["case_id", "created_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_candidate_review_decisions_append_only "
        "BEFORE UPDATE OR DELETE ON candidate_review_decisions "
        "FOR EACH ROW EXECUTE FUNCTION tracex_reject_integrity_record_mutation()"
    )

    op.create_table(
        "hypotheses",
        sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("supporting_observation_ids", postgresql.JSONB(), nullable=False),
        sa.Column("supporting_candidate_ids", postgresql.JSONB(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("statement_commitment_sha256", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("rationale_commitment_sha256", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('needs_review', 'accepted_by_reviewer', 'rejected_by_reviewer')",
            name="ck_hypotheses_status",
        ),
    )
    op.create_index("ix_hypotheses_case_created", "hypotheses", ["case_id", "created_at"])

    op.create_table(
        "hypothesis_actions",
        sa.Column("hypothesis_action_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "hypothesis_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hypotheses.hypothesis_id"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("rationale_commitment_sha256", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "action IN ('created', 'accepted_by_reviewer', 'rejected_by_reviewer')",
            name="ck_hypothesis_actions_action",
        ),
        sa.UniqueConstraint(
            "case_id",
            "idempotency_key",
            name="uq_hypothesis_actions_case_idempotency",
        ),
    )
    op.create_index(
        "ix_hypothesis_actions_case_created", "hypothesis_actions", ["case_id", "created_at"]
    )
    op.execute(
        "CREATE TRIGGER trg_hypothesis_actions_append_only "
        "BEFORE UPDATE OR DELETE ON hypothesis_actions "
        "FOR EACH ROW EXECUTE FUNCTION tracex_reject_integrity_record_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_hypothesis_actions_append_only ON hypothesis_actions")
    op.drop_index("ix_hypothesis_actions_case_created", table_name="hypothesis_actions")
    op.drop_table("hypothesis_actions")

    op.drop_index("ix_hypotheses_case_created", table_name="hypotheses")
    op.drop_table("hypotheses")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_candidate_review_decisions_append_only "
        "ON candidate_review_decisions"
    )
    op.drop_index(
        "ix_candidate_review_decisions_case_created", table_name="candidate_review_decisions"
    )
    op.drop_table("candidate_review_decisions")
