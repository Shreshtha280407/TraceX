"""Bounded, deterministic grouping of observations, not ownership or wrongdoing.

The anchor set is frozen at the representative. Adding a member NEVER expands
connectivity. This deliberately fragments very long episodes rather than joining
a whole case through shared hubs. Evidence remains on the original findings.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from sqlalchemy import func, select, update

from app.engine.graph.query import _path
from app.models import (
    FindingParticipation,
    FindingRecord,
    InvestigationAnchor,
    InvestigationGroup,
    InvestigationMember,
    InvestigationReplacement,
    InvestigationRun,
    InvestigationSubject,
    Snapshot,
)

VERSION = "observed-episode-groups-v1"
QUEUE_POLICY = "group-family-round-robin-v1"
UNRESOLVED = ("open", "escalated", "needs_data_review")
POLICY = {"version": VERSION, "queue": QUEUE_POLICY, "utc_episode_seconds": 86400,
          "max_members": 256, "max_representative_anchors": 256,
          "connection": "fixed representative canonical UTXO transaction overlap for peeling; otherwise exact source-record overlap plus same focal and rule/version",
          "no_transitive_anchor_expansion": True, "unknown_connectivity": "singleton",
          "priority": "workflow band, independent rule/version family rank, family, stable group ID; no member-count boost",
          "group_target": "observed connected pattern episode requiring investigation; not transaction criminality or confirmation of every member claim"}
PROCEDURE_SHA256 = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode() + Path(__file__).read_bytes()).hexdigest()


def identifier(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def observations(finding, cursor):
    """Canonical SPENT_BY endpoints, or exact immutable source locators.

    IP/ASN/address/cluster co-occurrence is never a connectivity anchor.
    Graph nodes merely asserted in detector JSON are not sufficient for peeling.
    """
    detector = (finding.feature_vector or {}).get("detector_result") or {}
    path = detector.get("graph_path") or {}
    edge_ids = sorted(set(path.get("edge_ids") or []))[:256]
    txs = {finding.entity_ref} if finding.entity_ref.startswith("tx:") else set()
    anchors = set()
    peeling = "peel" in finding.rule_id.lower()
    if peeling and cursor is not None and edge_ids:
        rows = cursor.execute("SELECT from_node, to_node FROM edges WHERE edge_type='SPENT_BY' AND edge_id IN (SELECT unnest(?))", [edge_ids]).fetchall()
        for previous, spender in rows:
            if previous.startswith("out:") and spender.startswith("tx:"):
                txs.update(("tx:" + previous[4:].rsplit(":", 1)[0], spender))
        anchors = {"utxo:" + tx for tx in txs} if rows else set()
    else:
        for ref in finding.source_refs or []:
            if ref.get("evidence_id") and ref.get("locator"):
                anchors.add("source:" + json.dumps({k: ref.get(k) for k in ("evidence_id", "locator", "source_sha256")}, sort_keys=True))
    entities = {finding.entity_ref} if not finding.entity_ref.startswith("tx:") else set()
    return {hashlib.sha256(token.encode()).hexdigest() for token in sorted(anchors)[:256]}, txs, entities


def _input_digest(session, snapshot_id, graph_sha):
    digest = hashlib.sha256((PROCEDURE_SHA256 + graph_sha).encode())
    count = 0
    rows = session.execute(select(FindingRecord.id, FindingRecord.rule_id, FindingRecord.rule_version,
                                  FindingRecord.entity_ref, FindingRecord.window_start, FindingRecord.window_end,
                                  FindingRecord.feature_vector_hash, FindingRecord.claim, FindingRecord.source_refs)
                           .where(FindingRecord.snapshot_id == snapshot_id).order_by(FindingRecord.id)
                           .execution_options(yield_per=256))
    for row in rows:
        digest.update(json.dumps(list(row), default=str, sort_keys=True).encode())
        count += 1
    participation = session.execute(select(FindingParticipation.finding_id, FindingParticipation.transaction_ref)
        .join(FindingRecord).where(FindingRecord.snapshot_id == snapshot_id)
        .order_by(FindingParticipation.finding_id, FindingParticipation.transaction_ref).execution_options(yield_per=256))
    for row in participation:
        digest.update(json.dumps(list(row)).encode())
    return digest.hexdigest(), count


def materialize(session, *, case_id, snapshot_id, graph, evidence_root):
    """Publish atomically; a failed attempt leaves no partial active generation.

    A snapshot row lock serializes concurrent retries. IDs depend on immutable
    inputs, not review state. A changed input creates replacements, never edits
    a published group's membership or copies its proposition decision.
    """
    snapshot = session.scalar(select(Snapshot).where(Snapshot.id == snapshot_id, Snapshot.case_id == case_id).with_for_update())
    if snapshot is None or graph.case_id != case_id or graph.snapshot_id != snapshot_id:
        raise ValueError("grouping requires a case-scoped snapshot and graph")
    digest, expected = _input_digest(session, snapshot_id, graph.sha256)
    run_id = identifier(f"{case_id}:{snapshot_id}:{VERSION}:{digest}")
    prior = session.get(InvestigationRun, run_id)
    if prior is not None and prior.state == "complete":
        return {"run_id": run_id, "procedure_version": VERSION, "procedure_sha256": PROCEDURE_SHA256,
                "underlying_findings": expected, "groups": session.scalar(select(func.count()).select_from(InvestigationGroup).where(InvestigationGroup.run_id == run_id)), "reused": True}
    # Worker-local reader, not the API's persistent graph cache. Otherwise a
    # post-stage reader could retain native allocations and lock a different
    # DuckDB configuration out of the same immutable file.
    cursor = duckdb.connect(str(_path(evidence_root, graph.storage_relative_path)), read_only=True,
                            config={"threads": 1, "memory_limit": "256MB"})
    try:
        with session.begin_nested():
            run = InvestigationRun(id=run_id, case_id=case_id, snapshot_id=snapshot_id, procedure_version=VERSION,
                                   procedure_sha256=PROCEDURE_SHA256, input_sha256=digest, state="building", active=False)
            session.add(run)
            session.flush()
            query = select(FindingRecord).where(FindingRecord.snapshot_id == snapshot_id).order_by(
                FindingRecord.window_start, FindingRecord.window_end, FindingRecord.rule_id,
                FindingRecord.rule_version, FindingRecord.entity_ref, FindingRecord.id).execution_options(yield_per=256)
            groups = members = 0
            for finding in session.scalars(query):
                anchors, txs, entities = observations(finding, cursor)
                # Complete normalized participation for newly materialized
                # deterministic findings. Legacy rows retain explicit coverage
                # limitations rather than inventing missing graph membership.
                txs.update(session.scalars(select(FindingParticipation.transaction_ref).where(FindingParticipation.finding_id == finding.id)))
                start, end = utc(finding.window_start), utc(finding.window_end)
                episode = datetime.fromtimestamp(int(start.timestamp()) // 86400 * 86400, UTC)
                family = f"{finding.rule_id}/{finding.rule_version}"
                peeling = "peel" in finding.rule_id.lower()
                # Long/cross-boundary windows remain singletons. No unknown-as-benign.
                bounded = end.timestamp() <= episode.timestamp() + 86400 and end >= start
                group = None
                if anchors and bounded:
                    match = select(InvestigationGroup).join(InvestigationAnchor).where(
                        InvestigationGroup.run_id == run_id, InvestigationGroup.family == family,
                        InvestigationGroup.episode_start == episode, InvestigationGroup.member_count < 256,
                        InvestigationAnchor.token.in_(anchors))
                    if not peeling:
                        match = match.where(InvestigationGroup.focal_ref == finding.entity_ref)
                    group = session.scalar(match.order_by(InvestigationGroup.id).limit(1))
                if group is None:
                    group = InvestigationGroup(id=identifier(f"{run_id}:{finding.id}"), run_id=run_id, case_id=case_id,
                        snapshot_id=snapshot_id, family=family, episode_type="peeling_episode" if peeling else "repeated_observation_episode",
                        proposition=f"Investigate the observed {finding.rule_id} pattern as a bounded, directly connected episode. This proposition does not assert ownership, wrongdoing, or confirm every member claim.",
                        focal_ref=finding.entity_ref, episode_start=episode, window_start=start, window_end=end,
                        representative_id=finding.id, representative_score=finding.raw_score, member_count=0,
                        transaction_count=0, entity_count=0, status="open", review_version=1,
                        rationale={"policy": POLICY, "connection": "canonical SPENT_BY endpoint overlap" if peeling else "exact immutable source record plus same focal and detector",
                                   "representative_anchors": len(anchors), "fixed_backbone": True,
                                   "coverage": "Counts enumerate verified graph endpoints and focal references, not inferred wallet owners. Capped detector paths can omit additional participation.",
                                   "singleton_reason": None if anchors and bounded else "No verified connectivity or window exceeds fixed UTC episode boundary"})
                    session.add(group)
                    session.flush()
                    if bounded:
                        session.add_all(InvestigationAnchor(group_id=group.id, token=token) for token in sorted(anchors))
                    # Link prior generations using shared immutable member IDs;
                    # prior reviewed groups remain accessible and unchanged.
                    groups += 1
                previous = session.scalars(select(InvestigationMember.group_id).join(InvestigationGroup).join(InvestigationRun)
                    .where(InvestigationMember.finding_id == finding.id, InvestigationRun.id != run_id,
                           InvestigationRun.case_id == case_id, InvestigationRun.state == "complete").distinct())
                for old in previous:
                    if session.get(InvestigationReplacement, (old, group.id)) is None:
                        session.add(InvestigationReplacement(prior_id=old, replacement_id=group.id,
                            reason="New immutable grouping generation; overlapping source finding; no decision inherited"))
                group.window_start = min(utc(group.window_start), start)
                group.window_end = max(utc(group.window_end), end)
                session.add(InvestigationMember(group_id=group.id, finding_id=finding.id, ordinal=group.member_count))
                group.member_count += 1
                existing = set(session.execute(select(InvestigationSubject.kind, InvestigationSubject.ref).where(InvestigationSubject.group_id == group.id)))
                subjects = {("transaction", ref) for ref in txs} | {("entity", ref) for ref in entities}
                session.add_all(InvestigationSubject(group_id=group.id, kind=kind, ref=ref) for kind, ref in sorted(subjects - existing))
                group.transaction_count += len({ref for kind, ref in subjects - existing if kind == "transaction"})
                group.entity_count += len({ref for kind, ref in subjects - existing if kind == "entity"})
                session.flush()
                members += 1
            if members != expected:
                raise RuntimeError("group membership completeness check failed")
            session.execute(update(InvestigationRun).where(InvestigationRun.snapshot_id == snapshot_id, InvestigationRun.id != run_id).values(active=False))
            run.state, run.active = "complete", True
            session.flush()
        return {"run_id": run_id, "procedure_version": VERSION, "procedure_sha256": PROCEDURE_SHA256,
                "underlying_findings": members, "groups": groups, "reused": False}
    finally:
        cursor.close()
