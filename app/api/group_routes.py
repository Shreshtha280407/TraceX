"""Case-authorized investigation queue and independently reviewed propositions."""
# ruff: noqa: B008
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case as sql_case
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_case_member
from app.db import get_session
from app.engine.evidence import reference_status, structured_evidence
from app.engine.investigations import POLICY, QUEUE_POLICY, UNRESOLVED
from app.events import append_event
from app.models import (
    AuditRecord,
    CaseMembership,
    EvidenceSource,
    FindingRecord,
    InvestigationGroup,
    InvestigationMember,
    InvestigationReplacement,
    InvestigationReview,
    InvestigationRun,
    InvestigationSubject,
    User,
)

router = APIRouter(prefix="/v1")


def view(row):
    return {"group_id": row.id, "case_id": row.case_id, "snapshot_id": row.snapshot_id, "run_id": row.run_id,
            "family": row.family, "episode_type": row.episode_type, "proposition": row.proposition,
            "focal_ref": row.focal_ref, "window_start": row.window_start.isoformat(), "window_end": row.window_end.isoformat(),
            "member_count": row.member_count, "transaction_count": row.transaction_count, "entity_count": row.entity_count,
            "representative_finding_id": row.representative_id, "status": row.status, "review_version": row.review_version}


def scoped(session, group_id, user):
    row = session.get(InvestigationGroup, group_id)
    if row is None:
        raise HTTPException(404, "Investigation group not found")
    require_case_member(row.case_id, user, session)
    if session.get(InvestigationRun, row.run_id).state != "complete":
        raise HTTPException(409, "Grouping generation has not been published")
    return row


def queue_query(case_id):
    # Raw scores are ordered ONLY within an identical rule/version family.
    band = sql_case((InvestigationGroup.status == "escalated", 0),
                    (InvestigationGroup.status == "needs_data_review", 1), else_=2)
    rank = func.row_number().over(partition_by=(band, InvestigationGroup.family),
        order_by=(InvestigationGroup.representative_score.desc(), InvestigationGroup.id))
    return select(InvestigationGroup.id.label("id"), band.label("band"), rank.label("family_rank"),
                  InvestigationGroup.family.label("family")).join(InvestigationRun).where(
        InvestigationRun.case_id == case_id, InvestigationRun.active.is_(True), InvestigationRun.state == "complete",
        InvestigationGroup.status.in_(UNRESOLVED)).subquery()


@router.get("/cases/{case_id}/investigation-queue")
def queue(case_id: str, capacity: int = Query(100, ge=0, le=10000), limit: int = Query(20, ge=1, le=200),
          offset: int = Query(0, ge=0), scope: str = Query("queue", pattern="^(queue|backlog|all|history)$"),
          review_state: str | None = Query(None, pattern="^(open|triaged|confirmed|dismissed|escalated|needs_data_review)$"),
          family: str | None = None, user: User = Depends(current_user), session: Session = Depends(get_session)):
    require_case_member(case_id, user, session)
    active = select(InvestigationGroup).join(InvestigationRun).where(InvestigationRun.case_id == case_id,
        InvestigationRun.active.is_(True), InvestigationRun.state == "complete")
    total = session.scalar(select(func.count()).select_from(active.subquery()))
    # A pre-grouping import or failed/in-flight generation must not look like
    # a successfully empty review workload. Count distinct covered findings by
    # indexed membership existence, not by materializing evidence or summing
    # historical generations (which would double-count replacements).
    covered = select(InvestigationMember.finding_id).join(InvestigationGroup).join(InvestigationRun).where(
        InvestigationMember.finding_id == FindingRecord.id, InvestigationRun.case_id == case_id,
        InvestigationRun.active.is_(True), InvestigationRun.state == "complete").exists()
    underlying, grouped = session.execute(select(func.count(), func.coalesce(func.sum(
        sql_case((covered, 1), else_=0)), 0)).select_from(FindingRecord).where(
        FindingRecord.case_id == case_id)).one()
    ungrouped = underlying - grouped
    ranked = queue_query(case_id)
    unresolved = session.scalar(select(func.count()).select_from(ranked))
    order = (ranked.c.band, ranked.c.family_rank, ranked.c.family, ranked.c.id)
    selected = select(ranked.c.id).order_by(*order).limit(capacity).subquery()
    if scope in {"queue", "backlog"}:
        query = select(InvestigationGroup).join(ranked, ranked.c.id == InvestigationGroup.id)
        query = query.where(InvestigationGroup.id.in_(select(selected.c.id)) if scope == "queue" else ~InvestigationGroup.id.in_(select(selected.c.id))).order_by(*order)
    elif scope == "history":
        query = select(InvestigationGroup).join(InvestigationRun).where(InvestigationRun.case_id == case_id,
            InvestigationRun.state == "complete").order_by(InvestigationGroup.window_start, InvestigationGroup.id)
    else:
        query = active.order_by(InvestigationGroup.family, InvestigationGroup.window_start, InvestigationGroup.id)
    if review_state:
        query = query.where(InvestigationGroup.status == review_state)
    if family:
        query = query.where(InvestigationGroup.family == family)
    filtered = session.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {"policy": QUEUE_POLICY, "priority_is_probability": False, "capacity": capacity,
            "underlying_findings": underlying, "investigation_groups": total, "unresolved_groups": unresolved,
            "queued_groups": min(capacity, unresolved), "backlog_groups": max(0, unresolved - capacity),
            "grouping_coverage": {"state": "incomplete" if ungrouped else "complete",
                "grouped_findings": underlying - ungrouped, "ungrouped_findings": ungrouped,
                "scope": "Currently stored findings only; not a declaration of analysis completion",
                "reason": "Some findings have no membership in a published active generation. Check investigation_grouping; older imports require authorized analysis retry." if ungrouped else None},
            "filtered_total": filtered, "offset": offset, "scope": scope,
            "items": [view(row) for row in session.scalars(query.offset(offset).limit(limit))]}


@router.get("/investigation-groups/{group_id}")
def detail(group_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    row = scoped(session, group_id, user)
    run = session.get(InvestigationRun, row.run_id)
    representative = session.get(FindingRecord, row.representative_id)
    from app.api.routes import finding_review_history
    counts = dict(session.execute(select(FindingRecord.status, func.count()).join(InvestigationMember,
        InvestigationMember.finding_id == FindingRecord.id).where(InvestigationMember.group_id == group_id).group_by(FindingRecord.status)).all())
    return {**view(row), "grouping_version": run.procedure_version, "procedure_sha256": run.procedure_sha256,
            "input_sha256": run.input_sha256, "active_generation": run.active, "rationale": row.rationale,
            "representative_evidence": structured_evidence(session, representative, finding_review_history(session, representative.id)),
            "evidence_scope": "Representative only. Supporting/opposing evidence and detector/scorer/confidence pins for every member remain separately accessible via paginated members.",
            "member_decisions": counts, "mixed_member_decisions": len(counts) > 1,
            "group_decision_scope": POLICY["group_target"],
            "history_url": f"/v1/investigation-groups/{group_id}/reviews",
            "members_url": f"/v1/investigation-groups/{group_id}/members"}


@router.get("/investigation-groups/{group_id}/members")
@router.get("/investigation-groups/{group_id}/export")
def members(group_id: str, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0),
            user: User = Depends(current_user), session: Session = Depends(get_session)):
    row = scoped(session, group_id, user)
    from app.api.routes import finding_review_history, finding_view
    findings = session.scalars(select(FindingRecord).join(InvestigationMember, InvestigationMember.finding_id == FindingRecord.id)
        .where(InvestigationMember.group_id == group_id).order_by(InvestigationMember.ordinal).offset(offset).limit(limit))
    return {"group": view(row), "total": row.member_count, "offset": offset, "limit": limit,
            "items": [{"finding": finding_view(finding, session),
                       "structured_evidence": structured_evidence(session, finding, finding_review_history(session, finding.id))} for finding in findings],
            "next_offset": offset + limit if offset + limit < row.member_count else None,
            "export_scope": "Bounded page, not a whole-case evidence copy"}


@router.get("/investigation-groups/{group_id}/subjects")
def subjects(group_id: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
             user: User = Depends(current_user), session: Session = Depends(get_session)):
    row = scoped(session, group_id, user)
    query = select(InvestigationSubject.kind, InvestigationSubject.ref).where(InvestigationSubject.group_id == group_id)
    return {"group_id": row.id, "items": [dict(item._mapping) for item in session.execute(query.order_by(InvestigationSubject.kind, InvestigationSubject.ref).offset(offset).limit(limit))], "offset": offset}


@router.get("/investigation-groups/{group_id}/replacements")
def replacements(group_id: str, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0),
                 user: User = Depends(current_user), session: Session = Depends(get_session)):
    scoped(session, group_id, user)
    from sqlalchemy import or_
    query = select(InvestigationReplacement).where(or_(InvestigationReplacement.prior_id == group_id, InvestigationReplacement.replacement_id == group_id))
    return {"items": [{"prior_id": r.prior_id, "replacement_id": r.replacement_id, "reason": r.reason}
                      for r in session.scalars(query.order_by(InvestigationReplacement.prior_id, InvestigationReplacement.replacement_id).offset(offset).limit(limit))]}


@router.get("/investigation-groups/{group_id}/reviews")
def history(group_id: str, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0),
            user: User = Depends(current_user), session: Session = Depends(get_session)):
    scoped(session, group_id, user)
    return {"items": [{"review_id": r.id, "review_version": r.review_version, "disposition": r.disposition,
                       "reason": r.reason, "counterevidence_refs": r.counterevidence_refs, "created_at": r.created_at.isoformat()}
                      for r in session.scalars(select(InvestigationReview).where(InvestigationReview.group_id == group_id)
                        .order_by(InvestigationReview.review_version).offset(offset).limit(limit))]}


class GroupDecision(BaseModel):
    expected_review_version: int = Field(ge=1)
    disposition: str = Field(pattern="^(open|triaged|confirmed|dismissed|escalated|needs_data_review)$")
    reason: str = Field(min_length=1, max_length=2000)
    counterevidence_refs: list[dict] = Field(default_factory=list, max_length=20)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("A recorded group decision reason cannot be blank")
        return value.strip()


@router.post("/investigation-groups/{group_id}/reviews", status_code=201)
def decide(group_id: str, body: GroupDecision, user: User = Depends(current_user), session: Session = Depends(get_session)):
    row = scoped(session, group_id, user)
    role = session.scalar(select(CaseMembership.role).where(CaseMembership.case_id == row.case_id, CaseMembership.user_id == user.id))
    if role not in {"case_lead", "reviewer"}:
        raise HTTPException(403, "Reviewer or case lead role is required")
    for ref in body.counterevidence_refs:
        if reference_status(session, row.case_id, ref) != "receipt_approved":
            raise HTTPException(422, "Counter-evidence locator/hash is stale, unauthorized or outside checked receipts")
        ref["source_sha256"] = session.get(EvidenceSource, ref["evidence_id"]).sha256
    changed = session.execute(update(InvestigationGroup).where(InvestigationGroup.id == group_id,
        InvestigationGroup.review_version == body.expected_review_version).values(status=body.disposition, review_version=InvestigationGroup.review_version + 1))
    if changed.rowcount != 1:
        raise HTTPException(409, "Group review version changed; reopen before deciding")
    review = InvestigationReview(group_id=group_id, actor_id=user.id, review_version=body.expected_review_version,
                                 disposition=body.disposition, reason=body.reason, counterevidence_refs=body.counterevidence_refs)
    session.add(review)
    session.add(AuditRecord(case_id=row.case_id, actor_id=user.id, action="investigation_group.reviewed",
                           target_type="investigation_group", target_id=group_id,
                           detail={"disposition": body.disposition, "scope": POLICY["group_target"]}))
    append_event(session, case_id=row.case_id, event_type="investigation_group.reviewed", stage="reviewed",
                 payload={"group_id": group_id, "disposition": body.disposition})
    session.commit()
    session.refresh(row)
    return {"group": view(row), "review_id": review.id, "individual_decisions_changed": 0}
