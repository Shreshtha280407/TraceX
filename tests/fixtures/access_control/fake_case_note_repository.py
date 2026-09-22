"""In-memory duck-typed stand-in for `CaseNoteRepository` (Gap-Closure WP-5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.modules.access_control.notes_models import CaseNoteRecord


class FakeCaseNoteRepository:
    def __init__(self) -> None:
        self.notes: dict[UUID, CaseNoteRecord] = {}

    async def close(self) -> None:
        pass

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
        self.notes[record.note_id] = record
        return record

    async def list_notes(
        self,
        case_id: UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
        after: Any = None,
    ) -> list[CaseNoteRecord]:
        notes = sorted(
            (n for n in self.notes.values() if n.case_id == case_id),
            key=lambda n: (n.created_at, n.note_id),
            reverse=True,
        )
        if after is not None:
            notes = [
                n for n in notes if (n.created_at, n.note_id) < (after.created_at, after.row_id)
            ]
        else:
            notes = notes[offset:]
        return notes[:limit] if limit is not None else notes
