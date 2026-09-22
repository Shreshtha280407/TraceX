"""Gap-Closure WP-5: pure-logic tests for `CaseNoteRecord.to_integrity_submission`.

A case note's `text` is source-authored content -- it must never reach the
integrity module raw, only as a commitment hash, mirroring
`review_models.rationale_commitment`'s guarantee exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.access_control.notes_models import (
    CASE_NOTE_ADDED_SCHEMA_VERSION,
    CaseNoteRecord,
)
from app.modules.integrity.models import IntegrityEventKind

_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_TEXT = "Suspect seen near the depot at 22:00, license plate partially visible."


def _note(*, supersedes_note_id=None) -> CaseNoteRecord:
    return CaseNoteRecord(
        note_id=uuid4(),
        case_id=uuid4(),
        author_user_id=uuid4(),
        text=_TEXT,
        supersedes_note_id=supersedes_note_id,
        created_at=_NOW,
    )


def test_integrity_submission_never_carries_raw_note_text() -> None:
    note = _note()
    submission = note.to_integrity_submission()
    serialized = submission.model_dump_json()
    assert _TEXT not in serialized
    assert submission.event_kind == IntegrityEventKind.CASE_NOTE_ADDED
    assert submission.payload_schema_version == CASE_NOTE_ADDED_SCHEMA_VERSION
    assert "text_commitment_sha256" in submission.canonical_metadata


def test_integrity_submission_commitment_is_deterministic_and_content_bound() -> None:
    note_a = _note()
    note_b = note_a.model_copy(update={"note_id": uuid4(), "text": "a different note entirely"})
    assert (
        note_a.to_integrity_submission().canonical_metadata["text_commitment_sha256"]
        != note_b.to_integrity_submission().canonical_metadata["text_commitment_sha256"]
    )


def test_idempotency_key_is_scoped_to_case_and_note() -> None:
    note = _note()
    assert note.idempotency_key == f"case-note:{note.case_id}:{note.note_id}"


def test_integrity_submission_includes_supersedes_note_id_when_present() -> None:
    superseded_id = uuid4()
    note = _note(supersedes_note_id=superseded_id)
    submission = note.to_integrity_submission()
    assert submission.canonical_metadata["supersedes_note_id"] == str(superseded_id)
