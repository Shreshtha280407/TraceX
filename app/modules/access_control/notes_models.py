"""Gap-Closure WP-4 (G3): case notes -- append-only case narrative.

An "edit" is a new row with `supersedes_note_id` set, never an UPDATE
(same append-only pattern as `entity_review_decisions`/
`candidate_review_decisions`/`integrity_events`). Visibility: any author
can always read their own note; only `CASE_OWNER`/`CASE_MANAGER`/
`REVIEWER` (via `CaseAction.CASE_NOTE_READ_ALL`) can read every note --
enforced in `notes_service.py`, not this module.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.core.canonical import canonical_sha256
from app.modules.access_control.models import AccessControlModel
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission

MAX_NOTE_LENGTH = 8_000
CASE_NOTE_ADDED_SCHEMA_VERSION = "case_note_added.v1"


class CaseNoteCreateRequest(AccessControlModel):
    text: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)
    supersedes_note_id: UUID | None = None


class CaseNoteRecord(AccessControlModel):
    note_id: UUID
    case_id: UUID
    author_user_id: UUID
    text: str
    supersedes_note_id: UUID | None
    created_at: datetime

    @property
    def idempotency_key(self) -> str:
        return f"case-note:{self.case_id}:{self.note_id}"

    def to_integrity_submission(self) -> IntegrityEventSubmission:
        """`text` never leaves this method -- only its commitment hash does,
        matching `review_models.rationale_commitment`'s never-the-raw-content
        rule exactly."""
        return IntegrityEventSubmission(
            case_id=self.case_id,
            event_kind=IntegrityEventKind.CASE_NOTE_ADDED,
            subject_type="case_note",
            subject_id=str(self.note_id),
            canonical_metadata={
                "note_id": str(self.note_id),
                "author_user_id": str(self.author_user_id),
                "supersedes_note_id": (
                    str(self.supersedes_note_id) if self.supersedes_note_id is not None else None
                ),
                "text_commitment_sha256": canonical_sha256({"text": self.text}),
            },
            payload_schema_version=CASE_NOTE_ADDED_SCHEMA_VERSION,
            source_created_at=self.created_at,
            idempotency_key=self.idempotency_key,
        )


class CaseNoteListResponse(AccessControlModel):
    items: tuple[CaseNoteRecord, ...]
    #: Gap-Closure re-close (G17): an opaque, case-bound, tamper-evident
    #: cursor (see `core.pagination`) a caller passes back as `?cursor=...`
    #: to fetch the next page. `None` when this page was empty.
    next_cursor: str | None = None


__all__ = [
    "CASE_NOTE_ADDED_SCHEMA_VERSION",
    "MAX_NOTE_LENGTH",
    "CaseNoteCreateRequest",
    "CaseNoteListResponse",
    "CaseNoteRecord",
]
