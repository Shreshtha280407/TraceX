"""Enforce append-only durable integrity records in PostgreSQL.

Revision ID: c42d3e4f5a6b
Revises: 8f68fb441037
Create Date: 2026-09-15

Ordinary application/database roles may insert integrity records but cannot
mutate or delete them. A PostgreSQL superuser remains a trust boundary and
can disable triggers; this is intentionally not represented as absolute
protection from a fully privileged database operator.
"""

from __future__ import annotations

from alembic import op

revision = "c42d3e4f5a6b"
down_revision = "8f68fb441037"
branch_labels = None
depends_on = None

_TABLES = ("integrity_events", "merkle_checkpoints", "checkpoint_signatures")


def upgrade() -> None:
    op.execute(
        "CREATE FUNCTION tracex_reject_integrity_record_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'integrity records are append-only' USING ERRCODE = '55000'; "
        "END; $$"
    )
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION tracex_reject_integrity_record_mutation()"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS tracex_reject_integrity_record_mutation()")
