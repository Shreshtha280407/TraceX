"""Measured product-queue gates, separate from owner label-applicability approval."""
import hashlib
import json
import math

POLICY = "product-queue-quality-gate-v2"
LEGACY_POLICY = "product-queue-quality-gate-v1"


def assess(manifest, reports, *, frozen=None):
    errors, datasets = [], []
    reductions = []
    fractions, capacities = [], []
    release = manifest["release_id"]
    provenance = manifest.get("provenance", {})
    expected = provenance.get("registered_final_ids", [])
    supplied = [report.get("final_id") for report in reports]
    if not expected or len(expected) != len(set(expected)):
        errors.append({"dataset": None, "reason": "Artifact lacks a distinct registered final registry; legacy artifacts require a new protocol, not reinterpretation."})
    if len(supplied) != len(set(supplied)) or sorted(str(v) for v in supplied) != sorted(str(v) for v in expected):
        errors.append({"dataset": None, "reason": "Every required registered final must be supplied exactly once; duplicate, omitted or unrelated final report."})
    if frozen is None or frozen.get("protocol_sha256") != provenance.get("protocol_sha256") or frozen.get("release_id") != release:
        errors.append({"dataset": None, "reason": "Pinned frozen selection/protocol provenance missing or mismatched."})
    registry = (frozen or {}).get("final_registry", {})
    manifest_sha = hashlib.sha256(json.dumps(manifest, indent=2, sort_keys=True).encode()).hexdigest()
    if (frozen or {}).get("manifest_sha256") != manifest_sha:
        errors.append({"dataset": None, "reason": "Frozen selection does not pin the exact serialized artifact manifest, calibration and release identity."})
    if set(registry) != set(expected):
        errors.append({"dataset": None, "reason": "Frozen source-fingerprint registry does not match required finals."})
    if not reports:
        errors.append({"dataset": None, "reason": "No frozen transfer quality reports supplied."})

    def numeric(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    for report in reports:
        dataset = report.get("dataset", "unknown")
        datasets.append(dataset)
        def fail(reason, dataset=dataset):
            errors.append({"dataset": dataset, "reason": reason})
        identity = report.get("model", {})
        if any(identity.get(field) != manifest.get(field) for field in ("release_id", "payload_sha256", "feature_sha256", "feature_contract", "queue_policy")):
            fail("Report does not identify these exact frozen weights and feature contract.")
        if not report.get("truth_sha256") or not report.get("protocol_sha256") or not report.get("procedure", "").startswith("frozen fitted-weight transfer"):
            fail("Frozen transfer protocol/truth provenance is missing; research training tables are not promotion evidence.")
        if report.get("protocol_sha256") != provenance.get("protocol_sha256"):
            fail("Report protocol hash differs from the fitted artifact's registered provenance.")
        pinned = registry.get(report.get("final_id"), {})
        if not report.get("source_sha256") or pinned.get("source_sha256") != report.get("source_sha256"):
            fail("Stable dataset/source fingerprint is absent, substituted or not pinned before final evaluation.")
        if pinned.get("truth_sha256") != report.get("truth_sha256"):
            fail("Final truth fingerprint differs from the registered frozen source/truth pair.")
        if report.get("grouping_sha256") != provenance.get("grouping_sha256") or not provenance.get("grouping_sha256"):
            fail("Grouping procedure was not frozen with these fitted weights.")
        group = report.get("group_quality", {})
        group_identity = {"final_id": report.get("final_id"), "source_sha256": report.get("source_sha256"),
                          "protocol_sha256": report.get("protocol_sha256"), "release_id": release}
        from app.engine.investigations import QUEUE_POLICY
        if any(not value or group.get(key) != value for key, value in group_identity.items()) or group.get("queue_policy") != QUEUE_POLICY:
            fail("Group metrics are substituted/unbound: exact final/source/protocol/scorer and frozen group queue policy are required.")
        if group.get("status") != "EVALUATED" or group.get("grouping_sha256") != provenance.get("grouping_sha256"):
            fail("Actual frozen investigation-group queue quality is missing or not evaluable.")
        elif not numeric(group.get("p_at_100")) or group["p_at_100"] < .90 or group.get("capacity") != 100:
            fail("Actual group proposition P@100 >=0.90 at deployed capacity 100 is required.")
        for task in ("motif", "surge", "discrimination"):
            row = report.get("results", {}).get(task, {})
            population = row.get("population")
            if not isinstance(population, int) or isinstance(population, bool) or population < 100:
                fail(f"{task}: at least 100 labelled observations are required to exercise P@100.")
            ap = row.get("ap")
            if not numeric(ap) or not 0 <= ap <= 1 or (task != "discrimination" and ap < .85):
                fail(f"{task}: AP must be measured; motif/surge preferred target is >=0.85.")
            p100 = row.get("p_at_100")
            if not numeric(p100) or not .90 <= p100 <= 1:
                fail(f"{task}: measurable P@100 >=0.90 is required for promotion, not an unexercised gate.")
            delta = report.get("comparison", {}).get(task, {}).get("ap_delta")
            if not numeric(delta) or not 0 <= delta <= 1:
                fail(f"{task}: AP regression is unknown or negative.")
        queue = report.get("queue_comparison", {})
        if queue.get("population_scope") != "all time-eligible canonical transactions" or queue.get("eligible_transactions") != queue.get("labelled_transactions"):
            fail("Full eligible-population truth coverage is missing; subset AP is not deployed-queue precision/recall.")
        if queue.get("status") != "EVALUATED" or queue.get("candidate_policy") != manifest.get("queue_policy"):
            fail("Exact deployed queue policy was not evaluated at matched capacity.")
            continue
        if not numeric(queue.get("merchant_controls")) or queue["merchant_controls"] <= 0:
            fail("No measured benign merchant controls.")
        capacity = queue.get("review_budget")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity != 100 or any(
                row.get("review_budget") != capacity for row in report.get("results", {}).values()):
            fail("Reviewer capacity must match the deployed default of 100 across metrics and queue comparison.")
        fraction = queue.get("finding_budget_fraction")
        if provenance.get("review_budget") != capacity or provenance.get("finding_budget_fraction") != fraction or (frozen or {}).get("review_budget") != capacity or (frozen or {}).get("finding_budget_fraction") != fraction:
            fail("Reviewer capacity/retention must match fitted artifact provenance and frozen selection, not a post-final policy choice.")
        fractions.append(fraction)
        capacities.append(capacity)
        if not numeric(fraction) or not 0 < fraction <= 1 or any(
                not isinstance(queue.get(field), int) or isinstance(queue.get(field), bool)
                or not isinstance(capacity, int) or queue[field] < capacity
                for field in ("candidate_available_findings", "baseline_available_findings")):
            fail("Product retained-finding fraction/availability must support this review capacity for both scorers.")
        newer, older = queue.get("candidate", {}), queue.get("baseline", {})
        for field in ("precision", "recall", "benign_in_queue", "merchant_false_positives"):
            a, b = newer.get(field), older.get(field)
            bounded = (0 <= a <= 1 and 0 <= b <= 1) if field in {"precision", "recall"} and numeric(a) and numeric(b) else (
                isinstance(a, int) and not isinstance(a, bool) and isinstance(b, int) and not isinstance(b, bool)
                and a >= 0 and b >= 0 and isinstance(capacity, int) and a <= capacity and b <= capacity)
            if not numeric(a) or not numeric(b) or not bounded:
                fail(f"Queue {field} is missing/invalid.")
            elif (a < b if field in {"precision", "recall"} else a > b):
                fail(f"Queue {field} regresses against v2 at the same review capacity.")
        if not numeric(newer.get("precision")) or newer["precision"] < .90:
            fail("Deployed queue precision must be >=0.90 at the documented review capacity.")
        if numeric(newer.get("merchant_false_positives")) and numeric(older.get("merchant_false_positives")):
            reductions.append(older["merchant_false_positives"] - newer["merchant_false_positives"])
    if not reductions or not any(value > 0 for value in reductions):
        errors.append({"dataset": None, "reason": "No measured merchant queue false-positive reduction; better explanations alone are not precision evidence."})
    if fractions and any(value != fractions[0] for value in fractions[1:]) or capacities and any(value != capacities[0] for value in capacities[1:]):
        errors.append({"dataset": None, "reason": "Registered reports disagree on retained-finding fraction or review capacity."})
    return {"policy": POLICY, "status": "BLOCKED" if errors else "PASSED", "errors": errors,
        "release_id": release, "payload_sha256": manifest["payload_sha256"], "feature_sha256": manifest["feature_sha256"],
        "feature_contract": manifest.get("feature_contract"), "queue_policy": manifest.get("queue_policy"),
        "datasets": datasets, "merchant_false_positive_reductions": reductions,
        "finding_budget_fraction": fractions[0] if fractions else None, "review_budget": capacities[0] if capacities else None,
        "protocol_sha256": provenance.get("protocol_sha256"), "registered_final_ids": expected,
        "frozen_manifest_sha256": (frozen or {}).get("manifest_sha256"),
        "grouping_sha256": provenance.get("grouping_sha256"), "group_capacity": 100,
        "limitations": "Quantitative gates are necessary, not representative-label approval or a guarantee on arbitrary uploads. Point estimates do not establish statistical significance."}
