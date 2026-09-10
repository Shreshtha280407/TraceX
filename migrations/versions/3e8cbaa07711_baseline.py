"""baseline

Establishes Alembic version tracking. No domain tables exist in Phase 1;
later phases add their own revisions on top of this baseline.

Revision ID: 3e8cbaa07711
Revises:
Create Date: 2026-09-10

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "3e8cbaa07711"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
