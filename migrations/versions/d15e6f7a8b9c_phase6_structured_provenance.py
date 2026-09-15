"""phase 6 structured observation integrity provenance

Revision ID: d15e6f7a8b9c
Revises: c42d3e4f5a6b
Create Date: 2026-09-15

Stores only versioned safe metadata commitments for document/OCR, CDR, and
finance observations.  The same database-level append-only boundary used by
the original integrity records protects this durable projection.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d15e6f7a8b9c"
down_revision = "c42d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "structured_observation_integrity_provenance",
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
        sa.UniqueConstraint(
            "case_id",
            "observation_id",
            "schema_version",
            name="uq_structured_integrity_provenance_case_observation_schema",
        ),
        sa.UniqueConstraint(
            "case_id",
            "idempotency_key",
            name="uq_structured_integrity_provenance_case_idempotency",
        ),
    )
    op.create_index(
        "ix_structured_integrity_provenance_case_created",
        "structured_observation_integrity_provenance",
        ["case_id", "created_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_structured_observation_integrity_provenance_append_only "
        "BEFORE UPDATE OR DELETE ON structured_observation_integrity_provenance "
        "FOR EACH ROW EXECUTE FUNCTION tracex_reject_integrity_record_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_structured_observation_integrity_provenance_append_only "
        "ON structured_observation_integrity_provenance"
    )
    op.drop_index(
        "ix_structured_integrity_provenance_case_created",
        table_name="structured_observation_integrity_provenance",
    )
    op.drop_table("structured_observation_integrity_provenance")
