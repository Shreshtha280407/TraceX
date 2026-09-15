"""add append-only visual and communication integrity provenance

Revision ID: e26f7a8b9c0d
Revises: d15e6f7a8b9c
Create Date: 2026-09-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e26f7a8b9c0d"
down_revision = "d15e6f7a8b9c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "modality_observation_integrity_provenance",
        sa.Column("provenance_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provenance_kind", sa.Text(), nullable=False),
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
            name="uq_modality_integrity_provenance_case_observation_schema",
        ),
        sa.UniqueConstraint(
            "case_id",
            "idempotency_key",
            name="uq_modality_integrity_provenance_case_idempotency",
        ),
    )
    op.create_index(
        "ix_modality_integrity_provenance_case_created",
        "modality_observation_integrity_provenance",
        ["case_id", "created_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_modality_observation_integrity_provenance_append_only "
        "BEFORE UPDATE OR DELETE ON modality_observation_integrity_provenance "
        "FOR EACH ROW EXECUTE FUNCTION tracex_reject_integrity_record_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_modality_observation_integrity_provenance_append_only "
        "ON modality_observation_integrity_provenance"
    )
    op.drop_index(
        "ix_modality_integrity_provenance_case_created",
        table_name="modality_observation_integrity_provenance",
    )
    op.drop_table("modality_observation_integrity_provenance")
