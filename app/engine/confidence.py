"""Calibrated confidence for every finding, computed when it is read.

A finding's `raw_score` is a rule-specific triage score (a peeling-chain score
in 0..1, a window-rule score in 45..100, a fused anomaly z-score...). It ranks,
but it is not a probability. The confidence attached here is:

* **calibrated** (rules with labelled evidence): P(true laundering-pattern lead
  | rule, raw score), an isotonic map fitted per rule on the labelled synthetic
  fixture (scripts/calibrate_confidence.py -> calibration/confidence-v1.json),
  with its measured out-of-sample reliability (Brier score, expected
  calibration error) and a reliability grade;
* **statistical** (network-correlation rules): 1 - the Bonferroni-adjusted
  p-value of the concentration test, i.e. confidence that the concentration is
  not chance under that snapshot's own base rates;
* plus, for anomaly-stack findings, the label-free `anomaly_p_value` -- the
  probability that an ordinary transaction from the snapshot's own reference
  period scores at least this high (one-sided tail of the fused z-score).

Computed at read time, so stored findings and their hashes never change when a
calibration is refitted, and old cases pick up a new calibration immediately.
"""

from __future__ import annotations

import bisect
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
        return json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
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


def confidence_for(rule_id: str, rule_version: str, raw_score: float, feature_vector: dict | None) -> dict[str, Any]:
    feature_vector = feature_vector or {}
    calibration = _calibration()
    if rule_version == "network-correlation-v1":
        adjusted = feature_vector.get("adjusted_p_value")
        if adjusted is not None:
            return {
                "value": round(max(0.0, min(1.0, 1.0 - float(adjusted))), 6),
                "method": "statistical",
                "basis": "1 - Bonferroni-adjusted binomial p-value of the relay concentration in this snapshot",
                "grade": "statistical",
            }
        observations = feature_vector.get("observations")
        return {
            "value": None,
            "method": "evidence_check",
            "basis": f"reported network metadata contradicts the offline Geo-IP database on {observations} "
                     "observation(s); a data-consistency check, not a probability",
            "grade": "not_applicable",
        }
    entry = calibration.get("rules", {}).get(rule_id)
    result: dict[str, Any]
    if entry is None:
        result = {"value": None, "method": "uncalibrated", "grade": "unvalidated",
                  "basis": "no labelled evidence is available for this rule yet; rank by raw score only"}
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
            "basis": calibration.get("definition"),
            "grade": _grade(entry),
            "rule_base_rate": entry.get("base_rate"),
            "reliability": {
                key: validation.get(key) for key in ("findings", "positives", "brier", "ece", "auc")
                if key in validation
            },
        }
    if rule_version.startswith("anomaly-stack-"):
        p_value = anomaly_p_value(feature_vector.get("fused_score", raw_score))
        result["anomaly_p_value"] = p_value
        network = feature_vector.get("network_context") or {}
        if network:
            result["network_corroboration"] = network.get("score")
    return result
