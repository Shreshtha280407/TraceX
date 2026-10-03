"""Explicit labelled-population contract; triage is not confirmation of crime."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.models import Case, FindingRecord, ReviewDecisionRecord


def analyst_label_contract(session, *, case_id: str, snapshot_id: str, cutoff: datetime) -> dict:
    case = session.get(Case, case_id)
    rows = session.execute(select(FindingRecord, ReviewDecisionRecord).join(
        ReviewDecisionRecord, ReviewDecisionRecord.finding_id == FindingRecord.id).where(
        FindingRecord.case_id == case_id, FindingRecord.snapshot_id == snapshot_id,
        # Scoring/evidence are immutable for this finding ID. Review changes
        # increment presentation versions only. A later review must not erase
        # an earlier as-of label when the caller's cutoff precedes that review.
        ReviewDecisionRecord.finding_version <= FindingRecord.finding_version,
        ReviewDecisionRecord.finding_version >= 1,
        ReviewDecisionRecord.created_at <= cutoff).order_by(ReviewDecisionRecord.created_at, ReviewDecisionRecord.id)).all()
    latest = {}
    for finding, decision in rows:
        if finding.entity_ref.startswith("tx:"):
            # Decisions concern a finding proposition, not every proposition
            # about its transaction (and never proof of criminal activity).
            latest[finding.id] = (finding, decision)
    records = []
    for _, (finding, decision) in sorted(latest.items()):
        # Escalation, needs_data_review and unreviewed rows remain unknown.
        label = {"confirmed": True, "dismissed": False}.get(decision.disposition)
        records.append({"transaction": finding.entity_ref, "label": label, "label_mask": label is not None,
                        "label_source": "analyst_review", "target": "reviewed-finding-proposition-v2",
                        "proposition": finding.rule_id,
                        "finding_id": finding.id, "finding_version": finding.finding_version,
                        "reviewed_finding_version": decision.finding_version,
                        "decision_id": decision.id, "decision_time": decision.created_at.isoformat(),
                        "feature_as_of": finding.window_end.isoformat(), "snapshot_id": snapshot_id,
                        "rule_version": finding.rule_version, "case_id": case_id})
        records[-1]["feature_vector_hash"] = finding.feature_vector_hash
    positive = sum(r["label"] is True for r in records)
    negative = sum(r["label"] is False for r in records)
    enough = positive >= 10 and negative >= 10 and positive + negative >= 50
    return {"version": "analyst-label-contract-v2", "records": records, "positive": positive, "negative": negative,
            "known": positive + negative, "unknown": sum(not r["label_mask"] for r in records),
            "cutoff": cutoff.isoformat(), "synthetic_case": bool(case and case.synthetic),
            "training_eligible": False, "class_support_sufficient": enough,
            "reason": "Representative independently confirmed outcomes and disjoint validation are required; "
                      "reviewed-alert selection bias is unresolved. No automatic supervised deployment.",
            "unreviewed_policy": "unknown, never a negative", "conflict_policy": "latest version-matched decision wins",
            "transaction_aggregation": "forbidden without a separately defined and independently labelled target",
            "positive_confirmation_capability": "confirmed means the reviewed pattern proposition only; triaged and escalated remain unknown",
            "production_fallback": "anomaly-stack-v2"}
