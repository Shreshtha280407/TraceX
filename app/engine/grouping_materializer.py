"""Disk-indexed execution of the frozen observed-episode-groups-v1 procedure.

The reference implementation in investigations.py remains byte-for-byte frozen.
This executor changes transport/storage, NOT anchors, ordering, IDs or decisions.
One bounded page replaces per-finding PostgreSQL round trips and graph scans.
Only a complete generation is published; the private index is never evidence.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb
from sqlalchemy import func, select, update

from app.db import bulk_insert_serialized
from app.engine import investigations as policy
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
from app.resources import admit_disk_allocation

EXECUTION_VERSION = "disk-indexed-grouping-v1"
EXECUTION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
PAGE = 256
logger = logging.getLogger(__name__)


class _Edges:
    """Serve the reference observation extractor from one page's canonical rows."""

    def __init__(self, graph, findings):
        self.rows = defaultdict(list)
        requested = set()
        for finding in findings:
            if "peel" in finding.rule_id.lower():
                path = ((finding.feature_vector or {}).get("detector_result") or {}).get("graph_path") or {}
                requested.update(sorted(set(path.get("edge_ids") or []))[:256])
        if requested:
            for edge_id, previous, spender in graph.execute(
                "SELECT edge_id, from_node, to_node FROM edges "
                "WHERE edge_type='SPENT_BY' AND edge_id IN (SELECT unnest(?))", [sorted(requested)]
            ).fetchall():
                self.rows[edge_id].append((previous, spender))

    def execute(self, _query, parameters):
        self.selected = [row for edge_id in parameters[0] for row in self.rows.get(edge_id, ())]
        return self

    def fetchall(self):
        return self.selected


def _index(path):
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA cache_size=-8192;
        PRAGMA temp_store=FILE;
        PRAGMA journal_mode=OFF;
        CREATE TABLE groups(id TEXT PRIMARY KEY, family TEXT, episode INTEGER,
            focal TEXT, members INTEGER, start TEXT, end TEXT, txs INTEGER, entities INTEGER, payload TEXT);
        CREATE TABLE anchors(group_id TEXT, token TEXT, PRIMARY KEY(group_id, token));
        CREATE INDEX anchors_token ON anchors(token, group_id);
        CREATE TABLE eligible_anchors(group_id TEXT, token TEXT, PRIMARY KEY(group_id, token));
        CREATE INDEX eligible_anchor_token ON eligible_anchors(token, group_id);
        CREATE TABLE subjects(group_id TEXT, kind TEXT, ref TEXT, PRIMARY KEY(group_id, kind, ref));
        CREATE TABLE members(group_id TEXT, finding_id TEXT, ordinal INTEGER);
        CREATE TABLE replacements(prior_id TEXT, replacement_id TEXT, reason TEXT,
            PRIMARY KEY(prior_id, replacement_id));
    """)
    return db


def _pages(db, query):
    cursor = db.execute(query)
    while rows := cursor.fetchmany(PAGE):
        yield rows


def _publish(session, db, progress, expected, group_count):
    for page in _pages(db, "SELECT id,members,start,end,txs,entities,payload FROM groups ORDER BY id"):
        rows = []
        for gid, members, start, end, txs, entities, payload in page:
            row = json.loads(payload)
            row.update(id=gid, member_count=members, transaction_count=txs, entity_count=entities,
                       window_start=datetime.fromisoformat(start), window_end=datetime.fromisoformat(end))
            row["episode_start"] = datetime.fromisoformat(row["episode_start"])
            rows.append(row)
        bulk_insert_serialized(session, InvestigationGroup, rows, frozenset({"rationale"}))
        progress({"phase": "publishing", "prepared_findings": expected, "total_findings": expected,
                  "prepared_groups": group_count})
    for model, query, fields in (
        (InvestigationAnchor, "SELECT group_id,token FROM anchors", ("group_id", "token")),
        (InvestigationMember, "SELECT group_id,finding_id,ordinal FROM members", ("group_id", "finding_id", "ordinal")),
        (InvestigationSubject, "SELECT group_id,kind,ref FROM subjects", ("group_id", "kind", "ref")),
        (InvestigationReplacement, "SELECT prior_id,replacement_id,reason FROM replacements", ("prior_id", "replacement_id", "reason")),
    ):
        for page in _pages(db, query):
            bulk_insert_serialized(session, model, [dict(zip(fields, row, strict=True)) for row in page], frozenset())
            progress({"phase": "publishing", "prepared_findings": expected, "total_findings": expected,
                      "prepared_groups": group_count})


def materialize(session, *, case_id, snapshot_id, graph, evidence_root, progress=None):
    progress = progress or (lambda _details: None)
    started = time.monotonic()
    progress({"phase": "verifying_inputs", "prepared_findings": 0, "total_findings": None, "prepared_groups": 0})
    snapshot = session.scalar(select(Snapshot).where(
        Snapshot.id == snapshot_id, Snapshot.case_id == case_id).with_for_update())
    if snapshot is None or graph.case_id != case_id or graph.snapshot_id != snapshot_id:
        raise ValueError("grouping requires a case-scoped snapshot and graph")
    digest, expected = policy._input_digest(session, snapshot_id, graph.sha256)
    run_id = policy.identifier(f"{case_id}:{snapshot_id}:{policy.VERSION}:{digest}")
    prior = session.get(InvestigationRun, run_id)
    base = {"run_id": run_id, "procedure_version": policy.VERSION, "procedure_sha256": policy.PROCEDURE_SHA256,
            "underlying_findings": expected, "execution_version": EXECUTION_VERSION,
            "execution_sha256": EXECUTION_SHA256}
    if prior is not None and prior.state == "complete":
        return {**base, "groups": session.scalar(select(func.count()).select_from(InvestigationGroup).where(
            InvestigationGroup.run_id == run_id)), "reused": True}
    # Conservative disk admission includes worst-case 256 representative anchors
    # and subjects per finding; all existing case data remains untouched.
    scratch = evidence_root / ".grouping-scratch"
    admit_disk_allocation("investigation grouping", scratch, expected * 96 * 1024)
    scratch.mkdir(parents=True, exist_ok=True)
    graph_reader = duckdb.connect(str(_path(evidence_root, graph.storage_relative_path)), read_only=True,
                                  config={"threads": 1, "memory_limit": "256MB"})
    groups = members = 0
    try:
        with TemporaryDirectory(prefix="grouping-", dir=scratch) as directory:
            db = _index(Path(directory) / "index.sqlite")
            try:
                with session.begin_nested():
                    run = InvestigationRun(id=run_id, case_id=case_id, snapshot_id=snapshot_id,
                        procedure_version=policy.VERSION, procedure_sha256=policy.PROCEDURE_SHA256,
                        input_sha256=digest, state="building", active=False)
                    session.add(run)
                    session.flush()
                    query = select(FindingRecord).where(FindingRecord.snapshot_id == snapshot_id).order_by(
                        FindingRecord.window_start, FindingRecord.window_end, FindingRecord.rule_id,
                        FindingRecord.rule_version, FindingRecord.entity_ref, FindingRecord.id).execution_options(yield_per=PAGE)
                    for page in session.scalars(query).partitions(PAGE):
                        ids = [finding.id for finding in page]
                        participation = defaultdict(set)
                        for fid, ref in session.execute(select(FindingParticipation.finding_id,
                                FindingParticipation.transaction_ref).where(FindingParticipation.finding_id.in_(ids))):
                            participation[fid].add(ref)
                        previous = defaultdict(set)
                        for fid, old in session.execute(select(InvestigationMember.finding_id, InvestigationMember.group_id)
                                .join(InvestigationGroup).join(InvestigationRun).where(
                                    InvestigationMember.finding_id.in_(ids), InvestigationRun.id != run_id,
                                    InvestigationRun.case_id == case_id, InvestigationRun.state == "complete").distinct()):
                            previous[fid].add(old)
                        edges = _Edges(graph_reader, page)
                        for finding in page:
                            anchors, txs, entities = policy.observations(finding, edges)
                            txs.update(participation[finding.id])
                            start, end = policy.utc(finding.window_start), policy.utc(finding.window_end)
                            episode = datetime.fromtimestamp(int(start.timestamp()) // 86400 * 86400, UTC)
                            family = f"{finding.rule_id}/{finding.rule_version}"
                            peeling = "peel" in finding.rule_id.lower()
                            bounded = end.timestamp() <= episode.timestamp() + 86400 and end >= start
                            group = None
                            if anchors and bounded:
                                sql = ("SELECT g.id,g.members,g.start,g.end,g.txs,g.entities FROM groups g "
                                       "JOIN eligible_anchors a ON a.group_id=g.id WHERE g.family=? AND g.episode=? "
                                       "AND g.members<256 AND a.token IN (" + ",".join("?" for _ in anchors) + ")")
                                params = [family, int(episode.timestamp()), *sorted(anchors)]
                                if not peeling:
                                    sql += " AND g.focal=?"
                                    params.append(finding.entity_ref)
                                group = db.execute(sql + " ORDER BY g.id LIMIT 1", params).fetchone()
                            if group is None:
                                gid = policy.identifier(f"{run_id}:{finding.id}")
                                rationale = {"policy": policy.POLICY,
                                    "connection": "canonical SPENT_BY endpoint overlap" if peeling else "exact immutable source record plus same focal and detector",
                                    "representative_anchors": len(anchors), "fixed_backbone": True,
                                    "coverage": "Counts enumerate verified graph endpoints and focal references, not inferred wallet owners. Capped detector paths can omit additional participation.",
                                    "singleton_reason": None if anchors and bounded else "No verified connectivity or window exceeds fixed UTC episode boundary"}
                                payload = {"run_id": run_id, "case_id": case_id, "snapshot_id": snapshot_id,
                                    "family": family, "episode_type": "peeling_episode" if peeling else "repeated_observation_episode",
                                    "proposition": f"Investigate the observed {finding.rule_id} pattern as a bounded, directly connected episode. This proposition does not assert ownership, wrongdoing, or confirm every member claim.",
                                    "focal_ref": finding.entity_ref, "episode_start": episode.isoformat(),
                                    "representative_id": finding.id, "representative_score": finding.raw_score,
                                    "status": "open", "review_version": 1, "rationale": json.dumps(rationale)}
                                db.execute("INSERT INTO groups VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    (gid, family, int(episode.timestamp()), finding.entity_ref, 0,
                                     start.isoformat(), end.isoformat(), 0, 0, json.dumps(payload)))
                                if bounded:
                                    db.executemany("INSERT INTO anchors VALUES (?,?)", [(gid, a) for a in sorted(anchors)])
                                    db.executemany("INSERT INTO eligible_anchors VALUES (?,?)", [(gid, a) for a in sorted(anchors)])
                                groups += 1
                                group = (gid, 0, start.isoformat(), end.isoformat(), 0, 0)
                            gid, ordinal, old_start, old_end, tx_count, entity_count = group
                            for old in sorted(previous[finding.id]):
                                db.execute("INSERT OR IGNORE INTO replacements VALUES (?,?,?)", (old, gid,
                                    "New immutable grouping generation; overlapping source finding; no decision inherited"))
                            db.execute("INSERT INTO members VALUES (?,?,?)", (gid, finding.id, ordinal))
                            for kind, ref in sorted({("transaction", ref) for ref in txs} | {("entity", ref) for ref in entities}):
                                inserted = db.execute("INSERT OR IGNORE INTO subjects VALUES (?,?,?)", (gid, kind, ref)).rowcount
                                tx_count += inserted if kind == "transaction" else 0
                                entity_count += inserted if kind == "entity" else 0
                            db.execute("UPDATE groups SET members=?,start=?,end=?,txs=?,entities=? WHERE id=?",
                                (ordinal + 1, min(datetime.fromisoformat(old_start), start).isoformat(),
                                 max(datetime.fromisoformat(old_end), end).isoformat(), tx_count, entity_count, gid))
                            if ordinal + 1 == 256:
                                # Full groups cannot match again. Retain their canonical
                                # anchors for publication, but remove them from lookup so
                                # repeated hubs do not scan all prior full episodes.
                                db.execute("DELETE FROM eligible_anchors WHERE group_id=?", (gid,))
                            members += 1
                        progress({"phase": "building", "prepared_findings": members, "total_findings": expected,
                                  "prepared_groups": groups})
                    if members != expected:
                        raise RuntimeError("group membership completeness check failed")
                    _publish(session, db, progress, expected, groups)
                    session.execute(update(InvestigationRun).where(InvestigationRun.snapshot_id == snapshot_id,
                        InvestigationRun.id != run_id).values(active=False))
                    run.state, run.active = "complete", True
                    session.flush()
            finally:
                db.close()
        logger.info("review grouping: %s findings, %s groups, %.3f seconds (%s)",
                    members, groups, time.monotonic() - started, EXECUTION_VERSION)
        return {**base, "underlying_findings": members, "groups": groups, "reused": False}
    finally:
        graph_reader.close()
