"""Case-scoped Phase 1 HTTP endpoints."""

# ruff: noqa: B008  # FastAPI declares dependencies through parameter defaults.
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_case_member
from app.auth.security import hash_password, issue_session_token, verify_password
from app.config import settings
from app.db import get_session
from app.engine.adapters import SourceParseError, rows_for_source
from app.engine.findings import refresh_synthetic_seed_proximity
from app.engine.graph import GraphQueryError, query_neighbourhood
from app.events import append_event, event_envelope
from app.jobs.service import create_or_reuse_job, job_view
from app.models import (
    AuditRecord,
    Case,
    CaseEvent,
    CaseMembership,
    EvidenceSource,
    FeatureRecord,
    FindingRecord,
    GraphSnapshot,
    ImportJob,
    ReviewDecisionRecord,
    Snapshot,
    SyntheticReviewSeed,
    User,
    WorkerHeartbeat,
)
from app.storage.raw import UploadRejected, store_upload

router = APIRouter(prefix="/v1")


class SignupRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    display_name: str
    password: str


class CaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    synthetic: bool = False


class CaseMemberCreate(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    role: str = Field(pattern="^(case_lead|analyst|reviewer)$")


class ReviewCreate(BaseModel):
    expected_finding_version: int = Field(ge=1)
    disposition: str = Field(pattern="^(open|triaged|dismissed|escalated|needs_data_review)$")
    reason: str = Field(min_length=1, max_length=2000)
    counterevidence_refs: list[dict] = Field(default_factory=list, max_length=20)


class SyntheticReviewSeedCreate(BaseModel):
    """Synthetic-only context for deterministic evaluation, never attribution."""

    seed_snapshot_id: str = Field(min_length=1, max_length=128)
    seed_entity_id: str | None = Field(default=None, min_length=1, max_length=512)
    seed_address_id: str | None = Field(default=None, min_length=1, max_length=512)
    seed_reason: str = Field(min_length=1, max_length=2000)

    def entity_ref(self) -> str:
        value = self.seed_entity_id or self.seed_address_id
        if not value or (self.seed_entity_id and self.seed_address_id):
            raise ValueError("provide exactly one of seed_entity_id or seed_address_id")
        return value if value.startswith("address:") else f"address:{value}"


def case_view(case: Case) -> dict:
    return {
        "case_id": case.id,
        "name": case.name,
        "synthetic": case.synthetic,
        "created_at": case.created_at.isoformat() if case.created_at else None,
    }


@router.post("/auth/signup", status_code=status.HTTP_201_CREATED)
def signup(body: SignupRequest, session: Session = Depends(get_session)) -> dict:
    existing = session.scalar(select(User).where(User.external_subject == body.display_name))
    if existing is not None:
        raise HTTPException(status_code=409, detail="That name is taken")
    user = User(external_subject=body.display_name, password_hash=hash_password(body.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    token = issue_session_token(user.external_subject)
    return {"token": token, "actor": user.external_subject}


@router.post("/auth/login")
def login(body: LoginRequest, session: Session = Depends(get_session)) -> dict:
    user = session.scalar(select(User).where(User.external_subject == body.display_name))
    if user is None or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = issue_session_token(user.external_subject)
    return {"token": token, "actor": user.external_subject}


@router.get("/cases")
def list_my_cases(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """Cases the caller is a member of — powers Case Dashboard / Overview / post-auth redirect."""
    memberships = session.scalars(select(CaseMembership).where(CaseMembership.user_id == user.id))
    membership_rows = list(memberships)
    role_by_case = {membership.case_id: membership.role for membership in membership_rows}
    case_ids = list(role_by_case)
    cases = session.scalars(select(Case).where(Case.id.in_(case_ids))) if case_ids else []
    return {"cases": [{**case_view(case), "role": role_by_case[case.id]} for case in cases]}


@router.get("/cases/{case_id}")
def get_case(case_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """Case detail — powers Case Settings header/metadata panel and Access & Roles list."""
    case = require_case_member(case_id, user, session)
    memberships = session.scalars(select(CaseMembership).where(CaseMembership.case_id == case_id))
    members = [
        {"actor": member.external_subject, "role": membership.role}
        for membership, member in (
            (membership, session.get(User, membership.user_id)) for membership in memberships
        )
    ]
    return {**case_view(case), "members": members}


@router.get("/cases/{case_id}/sources")
def list_case_sources(
    case_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """Evidence sources for a case — powers Case Settings "Data Sources" panel."""
    require_case_member(case_id, user, session)
    sources = session.scalars(select(EvidenceSource).where(EvidenceSource.case_id == case_id).order_by(EvidenceSource.id))
    return {
        "sources": [
            {
                "source_id": s.id, "filename": s.original_filename, "sha256": s.sha256,
                "byte_size": s.byte_size, "source_format": s.source_format, "synthetic": s.synthetic,
            }
            for s in sources
        ]
    }


@router.get("/healthz")
def health(session: Session = Depends(get_session)) -> dict:
    session.execute(select(1))
    writable = settings.evidence_root
    try:
        writable.mkdir(parents=True, exist_ok=True)
        probe = writable / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"evidence vault unavailable: {exc}") from exc
    return {"status": "ok", "database": "ok", "evidence_vault": "ok"}


@router.get("/readyz")
def readiness(session: Session = Depends(get_session)) -> dict:
    health(session)
    heartbeat = session.scalar(select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc()).limit(1))
    if heartbeat is None:
        raise HTTPException(status_code=503, detail="worker heartbeat has not been observed")
    observed_at = heartbeat.heartbeat_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    if observed_at < datetime.now(UTC) - timedelta(seconds=settings.worker_health_seconds):
        raise HTTPException(status_code=503, detail="worker heartbeat is stale")
    return {"status": "ready", "worker_id": heartbeat.worker_id, "heartbeat_at": observed_at.isoformat()}


@router.post("/cases", status_code=status.HTTP_201_CREATED)
def create_case(body: CaseCreate, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    case = Case(name=body.name, synthetic=body.synthetic, created_by=user.id)
    session.add(case)
    session.flush()
    session.add(CaseMembership(case_id=case.id, user_id=user.id, role="case_lead"))
    session.add(
        AuditRecord(
            case_id=case.id,
            actor_id=user.id,
            action="case.created",
            target_type="case",
            target_id=case.id,
            detail={"synthetic": body.synthetic},
        )
    )
    session.commit()
    session.refresh(case)
    return case_view(case)


@router.post("/cases/{case_id}/members", status_code=status.HTTP_201_CREATED)
def add_case_member(
    case_id: str, body: CaseMemberCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    case = require_case_member(case_id, user, session)
    membership = session.scalar(
        select(CaseMembership).where(CaseMembership.case_id == case.id, CaseMembership.user_id == user.id)
    )
    if membership.role != "case_lead":
        raise HTTPException(status_code=403, detail="Only a case lead can add members")
    member = session.scalar(select(User).where(User.external_subject == body.actor))
    if member is None:
        member = User(external_subject=body.actor)
        session.add(member)
        session.flush()
    existing = session.get(CaseMembership, {"case_id": case.id, "user_id": member.id})
    if existing:
        existing.role = body.role
    else:
        session.add(CaseMembership(case_id=case.id, user_id=member.id, role=body.role))
    session.add(
        AuditRecord(
            case_id=case.id,
            actor_id=user.id,
            action="case.member_upserted",
            target_type="user",
            target_id=member.id,
            detail={"role": body.role},
        )
    )
    session.commit()
    return {"case_id": case.id, "actor": member.external_subject, "role": body.role}


@router.post("/cases/{case_id}/imports", status_code=status.HTTP_202_ACCEPTED)
async def create_import(
    case_id: str,
    file: UploadFile = File(...),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    require_case_member(case_id, user, session)
    try:
        stored = await store_upload(
            file, case_id=case_id, evidence_root=settings.evidence_root, max_bytes=settings.max_upload_bytes
        )
    except UploadRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    source = session.scalar(
        select(EvidenceSource).where(EvidenceSource.case_id == case_id, EvidenceSource.sha256 == stored.sha256)
    )
    if source is None:
        source = EvidenceSource(
            case_id=case_id,
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            original_filename=stored.filename,
            source_format=stored.source_format,
            storage_relative_path=stored.relative_path,
            synthetic=False,
        )
        session.add(source)
        session.flush()
        session.add(
            AuditRecord(
                case_id=case_id,
                actor_id=user.id,
                action="source.preserved",
                target_type="evidence_source",
                target_id=source.id,
                detail={"sha256": source.sha256, "bytes": source.byte_size},
            )
        )
    try:
        job, reused = create_or_reuse_job(
            session, case_id=case_id, source=source, idempotency_key=idempotency_key, actor=user
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    session.refresh(job)
    return {
        "import_id": job.id,
        "job_id": job.id,
        "source_id": source.id,
        "idempotent_replay": reused,
        "state": job.state,
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    job = session.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    require_case_member(job.case_id, user, session)
    return job_view(job)


@router.get("/evidence/{source_id}/records")
def get_evidence_record(
    source_id: str,
    locator: str = Query(min_length=1, max_length=1024),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    source = session.get(EvidenceSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Evidence source not found")
    require_case_member(source.case_id, user, session)
    from app.storage.raw import resolve_source

    try:
        for row in rows_for_source(
            resolve_source(settings.evidence_root, source.storage_relative_path), source.source_format
        ):
            if row.locator == locator:
                return {
                    "source_id": source.id,
                    "source_sha256": source.sha256,
                    "locator_type": row.locator_type,
                    "locator": row.locator,
                    "record": row.value,
                }
    except SourceParseError as exc:
        raise HTTPException(status_code=409, detail=f"Stored source cannot be replayed: {exc}") from exc
    raise HTTPException(status_code=404, detail="Source locator not found")


@router.get("/cases/{case_id}/graph")
def get_graph(
    case_id: str,
    seed: str = Query(min_length=1, max_length=512),
    depth: int = Query(default=1, ge=1, le=5),
    node_limit: int = Query(default=200, ge=1, le=1000),
    edge_limit: int = Query(default=500, ge=1, le=3000),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    require_case_member(case_id, user, session)
    graph = session.scalar(
        select(GraphSnapshot)
        .where(GraphSnapshot.case_id == case_id, GraphSnapshot.state == "complete")
        .order_by(GraphSnapshot.created_at.desc())
        .limit(1)
    )
    if graph is None:
        raise HTTPException(status_code=409, detail="No completed graph snapshot is available for this case")
    try:
        return query_neighbourhood(
            evidence_root=settings.evidence_root,
            graph=graph,
            seed=seed,
            depth=depth,
            node_limit=node_limit,
            edge_limit=edge_limit,
        )
    except GraphQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/cases/{case_id}/synthetic-review-seeds", status_code=status.HTTP_201_CREATED)
def create_synthetic_review_seed(
    case_id: str,
    body: SyntheticReviewSeedCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Add explicit synthetic evaluation context to one completed case snapshot."""
    case = require_case_member(case_id, user, session)
    if not case.synthetic:
        raise HTTPException(status_code=422, detail="Synthetic review seeds are allowed only in synthetic evaluation/demo cases")
    snapshot = session.get(Snapshot, body.seed_snapshot_id)
    if snapshot is None or snapshot.case_id != case_id or snapshot.state != "complete":
        raise HTTPException(status_code=422, detail="seed_snapshot_id must name a completed snapshot in this case")
    graph = session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == snapshot.id, GraphSnapshot.state == "complete"))
    if graph is None:
        raise HTTPException(status_code=409, detail="No completed graph snapshot is available for this seed snapshot")
    try:
        entity_ref = body.entity_ref()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    seed = session.scalar(
        select(SyntheticReviewSeed).where(
            SyntheticReviewSeed.snapshot_id == snapshot.id,
            SyntheticReviewSeed.seed_entity_ref == entity_ref,
            SyntheticReviewSeed.seed_reason == body.seed_reason,
        )
    )
    if seed is None:
        seed = SyntheticReviewSeed(
            case_id=case_id, snapshot_id=snapshot.id, seed_entity_ref=entity_ref, seed_reason=body.seed_reason,
            synthetic=True, created_by=user.id,
        )
        session.add(seed)
        session.flush()
        refresh_synthetic_seed_proximity(session, evidence_root=settings.evidence_root, snapshot=snapshot, graph=graph)
        session.add(
            AuditRecord(
                case_id=case_id, actor_id=user.id, action="synthetic_review_seed.created", target_type="synthetic_review_seed",
                target_id=seed.id, detail={"snapshot_id": snapshot.id, "seed_entity_ref": entity_ref, "synthetic": True},
            )
        )
        session.commit()
    return {
        "synthetic_review_seed_id": seed.id, "seed_entity_id": seed.seed_entity_ref, "seed_reason": seed.seed_reason,
        "seed_snapshot_id": seed.snapshot_id, "synthetic": True,
    }


def finding_view(finding: FindingRecord) -> dict:
    detector = (finding.feature_vector or {}).get("detector_result", {})
    return {
        "finding_id": finding.id,
        "finding_type": finding.rule_id,
        "finding_version": finding.finding_version,
        "case_id": finding.case_id,
        "snapshot_id": finding.snapshot_id,
        "graph_snapshot_id": finding.graph_snapshot_id,
        "entity_or_transaction_id": finding.entity_ref,
        "entity_ref": finding.entity_ref,
        "window_start": finding.window_start.isoformat(),
        "window_end": finding.window_end.isoformat(),
        "rule_id": finding.rule_id,
        "rule_version": finding.rule_version,
        "claim": finding.claim,
        "raw_score": finding.raw_score,
        "score": finding.raw_score,
        "rank": finding.rank,
        "coverage": finding.coverage,
        "uncertainty": detector.get("uncertainty", {"scope": "See coverage and source evidence."}),
        "reason_codes": detector.get("reason_codes", []),
        "explanation": detector.get("explanation", finding.explanations[0] if finding.explanations else finding.claim),
        "evidence_refs": detector.get("evidence_refs", finding.source_refs),
        "graph_path": detector.get("graph_path"),
        "feature_vector_hash": finding.feature_vector_hash,
        "explanations": finding.explanations,
        "benign_alternatives": finding.benign_alternatives,
        "opposing_evidence": finding.opposing_evidence,
        "source_refs": finding.source_refs,
        "status": finding.status,
    }


def review_view(review: ReviewDecisionRecord) -> dict:
    return {
        "review_id": review.id,
        "finding_id": review.finding_id,
        "finding_version": review.finding_version,
        "disposition": review.disposition,
        "reason": review.reason,
        "counterevidence_refs": review.counterevidence_refs,
        "prior_review_id": review.prior_review_id,
        "created_at": review.created_at.isoformat() if review.created_at else None,
    }


def finding_review_history(session: Session, finding_id: str) -> list[dict]:
    reviews = session.scalars(
        select(ReviewDecisionRecord)
        .where(ReviewDecisionRecord.finding_id == finding_id)
        .order_by(ReviewDecisionRecord.created_at, ReviewDecisionRecord.id)
    )
    return [review_view(review) for review in reviews]


def finding_audit_history(session: Session, finding_id: str) -> list[dict]:
    records = session.scalars(
        select(AuditRecord)
        .where(AuditRecord.target_type == "finding", AuditRecord.target_id == finding_id)
        .order_by(AuditRecord.created_at, AuditRecord.id)
    )
    return [
        {
            "audit_id": record.id,
            "action": record.action,
            "detail": record.detail,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }
        for record in records
    ]


@router.get("/cases/{case_id}/findings")
def list_findings(
    case_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    require_case_member(case_id, user, session)
    records = list(
        session.scalars(
            select(FindingRecord)
            .where(FindingRecord.case_id == case_id)
            .order_by(FindingRecord.rank, FindingRecord.created_at)
            .offset(offset)
            .limit(limit)
        )
    )
    return {
        "findings": [finding_view(record) for record in records],
        "limit": limit,
        "offset": offset,
        "method": "deterministic-v1",
        "ml_enabled": False,
    }


@router.get("/findings/{finding_id}/evidence")
def finding_evidence(
    finding_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    finding = session.get(FindingRecord, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    require_case_member(finding.case_id, user, session)
    return {
        "finding": finding_view(finding),
        "feature_vector": finding.feature_vector,
        "source_refs": finding.source_refs,
        "coverage": finding.coverage,
        "opposing_evidence": finding.opposing_evidence,
        "review_history": finding_review_history(session, finding.id),
        "audit_history": finding_audit_history(session, finding.id),
        "replay_contract": {
            "snapshot_id": finding.snapshot_id,
            "graph_snapshot_id": finding.graph_snapshot_id,
            "rule_id": finding.rule_id,
            "rule_version": finding.rule_version,
            "ml_enabled": False,
        },
    }


@router.get("/cases/{case_id}/findings/export")
def export_findings(case_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    require_case_member(case_id, user, session)
    findings = list(
        session.scalars(
            select(FindingRecord).where(FindingRecord.case_id == case_id).order_by(FindingRecord.rank, FindingRecord.id)
        )
    )
    return {
        "case_id": case_id,
        "method": "deterministic-v1",
        "ml_enabled": False,
        "limitations": [
            "Findings prioritize observed patterns for review; they do not establish ownership, origin, or wrongdoing.",
            "Time windows and spend links are limited to committed source coverage.",
        ],
        "findings": [
            {
                "finding": finding_view(finding),
                "feature_vector": finding.feature_vector,
                "source_refs": finding.source_refs,
                "opposing_evidence": finding.opposing_evidence,
                "review_history": finding_review_history(session, finding.id),
                "audit_history": finding_audit_history(session, finding.id),
            }
            for finding in findings
        ],
    }


@router.get("/cases/{case_id}/features/export")
def export_features(
    case_id: str,
    snapshot_id: str | None = Query(default=None, min_length=1, max_length=128),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Export every persisted address-plus-time-window feature row for Phase 5A handoff."""
    require_case_member(case_id, user, session)
    statement = select(FeatureRecord).where(FeatureRecord.case_id == case_id)
    if snapshot_id:
        statement = statement.where(FeatureRecord.snapshot_id == snapshot_id)
    rows = list(session.scalars(statement.order_by(FeatureRecord.snapshot_id, FeatureRecord.window_start, FeatureRecord.entity_ref)))
    return {
        "case_id": case_id,
        "snapshot_id": snapshot_id,
        "feature_schema_version": "phase4.1-feature-v1",
        "ml_enabled": False,
        "rows": [
            {
                "feature_row_id": row.id, "entity_ref": row.entity_ref, "snapshot_id": row.snapshot_id,
                "graph_snapshot_id": row.graph_snapshot_id, "window_start": row.window_start.isoformat(),
                "window_end": row.window_end.isoformat(), "coverage": row.coverage, "source_refs": row.source_refs,
                "features": row.feature_vector,
            }
            for row in rows
        ],
    }


@router.post("/findings/{finding_id}/reviews", status_code=status.HTTP_201_CREATED)
def review_finding(
    finding_id: str, body: ReviewCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    finding = session.get(FindingRecord, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    require_case_member(finding.case_id, user, session)
    membership = session.scalar(
        select(CaseMembership).where(CaseMembership.case_id == finding.case_id, CaseMembership.user_id == user.id)
    )
    if membership is None or membership.role not in {"case_lead", "reviewer"}:
        raise HTTPException(status_code=403, detail="Reviewer or case lead role is required")
    for reference in body.counterevidence_refs:
        evidence_id = reference.get("evidence_id")
        locator = reference.get("locator")
        if not isinstance(evidence_id, str) or not evidence_id or not isinstance(locator, str) or not locator:
            raise HTTPException(
                status_code=422, detail="counterevidence_refs require non-empty evidence_id and locator"
            )
        source = session.get(EvidenceSource, evidence_id)
        if source is None or source.case_id != finding.case_id:
            raise HTTPException(status_code=422, detail="counterevidence reference is not a source in this case")
    previous = session.scalar(
        select(ReviewDecisionRecord)
        .where(ReviewDecisionRecord.finding_id == finding.id)
        .order_by(ReviewDecisionRecord.created_at.desc())
        .limit(1)
    )
    review = ReviewDecisionRecord(
        case_id=finding.case_id,
        finding_id=finding.id,
        finding_version=finding.finding_version,
        actor_id=user.id,
        disposition=body.disposition,
        reason=body.reason,
        counterevidence_refs=body.counterevidence_refs,
        prior_review_id=previous.id if previous else None,
    )
    updated = session.execute(
        update(FindingRecord)
        .where(FindingRecord.id == finding.id, FindingRecord.finding_version == body.expected_finding_version)
        .values(status=body.disposition, finding_version=FindingRecord.finding_version + 1)
    )
    if updated.rowcount != 1:
        session.expire(finding)
        session.refresh(finding)
        raise HTTPException(
            status_code=409,
            detail={
                "expected_finding_version": body.expected_finding_version,
                "actual_finding_version": finding.finding_version,
            },
        )
    session.refresh(finding)
    session.add(review)
    session.add(
        AuditRecord(
            case_id=finding.case_id,
            actor_id=user.id,
            action="finding.reviewed",
            target_type="finding",
            target_id=finding.id,
            detail={
                "disposition": body.disposition,
                "review_id": review.id,
                "counterevidence_ref_count": len(body.counterevidence_refs),
            },
        )
    )
    append_event(
        session,
        case_id=finding.case_id,
        event_type="finding.reviewed",
        stage="reviewed",
        payload={"finding_id": finding.id, "finding_version": finding.finding_version, "disposition": body.disposition},
    )
    session.commit()
    return {"review_id": review.id, "finding": finding_view(finding)}


async def sse_events(session: Session, case_id: str, after_sequence: int, follow: bool) -> AsyncIterator[str]:
    emitted = after_sequence
    while True:
        events = list(
            session.scalars(
                select(CaseEvent)
                .where(CaseEvent.case_id == case_id, CaseEvent.sequence > emitted)
                .order_by(CaseEvent.sequence)
            )
        )
        for event in events:
            emitted = event.sequence
            yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {json.dumps(event_envelope(event), separators=(',', ':'))}\n\n"
        if not follow:
            return
        yield ": heartbeat\n\n"
        await asyncio.sleep(settings.event_heartbeat_seconds)
        session.expire_all()


@router.get("/cases/{case_id}/events")
def get_case_events(
    case_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after: int | None = Query(default=None, ge=0),
    follow: bool = Query(default=False),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    require_case_member(case_id, user, session)
    if last_event_id and after is not None:
        raise HTTPException(status_code=400, detail="Use Last-Event-ID or after, not both")
    try:
        sequence = int(last_event_id) if last_event_id is not None else (after or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
    return StreamingResponse(
        sse_events(session, case_id, sequence, follow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
