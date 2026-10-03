"""Eligible frozen candidate integration with ordinary evidence and review queue."""
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import func, select

from app.ml import candidate
from app.ml.facts import attach_network_observations, facts_from_records
from app.ml.findings import MLFindingResult, _hash, _source_refs_by_txid
from app.models import FindingRecord


def materialize(session, *, settings, snapshot, graph, store, records, manifest, models, budget):
    release = manifest["release_id"]
    existing = session.scalar(select(FindingRecord).where(FindingRecord.snapshot_id == snapshot.id,
        FindingRecord.rule_version == release).limit(1))
    if existing:
        count = session.scalar(select(func.count()).select_from(FindingRecord).where(
            FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == release))
        return MLFindingResult(0, existing.coverage["scored_transactions"], ("frozen_candidate",), budget,
                               count, release_id=release, model_run_id=existing.feature_vector["model_run_id"])
    extra = manifest["expanded_artifact_bytes"] * 3
    if store is not None:
        facts = store.ml_facts(extra_model_bytes=extra)
    else:
        from app.resources import admit_global_allocation
        admit_global_allocation("candidate ML joint fitted weights/features", len(records.get("transactions", [])) * 1600 +
            len(records.get("outputs", [])) * 384 + len(records.get("inputs", [])) * 80 + extra)
        facts = facts_from_records(records)
    attach_network_observations(facts, store.network_observations() if store is not None else records.get("network_observations", []))
    if facts.transaction_count < 50:
        return MLFindingResult(0, facts.transaction_count, ("frozen_candidate",), budget, 0, release_id=release)
    matrix = candidate.features(facts)
    scores = candidate.infer(models, matrix)
    # Queue policy is a versioned max of task-pattern probabilities, NOT risk.
    score = np.maximum(scores["motif"], scores["surge"])
    capacity = max(1, int(np.ceil(len(score) * budget)))
    order = sorted(range(len(score)), key=lambda i: (-float(score[i]), facts.txids[i]))[:capacity]
    refs = store.source_refs_by_txid([facts.txids[i] for i in order]) if store else _source_refs_by_txid(records)
    run_id = _hash({"snapshot": snapshot.id, "release": release, "manifest": settings.candidate_manifest_sha256, "budget": budget})[:24]
    explanation = candidate.sensitivity(models, matrix, order)
    coverage = {"scored_transactions": len(score), "graph": graph.coverage,
                "complete": bool((graph.coverage or {}).get("spend_lineage_complete")),
                "release_id": release, "model_run_id": run_id,
                "notes": ["Frozen transfer inference: no upload labels or adaptation used.",
                          "Per-task calibration applicability is restricted to the approved labelled distribution; not criminality."]}
    for rank, index in enumerate(order, 1):
        window = datetime.fromtimestamp(int(facts.tx_time[index]) // 900 * 900, UTC)
        vector = {"feature_contract_version": candidate.CONTRACT, "feature_contract_sha256": candidate.CONTRACT_HASH,
                  "release_id": release, "release_manifest_sha256": settings.candidate_manifest_sha256,
                  "model_run_id": run_id, "artifact_sha256": manifest["payload_sha256"],
                  "eligibility": manifest["eligibility"], "promotion": manifest.get("promotion"),
                  "task_scores": {task: float(values[index]) for task, values in scores.items()},
                  "queue_policy": "max-motif-surge-stable-txid-v1; not calibrated cross-family risk",
                  "structure": dict(zip(candidate.COLUMNS, map(float, matrix[index]), strict=True)),
                  "comparison_baselines": {"per_feature_training_median": manifest["provenance"].get("training_feature_baselines"),
                      "scope": "frozen permitted training population; not a universal benign baseline"},
                  "calibration_provenance": {"meaning": manifest["calibration"], "provenance": manifest["provenance"]},
                  "scoring_scope": "frozen causal features, prior completed bucket; no inference adaptation",
                  "explanation_method": "column-zero score sensitivity, not causal attribution or a valid counterfactual",
                  "sensitivity": explanation.get(index, {"status": "not_computed", "limit": 20})}
        session.add(FindingRecord(case_id=snapshot.case_id, snapshot_id=snapshot.id, graph_snapshot_id=graph.id,
            entity_ref="tx:" + facts.txids[index], window_start=window, window_end=window + timedelta(seconds=900),
            rule_id="candidate_pattern_triage", rule_version=release,
            claim=f"Transaction {facts.txids[index]} is a frozen candidate pattern-triage item, rank {rank}; not a wrongdoing verdict.",
            raw_score=float(score[index]), rank=rank, coverage=coverage, feature_vector=vector,
            feature_vector_hash=_hash(vector), explanations=["Fitted weights and calibration were frozen before this case was observed."],
            benign_alternatives=["Payroll, batching, exchange activity and privacy-preserving collaboration can match these structures."],
            opposing_evidence=[{"kind": "method_limitation", "statement": "Pattern probability is not criminality; arbitrary unlabelled uploads have no measured AP.", "source_refs": []}],
            source_refs=refs.get(facts.txids[index], []), status="open"))
    return MLFindingResult(len(order), len(score), ("frozen_candidate",), budget, len(order), release_id=release, model_run_id=run_id)


def score_or_fallback(session, *, settings, snapshot, graph, budget, records=None, store=None):
    """Never silently mix scorers on retry or change reviewed snapshot rows."""
    from app.ml.findings import materialize_ml_findings
    from app.models import AnalysisStage, Case, ImportJob

    prior = session.scalar(select(FindingRecord).where(FindingRecord.snapshot_id == snapshot.id,
        FindingRecord.rule_version.like("anomaly-stack-%")).limit(1))
    decision = {"status": "fallback", "reason": "v2 default; no candidate configured"}
    case = session.get(Case, snapshot.case_id)
    if prior:
        count = session.scalar(select(func.count()).select_from(FindingRecord).where(
            FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == prior.rule_version))
        result = MLFindingResult(0, prior.coverage.get("scored_transactions", 0), ("retained",), budget,
                                 count, release_id=prior.rule_version, model_run_id=prior.feature_vector.get("model_run_id"))
        return result, {"status": "retained", "reason": "immutable prior successful scorer retained; reviews/pins unchanged"}
    successful = session.scalar(select(AnalysisStage).join(ImportJob, AnalysisStage.job_id == ImportJob.id)
        .where(ImportJob.snapshot_id == snapshot.id, AnalysisStage.name == "ml_scoring",
               AnalysisStage.status.in_(["complete", "written", "no_rows_flagged"]))
        .order_by(AnalysisStage.attempt.desc()).limit(1))
    if successful and successful.details.get("release_id"):
        # Zero flagged rows still pin a successful procedure; do not select a
        # different scorer because the old success produced no FindingRecord.
        result = MLFindingResult(0, successful.details.get("scored_transactions", 0), ("retained",), budget,
            successful.details.get("threshold_candidates", 0), release_id=successful.details["release_id"],
            model_run_id=successful.details.get("model_run_id"))
        return result, {"status": "retained", "reason": "prior successful zero-row scoring procedure retained"}
    if getattr(settings, "candidate_directory", None) and case.scoring_mode != "unsupervised":
        try:
            manifest, models = candidate.load(settings.candidate_directory, settings.candidate_manifest_sha256)
            allowed, reason = candidate.eligibility(manifest, case)
            decision = {"status": "eligible" if allowed else "fallback", "reason": reason,
                        "candidate_release": manifest["release_id"], "eligibility": manifest["eligibility"]}
            if allowed:
                # Savepoint ensures partial candidate writes cannot leak into fallback.
                with session.begin_nested():
                    result = materialize(session, settings=settings, snapshot=snapshot, graph=graph,
                        store=store, records=records, manifest=manifest, models=models, budget=budget)
                return result, decision
        except Exception as error:  # noqa: BLE001 - explicit, eligible unsupervised fallback
            decision = {"status": "fallback", "reason": f"candidate rejected: {type(error).__name__}: {str(error)[:500]}"}
    return materialize_ml_findings(session, evidence_root=settings.evidence_root, snapshot=snapshot,
        graph=graph, budget=budget, records=records, store=store), decision
