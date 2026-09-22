"""PostgreSQL durable boundary for case notes (Gap-Closure WP-4, G3)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.core.pagination import CursorPosition
from app.modules.access_control.notes_models import CaseNoteRecord

metadata = sa.MetaData()

case_notes_table = sa.Table(
    "case_notes",
    metadata,
    sa.Column("note_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("text", sa.Text(), nullable=False),
    sa.Column("supersedes_note_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(str(settings.postgres_dsn))


def _record(row: sa.RowMapping) -> CaseNoteRecord:
    return CaseNoteRecord.model_validate(dict(row))


class CaseNoteRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    async def create_note(
        self,
        *,
        case_id: UUID,
        author_user_id: UUID,
        text: str,
        supersedes_note_id: UUID | None,
        now: datetime | None = None,
    ) -> CaseNoteRecord:
        record = CaseNoteRecord(
            note_id=uuid4(),
            case_id=case_id,
            author_user_id=author_user_id,
            text=text,
            supersedes_note_id=supersedes_note_id,
            created_at=now or datetime.now(UTC),
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(case_notes_table).values(
                    note_id=record.note_id,
                    case_id=record.case_id,
                    author_user_id=record.author_user_id,
                    text=record.text,
                    supersedes_note_id=record.supersedes_note_id,
                    created_at=record.created_at,
                )
            )
        return record

    async def list_notes(
        self,
        case_id: UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
        after: CursorPosition | None = None,
    ) -> list[CaseNoteRecord]:
        """Gap-Closure WP-6/re-close (G7/G17 pagination): `after` (an opaque,
        case-bound, tamper-evident cursor -- see `core.pagination`) is real
        keyset pagination and takes precedence over `offset` when given.
        Rows are ordered `(created_at DESC, note_id DESC)` -- a composite,
        fully deterministic order required for keyset pagination to never
        skip or repeat a row even when several share the same `created_at`.
        """
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("case note query limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("case note query offset must be >= 0")
        statement = (
            sa.select(case_notes_table)
            .where(case_notes_table.c.case_id == case_id)
            .order_by(case_notes_table.c.created_at.desc(), case_notes_table.c.note_id.desc())
        )
        if after is not None:
            statement = statement.where(
                sa.or_(
                    case_notes_table.c.created_at < after.created_at,
                    sa.and_(
                        case_notes_table.c.created_at == after.created_at,
                        case_notes_table.c.note_id < after.row_id,
                    ),
                )
            )
        else:
            statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [_record(row) for row in rows]


__all__ = ["CaseNoteRepository", "create_engine"]
