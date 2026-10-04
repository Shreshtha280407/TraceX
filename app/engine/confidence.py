"""Version matched synthetic calibration and immutable finding provenance.

Network statistics and theoretical Gaussian tails are not posterior probabilities.
Legacy findings without pinned provenance remain explicitly unvalidated.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

CALIBRATION_PATH = Path(__file__).resolve().parent / "calibration" / "confidence-v1.json"
#: Out-of-sample expected calibration error at or below this grades "good".
GOOD_ECE = 0.1


@lru_cache(maxsize=1)
def _calibration() -> dict[str, Any]:
    try:
        raw = CALIBRATION_PATH.read_bytes()
        value = json.loads(raw)
        value["_registry_sha256"] = hashlib.sha256(raw).hexdigest()
        return value
    except (OSError, ValueError):
        return {"calibration_id": None, "rules": {}}


def _interpolate(xs: list[float], ys: list[float], value: float) -> float:
    if not xs:
        return float("nan")
    if value <= xs[0]:
        return ys[0]
    if value >= xs[-1]:
        return ys[-1]
    index = bisect.bisect_right(xs, value)
    x0, x1, y0, y1 = xs[index - 1], xs[index], ys[index - 1], ys[index]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (value - x0) / (x1 - x0)


def _grade(entry: dict[str, Any]) -> str:
    validation = entry.get("validation") or {}
    if not validation:
        return "unvalidated"
    ece = validation.get("ece")
    if ece is None:
        return "unvalidated"
    return "good" if ece <= GOOD_ECE else "weak"


def anomaly_p_value(fused_score: float | None) -> float | None:
    """One-sided upper-tail p-value of a Stouffer z-score."""
    if fused_score is None or not math.isfinite(fused_score):
        return None
    return 0.5 * math.erfc(fused_score / math.sqrt(2.0))


TARGET = "synthetic-pattern-majority-v1"


def feature_contract(rule_version):
    if rule_version.startswith("anomaly-stack-candidate-v2-"):
        return "causal-structure-recipient-history-v2"
    if rule_version.startswith("anomaly-stack-candidate-"):
        return "causal-structure-prior-bucket-v1"
    if rule_version.startswith("anomaly-stack-") and not rule_version.startswith("anomaly-stack-candidate-"):
        return rule_version
    return "phase4.1-feature-v1" if rule_version == "deterministic-v1" else rule_version


def confidence_for(rule_id: str, rule_version: str, raw_score: float, feature_vector: dict | None,
                   *, contract: str | None = None, target: str = TARGET, applicable: bool = True) -> dict[str, Any]:
    feature_vector = feature_vector or {}
    calibration = _calibration()
    if rule_version == "network-correlation-v1":
        adjusted = feature_vector.get("adjusted_p_value")
        if adjusted is not None:
            return {
                "value": round(max(0.0, min(1.0, 1.0 - float(adjusted))), 6),
                "method": "statistical",
                "basis": "1 - Bonferroni-adjusted binomial p-value; a transformed test statistic",
                "grade": "statistical",
                "adjusted_p_value": float(adjusted),
                "null_hypothesis": "independent relay observations follow the snapshot's endpoint base rates",
                "test_count": feature_vector.get("test_count", feature_vector.get("tests")),
                "limitations": "A transformed hypothesis-test statistic; not wallet ownership or a posterior probability.",
            }
        observations = feature_vector.get("observations")
        return {
            "value": None,
            "method": "evidence_check",
            "basis": f"reported network metadata contradicts the offline Geo-IP database on {observations} "
                     "observation(s); a data-consistency check, not a probability",
            "grade": "not_applicable",
        }
    contract = contract or feature_contract(rule_version)
    key = f"{rule_id}|{rule_version}|{contract}|{target}"
    entry = calibration.get("rules", {}).get(key, calibration.get("rules", {}).get(rule_id))
    if entry and (entry.get("rule_version") != rule_version or
                  entry.get("feature_contract", feature_contract(entry.get("rule_version", ""))) != contract or
                  entry.get("target_definition", TARGET) != target or not applicable):
        entry = None
    result: dict[str, Any]
    if entry is None:
        result = {"value": None, "method": "uncalibrated", "grade": "unvalidated",
                  "basis": "no applicable version-matched calibration; rank by raw score only"}
    else:
        if entry.get("method") == "isotonic":
            value = _interpolate(entry["x"], entry["y"], float(raw_score))
        else:
            value = float(entry.get("constant", entry.get("base_rate", 0.0)))
        validation = entry.get("validation") or {}
        result = {
            "value": round(value, 6),
            "method": "calibrated",
            "calibration_id": calibration.get("calibration_id"),
            "basis": "Synthetic benchmark calibration: " + str(calibration.get("definition")),
            "grade": _grade(entry),
            "rule_base_rate": entry.get("base_rate"),
            "reliability": {
                key: validation.get(key) for key in ("findings", "positives", "brier", "ece", "auc")
                if key in validation
            },
        }
        result.update({"calibration_sha256": calibration.get("_registry_sha256") or hashlib.sha256(
            json.dumps(calibration, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "registry_key": [rule_id, rule_version, contract, target],
            "training_provenance": calibration.get("fixture"), "split_policy": calibration.get("fit"),
            "applicability_domain": "labelled synthetic fixture; not probability of real-world criminality"})
    if rule_version.startswith("anomaly-stack-") and not rule_version.startswith("anomaly-stack-candidate-"):
        p_value = anomaly_p_value(feature_vector.get("fused_score", raw_score))
        result["anomaly_p_value"] = p_value
        result["anomaly_tail_basis"] = "theoretical Gaussian upper tail; fused dependence/distribution not validated"
        network = feature_vector.get("network_context") or {}
        if network:
            result["network_corroboration"] = network.get("score")
    return result


def pin_snapshot_confidence(session, snapshot_id):
    from sqlalchemy import exists, insert, select

    from app.models import Case, EvidenceSource, FindingConfidence, FindingRecord, ImportJob, Snapshot

    session.flush()
    snapshot = session.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise ValueError("snapshot missing while pinning confidence")
    job = session.get(ImportJob, snapshot.job_id)
    source = session.get(EvidenceSource, job.source_id) if job else None
    case = session.get(Case, snapshot.case_id)
    known_sources = _calibration().get("fixture", {}).get("ingestion_sha256", [])
    applicable = bool(case and case.synthetic and source and source.sha256 in known_sources)
    # Narrow projection and Core inserts keep a fixed batch resident. Do not
    # commit here: findings and their original provenance must commit together.
    findings = session.execute(select(FindingRecord.id, FindingRecord.case_id, FindingRecord.rule_id,
        FindingRecord.rule_version, FindingRecord.raw_score, FindingRecord.feature_vector).where(
        FindingRecord.snapshot_id == snapshot_id,
        # NOT IN can materialize the entire pin table and scan it again for
        # every finding once PostgreSQL's work_mem hash threshold is crossed.
        # finding_id is a non-null primary key, so this correlated anti-join
        # selects exactly the same unpinned rows without that quadratic plan.
        ~exists().where(FindingConfidence.finding_id == FindingRecord.id)).execution_options(yield_per=256))
    for batch in findings.partitions(256):
        session.execute(insert(FindingConfidence.__table__), [
            {"finding_id": finding.id, "case_id": finding.case_id,
             "provenance": confidence_for(finding.rule_id, finding.rule_version, finding.raw_score,
                                          finding.feature_vector, applicable=applicable)}
            for finding in batch])
    session.flush()


def finding_confidence(session, finding):
    from app.models import FindingConfidence

    pinned = session.get(FindingConfidence, finding.id)
    if pinned is not None:
        return pinned.provenance
    return {"value": None, "method": "uncalibrated", "grade": "unvalidated",
            "basis": "legacy finding: original calibration provenance unknown"}
