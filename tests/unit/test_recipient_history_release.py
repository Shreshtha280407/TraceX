"""Small recipient-history and release gates; no large or old final datasets."""
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.engine.confidence import feature_contract
from app.ml import candidate, promotion
from app.ml.facts import facts_from_records
from app.ml.recipient_history import opposing_observations, recipient_history
from scripts.candidate_lifecycle import queue_comparison


def history_facts():
    stamps = [0, 0, 10, 10, 30]
    records = {"transactions": [], "outputs": [], "inputs": []}
    for i, second in enumerate(stamps):
        txid = f"{i:064x}"
        records["transactions"].append({"txid": txid, "block_time": f"2026-01-01T00:00:{second:02d}Z", "fee_sats": 0})
        records["outputs"].append({"txid": txid, "vout": 0, "address": "recurrent", "amount_sats": 1000})
    records["outputs"].extend([{**records["outputs"][0], "vout": 1}, {**records["outputs"][0], "vout": 2, "address": None}])
    return facts_from_records(records), records


def test_prior_recipients_exclude_equal_time_peers_count_transactions_not_outputs():
    facts, _ = history_facts()
    matrix, witnesses = recipient_history(facts, selected=[4])
    assert matrix[0].tolist() == pytest.approx([2 / 3, 0, 0, 0])
    assert matrix[1].tolist() == [1, 0, 0, 0]
    assert matrix[2].tolist() == [1, 1, 2, 0]
    assert matrix[3].tolist() == [1, 1, 2, 0]
    assert matrix[4].tolist() == [1, 1, 4, 10]
    assert witnesses[4][0]["witness_txids"] == [facts.txids[0], facts.txids[3]]
    absent_matrix, bounded_witnesses = recipient_history(facts, selected=[4], feature_values=False)
    assert absent_matrix is None and bounded_witnesses == witnesses


def test_candidate_workspace_admission_uses_metadata_only_and_preserves_base_estimate():
    from app.ml.recipient_history import working_set_bytes
    from app.resources import global_stage_estimates
    counts = {"transactions": 3_050_000, "outputs": 8_050_000, "inputs": 4_550_000}
    extra = working_set_bytes(counts["transactions"], counts["outputs"]) + (64 << 20) * 3
    baseline = global_stage_estimates(counts, native_bytes=1 << 30)
    candidate_estimate = global_stage_estimates(counts, native_bytes=1 << 30, candidate_extra_bytes=extra)
    assert candidate_estimate["ml_joint_bytes"] == baseline["ml_joint_bytes"] + extra
    assert candidate_estimate["candidate_extra_bytes"] == extra
    assert candidate_estimate["analytics_joint_bytes"] == baseline["analytics_joint_bytes"]


def test_context_is_cutoff_and_fragment_order_invariant_and_missing_not_benign():
    facts, records = history_facts()
    small = facts_from_records({key: [row for row in rows if row["txid"] < f"{4:064x}"] for key, rows in records.items()})
    np.testing.assert_array_equal(candidate.features(facts)[:4], candidate.features(small))
    reversed_facts = facts_from_records({key: list(reversed(rows)) for key, rows in records.items()})
    np.testing.assert_array_equal(candidate.features(facts), candidate.features(reversed_facts))
    for row in records["outputs"]:
        row["address"] = None
    missing, _ = recipient_history(facts_from_records(records), selected=[4])
    assert np.all(missing == 0)  # Includes explicit zero address-coverage, not a benign flag.


def test_observed_opposition_requires_both_source_witnesses_and_qualifies_novelty():
    facts, _ = history_facts()
    _, witnesses = recipient_history(facts, selected=[4])
    refs = {facts.txids[0]: [{"evidence_id": "case-source", "locator": "/0", "source_sha256": "pinned"}],
            facts.txids[3]: [{"evidence_id": "case-source", "locator": "/3", "source_sha256": "pinned"}]}
    rows = opposing_observations(witnesses[4], refs)
    assert len(rows) == 1 and len(rows[0]["source_refs"]) == 2
    assert "does not refute the structural match" in rows[0]["statement"]
    assert "ownership or innocence" in rows[0]["statement"]
    del refs[facts.txids[3]]
    assert opposing_observations(witnesses[4], refs) == []


def test_legacy_fitted_weights_features_queue_and_explanations_remain_compatible(tmp_path):
    rng = np.random.default_rng(42)
    matrix = rng.normal(size=(64, len(candidate.LEGACY_COLUMNS))).astype(np.float32)
    labels = {task: matrix[:, i] > 0 for i, task in enumerate(candidate.TASKS)}
    models = candidate.train(matrix, labels, matrix, labels, contract=candidate.LEGACY_CONTRACT)
    manifest = candidate.save(tmp_path / "legacy", models, name="hist", provenance={"test_only": True}, contract=candidate.LEGACY_CONTRACT)
    loaded_manifest, loaded = candidate.load(tmp_path / "legacy", candidate.sha(tmp_path / "legacy/manifest.json"))
    assert loaded_manifest == manifest and manifest["release_id"].startswith("anomaly-stack-candidate-v1-")
    old = candidate.infer(models, matrix)
    for task, value in candidate.infer(loaded, matrix).items():
        np.testing.assert_array_equal(value, old[task])
    np.testing.assert_array_equal(candidate.queue_priority(old, contract=candidate.LEGACY_CONTRACT), np.maximum(old["motif"], old["surge"]))
    assert candidate.sensitivity(loaded, matrix, [0], columns=candidate.LEGACY_COLUMNS)
    facts, _ = history_facts()
    np.testing.assert_array_equal(candidate.features(facts)[:, :len(candidate.LEGACY_COLUMNS)], candidate.features(facts, contract=candidate.LEGACY_CONTRACT))
    assert feature_contract(manifest["release_id"]) == candidate.LEGACY_CONTRACT
    old_approved = {**manifest, "eligibility": "validated_candidate", "promotion": {"representative_labels": True, "domain": "old-approved"}}
    assert candidate.eligibility(old_approved, SimpleNamespace(scoring_mode="validated_candidate", candidate_domain="old-approved"))[0]
    assert not candidate.eligibility(old_approved, SimpleNamespace(scoring_mode="auto_eligible", candidate_domain="old-approved"))[0]


def test_context_priority_does_not_hide_structural_match_scores():
    scores = {"motif": np.array([.99, .97]), "surge": np.array([.95, .92]), "discrimination": np.array([.1, .9])}
    assert candidate.queue_priority(scores).tolist() == [.1, .9]
    assert scores["motif"].tolist() == [.99, .97]
    assert feature_contract("anomaly-stack-candidate-v2-test") == candidate.CONTRACT


def test_queue_comparison_uses_actual_priority_same_capacity_and_missing_controls():
    scores = {"motif": np.array([.99, .9, .8, .7]), "surge": np.zeros(4), "discrimination": np.array([.1, .9, .8, .2])}
    baseline = {task: scores["motif"] for task in candidate.TASKS}
    labels = {task: np.array([False, True, True, False]) for task in candidate.TASKS}
    families, benign = ["benign_merchant", "pattern", "pattern", "benign_batch"], np.array([True, False, False, True])
    compared = queue_comparison(scores, baseline, labels, families, benign, 2)
    assert compared["status"] == "EVALUATED"
    assert compared["candidate_policy"] == candidate.QUEUE_POLICIES[candidate.CONTRACT]
    assert compared["candidate"]["precision"] == 1 and compared["baseline"]["precision"] == .5
    assert compared["delta"]["merchant_false_positive_reduction"] == 1
    assert queue_comparison(scores, baseline, labels, families, benign, 100)["status"] == "NOT EVALUABLE"
    assert queue_comparison(scores, baseline, labels, ["unknown"] * 4, benign, 2)["status"] == "NOT EVALUABLE"
    partial = queue_comparison(scores, baseline, labels, families, benign, 2,
        population_scope={"eligible_transactions": 20, "labelled_transactions": 4, "population_scope": "all time-eligible canonical transactions"})
    assert partial["status"] == "NOT EVALUABLE" and "Unlabelled transactions are not negatives" in partial["reasons"][0]
    unavailable = queue_comparison(scores, baseline, labels, families, benign, 2,
        population_scope={"eligible_transactions": 4, "labelled_transactions": 4, "population_scope": "all time-eligible canonical transactions",
            "candidate_available_findings": 1, "baseline_available_findings": 4, "finding_budget_fraction": .01})
    assert unavailable["status"] == "NOT EVALUABLE" and "materialized product queue" in unavailable["reasons"][0]


def promotable_report(manifest, fraction=1.):
    from app.engine.investigations import PROCEDURE_SHA256, QUEUE_POLICY
    provenance = manifest.setdefault("provenance", {})
    provenance.setdefault("protocol_sha256", "test-protocol")
    provenance.update(registered_final_ids=["test-final-id"], grouping_sha256=PROCEDURE_SHA256,
                      review_budget=100, finding_budget_fraction=fraction)
    return {"dataset": "metadata-only-validation", "model": manifest,
        "final_id": "test-final-id", "source_sha256": "test-source-fingerprint", "grouping_sha256": PROCEDURE_SHA256,
        "group_quality": {"status": "EVALUATED", "grouping_sha256": PROCEDURE_SHA256, "capacity": 100, "p_at_100": .95,
                          "final_id": "test-final-id", "source_sha256": "test-source-fingerprint", "protocol_sha256": provenance["protocol_sha256"],
                          "release_id": manifest["release_id"], "queue_policy": QUEUE_POLICY},
        "procedure": "frozen fitted-weight transfer; test only", "truth_sha256": "test-truth", "protocol_sha256": provenance["protocol_sha256"],
        "results": {task: {"ap": .9, "p_at_100": .95, "population": 200, "review_budget": 100} for task in candidate.TASKS},
        "comparison": {task: {"ap_delta": .05} for task in candidate.TASKS},
        "queue_comparison": {"status": "EVALUATED", "population_scope": "all time-eligible canonical transactions", "eligible_transactions": 200, "labelled_transactions": 200,
            "finding_budget_fraction": fraction, "candidate_available_findings": int(200 * fraction), "baseline_available_findings": 200,
            "candidate_policy": manifest["queue_policy"], "review_budget": 100, "merchant_controls": 30,
            "candidate": {"precision": .95, "recall": .5, "merchant_false_positives": 2, "benign_in_queue": 5},
            "baseline": {"precision": .9, "recall": .45, "merchant_false_positives": 6, "benign_in_queue": 10}}}


def frozen_metadata(manifest):
    """Fabricated METADATA UNIT TEST only; never product quality evidence."""
    return {"protocol_sha256": manifest["provenance"]["protocol_sha256"], "release_id": manifest["release_id"],
            "manifest_sha256": hashlib.sha256(json.dumps(manifest, indent=2, sort_keys=True).encode()).hexdigest(),
            "final_registry": {"test-final-id": {"source_sha256": "test-source-fingerprint", "truth_sha256": "test-truth"}},
            "review_budget": manifest["provenance"]["review_budget"], "finding_budget_fraction": manifest["provenance"]["finding_budget_fraction"]}


@pytest.mark.parametrize("failure", ["missing_ap", "wrong_weights", "wrong_policy", "precision_regression", "recall_regression", "merchant_regression", "no_reduction", "missing_controls", "missing_merchant_metric", "missing_capacity", "partial_truth", "candidate_capacity", "baseline_capacity", "unknown_fraction"])
def test_production_gate_blocks_missing_metrics_and_regressions(failure):
    manifest = {"release_id": "test-v2", "payload_sha256": "weights", "feature_sha256": "features", "queue_policy": candidate.QUEUE_POLICIES[candidate.CONTRACT]}
    report = promotable_report(manifest.copy())
    queue = report["queue_comparison"]
    if failure == "missing_ap": report["results"]["surge"]["ap"] = None
    elif failure == "wrong_weights": report["model"]["payload_sha256"] = "other"
    elif failure == "wrong_policy": queue["candidate_policy"] = "legacy"
    elif failure == "precision_regression": queue["candidate"]["precision"] = .8
    elif failure == "recall_regression": queue["candidate"]["recall"] = .4
    elif failure == "merchant_regression": queue["candidate"]["merchant_false_positives"] = 7
    elif failure == "no_reduction": queue["candidate"]["merchant_false_positives"] = 6
    elif failure == "missing_controls": queue["merchant_controls"] = 0
    elif failure == "missing_merchant_metric": del queue["candidate"]["merchant_false_positives"]
    elif failure == "missing_capacity": del queue["review_budget"]
    elif failure == "partial_truth": queue["labelled_transactions"] = 100
    elif failure == "candidate_capacity": queue["candidate_available_findings"] = 2
    elif failure == "baseline_capacity": queue["baseline_available_findings"] = 20
    elif failure == "unknown_fraction": del queue["finding_budget_fraction"]
    proof = promotion.assess(manifest, [report])
    assert proof["status"] == "BLOCKED" and proof["errors"]


def test_production_gate_requires_quality_plus_independent_applicability():
    manifest = {"release_id": "test-v2", "payload_sha256": "weights", "feature_sha256": "features", "queue_policy": candidate.QUEUE_POLICIES[candidate.CONTRACT],
        "feature_contract": candidate.CONTRACT, "eligibility": "validated_candidate"}
    report = promotable_report(manifest)
    proof = promotion.assess(manifest, [report], frozen=frozen_metadata(manifest))
    assert proof["status"] == "PASSED"
    proof["reports_sha256"] = ["test-metadata-only"]
    manifest["provenance"]["parent_manifest_sha256"] = proof["frozen_manifest_sha256"]
    manifest["promotion"] = {"representative_labels": True, "domain": "approved-test-domain", "quality_validation": proof}
    case = SimpleNamespace(scoring_mode="validated_candidate", candidate_domain="approved-test-domain")
    assert candidate.eligibility(manifest, case)[0]
    assert not candidate.eligibility(manifest, case, finding_budget=.01)[0]
    proof["policy"] = "unregistered-gate"
    assert not candidate.eligibility(manifest, case)[0]
    proof["policy"] = promotion.POLICY
    case.scoring_mode = "auto_eligible"
    assert candidate.eligibility(manifest, case)[0]
    case.candidate_domain = "arbitrary-upload"
    assert not candidate.eligibility(manifest, case)[0]
    assert promotion.assess(manifest, [])["status"] == "BLOCKED"


def test_empty_evaluation_labels_are_explicitly_unevaluable_not_key_errors():
    from scripts.candidate_lifecycle import metrics
    values = {task: np.array([]) for task in candidate.TASKS}
    result = metrics(values, values, budget=20)
    assert all(row["status"] == "no labelled observations" and row["ap"] is None and row["p_at_100"] is None for row in result.values())


def demo_population(role, count=256):
    """Predeclared toy review-interest target, NOT representative merchant truth."""
    records = {"transactions": [], "outputs": [], "inputs": []}
    flags = {}
    prefix = hashlib.sha256(role.encode()).hexdigest()[:32]
    for i in range(count):
        txid = prefix + f"{i:032x}"
        kind = i % 3
        records["transactions"].append({"txid": txid, "block_time": f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00Z", "fee_sats": 0})
        for j in range(3 if kind != 2 else 1):
            address = f"{role}-recurrent-{j}" if kind == 0 else f"{role}-{i}-{j}"
            records["outputs"].append({"txid": txid, "vout": j, "address": address, "amount_sats": 1000, "script_type": "p2wpkh"})
        if kind != 2:
            records["inputs"].extend({"txid": txid, "vin": j, "prev_txid": None, "prev_vout": None} for j in range(3))
        flags[txid] = kind
    facts = facts_from_records(records)
    matrix = candidate.features(facts)
    labels = {"motif": np.array([flags[txid] != 2 for txid in facts.txids]),
        "surge": matrix[:, candidate.COLUMNS.index("prior_completed_equal_output_count")] >= 2,
        "discrimination": np.array([flags[txid] == 1 for txid in facts.txids])}
    return matrix, labels, np.array([flags[txid] == 0 for txid in facts.txids])


def test_frozen_tiny_demo_preserves_benign_structural_scores_but_improves_review_queue(tmp_path):
    training, labels, _ = demo_population("train")
    calibration, clabels, _ = demo_population("calibration")
    final, flabels, merchant = demo_population("fresh-final")
    models = candidate.train(training, labels, calibration, clabels)
    path = tmp_path / "demo-only"
    manifest = candidate.save(path, models, name="hist", provenance={"scope": "toy recurrence intervention only; NOT representative real-case quality"})
    _, frozen = candidate.load(path, candidate.sha(path / "manifest.json"))
    scores = candidate.infer(frozen, final)  # No final labels enter inference.
    priority = candidate.queue_priority(scores)
    legacy = candidate.queue_priority(scores, contract=candidate.LEGACY_CONTRACT)
    new_order, old_order = np.argsort(-priority, kind="stable")[:20], np.argsort(-legacy, kind="stable")[:20]
    assert flabels["discrimination"][new_order].mean() > flabels["discrimination"][old_order].mean()
    assert merchant[new_order].sum() < merchant[old_order].sum()
    assert scores["motif"][merchant].mean() > .8  # Correct benign structural matches are not hidden.
    assert manifest["eligibility"] == "synthetic_demo"
    assert not candidate.eligibility(manifest, SimpleNamespace(scoring_mode="unsupervised"))[0]
    result = {"status": "CONTROLLED_UNIT_ONLY", "scope": "Controlled unit intervention, 3 isomorphic 256-TX role fixtures; NOT independent generalization, NOT default-v2 merchant precision or production acceptance",
        "feature_contract": candidate.CONTRACT, "model": {"release_id": manifest["release_id"], "eligibility": "synthetic_demo", "payload_sha256": manifest["payload_sha256"]},
        "manifest_sha256": candidate.sha(path / "manifest.json"),
        "results": {"review_capacity": 20, "merchant_controls": int(merchant.sum()),
        "candidate_v2": {"precision": float(flabels["discrimination"][new_order].mean()), "merchant_entries": int(merchant[new_order].sum())},
        "same_weights_legacy_queue": {"precision": float(flabels["discrimination"][old_order].mean()), "merchant_entries": int(merchant[old_order].sum())},
        "preserved_merchant_motif_mean": float(scores["motif"][merchant].mean())}}
    (tmp_path / "controlled-queue-result.json").write_text(json.dumps(result, indent=2))
    print("CONTROLLED_QUEUE_RESULT=" + json.dumps(result))


def test_new_cases_default_to_auto_approved_but_never_auto_enable_demo():
    from app.api.routes import CaseCreate
    body = CaseCreate(name="Real upload")
    assert body.scoring_mode == "auto_eligible" and body.synthetic is False and body.candidate_domain is None
    manifest = {"eligibility": "synthetic_demo", "feature_contract": candidate.CONTRACT}
    case = SimpleNamespace(scoring_mode="auto_eligible", synthetic=True, candidate_domain="synthetic-demo")
    assert not candidate.eligibility(manifest, case)[0]


def test_promotion_cli_preserves_exact_frozen_payload_and_blocked_diagnostics(tmp_path):
    from scripts import candidate_lifecycle as lifecycle
    matrix, labels, _ = demo_population("promotion-unit", count=128)
    models = candidate.train(matrix, labels, matrix, labels)
    source, approved = tmp_path / "source", tmp_path / "approved"
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({"TEST_METADATA_ONLY": True}))
    manifest = candidate.save(source, models, name="hist", provenance={"test_only": True, "protocol_sha256": candidate.sha(protocol)})
    truth_report, approval = tmp_path / "metadata-only.json", tmp_path / "approval.json"
    truth_report.write_text(json.dumps(promotable_report(manifest)))
    (source / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    protocol.with_name(protocol.name + ".frozen.json").write_text(json.dumps(frozen_metadata(manifest)))
    approval.write_text(json.dumps({"representative_labels": True, "domain": "metadata-test-only", "approved_by": "unit-test",
        "decision_reason": "TEST ONLY, not a real promotion", "label_provenance": "unit metadata", "validation_reports": [str(truth_report)], "limitations": ["not production evidence"]}))
    flags = ["promote", "--artifact", str(source), "--manifest-sha256", candidate.sha(source / "manifest.json"),
        "--approval", str(approval), "--quality-result", str(truth_report), "--protocol", str(protocol)]
    assert lifecycle.main([*flags, "--output", str(approved)]) == 0
    new_manifest, _ = candidate.load(approved, candidate.sha(approved / "manifest.json"))
    assert (approved / "weights.joblib").read_bytes() == (source / "weights.joblib").read_bytes()
    assert new_manifest["release_id"] == manifest["release_id"]
    assert candidate.eligibility(new_manifest, SimpleNamespace(scoring_mode="auto_eligible", candidate_domain="metadata-test-only"))[0]
    blocked = tmp_path / "blocked"
    report = promotable_report(manifest)
    report["queue_comparison"]["candidate"]["precision"] = None
    truth_report.write_text(json.dumps(report))
    with pytest.raises(SystemExit):
        lifecycle.main([*flags, "--output", str(blocked)])
    assert not blocked.exists()
    decision = json.loads((tmp_path / "blocked.promotion-decision.json").read_text())
    assert decision["status"] == "BLOCKED" and decision["errors"]


def test_real_worker_auto_approved_routing_retains_structures_and_source_opposition(tmp_path_factory, tmp_path, monkeypatch):
    import test_ml_pipeline_integration as integration
    from sqlalchemy import select

    from app.api.analysis_routes import review_queue
    from app.engine.evidence import structured_evidence
    from app.models import FindingRecord, User
    matrix, labels, _ = demo_population("integration-model", count=128)
    models = candidate.train(matrix, labels, matrix, labels)
    source = tmp_path / "source"
    manifest = candidate.save(source, models, name="hist", provenance={"test_only": True})
    report = promotable_report(manifest, fraction=.5)
    (source / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    proof = promotion.assess(manifest, [report], frozen=frozen_metadata(manifest))
    proof["reports_sha256"] = ["metadata-only-test"]
    approved = tmp_path / "approved"
    candidate.promote_verified(source, approved, candidate.sha(source / "manifest.json"),
        {"representative_labels": True, "domain": "test-only", "approved_by": "unit test", "decision_reason": "TEST ONLY", "quality_validation": proof}, "test-approval")
    rows = integration._rows()
    recurrent = rows[0]["outputs"][0]["address"]
    for row in rows[:60]:
        row["outputs"][0]["address"] = recurrent
        row["output_addresses"] = [recurrent]
    monkeypatch.setattr(integration, "_rows", lambda: rows)
    sessions, _, case_id, _, user_id, _ = integration._ingest_fresh(tmp_path_factory, label="auto-approved-unit",
        settings_overrides={"ml_review_budget": .5, "candidate_directory": approved,
            "candidate_manifest_sha256": candidate.sha(approved / "manifest.json")},
        case_overrides={"scoring_mode": "auto_eligible", "candidate_domain": "test-only"})
    with sessions() as session:
        findings = list(session.scalars(select(FindingRecord).where(FindingRecord.rule_version == manifest["release_id"])))
        assert findings and all(row.raw_score == row.feature_vector["task_scores"]["discrimination"] for row in findings)
        assert session.scalar(select(FindingRecord).where(FindingRecord.rule_version == "deterministic-v1"))
        observed = [row for row in findings if any(item["kind"] == "observed_recipient_recurrence" for item in row.opposing_evidence)]
        assert observed and structured_evidence(session, observed[0], [])["observed_counter_evidence"]
        queue = review_queue(case_id, snapshot_id=None, k=4, fraction=None, limit=4, offset=0, review_state=None,
            user=session.get(User, user_id), session=session)
        assert queue["scorer"] == manifest["release_id"] and queue["eligibility_decision"]["status"] == "eligible"
