"""entity layer: entities, entity_resolution_candidates, entity_review_decisions

Closes gap register G2. `entities` stores `EntityV1` (`app/contracts/entity.py`,
frozen) rows -- the first production writer of that contract.
`entity_resolution_candidates` are POSSIBLY_SAME_AS-style propositions the
existing observation-retrieval cascade (`graph.intelligence.retrieval`)
produces between two entities; nothing here merges them. `entity_review_
decisions` is the only authorized path that can treat two entities as the
same identity (`verified_same`) or retract that (`split`) -- append-only,
mirroring `candidate_review_decisions`' trigger precedent, but (unlike that
table) allows more than one decision per candidate pair over time, since a
merge must be reversible.

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2b3c4d5e6f7a"
down_revision: str | None = "1a2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("canonical_label", sa.Text(), nullable=False),
        sa.Column(
            "aliases", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "stable_identifiers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "attributes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("created_from_observation_ids", postgresql.JSONB(), nullable=False),
        sa.Column(
            "review_status", sa.Text(), nullable=False, server_default=sa.text("'unreviewed'")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_observation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="The single observation this entity was created from (WP-2 scope).",
        ),
        sa.UniqueConstraint(
            "case_id", "source_observation_id", name="uq_entities_case_source_observation"
        ),
    )
    op.create_index("ix_entities_case_id", "entities", ["case_id"])

    op.create_table(
        "entity_resolution_candidates",
        sa.Column(
            "entity_resolution_candidate_id", postgresql.UUID(as_uuid=True), primary_key=True
        ),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "left_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.entity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "right_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.entity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column(
            "identifier_types",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("vector_score", sa.Float(), nullable=True),
        sa.Column(
            "contradiction_reasons",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("supporting_observation_ids", postgresql.JSONB(), nullable=False),
        sa.Column("config_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "case_id",
            "left_entity_id",
            "right_entity_id",
            "config_version",
            name="uq_entity_resolution_candidates_pair",
        ),
    )
    op.create_index(
        "ix_entity_resolution_candidates_case_id", "entity_resolution_candidates", ["case_id"]
    )

    op.create_table(
        "entity_review_decisions",
        sa.Column("entity_review_decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "entity_resolution_candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "entity_resolution_candidates.entity_resolution_candidate_id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("rationale_commitment_sha256", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_entity_review_decisions_candidate_id",
        "entity_review_decisions",
        ["entity_resolution_candidate_id"],
    )
    op.create_index("ix_entity_review_decisions_case_id", "entity_review_decisions", ["case_id"])

    # Append-only, reusing the exact trigger function Nipun's integrity
    # tables and Shreshtha's candidate_review_decisions table already use.
    op.execute(
        "CREATE TRIGGER trg_entity_review_decisions_append_only "
        "BEFORE UPDATE OR DELETE ON entity_review_decisions "
        "FOR EACH ROW EXECUTE FUNCTION tracex_reject_integrity_record_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_entity_review_decisions_append_only ON entity_review_decisions"
    )
    op.drop_table("entity_review_decisions")
    op.drop_table("entity_resolution_candidates")
    op.drop_table("entities")
