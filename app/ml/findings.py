"""Run the stack on a committed snapshot and write reviewable findings.

This is what makes the stack part of TraceX rather than a bench experiment. It
reads the same receipt-approved canonical fragments the graph builder and the
deterministic detectors read, and it writes `FindingRecord` rows through the same
table the existing `GET /v1/cases/{case_id}/findings` endpoint already serves — so
the ranking reaches the API and the frontend without a new surface.

Three boundaries are kept exactly where Phase 4 put them:

* a finding is a **review queue item**, never a verdict. Every row carries benign
  alternatives and an explicit opposing-evidence limitation;
* every row carries the source locators of the facts it was computed from, so a
  reviewer can reopen the exact raw record;
* the four Phase 4.1 `risk_*` fields stay out of the model input entirely.

Only the unsupervised layers run here. The supervised layer needs reviewer
decisions, and until those exist it is a research comparator, not something that
should be writing into a case.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import append_event
from app.ml import grains, layers
from app.ml.facts import facts_from_records
from app.ml.fusion import stouffer_fuse
from app.ml_release_constants import ML_RULE_VERSION
from app.models import FeatureRecord, FindingRecord, GraphSnapshot, Snapshot

WINDOW_SECONDS = 900
#: The combination the ablation selected on the fixture, using unsupervised layers
#: only. Recorded here rather than tuned at call time so a stored finding always
#: names the configuration that produced it. This is the Phase 5B frozen decision
#: (`experiments/model_decision.md`) and must not change without a new release ID.
DEFAULT_LAYERS = ("A_global", "D_burst")
DEFAULT_BUDGET = 0.01
FUSION_METHOD = "stouffer"
#: The scoring release identity. There is no static model artifact -- the stack
#: is refit on each snapshot's own reference period at scoring time -- so this ID
#: names the frozen *procedure* (layers, fusion, feature contract, training seed)
#: rather than a `.joblib` file. Bump it whenever any of `release_identity()`'s
#: fields change; the bump automatically changes `model_run_id` for every finding
#: scored afterward, so a reviewer can always tell which procedure produced a row.
RELEASE_ID = "anomaly-stack-v1"


def release_identity() -> dict[str, Any]:
    """The frozen scoring-release identity, derived from code, not configuration.

    Every field here is a Phase 5B frozen decision: the layer combination, the
    fusion method, the default review budget, the training seed used by the
    stratified/global Isolation Forest and MiniBatchKMeans fits inside layer A,
    and the transaction feature contract layer A actually scores on (layer D
    derives its motif count series from the same table's family flags, so it
    adds no columns of its own). Changing any of these is a new release and must
    go through `experiments/model_decision.md`, not a silent code edit.
    """
    return {
        "release_id": RELEASE_ID,
        "layers": list(DEFAULT_LAYERS),
        "fusion_method": FUSION_METHOD,
        "default_review_budget": DEFAULT_BUDGET,
        "training_seed": layers.SEED,
        "feature_contract_version": ML_RULE_VERSION,
        "feature_columns_sha256": hashlib.sha256(",".join(grains.TRANSACTION_COLUMNS).encode("utf-8")).hexdigest(),
        "fitting_strategy": (
            "dynamic: refit on each snapshot's own earliest reference_fraction of "
            "transactions at scoring time; no static model artifact is loaded or shipped"
        ),
    }


def release_manifest_sha256() -> str:
    """A digest over the frozen identity -- the "signature" every finding is tied to.

    Computed from code constants only, so it is reproducible by anyone reading
    this file; it does not depend on any file outside the repository being present
    at runtime, which keeps `materialize_ml_findings` deployable without also
    shipping `experiments/`.
    """
    return hashlib.sha256(json.dumps(release_identity(), sort_keys=True).encode("utf-8")).hexdigest()


def model_run_id(*, snapshot_id: str, budget: float) -> str:
    """A stable, deterministic ID naming exactly which scoring release, applied to
    exactly which snapshot and budget, produced a set of findings.

    Deterministic (not a random UUID) on purpose: re-scoring the same snapshot at
    the same budget under an unchanged release always reproduces the same ID, so
    a reviewer -- or a test -- can recompute it independently rather than trusting
    a value stamped at write time. It changes automatically if the release
    identity changes, which is what makes it a safe substitute for a versioned
    static artifact hash.
    """
    payload = f"{RELEASE_ID}:{release_manifest_sha256()[:16]}:{snapshot_id}:{budget:.6f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass
class MLFindingResult:
    written: int
    scored_transactions: int
    layers_used: tuple[str, ...]
    budget: float
    flagged: int
    release_id: str = RELEASE_ID
    model_run_id: str | None = None


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _source_refs_by_txid(records: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Map each transaction to the source locators of its own canonical facts.

    Without this a stored finding could not be traced back to the raw record, which
    is the property the whole evidence chain rests on.
    """
    refs: dict[str, list[dict]] = {}
    seen: dict[str, set[str]] = {}
    for kind in ("transactions", "inputs", "outputs"):
        for fact in records.get(kind, []):
            txid = str(fact.get("txid", ""))
            if not txid:
                continue
            bucket = refs.setdefault(txid, [])
            marks = seen.setdefault(txid, set())
            for reference in fact.get("source_refs", []) or []:
                key = json.dumps(reference, sort_keys=True)
                if key not in marks:
                    marks.add(key)
                    bucket.append(reference)
    return refs


def _coverage(
    graph: GraphSnapshot, scored: int, flagged: int, budget: float, *, release_id: str, run_id: str
) -> dict[str, Any]:
    graph_coverage = graph.coverage or {}
    return {
        "complete": bool(graph_coverage.get("spend_lineage_complete")),
        "spend_lineage_complete": bool(graph_coverage.get("spend_lineage_complete")),
        "window_boundary_incomplete": True,
        "scored_transactions": scored,
        "flagged_at_budget": flagged,
        "review_budget_fraction": budget,
        "release_id": release_id,
        "model_run_id": run_id,
        "release_manifest_sha256": release_manifest_sha256(),
        "notes": [
            "Scores are a triage ranking over the committed snapshot, not a probability of criminality.",
            ("Every feature is computed only from facts at or before its own transaction's timestamp; "
             "activity outside the committed snapshot is unknown, not absent."),
            "No supervised model contributed to this ranking.",
            ("This model is dynamically fitted on the committed snapshot's own reference period at "
             "scoring time; there is no static model artifact. release_manifest_sha256 identifies "
             "the frozen procedure, not a loaded file -- see experiments/model_decision.md."),
        ],
        "graph": graph_coverage,
    }


def materialize_ml_findings(
    session: Session,
    *,
    evidence_root: Path,
    snapshot: Snapshot,
    graph: GraphSnapshot,
    layer_names: tuple[str, ...] = DEFAULT_LAYERS,
    budget: float = DEFAULT_BUDGET,
    reference_fraction: float = 0.7,
) -> MLFindingResult:
    """Score a committed snapshot and store the flagged transactions as findings.

    The reference distribution used for calibration is the snapshot's own earliest
    `reference_fraction` of transactions by time. That keeps the threshold a
    property of observed history rather than of the rows being scored — the same
    discipline the offline harness uses for its train split, applied where there
    are no splits.
    """
    from app.engine.graph.builder import _facts

    if session.scalar(
        select(FindingRecord.id)
        .where(FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == ML_RULE_VERSION)
        .limit(1)
    ):
        return MLFindingResult(0, 0, layer_names, budget, 0, model_run_id=model_run_id(
            snapshot_id=snapshot.id, budget=budget
        ))

    records = _facts(session, evidence_root, snapshot.id)
    facts = facts_from_records(records)
    if facts.transaction_count < 50:
        # Too little committed evidence for a reference distribution to mean
        # anything; store nothing rather than rank noise.
        return MLFindingResult(0, facts.transaction_count, layer_names, budget, 0, model_run_id=model_run_id(
            snapshot_id=snapshot.id, budget=budget
        ))

    cut = int(facts.transaction_count * reference_fraction)
    reference = np.zeros(facts.transaction_count, dtype=bool)
    reference[:max(cut, 1)] = True  # facts_from_records orders by time

    transaction_table = grains.build_transaction_table(facts)
    family_flags = layers.motif_family_flags(facts, transaction_table)

    available: dict[str, layers.LayerScore] = {}
    if any(name.startswith("A_") for name in layer_names):
        structure = layers.layer_a_structure(transaction_table, reference, stratified=False)
        structure.name = "A_global"
        available["A_global"] = structure
        available["A_structure"] = layers.layer_a_structure(transaction_table, reference, stratified=True)
    if "B_latency" in layer_names:
        horizon = int(facts.tx_time[reference].max())
        available["B_latency"] = layers.layer_b_latency(
            grains.build_latency_table(facts), facts, reference,
            reference_latency=grains.build_latency_table(facts, horizon=horizon),
        )
    if "C_history" in layer_names:
        motif_flag = np.maximum(family_flags["coinjoin_like"], family_flags["peel_step"])
        available["C_history"] = layers.layer_c_history(
            grains.build_history_table(facts, motif_flag=motif_flag), facts, reference
        )
    if "D_burst" in layer_names:
        available["D_burst"] = layers.layer_d_burst(facts, family_flags, reference=reference)
    if "E_graph" in layer_names:
        available["E_graph"] = layers.layer_e_graph(grains.build_graph_table(facts), reference)

    missing = [name for name in layer_names if name not in available]
    if missing:
        raise ValueError(f"requested layers are not available: {missing}")

    selected = {name: available[name].score for name in layer_names}
    if len(selected) == 1:
        score = next(iter(selected.values()))
        threshold = float(np.quantile(score[reference], 1.0 - budget))
    else:
        fused = stouffer_fuse(selected, reference, budget=budget)
        score, threshold = fused.score, fused.threshold

    flagged = np.flatnonzero(score >= threshold)
    order = flagged[np.argsort(-score[flagged], kind="stable")]
    refs_by_txid = _source_refs_by_txid(records)
    run_id = model_run_id(snapshot_id=snapshot.id, budget=budget)
    coverage = _coverage(
        graph, facts.transaction_count, int(flagged.size), budget, release_id=RELEASE_ID, run_id=run_id
    )

    out_starts, out_order = facts.outputs_of()
    attribution = available.get("A_global") or next(iter(available.values()))
    columns = attribution.attribution_columns

    written = 0
    for rank, transaction in enumerate(order, 1):
        txid = facts.txids[transaction]
        observed = datetime.fromtimestamp(int(facts.tx_time[transaction]), tz=UTC)
        window_start = datetime.fromtimestamp(
            int(facts.tx_time[transaction]) // WINDOW_SECONDS * WINDOW_SECONDS, tz=UTC
        )
        drivers: list[str] = []
        if attribution.attribution is not None:
            contributions = attribution.attribution[transaction]
            for index in np.argsort(-contributions)[:4]:
                drivers.append(f"{columns[index]}={float(contributions[index]):.2f}")

        feature_vector = {
            "feature_contract_version": ML_RULE_VERSION,
            "release_id": RELEASE_ID,
            "model_run_id": run_id,
            "layers": {name: float(available[name].score[transaction]) for name in layer_names},
            "fused_score": float(score[transaction]),
            "threshold": float(threshold),
            "top_feature_contributions": drivers,
            "shape_family": int(available["A_structure"].detail["shape_family"][transaction])
            if "A_structure" in available else None,
            "structure": {
                name: float(transaction_table.matrix[transaction, index])
                for index, name in enumerate(transaction_table.columns)
            },
        }
        outs = out_order[out_starts[transaction]:out_starts[transaction + 1]]
        addresses = sorted({
            facts.addresses[slot] for slot in facts.out_addr[outs] if slot >= 0
        })
        session.add(
            FindingRecord(
                case_id=snapshot.case_id,
                snapshot_id=snapshot.id,
                graph_snapshot_id=graph.id,
                entity_ref=f"transaction:{txid}",
                window_start=window_start,
                window_end=window_start + timedelta(seconds=WINDOW_SECONDS),
                rule_id="anomaly_stack_rank",
                rule_version=ML_RULE_VERSION,
                claim=(
                    f"Transaction {txid} ranks {rank} of {len(order)} in the committed snapshot's "
                    f"anomaly triage queue at a {budget:.1%} review budget; prioritize it for reviewer assessment."
                ),
                raw_score=float(score[transaction]),
                rank=rank,
                coverage=coverage,
                feature_vector=feature_vector,
                feature_vector_hash=_hash(feature_vector),
                explanations=[
                    "Ranked by " + " + ".join(layer_names)
                    + (f"; strongest feature contributions: {', '.join(drivers)}" if drivers else ""),
                    f"Observed at {observed.isoformat()}; {len(addresses)} output address(es) in this transaction.",
                ],
                benign_alternatives=[
                    ("Unusual transaction structure has ordinary explanations: exchange batching, "
                     "treasury consolidation, payroll runs, and wallet change handling all produce "
                     "shapes that rank highly here."),
                    ("A ranking is triage priority only. It is not a probability of criminality and "
                     "establishes no ownership, origin, or wrongdoing."),
                ],
                opposing_evidence=[
                    {
                        "kind": "coverage_limitation",
                        "statement": (
                            "This snapshot contains no ownership attribution and no independently "
                            "verified benign context; an anomaly score alone cannot establish either."
                        ),
                        "source_refs": [],
                    },
                    {
                        "kind": "method_limitation",
                        "statement": (
                            "Scores are calibrated against this snapshot's own earlier transactions. "
                            "A snapshot whose committed coverage is short or unrepresentative will "
                            "produce a correspondingly weak baseline."
                        ),
                        "source_refs": [],
                    },
                ],
                source_refs=refs_by_txid.get(txid, []),
                status="open",
            )
        )
        written += 1

    if written:
        append_event(
            session,
            case_id=snapshot.case_id,
            event_type="finding.updated",
            stage="findings_ready",
            payload={
                "snapshot_id": snapshot.id,
                "finding_count": written,
                "method": ML_RULE_VERSION,
                "release_id": RELEASE_ID,
                "model_run_id": run_id,
                "ml_enabled": True,
                "supervised": False,
                "layers": list(layer_names),
                "review_budget_fraction": budget,
            },
        )
    return MLFindingResult(
        written, facts.transaction_count, layer_names, budget, int(flagged.size), model_run_id=run_id
    )


def review_decision_labels(session: Session, *, case_id: str, facts) -> np.ndarray | None:
    """Labels from analyst review decisions — the only non-circular label source.

    Returns a per-transaction boolean of "a reviewer confirmed this lead", or
    `None` when too few decisions exist to train on. Training the supervised layer
    on `evaluation_truth.json` is a demo; training it on this is the real thing,
    because a reviewer's confirm/dismiss is independent of the rule that surfaced
    the finding.
    """
    from app.models import ReviewDecisionRecord

    rows = session.execute(
        select(FindingRecord.entity_ref, ReviewDecisionRecord.disposition, ReviewDecisionRecord.created_at)
        .join(ReviewDecisionRecord, ReviewDecisionRecord.finding_id == FindingRecord.id)
        .where(FindingRecord.case_id == case_id)
        .order_by(ReviewDecisionRecord.created_at)
    ).all()
    if not rows:
        return None
    labels = np.zeros(facts.transaction_count, dtype=bool)
    decided = 0
    for entity_ref, disposition, _ in rows:
        txid = str(entity_ref).removeprefix("transaction:")
        slot = facts.tx_index.get(txid, -1)
        if slot < 0:
            continue
        # Later decisions supersede earlier ones for the same transaction, which is
        # why the query is ordered by decision time.
        labels[slot] = disposition in {"confirmed", "escalated"}
        decided += 1
    if decided < 50 or labels.sum() < 10:
        return None
    return labels


def feature_record_count(session: Session, snapshot_id: str) -> int:
    """Phase 4 feature rows for this snapshot, for reconciliation in smoke tests."""
    return int(session.scalar(
        select(FeatureRecord.id).where(FeatureRecord.snapshot_id == snapshot_id).limit(1)
    ) is not None)
