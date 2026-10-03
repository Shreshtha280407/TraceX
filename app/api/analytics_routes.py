"""Entities, network correlation, Geo-IP and risk-propagation endpoints."""

# ruff: noqa: B008  # FastAPI declares dependencies through parameter defaults.
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_case_member
from app.config import settings
from app.db import get_session
from app.engine import analytics, geoip
from app.models import AnalyticsRevision, AnalyticsSnapshot, AuditRecord, FindingRecord, RiskRun, RiskSeed, User

router = APIRouter(prefix="/v1")


def _analytics(session: Session, case_id: str) -> AnalyticsSnapshot | AnalyticsRevision:
    record = analytics.latest_analytics(session, case_id)
    if record is None:
        raise HTTPException(
            status_code=409,
            detail="No entity/network analytics are available for this case yet (they are built at the end of an import)",
        )
    return record


def _latest_risk(session: Session, case_id: str, snapshot_id: str) -> RiskRun | None:
    run = session.scalar(
        select(RiskRun).where(RiskRun.case_id == case_id, RiskRun.snapshot_id == snapshot_id)
        .order_by(RiskRun.created_at.desc()).limit(1)
    )
    record = analytics.latest_analytics(session, case_id, snapshot_id)
    if run and record:
        pinned_id = run.parameters.get("analytics_snapshot_id")
        if pinned_id != record.id and (pinned_id or isinstance(record, AnalyticsRevision)):
            return None  # Old runs remain stored, but cannot masquerade as current.
    return run


def risk_lookup(session: Session, case_id: str, snapshot_id: str) -> dict[str, dict]:
    run = _latest_risk(session, case_id, snapshot_id)
    return {item["wallet"]: item for item in (run.scores if run else [])}


@router.get("/geoip/status")
def geoip_status(user: User = Depends(current_user)) -> dict:
    return geoip.status()


@router.get("/geoip/lookup")
def geoip_lookup(ip: str = Query(min_length=1, max_length=64), user: User = Depends(current_user)) -> dict:
    database = geoip.open_database()
    if database is None:
        scope, address = geoip.ip_scope(ip)
        return {"ip": ip, "scope": scope, "version": address.version if address else None, "database": "not installed"}
    return database.lookup(ip).as_dict()


@router.get("/cases/{case_id}/entities")
def list_case_entities(
    case_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    min_addresses: int = Query(default=2, ge=1),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    require_case_member(case_id, user, session)
    record = _analytics(session, case_id)
    cursor = analytics.cursor_for(settings.evidence_root, record)
    try:
        page = analytics.list_entities(cursor, limit=limit, offset=offset, min_addresses=min_addresses)
    finally:
        cursor.close()
    risk = risk_lookup(session, case_id, record.snapshot_id)
    for entity in page["entities"]:
        entity["risk"] = risk.get(entity["entity_id"])
    return {
        "analytics_snapshot_id": record.id,
        "snapshot_id": record.snapshot_id,
        "summary": record.summary,
        "limit": limit,
        "offset": offset,
        **page,
    }


def _open_findings_for(session: Session, snapshot_id: str, refs: list[str]) -> list[dict]:
    if not refs:
        return []
    rows = session.execute(
        select(FindingRecord.id, FindingRecord.rule_id, FindingRecord.entity_ref, FindingRecord.raw_score,
               FindingRecord.status)
        .where(FindingRecord.snapshot_id == snapshot_id, FindingRecord.entity_ref.in_(refs))
        .order_by(FindingRecord.rank).limit(100)
    ).all()
    return [
        {"finding_id": row[0], "rule_id": row[1], "entity_ref": row[2], "raw_score": row[3], "status": row[4]}
        for row in rows
    ]


@router.get("/cases/{case_id}/entities/{wallet}")
def get_case_entity(
    case_id: str, wallet: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """An entity cluster (E-...) or a single address, with its members, the
    transactions that link them, its relay endpoints and related findings."""
    require_case_member(case_id, user, session)
    record = _analytics(session, case_id)
    wallet = wallet.removeprefix("entity:").removeprefix("address:")
    cursor = analytics.cursor_for(settings.evidence_root, record)
    try:
        detail = analytics.entity_detail(cursor, wallet)
    finally:
        cursor.close()
    if detail is None:
        raise HTTPException(status_code=404, detail=f"{wallet} is not a wallet in this case's analytics snapshot")
    refs = [f"address:{address}" for address in detail["addresses"]] + [f"entity:{detail['wallet']}"]
    detail["findings"] = _open_findings_for(session, record.snapshot_id, refs)
    detail["risk"] = risk_lookup(session, case_id, record.snapshot_id).get(detail["wallet"])
    detail["analytics_snapshot_id"] = record.id
    if detail.get("entity") and detail["entity"]["address_count"] >= max(
        1000, 0.01 * (record.summary.get("clustered_addresses") or 0) * 10
    ):
        detail["caution"] = (
            "Very large cluster: common-input-ownership has chained many addresses together. This is typical of a "
            "service, exchange or consolidation hub -- or of the heuristic over-merging -- so review its linking "
            "transactions before treating it as one actor."
        )
    return detail


@router.get("/cases/{case_id}/entities/{wallet}/similar")
def get_similar_entities(
    case_id: str,
    wallet: str,
    limit: int = Query(default=10, ge=1, le=50),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Wallets whose position in the transaction graph is most similar
    (cosine similarity of spectral graph embeddings)."""
    require_case_member(case_id, user, session)
    record = _analytics(session, case_id)
    return analytics.similar_wallets(
        settings.evidence_root, record, wallet.removeprefix("entity:").removeprefix("address:"), limit=limit
    )


@router.get("/cases/{case_id}/network")
def get_case_network(case_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    require_case_member(case_id, user, session)
    record = _analytics(session, case_id)
    cursor = analytics.cursor_for(settings.evidence_root, record)
    try:
        view = analytics.network_summary(cursor)
    finally:
        cursor.close()
    view["geoip"] = geoip.status()
    view["network_finding_count"] = session.scalar(
        select(func.count()).select_from(FindingRecord).where(
            FindingRecord.snapshot_id == record.snapshot_id,
            FindingRecord.rule_version == analytics.NETWORK_RULE_VERSION,
        )
    ) or 0
    return view


@router.get("/cases/{case_id}/network/endpoints/{ip}")
def get_case_endpoint(
    case_id: str, ip: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    require_case_member(case_id, user, session)
    record = _analytics(session, case_id)
    cursor = analytics.cursor_for(settings.evidence_root, record)
    try:
        view = analytics.endpoint_view(cursor, ip)
    finally:
        cursor.close()
    if view is None:
        raise HTTPException(status_code=404, detail=f"{ip} was not observed in this case")
    view["findings"] = _open_findings_for(session, record.snapshot_id, [f"endpoint:{ip}"])
    return view


# --------------------------------------------------------------------------- #
# Risk propagation
# --------------------------------------------------------------------------- #


class RiskSeedCreate(BaseModel):
    wallet_ref: str = Field(min_length=1, max_length=512, description="address, address:..., entity:E-... or E-...")
    label: str = Field(default="illicit", min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=2000)
    weight: float = Field(default=1.0, gt=0.0, le=1.0)


def _seed_view(seed: RiskSeed) -> dict:
    return {
        "seed_id": seed.id, "wallet_ref": seed.wallet_ref, "label": seed.label, "reason": seed.reason,
        "weight": seed.weight, "source": seed.source,
        "created_at": seed.created_at.isoformat() if seed.created_at else None,
    }


def _run_view(run: RiskRun | None, limit: int) -> dict | None:
    if run is None:
        return None
    return {
        "risk_run_id": run.id, "snapshot_id": run.snapshot_id, "method_version": run.method_version,
        "parameters": run.parameters, "seeds": run.seeds, "summary": run.summary, "scores": run.scores[:limit],
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/cases/{case_id}/risk")
def get_case_risk(
    case_id: str,
    limit: int = Query(default=100, ge=1, le=2000),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    require_case_member(case_id, user, session)
    seeds = session.scalars(select(RiskSeed).where(RiskSeed.case_id == case_id).order_by(RiskSeed.created_at))
    record = analytics.latest_analytics(session, case_id)
    run = _latest_risk(session, case_id, record.snapshot_id) if record else None
    return {"seeds": [_seed_view(seed) for seed in seeds], "run": _run_view(run, limit),
            "analytics_available": record is not None}


def _recompute(session: Session, case_id: str, user: User) -> RiskRun | None:
    record = analytics.latest_analytics(session, case_id)
    if record is None:
        return None
    try:
        run = analytics.propagate_risk(
            session, evidence_root=settings.evidence_root, record=record, case_id=case_id, created_by=user.id
        )
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Risk propagation needs the `ml` extra (numpy/scipy)") from exc
    return run


@router.post("/cases/{case_id}/risk/seeds", status_code=status.HTTP_201_CREATED)
def add_risk_seed(
    case_id: str, body: RiskSeedCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """Mark a wallet as a known-illicit starting point and re-propagate."""
    require_case_member(case_id, user, session)
    reference = body.wallet_ref.strip()
    seed = session.scalar(select(RiskSeed).where(RiskSeed.case_id == case_id, RiskSeed.wallet_ref == reference))
    if seed is None:
        seed = RiskSeed(case_id=case_id, wallet_ref=reference, label=body.label, reason=body.reason,
                        weight=body.weight, source="analyst", created_by=user.id)
        session.add(seed)
    else:
        seed.label, seed.reason, seed.weight = body.label, body.reason, body.weight
    session.flush()
    session.add(AuditRecord(case_id=case_id, actor_id=user.id, action="risk_seed.upserted", target_type="risk_seed",
                            target_id=seed.id, detail={"wallet_ref": reference, "label": body.label,
                                                       "weight": body.weight}))
    run = _recompute(session, case_id, user)
    session.commit()
    return {"seed": _seed_view(seed), "run": _run_view(run, 100)}


@router.delete("/cases/{case_id}/risk/seeds/{seed_id}")
def delete_risk_seed(
    case_id: str, seed_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    require_case_member(case_id, user, session)
    seed = session.get(RiskSeed, seed_id)
    if seed is None or seed.case_id != case_id:
        raise HTTPException(status_code=404, detail="Risk seed not found")
    session.add(AuditRecord(case_id=case_id, actor_id=user.id, action="risk_seed.deleted", target_type="risk_seed",
                            target_id=seed.id, detail={"wallet_ref": seed.wallet_ref}))
    session.delete(seed)
    session.flush()
    run = _recompute(session, case_id, user)
    session.commit()
    return {"deleted": seed_id, "run": _run_view(run, 100)}


@router.post("/cases/{case_id}/risk/run")
def run_case_risk(case_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    require_case_member(case_id, user, session)
    run = _recompute(session, case_id, user)
    if run is None:
        raise HTTPException(status_code=409, detail="No analytics snapshot is available for this case yet")
    session.commit()
    return _run_view(run, 100)
