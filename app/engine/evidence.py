"""Deterministic evidence presentation; never generates or changes detector facts."""
from __future__ import annotations

import re

from sqlalchemy import select

from app.engine.confidence import finding_confidence
from app.models import EvidenceSource, FragmentReceipt, ImportJob

EMPTY_COUNTER = "No counter-evidence was identified within the supplied data and checked coverage."
POLICY = "family-context-round-robin-v2"


def reference_status(session, case_id, reference):
    """Receipt-approved locator metadata only. Raw replay separately verifies bytes."""
    source = session.get(EvidenceSource, reference.get("evidence_id"))
    if source is None or source.case_id != case_id:
        return "unavailable_or_out_of_case"
    if reference.get("source_sha256", source.sha256) != source.sha256:
        return "stale_source_hash"
    locator = reference.get("locator", "")
    if source.source_format in {"ndjson", "json"}:
        match = re.fullmatch(r"/(\d+)", locator)
        number = int(match[1]) + 1 if match else None
    elif source.source_format == "csv":
        match = re.fullmatch(r"record:([1-9]\d*)", locator)
        number = int(match[1]) if match else None
    else:
        match = re.fullmatch(r"/(?:transaction|record|row)\[([1-9]\d*)\]", locator)
        number = int(match[1]) if match else None
    if number is None:
        return "invalid_locator"
    receipt = session.scalar(select(FragmentReceipt.id).join(ImportJob, ImportJob.id == FragmentReceipt.job_id)
                             .where(ImportJob.source_id == source.id, FragmentReceipt.case_id == case_id,
                                    FragmentReceipt.source_record_start <= number,
                                    FragmentReceipt.source_record_end >= number).limit(1))
    return "receipt_approved" if receipt else "outside_checked_receipts"


def interpretation(finding):
    if finding.rule_version.startswith("anomaly-stack-"):
        category = "anomaly_triage"
    elif finding.rule_version.startswith("network-"):
        category = "network_correlation"
    else:
        category = "observed_structural_pattern"
    return {"category": category, "review_disposition": finding.status,
            "confirmed_proposition": finding.status == "confirmed",
            "suspicious_context_escalated": finding.status == "escalated",
            "scope": "Pattern, anomaly or association only; no identity, origin or criminality verdict."}


def structured_evidence(session, finding, reviews):
    vector = finding.feature_vector or {}
    detector = vector.get("detector_result", {})
    refs = finding.source_refs or []
    statuses = {}

    def checked(references):
        rows = []
        for ref in references:
            key = (ref.get("evidence_id"), ref.get("locator"), ref.get("source_sha256"))
            if key not in statuses:
                statuses[key] = reference_status(session, finding.case_id, ref)
            rows.append({**ref, "reference_status": statuses[key]})
        return rows

    opposing, limitations, unverified = [], [], []
    for item in finding.opposing_evidence or []:
        row = {**item, "source_refs": checked(item.get("source_refs", []))}
        if item.get("kind", "").endswith("limitation") or not row["source_refs"]:
            limitations.append(row.get("statement", "Unspecified stored limitation"))
        elif all(ref["reference_status"] == "receipt_approved" for ref in row["source_refs"]):
            opposing.append({**row, "basis": "stored detector observation"})
        else:
            unverified.append(row)
    for review in reviews:
        if review.get("counterevidence_refs"):
            row = {"kind": "reviewer_cited_counter_evidence", "statement": review["reason"],
                             "basis": "reviewer assertion, not an automatically verified benign verdict",
                             "source_refs": checked(review["counterevidence_refs"]),
                             "review_id": review["review_id"]}
            (opposing if all(ref["reference_status"] == "receipt_approved" for ref in row["source_refs"])
             else unverified).append(row)
    coverage = finding.coverage or {}
    if not coverage:
        limitations.append("Coverage metadata is absent; applicability is unknown.")
    if (coverage.get("graph") or coverage).get("spend_lineage_complete") is False:
        limitations.append("Missing prevouts mean incomplete lineage, not evidence of unrelated funds.")
    return {
        "schema": "structured-finding-evidence-v1", "proposition": finding.claim,
        "responsible_procedure": {"rule_id": finding.rule_id, "rule_version": finding.rule_version,
                                  "feature_hash": finding.feature_vector_hash},
        "interpretation": interpretation(finding),
        "supporting_observations": [{"statement": line, "source_refs": checked(refs)}
                                    for line in (finding.explanations or [finding.claim])],
        "supporting_refs": checked(refs), "observed_counter_evidence": opposing,
        "counter_evidence_summary": "Source-backed counter-evidence is listed below." if opposing else EMPTY_COUNTER,
        "unverified_opposing_references": unverified,
        "benign_alternatives": [{"statement": line, "basis": "plausible alternative, not observed counter-evidence"}
                                for line in (finding.benign_alternatives or [])],
        "missing_evidence": list(dict.fromkeys(limitations)), "coverage": coverage,
        "graph_associations": detector.get("graph_path"),
        "features": vector, "comparison_baselines": {**(vector.get("comparison_baselines") or {}), **{key: value for key, value in vector.items()
            if key.startswith("baseline_") or key in {"reference_cutoff_epoch", "reference_fraction", "threshold"}}},
        "review_decisions": reviews, "score_provenance": {**finding_confidence(session, finding),
            "candidate_calibration": vector.get("calibration_provenance"),
            "eligibility": vector.get("eligibility"), "artifact_sha256": vector.get("artifact_sha256"),
            "applicability_decision": vector.get("promotion")},
        "explanation_labels": {"ecod": "Unusual feature values, not attribution of IF/fused score or criminality",
            "isolation_forest": "Sensitivity to bounded perturbations, not causal evidence or a valid counterfactual",
            "fusion": vector.get("fusion_components", {"layer_inputs": vector.get("layers", {}),
                "scope": "Historical row: normalized fusion contributions were not retained"})},
        "contextual_review": {"policy": POLICY, "observed_counter_evidence_items": len(opposing),
            "coverage_requires_review": bool(limitations), "disposition": finding.status,
            "scope": "Context for investigator prioritization; raw observations and scores are never suppressed."},
    }
