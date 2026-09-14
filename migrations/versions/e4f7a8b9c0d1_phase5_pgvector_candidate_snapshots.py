"""Add bounded, case-scoped pgvector snapshots for Phase 5 retrieval.

Revision ID: e4f7a8b9c0d1
Revises: b5f8d7c2a1e0
Create Date: 2026-09-14

This is additive. Vectors are retrieval aids with provider/configuration and
source-snapshot hashes; neither the table nor the extension stores evidence
payloads or establishes identities.
"""

from __future__ import annotations

from alembic import op

revision = "e4f7a8b9c0d1"
down_revision = "b5f8d7c2a1e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "CREATE TABLE graph_intelligence_vectors ("
        "vector_id UUID PRIMARY KEY, "
        "case_id UUID NOT NULL, "
        "observation_id UUID NOT NULL, "
        "provider TEXT NOT NULL, "
        "provider_version TEXT NOT NULL, "
        "configuration_hash TEXT NOT NULL, "
        "source_snapshot_hash TEXT NOT NULL, "
        "embedding vector(32) NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL, "
        "CONSTRAINT uq_graph_intelligence_vectors_case_observation "
        "UNIQUE (case_id, observation_id)"
        ")"
    )
    op.create_index(
        "ix_graph_intelligence_vectors_case_created",
        "graph_intelligence_vectors",
        ["case_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_graph_intelligence_vectors_case_created", table_name="graph_intelligence_vectors"
    )
    op.drop_table("graph_intelligence_vectors")
    # The extension can be used by another approved migration, so it is not
    # dropped during downgrade.
