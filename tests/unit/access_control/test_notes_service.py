"""Gap-Closure WP-4 (G3): case-note visibility -- author + CASE_OWNER/MANAGER/REVIEWER."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.access_control.models import CaseRole
from app.modules.access_control.notes_models import CaseNoteRecord
from app.modules.access_control.notes_service import list_visible_notes

_NOW = datetime(2026, 9, 22, tzinfo=UTC)


class _FakeNoteRepository:
    def __init__(self, notes: list[CaseNoteRecord]) -> None:
        self._notes = notes

    async def list_notes(self, case_id, *, limit=None, offset=0, after=None):
        return [n for n in self._notes if n.case_id == case_id][offset:]


def _note(case_id, author_user_id) -> CaseNoteRecord:
    return CaseNoteRecord(
        note_id=uuid4(),
        case_id=case_id,
        author_user_id=author_user_id,
        text="synthetic note",
        supersedes_note_id=None,
        created_at=_NOW,
    )


async def test_investigator_sees_only_their_own_notes() -> None:
    case_id = uuid4()
    author_a, author_b = uuid4(), uuid4()
    repository = _FakeNoteRepository([_note(case_id, author_a), _note(case_id, author_b)])

    visible = await list_visible_notes(
        repository, case_id=case_id, caller_user_id=author_a, caller_role=CaseRole.INVESTIGATOR
    )

    assert len(visible) == 1
    assert visible[0].author_user_id == author_a


async def test_reviewer_sees_every_note() -> None:
    case_id = uuid4()
    author_a, author_b = uuid4(), uuid4()
    repository = _FakeNoteRepository([_note(case_id, author_a), _note(case_id, author_b)])

    visible = await list_visible_notes(
        repository, case_id=case_id, caller_user_id=uuid4(), caller_role=CaseRole.REVIEWER
    )

    assert len(visible) == 2


async def test_case_owner_sees_every_note() -> None:
    case_id = uuid4()
    repository = _FakeNoteRepository([_note(case_id, uuid4()), _note(case_id, uuid4())])

    visible = await list_visible_notes(
        repository, case_id=case_id, caller_user_id=uuid4(), caller_role=CaseRole.CASE_OWNER
    )

    assert len(visible) == 2


async def test_notes_never_leak_across_cases() -> None:
    case_a, case_b = uuid4(), uuid4()
    author = uuid4()
    repository = _FakeNoteRepository([_note(case_a, author), _note(case_b, author)])

    visible = await list_visible_notes(
        repository, case_id=case_a, caller_user_id=author, caller_role=CaseRole.CASE_OWNER
    )

    assert len(visible) == 1
    assert visible[0].case_id == case_a
