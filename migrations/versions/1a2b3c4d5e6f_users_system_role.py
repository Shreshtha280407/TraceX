"""users.system_role: minimal additive admin-provisioning hook

Adds one nullable `system_role` column to `users`, constrained to `admin`
when set. This is the smallest additive model that lets Gate-closure WP-1
gate `POST /api/v1/auth/register` behind admin-only provisioning
(`POST /api/v1/admin/users`) without a new table or a breaking change to
any existing `UserRecord`/`PublicUser` shape -- both gain the field with a
`None` default. See `docs/decisions/ADR-019-admin-provisioning-and-case-
management.md` for the reasoning.

Revision ID: 1a2b3c4d5e6f
Revises: a3b4c5d6e7f8
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("system_role", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_users_system_role", "users", "system_role IS NULL OR system_role IN ('admin')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_system_role", "users", type_="check")
    op.drop_column("users", "system_role")
