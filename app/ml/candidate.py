"""Frozen, owner-trusted candidates; never derives labels from investigator reviews.

Artifacts contain fitted weights (joblib), not executable user uploads. The
operator must pin a separately trusted manifest digest BEFORE deserialization.
Synthetic eligibility is explicitly distinct from representative-label promotion.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib import metadata
from io import BytesIO
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, IsolationForest
from sklearn.isotonic import IsotonicRegression
from threadpoolctl import threadpool_limits

from app.ml import grains

CONTRACT = "causal-structure-prior-bucket-v1"
COLUMNS = (*grains.TRANSACTION_COLUMNS, "prior_completed_equal_output_count", "relay_metadata_missing")
CONTRACT_HASH = hashlib.sha256(json.dumps([CONTRACT, COLUMNS]).encode()).hexdigest()
TASKS = ("motif", "surge", "discrimination")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def library_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        if name != "xgboost":
            raise
        return metadata.version("xgboost-cpu")


def features(facts):
    """Current transaction + strictly earlier parents + previous completed bucket.

    No future initialization, recipient identity, generator ID, truth, review or
    propagated risk is input. Equal-time peers cannot enter historical baseline.
    This is a NEW procedure, not a causal rebranding of deployed v2 burst.
    """
    previous = facts.in_prev.copy()
    valid = np.flatnonzero(previous >= 0)
    previous[valid[facts.tx_time[facts.out_tx[previous[valid]]] >= facts.tx_time[facts.in_tx[valid]]]] = -1
    table = grains.build_transaction_table(replace(facts, in_prev=previous))
    buckets = facts.tx_time // 900
    equal = table.matrix[:, COLUMNS.index("equal_group_exact")] >= 3
    unique, inverse = np.unique(buckets, return_inverse=True)
    counts = np.bincount(inverse, weights=equal, minlength=len(unique))
    positions = np.searchsorted(unique, buckets - 1)
    valid_bucket = positions < len(unique)
    valid_bucket[valid_bucket] &= unique[positions[valid_bucket]] == buckets[valid_bucket] - 1
    prior = np.zeros(len(buckets), dtype=np.float32)
    prior[valid_bucket] = counts[positions[valid_bucket]]
    missing = (facts.tx_src_ip < 0).astype(np.float32) if facts.tx_src_ip is not None else np.ones(len(buckets), dtype=np.float32)
    return np.column_stack((table.matrix, prior, missing)).astype(np.float32, copy=False)


def estimator(name, seed=42):
    if name in {"hist", "hybrid"}:
        return HistGradientBoostingClassifier(max_iter=64, max_leaf_nodes=15, random_state=seed)
    if name == "xgboost":
        from xgboost import XGBClassifier
        return XGBClassifier(n_estimators=64, max_depth=4, tree_method="hist", n_jobs=1, random_state=seed)
    if name == "lightgbm":
        from lightgbm import LGBMClassifier
        return LGBMClassifier(n_estimators=64, num_leaves=15, n_jobs=1, random_state=seed, verbosity=-1)
    raise ValueError("model must be hist, xgboost, lightgbm or hybrid")


def raw_score(model, matrix):
    value = model["classifier"].predict_proba(matrix)[:, 1]
    if "anomaly" in model:
        anomaly = -model["anomaly"].score_samples(matrix)
        tail = np.searchsorted(model["reference_anomaly"], anomaly, side="right") / len(model["reference_anomaly"])
        value = .5 * value + .5 * tail
    return value


def train(matrix, labels, calibration_matrix, calibration_labels, *, name="hist", seed=42):
    """Calibration is separate permitted data; validation is never fitted here."""
    if matrix.shape[1] != len(COLUMNS) or calibration_matrix.shape[1] != len(COLUMNS):
        raise ValueError("feature contract mismatch")
    models = {}
    with threadpool_limits(limits=1):
        for task in TASKS:
            y, cy = np.asarray(labels[task], bool), np.asarray(calibration_labels[task], bool)
            if np.unique(y).size != 2 or np.unique(cy).size != 2:
                raise ValueError(f"{task}: both classes required in training AND calibration")
            model = {"classifier": estimator(name, seed).fit(matrix, y)}
            if name == "hybrid":
                model["anomaly"] = IsolationForest(n_estimators=64, max_samples=min(256, len(matrix)), n_jobs=1, random_state=seed).fit(matrix)
                model["reference_anomaly"] = np.sort(-model["anomaly"].score_samples(matrix))
            model["calibration"] = IsotonicRegression(out_of_bounds="clip").fit(raw_score(model, calibration_matrix), cy)
            models[task] = model
    return models


def infer(models, matrix, *, batch=8192):
    result = {task: np.empty(len(matrix), dtype=np.float32) for task in TASKS}
    with threadpool_limits(limits=1):
        for start in range(0, len(matrix), batch):
            chunk = matrix[start:start + batch]
            for task in TASKS:
                result[task][start:start + batch] = models[task]["calibration"].predict(raw_score(models[task], chunk))
    return result


def save(directory, models, *, name, provenance, eligibility="synthetic_demo", promotion=None):
    if eligibility not in {"synthetic_demo", "validated_candidate"}:
        raise ValueError("unknown candidate eligibility")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    if eligibility != "synthetic_demo" and not (promotion and promotion.get("representative_labels") and promotion.get("domain") and promotion.get("decision_reason") and promotion.get("approved_by")):
        raise ValueError("production eligibility requires a representative-label applicability decision")
    payload = directory / "weights.joblib"
    expanded = BytesIO()
    joblib.dump(models, expanded, compress=0)
    expanded_bytes = expanded.tell()
    if expanded_bytes > 256 << 20:
        raise ValueError("fitted candidate exceeds 256 MiB expanded artifact support limit")
    joblib.dump(models, payload, compress=3)
    manifest = {"schema": 1, "feature_contract": CONTRACT, "feature_sha256": CONTRACT_HASH,
                "columns": list(COLUMNS), "tasks": list(TASKS), "model": name,
                "payload_sha256": sha(payload), "expanded_artifact_bytes": expanded_bytes,
                "eligibility": eligibility, "promotion": promotion,
                "provenance": provenance, "calibration": "separate labelled reference; per-task pattern frequency, NOT criminality",
                "versions": {n: metadata.version(n) for n in ("numpy", "scikit-learn", "joblib")}}
    for package in ({"xgboost": ["xgboost"], "lightgbm": ["lightgbm"]}.get(name, [])):
        manifest["versions"][package] = library_version(package)
    manifest["release_id"] = "anomaly-stack-candidate-v1-" + manifest["payload_sha256"][:16]
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def load(directory, expected_manifest_sha):
    directory = Path(directory)
    if (directory / "manifest.json").stat().st_size > 1 << 20 or (directory / "weights.joblib").stat().st_size > 128 << 20:
        raise ValueError("candidate exceeds bounded manifest/model artifact limit (1/128 MiB)")
    manifest_bytes = (directory / "manifest.json").read_bytes()
    if not expected_manifest_sha or hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_sha:
        raise ValueError("candidate manifest is not externally trusted / digest mismatch")
    manifest = json.loads(manifest_bytes)
    if manifest.get("feature_sha256") != CONTRACT_HASH or manifest.get("columns") != list(COLUMNS):
        raise ValueError("candidate feature contract mismatch")
    from app.resources import admit_global_allocation
    expanded = manifest.get("expanded_artifact_bytes")
    if not isinstance(expanded, int) or not 0 < expanded <= 256 << 20:
        raise ValueError("candidate expanded working set is missing or unsupported")
    admit_global_allocation("trusted candidate deserialization", expanded * 3 + (128 << 20))
    for name, version in manifest["versions"].items():
        if library_version(name) != version:
            raise ValueError(f"candidate runtime version mismatch: {name}")
    payload = (directory / "weights.joblib").read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest["payload_sha256"]:
        raise ValueError("candidate fitted-weight integrity mismatch")
    return manifest, joblib.load(BytesIO(payload))


def eligibility(manifest, case):
    if case.scoring_mode == "synthetic_demo":
        return (bool(case.synthetic and manifest["eligibility"] == "synthetic_demo"), "explicit synthetic/demo only")
    if case.scoring_mode == "validated_candidate":
        decision = manifest.get("promotion") or {}
        eligible = manifest["eligibility"] == "validated_candidate" and decision.get("representative_labels") and case.candidate_domain == decision.get("domain")
        return bool(eligible), "representative-label approval and exact domain required"
    return False, "case uses unsupervised v2; no candidate opt-in"


def sensitivity(models, matrix, selected, *, limit=20):
    """Bounded column-zero perturbation; descriptive score sensitivity, not SHAP."""
    result = {}
    for index in selected[:limit]:
        original = matrix[index:index + 1]
        changed = np.repeat(original, len(COLUMNS), axis=0)
        changed[np.arange(len(COLUMNS)), np.arange(len(COLUMNS))] = 0
        baseline, perturbed = infer(models, original), infer(models, changed)
        result[int(index)] = {task: {column: float(baseline[task][0] - perturbed[task][i]) for i, column in enumerate(COLUMNS)} for task in TASKS}
    return result
