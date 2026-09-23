"""hypotheses.supporting_entity_resolution_candidate_ids (ADR-031)

Additive: a second, parallel citation column on `hypotheses`, alongside the
existing `supporting_candidate_ids`. `supporting_candidate_ids` cites a
Phase 5 correlation candidate (`candidate_links_table`); this new column
cites a WP-2 entity-resolution candidate (`entity_resolution_candidates_
table`) -- two different semantic claims, never merged. See ADR-031 for
the full root cause: the original column only ever validated against
`candidate_links_table` because ADR-015 (2026-09-15) predates ADR-020's
entity-resolution work, a timing accident rather than a deliberate
exclusion.

`server_default='[]'` backfills existing rows (real hypotheses already
exist in this environment) with an empty array -- every future insert
supplies an explicit value, matching `supporting_candidate_ids`'s own
`nullable=False` contract exactly.

Revision ID: 7a8b9c0d1e2f
Revises: 6f7a8b9c0d1e
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7a8b9c0d1e2f"
down_revision: str | None = "6f7a8b9c0d1e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hypotheses",
        sa.Column(
            "supporting_entity_resolution_candidate_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("hypotheses", "supporting_entity_resolution_candidate_ids")
