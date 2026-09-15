"""phase 6 integrity foundation

Revision ID: 8f68fb441037
Revises: f4a1c9e0d2b3
Create Date: 2026-09-15

Creates the durable boundary for Phase 6 tamper-evident integrity events,
Merkle checkpoints, and Ed25519 checkpoint signatures. See
`docs/architecture/phase-6-integrity.md` and
`docs/decisions/ADR-013-phase-6-integrity-checkpoints.md`.

`integrity_sequence_counters` gives each case its own gap-free 1..N
sequence, allocated with `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`
(see `app/modules/integrity/repository.py`) -- the existing
`worker_progress_events_ordinal_seq` (a plain Postgres `SEQUENCE`, see
`c1c9c1c9d9f1_observation_batch_ingestion.py`) is global, not per case, so
it can't give per-case contiguous ordering on its own.

`merkle_checkpoints` uses a GiST exclusion constraint (`btree_gist`) so two
checkpoint ranges for the same case can never overlap, enforced by the
database itself rather than only by an application-level check-then-insert
that a race could slip past.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "8f68fb441037"
down_revision = "f4a1c9e0d2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "integrity_sequence_counters",
        sa.Column("case_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
    )

    op.create_table(
        "integrity_events",
        sa.Column("integrity_event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("canonical_payload_sha256", sa.Text(), nullable=False),
        sa.Column("payload_schema_version", sa.Text(), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("sequence_number", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "case_id", "idempotency_key", name="uq_integrity_events_case_idempotency"
        ),
        sa.UniqueConstraint("case_id", "sequence_number", name="uq_integrity_events_case_sequence"),
        sa.CheckConstraint(
            "event_kind IN ("
            "'evidence_registered', 'observation_published', 'correlation_completed', "
            "'review_decision', 'hypothesis_action')",
            name="ck_integrity_events_event_kind",
        ),
    )
    op.create_index(
        "ix_integrity_events_case_created", "integrity_events", ["case_id", "created_at"]
    )

    op.create_table(
        "merkle_checkpoints",
        sa.Column("checkpoint_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_sequence", sa.BigInteger(), nullable=False),
        sa.Column("end_sequence", sa.BigInteger(), nullable=False),
        sa.Column("leaf_count", sa.Integer(), nullable=False),
        sa.Column("root_hash", sa.Text(), nullable=False),
        sa.Column("tree_format_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "case_id",
            "start_sequence",
            "end_sequence",
            name="uq_merkle_checkpoints_case_range",
        ),
        sa.CheckConstraint(
            "end_sequence >= start_sequence", name="ck_merkle_checkpoints_range_order"
        ),
    )
    op.execute(
        "ALTER TABLE merkle_checkpoints ADD CONSTRAINT ck_merkle_checkpoints_no_overlap "
        "EXCLUDE USING gist ("
        "case_id WITH =, "
        "int8range(start_sequence, end_sequence, '[]') WITH &&"
        ")"
    )
    op.create_index(
        "ix_merkle_checkpoints_case_created", "merkle_checkpoints", ["case_id", "created_at"]
    )

    op.create_table(
        "checkpoint_signatures",
        sa.Column("signature_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "checkpoint_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merkle_checkpoints.checkpoint_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("algorithm", sa.Text(), nullable=False),
        sa.Column("signature_encoding", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("public_key_b64", sa.Text(), nullable=False),
        sa.Column("public_key_fingerprint", sa.Text(), nullable=False),
        sa.Column("signed_root_hash", sa.Text(), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("algorithm = 'ed25519'", name="ck_checkpoint_signatures_algorithm"),
    )
    op.create_index("ix_checkpoint_signatures_case", "checkpoint_signatures", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_checkpoint_signatures_case", table_name="checkpoint_signatures")
    op.drop_table("checkpoint_signatures")
    op.drop_index("ix_merkle_checkpoints_case_created", table_name="merkle_checkpoints")
    op.execute("ALTER TABLE merkle_checkpoints DROP CONSTRAINT ck_merkle_checkpoints_no_overlap")
    op.drop_table("merkle_checkpoints")
    op.drop_index("ix_integrity_events_case_created", table_name="integrity_events")
    op.drop_table("integrity_events")
    op.drop_table("integrity_sequence_counters")
