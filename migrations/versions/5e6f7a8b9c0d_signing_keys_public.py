"""signing_keys_public: append-only public-key registry (G4)

Closes gap register G4 (part: signing-key registry). Each row records one
Ed25519 key's public identity (never its private material) at the moment
the `rotate-key` CLI command registers it. `key_id` is the primary key, so
a `key_id` can only ever be registered once -- a later registration under
the same `key_id` with different public-key bytes raises
`SigningKeyConflictError` in the repository rather than silently
overwriting. Append-only at the database level too (same
`tracex_reject_integrity_record_mutation` trigger precedent as
`integrity_events`/`merkle_checkpoints`/`checkpoint_signatures`), because
this table is exactly the kind of tamper-evident material a rotation
history must never allow silent mutation of.

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e6f7a8b9c0d"
down_revision: str | None = "4d5e6f7a8b9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signing_keys_public",
        sa.Column("key_id", sa.Text(), primary_key=True),
        sa.Column("algorithm", sa.Text(), nullable=False),
        sa.Column("public_key_b64", sa.Text(), nullable=False),
        sa.Column("public_key_fingerprint", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        "CREATE TRIGGER trg_signing_keys_public_append_only "
        "BEFORE UPDATE OR DELETE ON signing_keys_public "
        "FOR EACH ROW EXECUTE FUNCTION tracex_reject_integrity_record_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_signing_keys_public_append_only ON signing_keys_public")
    op.drop_table("signing_keys_public")
