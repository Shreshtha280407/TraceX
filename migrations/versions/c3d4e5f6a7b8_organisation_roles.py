"""Replace the legacy broad admin role with explicit organisation roles.

Existing deployment operators keep their accounts, password hashes, MFA
configuration, sessions, case memberships, cases, audit rows, and evidence:
only the nullable ``users.system_role`` value and its CHECK constraint change.
Every legacy ``admin`` becomes a ``provisioner`` before the new constraint is
installed.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_users_system_role", "users", type_="check")
    op.execute(sa.text("UPDATE users SET system_role = 'provisioner' WHERE system_role = 'admin'"))
    op.create_check_constraint(
        "ck_users_system_role",
        "users",
        "system_role IS NULL OR system_role IN ('provisioner', 'case_head')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_system_role", "users", type_="check")
    # The old schema has only one non-null role.  Mapping both explicit roles
    # back preserves account reachability for an operator who must roll back.
    op.execute(
        sa.text(
            "UPDATE users SET system_role = 'admin' "
            "WHERE system_role IN ('provisioner', 'case_head')"
        )
    )
    op.create_check_constraint(
        "ck_users_system_role", "users", "system_role IS NULL OR system_role IN ('admin')"
    )
