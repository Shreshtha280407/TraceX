"""Durable case event append/read helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Case, CaseEvent, ImportJob, OutboxEvent


def append_event(
    session: Session,
    *,
    case_id: str,
    event_type: str,
    stage: str,
    job: ImportJob | None = None,
    payload: dict[str, Any] | None = None,
) -> CaseEvent:
    # PostgreSQL serializes writers for a case, preventing duplicate event sequences.
    session.scalar(select(Case).where(Case.id == case_id).with_for_update())
    next_sequence = (session.scalar(select(func.max(CaseEvent.sequence)).where(CaseEvent.case_id == case_id)) or 0) + 1
    event = CaseEvent(
        case_id=case_id,
        sequence=next_sequence,
        event_type=event_type,
        job_id=job.id if job else None,
        stage=stage,
        snapshot_id=job.snapshot_id if job else None,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    session.add(
        OutboxEvent(
            case_id=case_id, event_id=event.id, topic=event_type, payload={"event_id": event.id, "case_id": case_id}
        )
    )
    return event


def event_envelope(event: CaseEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})
    return {
        "event_id": event.id,
        "seq": event.sequence,
        "case_id": event.case_id,
        "job_id": event.job_id,
        "type": event.event_type,
        "stage": event.stage,
        "snapshot_id": event.snapshot_id,
        "timestamp": event.created_at.isoformat() if event.created_at else None,
        "payload": payload,
    }
