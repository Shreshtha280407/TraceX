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
from scipy.stats import norm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import append_event
from app.ml import grains, layers
from app.ml.facts import attach_network_observations, facts_from_records
from app.ml.fusion import stouffer_fuse
from app.ml_release_constants import ML_RULE_VERSION
from app.models import FindingRecord, GraphSnapshot, Snapshot

WINDOW_SECONDS = 900
#: The combination the ablation selected on the fixture, using unsupervised layers
#: only. Recorded here rather than tuned at call time so a stored finding always
#: names the configuration that produced it. This is the Phase 5B frozen decision
#: (`experiments/model_decision.md`) and must not change without a new release ID.
DEFAULT_LAYERS = ("A_global", "D_burst")
#: Layer A is ranked by Isolation Forest alone from release v2 (ECOD stays as
#: the attribution); see experiments/model_decision_v2.md for the study.
A_SCORE_WITH = ("isolation_forest",)
DEFAULT_BUDGET = 0.01
FUSION_METHOD = "stouffer"
#: The scoring release identity. There is no static model artifact -- the stack
#: is refit on each snapshot's own reference period at scoring time -- so this ID
#: names the frozen *procedure* (layers, fusion, feature contract, training seed)
#: rather than a `.joblib` file. Bump it whenever any of `release_identity()`'s
#: fields change; the bump automatically changes `model_run_id` for every finding
#: scored afterward, so a reviewer can always tell which procedure produced a row.
RELEASE_ID = "anomaly-stack-v2"


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
        "layer_a_score": "+".join(A_SCORE_WITH) + " ranks; ECOD per-feature contributions as attribution",
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


def local_if_sensitivity(table, reference, transactions, *, limit=20):
    """Bounded descriptive perturbations of the exact global IF procedure.

    Replace one standardized feature with its reference median. This measures
    raw IF score response, NOT causal attribution, a valid alternative
    transaction, or a decomposition of the A+D fused rank.
    """
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler

    selected = np.asarray(transactions[:limit], dtype=np.int64)
    if not selected.size:
        return {}
    scaler = StandardScaler().fit(table.matrix[reference].astype(np.float64))
    fit = scaler.transform(table.matrix[reference].astype(np.float64))
    forest = IsolationForest(n_estimators=100, max_samples=min(256, fit.shape[0]),
                             random_state=layers.SEED, n_jobs=1).fit(fit)
    median = np.median(fit, axis=0)
    original = scaler.transform(table.matrix[selected].astype(np.float64))
    base = -forest.score_samples(original)
    changed = np.repeat(original, len(table.columns), axis=0)
    for row in range(selected.size):
        for column in range(len(table.columns)):
            changed[row * len(table.columns) + column, column] = median[column]
    delta = (base[:, None] + forest.score_samples(changed).reshape(selected.size, -1))
    return {int(transaction): {"method": "if-reference-median-perturbation-v1", "limit": limit,
            "scope": "raw global IF score only; not fused-score attribution, causation or valid counterfactual",
            "raw_if_score": float(base[row]), "score_decrease_by_feature": {
                column: float(delta[row, index]) for index, column in enumerate(table.columns)}}
            for row, transaction in enumerate(selected)}


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
            ("Structure and prior-activity context use observed snapshot facts; D uses full containing-bucket "
             "counts and is retrospective, including later observations in that bucket. Reference-period "
             "scores are in-sample. Activity outside the snapshot is unknown, not absent."),
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
    records: dict[str, list[dict]] | None = None,
    store=None,
) -> MLFindingResult:
    """Score a committed snapshot and store the flagged transactions as findings.

    The reference distribution used for calibration is the snapshot's own earliest
    `reference_fraction` of transactions by time. That keeps the threshold a
    property of observed history rather than of the rows being scored — the same
    discipline the offline harness uses for its train split, applied where there
    are no splits.
    """
    from app.engine.graph.builder import load_facts

    existing = session.scalar(
        select(FindingRecord)
        .where(FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == ML_RULE_VERSION)
        .limit(1)
    )
    if existing:
        from sqlalchemy import func

        flagged_count = session.scalar(select(func.count()).select_from(FindingRecord).where(
            FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == ML_RULE_VERSION)) or 0
        return MLFindingResult(0, int(existing.coverage.get("scored_transactions", 0)), layer_names,
                               budget, flagged_count, model_run_id=model_run_id(
            snapshot_id=snapshot.id, budget=budget
        ))

    if store is not None:
        # Bounded-memory path: arrays are streamed from the on-disk fact store
        # and source refs are looked up only for the flagged transactions.
        facts = store.ml_facts()
    else:
        if records is None:
            records = load_facts(session, evidence_root, snapshot.id)
        from app.resources import admit_global_allocation

        admit_global_allocation("ML", len(records.get("transactions", [])) * 1600 +
                                len(records.get("outputs", [])) * 384 + len(records.get("inputs", [])) * 80)
        facts = facts_from_records(records)
    # PS network layer: relay endpoint / ASN / reported country per transaction.
    attach_network_observations(
        facts, store.network_observations() if store is not None else records.get("network_observations", [])
    )
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
        structure = layers.layer_a_structure(transaction_table, reference, stratified=False, score_with=A_SCORE_WITH)
        structure.name = "A_global"
        available["A_global"] = structure
        if "A_structure" in layer_names:
            available["A_structure"] = layers.layer_a_structure(
                transaction_table, reference, stratified=True, score_with=A_SCORE_WITH)
        else:
            # v2 uses A_global+D; only the shape labels were ever exposed from
            # this second fit. Preserve them, omit eight unused IF/ECOD fits.
            available["A_structure"] = layers.LayerScore("A_structure", np.zeros(facts.transaction_count),
                detail={"shape_family": layers.shape_families(transaction_table, reference)})
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
    fused = None
    if len(selected) == 1:
        score = next(iter(selected.values()))
        threshold = float(np.quantile(score[reference], 1.0 - budget))
    else:
        fused = stouffer_fuse(selected, reference, budget=budget)
        score, threshold = fused.score, fused.threshold

    flagged = np.flatnonzero(score >= threshold)
    order = flagged[np.argsort(-score[flagged], kind="stable")]
    if store is not None:
        refs_by_txid = store.source_refs_by_txid([facts.txids[transaction] for transaction in order])
    else:
        refs_by_txid = _source_refs_by_txid(records)
    run_id = model_run_id(snapshot_id=snapshot.id, budget=budget)
    coverage = _coverage(
        graph, facts.transaction_count, int(flagged.size), budget, release_id=RELEASE_ID, run_id=run_id
    )

    # Network context corroborates a flagged transaction; it never re-ranks the
    # frozen release (the ablation found network features do not improve the
    # ranking on the fixture), but every ML finding now states it.
    network_table = grains.build_network_context(facts)
    network_score = None
    if network_table is not None:
        normalised = [
            layers._rank_normalise(network_table.matrix[:, column], reference)
            for column in range(network_table.matrix.shape[1])
        ]
        network_score = np.mean(normalised, axis=0)

    out_starts, out_order = facts.outputs_of()
    attribution = available.get("A_global") or next(iter(available.values()))
    columns = attribution.attribution_columns
    sensitivity = local_if_sensitivity(transaction_table, reference, order) if layer_names == DEFAULT_LAYERS else {}

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
            "feature_contract_sha256": hashlib.sha256(json.dumps({"version": ML_RULE_VERSION,
                "structure_columns": list(transaction_table.columns), "layers": list(layer_names)},
                sort_keys=True).encode()).hexdigest(),
            "release_manifest_sha256": release_manifest_sha256(),
            "release_id": RELEASE_ID,
            "model_run_id": run_id,
            "layers": {name: float(available[name].score[transaction]) for name in layer_names},
            "fusion_components": {
                "version": "stouffer-explanation-v1", "scope": "separate layer inputs, not criminality probabilities",
                "components": {name: {"raw_score": float(available[name].score[transaction]),
                    "reference_tail_p": float(fused.p_values[name][transaction]), "weight": fused.weights[name],
                    "normalized_z_contribution": float(norm.isf(fused.p_values[name][transaction]) * fused.weights[name] /
                        np.sqrt(sum(weight ** 2 for weight in fused.weights.values())))}
                    for name in layer_names} if fused is not None else {},
                "scoring_scope": "v2 burst is retrospective, not a causal prior-bucket detector",
            },
            "fused_score": float(score[transaction]),
            "threshold": float(threshold),
            "top_feature_contributions": drivers,
            "explanation_method": "ECOD feature-tail context; not attribution of Isolation Forest or fused score",
            "explanation_version": "feature-tail-context-v1",
            "local_if_sensitivity": sensitivity.get(int(transaction), {"status": "not_computed", "reason": "bounded to first 20 ranked findings or unsupported layer configuration"}),
            "fitting_scope": "reference_period_in_sample" if reference[transaction] else "forward_scored",
            "reference_fraction": reference_fraction,
            "reference_cutoff_epoch": int(facts.tx_time[reference].max()),
            "scoring_scope": "snapshot retrospective triage; D uses completed containing-bucket counts",
            "shape_family": int(available["A_structure"].detail["shape_family"][transaction])
            if "A_structure" in available else None,
            "structure": {
                name: float(transaction_table.matrix[transaction, index])
                for index, name in enumerate(transaction_table.columns)
            },
        }
        network_context = None
        network_lines: list[str] = []
        if network_table is not None and facts.tx_src_ip[transaction] >= 0:
            endpoint = facts.src_ips[facts.tx_src_ip[transaction]]
            asn = facts.asns[facts.tx_asn[transaction]] if facts.tx_asn[transaction] >= 0 else None
            country = facts.countries[facts.tx_country[transaction]] if facts.tx_country[transaction] >= 0 else None
            recent_endpoint = round(float(np.expm1(network_table.matrix[transaction, 0])))
            recent_asn = round(float(np.expm1(network_table.matrix[transaction, 1])))
            percentile = float(network_score[transaction])
            network_context = {
                "src_ip": endpoint, "asn": asn, "reported_country": country,
                "endpoint_tx_previous_hour": recent_endpoint, "asn_tx_previous_hour": recent_asn,
                "country_rarity": float(network_table.matrix[transaction, 2]),
                "score": percentile, "corroborates": percentile >= 0.95,
            }
            network_lines.append(
                f"Network context: first relayed by {endpoint}"
                + (f" ({asn}" + (f", reported {country}" if country else "") + ")" if asn else "")
                + f"; that endpoint relayed {recent_endpoint} other transaction(s) in the preceding hour. "
                + f"Network-context percentile {percentile:.0%}"
                + (" -- unusual relay activity corroborates this lead." if percentile >= 0.95 else ".")
            )
        feature_vector["network_context"] = network_context
        outs = out_order[out_starts[transaction]:out_starts[transaction + 1]]
        addresses = sorted({
            facts.addresses[slot] for slot in facts.out_addr[outs] if slot >= 0
        })
        session.add(
            FindingRecord(
                case_id=snapshot.case_id,
                snapshot_id=snapshot.id,
                graph_snapshot_id=graph.id,
                entity_ref=f"tx:{txid}",
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
                    + (f"; ECOD descriptive feature-tail context (not score attribution): {', '.join(drivers)}" if drivers else ""),
                    f"Observed at {observed.isoformat()}; {len(addresses)} output address(es) in this transaction.",
                    *network_lines,
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
                            "Ownership attribution and independently verified benign context are not "
                            "established by the checked scoring coverage; an anomaly score alone cannot establish either."
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
    """Disabled compatibility hook; use the masked analyst-label contract."""
    # A bare boolean array cannot represent unknown labels, cutoff or sampling
    # bias. Keep the legacy hook disabled; analyst_labels exposes an explicit
    # masked/provenanced contract for a separately validated future trainer.
    return None


def feature_record_count(session: Session, snapshot_id: str) -> int:
    """Phase 4 feature rows for this snapshot, for reconciliation in smoke tests."""
    from app.engine.feature_store import has_features

    return int(has_features(session, snapshot_id))
