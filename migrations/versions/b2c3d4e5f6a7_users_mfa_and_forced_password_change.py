"""users: TOTP MFA + admin-forced password change

Adds three additive, nullable-or-defaulted columns to `users` so existing
rows need no backfill: `must_change_password` (forced on next login for
admin-provisioned/reset accounts, false for everyone already active today),
`totp_secret` (the base32 TOTP shared secret once enrollment starts; never
returned from any API response), and `totp_enabled` (true only after the
investigator has confirmed a code against `totp_secret` -- see
`app/modules/access_control/totp.py` and `docs/decisions/ADR-033-frontend-
totp-mfa.md`).

Revision ID: b2c3d4e5f6a7
Revises: 7a8b9c0d1e2f
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "7a8b9c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("users", sa.Column("totp_secret", sa.Text(), nullable=True))
    op.add_column(
        "users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "must_change_password")
