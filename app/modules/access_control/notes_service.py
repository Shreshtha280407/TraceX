"""Gap-Closure WP-4 (G3): case-note visibility -- author + CASE_OWNER/MANAGER/REVIEWER."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog

from app.core.pagination import CursorPosition
from app.modules.access_control.models import ROLE_ACTIONS, CaseAction, CaseRole
from app.modules.access_control.notes_models import CaseNoteRecord
from app.modules.access_control.notes_repository import CaseNoteRepository
from app.modules.integrity.service import IntegrityService

logger = structlog.get_logger(__name__)


async def _record_integrity_event_safely(
    integrity_service: IntegrityService, note: CaseNoteRecord
) -> None:
    """Best-effort side effect, mirrors `review_service._record_integrity_event_safely`:
    the note has already durably committed by the time this runs, so a
    failure here is logged and left for `IntegrityReconciliationService`
    to repair -- never re-raised, never blocks note creation."""
    try:
        await integrity_service.record_integrity_event(note.to_integrity_submission())
    except Exception as exc:  # noqa: BLE001 - a side-effect failure must never propagate
        logger.warning(
            "integrity.event_record_failed",
            case_id=str(note.case_id),
            event_kind="case_note_added",
            subject_id=str(note.note_id),
            retry_state="reconciliation_pending",
            failure_category=type(exc).__name__,
        )


async def create_note(
    repository: CaseNoteRepository,
    integrity_service: IntegrityService,
    *,
    case_id: UUID,
    author_user_id: UUID,
    text: str,
    supersedes_note_id: UUID | None,
    now: datetime | None = None,
) -> CaseNoteRecord:
    note = await repository.create_note(
        case_id=case_id,
        author_user_id=author_user_id,
        text=text,
        supersedes_note_id=supersedes_note_id,
        now=now,
    )
    await _record_integrity_event_safely(integrity_service, note)
    return note


async def list_visible_notes(
    repository: CaseNoteRepository,
    *,
    case_id: UUID,
    caller_user_id: UUID,
    caller_role: CaseRole,
    limit: int | None = None,
    offset: int = 0,
    after: CursorPosition | None = None,
) -> list[CaseNoteRecord]:
    """Every note if the caller's role has `CASE_NOTE_READ_ALL`; otherwise
    only the caller's own notes -- never another author's text leaked to a
    lower-privileged member, and never a cross-case note (scoped by
    `case_id` at the repository/SQL level already)."""
    notes = await repository.list_notes(case_id, limit=limit, offset=offset, after=after)
    if CaseAction.CASE_NOTE_READ_ALL in ROLE_ACTIONS.get(caller_role, frozenset()):
        return notes
    return [note for note in notes if note.author_user_id == caller_user_id]


__all__ = ["create_note", "list_visible_notes"]
