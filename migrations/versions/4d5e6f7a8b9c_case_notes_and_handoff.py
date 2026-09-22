"""case_notes: append-only case narrative (G3)

Closes gap register G3 (part 1: case notes). `case_notes` rows are
append-only (edits = a new row with `supersedes_note_id` set, never an
UPDATE) -- same append-only trigger precedent as `entity_review_decisions`/
`candidate_review_decisions`/`integrity_events`. Handoff summaries
(the rest of G3) are computed at read time from existing durable data
(review/hypothesis decisions, open candidates, notes) -- no new table.

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4d5e6f7a8b9c"
down_revision: str | None = "3c4d5e6f7a8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "case_notes",
        sa.Column("note_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "supersedes_note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("case_notes.note_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_case_notes_case_id", "case_notes", ["case_id"])
    op.execute(
        "CREATE TRIGGER trg_case_notes_append_only "
        "BEFORE UPDATE OR DELETE ON case_notes "
        "FOR EACH ROW EXECUTE FUNCTION tracex_reject_integrity_record_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_case_notes_append_only ON case_notes")
    op.drop_table("case_notes")
