"""worker_credentials.last_seen_at: a real heartbeat registry (G16)

Closes gap register G16 ("GET /internal/workers with heartbeat registry").
`/readyz` has stated since Phase 2 that "no worker-heartbeat registry
exists" (`app/api/health.py::_operational_components`'s own docstring) --
this migration is the minimal, additive fix: one nullable column on the
existing `worker_credentials` table, updated by `require_worker_principal`
on every successful worker authentication (every worker-authenticated call
already proves liveness; no separate heartbeat endpoint is required to
populate it). No new table: a per-worker "last time this credential was
used successfully" is exactly what `worker_credentials` already models one
row per worker for.

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f7a8b9c0d1e"
down_revision: str | None = "5e6f7a8b9c0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_credentials", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("worker_credentials", "last_seen_at")
