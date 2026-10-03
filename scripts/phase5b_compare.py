"""Phase 5B orchestrator: real feature package -> 3 candidates -> one selection decision.

Comparison and selection only. Nothing here touches the production API, the findings
endpoint, the frontend, or the Docker image.

Thread limits MUST be set before numpy/sklearn import, so this happens at module load,
before those imports below.
"""

from __future__ import annotations

import os

_DEFAULT_THREADS = str(min(4, os.cpu_count() or 1))
_THREADS = os.environ.get("TRACEX_ML_THREADS", _DEFAULT_THREADS)
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, _THREADS)

import hashlib
import json
import multiprocessing as mp
import platform
import queue
import resource
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import phase5b_prepare_features as prepare

RSS_WARNING_MB = int(os.environ.get("TRACEX_ML_RSS_WARNING_MB", "24576"))  # configurable per runner; 24GB default for Aditya's 32GB MacBook, leaving ~8GB free for macOS
FIT_BUDGET_SECONDS = 600
REVIEW_BUDGET = 50
# Predeclared before any candidate is scored: a candidate must not raise benign FP/1,000
# windows over the rule baseline by more than this multiplicative-or-additive margin.
BENIGN_FP_MARGIN_MULTIPLIER = 2.0
BENIGN_FP_MARGIN_ADDITIVE = 5.0


def peak_rss_mb() -> float:
    """Peak RSS of this process. stdlib, POSIX, no new dependency — Linux reports KB,
    macOS reports bytes, so the two platforms need different normalization."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def thread_count() -> int:
    return int(_THREADS)


class _SleepForeverEstimator:
    """Test-only stand-in for a pathologically slow real estimator — lets
    tests/unit/test_phase5b_comparison.py prove fit_in_subprocess() genuinely
    terminates a runaway fit within budget, instead of only asserting that from
    reading the source. Never selected by name outside that one test."""

    def fit(self, x_train):
        time.sleep(3600)
        return self

    def score_samples(self, x):
        import numpy as np

        return np.zeros(len(x))


def _build_estimator(name: str, seed: int, n_jobs: int):
    """The single, picklable-by-name constructor — used both here and inside the
    subprocess worker below, so a candidate is defined in exactly one place."""
    if name == "isolation_forest":
        from sklearn.ensemble import IsolationForest

        return IsolationForest(random_state=seed, n_jobs=n_jobs, n_estimators=100)
    if name == "local_outlier_factor_novelty":
        from sklearn.neighbors import LocalOutlierFactor

        return LocalOutlierFactor(novelty=True, n_jobs=n_jobs, n_neighbors=20)
    if name == "_test_slow_sleep":
        return _SleepForeverEstimator()
    raise ValueError(f"unknown candidate name {name!r}")


def _fit_worker(name: str, seed: int, n_jobs: int, x_train, result_queue: mp.Queue) -> None:
    """Runs in a separate spawned process, never a thread — only a process can be
    forcibly killed if a candidate (LOF in particular) runs far longer than expected.
    Always puts a result, success or failure, so the parent's queue.get() never blocks
    on a child that crashed instead of timing out."""
    try:
        model = _build_estimator(name, seed, n_jobs)
        model.fit(x_train)
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        child_peak_rss_mb = raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024
        result_queue.put({"ok": True, "model": model, "peak_rss_mb": child_peak_rss_mb})
    except Exception as exc:  # noqa: BLE001 — every child-side failure must reach the parent, not vanish silently
        result_queue.put({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def fit_in_subprocess(
    *,
    name: str,
    seed: int,
    n_jobs: int,
    x_train,
    budget_seconds: int = FIT_BUDGET_SECONDS,
    label: str = "fit",
    progress_interval_seconds: float = 15.0,
) -> tuple[Any, float, float | None, str | None]:
    """Real, enforced wall-clock budget: fits `name` in a child process and terminates
    it if still running after `budget_seconds`, rather than only measuring elapsed time
    after an unbounded in-process .fit() call has already returned on its own.

    Polls in progress_interval_seconds slices (instead of one blocking wait for the
    whole budget) and prints a heartbeat each slice — on a laptop with no other
    output, a silent 600s wait is indistinguishable from a hang; Aditya needs to see
    it's still working and roughly how much budget is left.

    Returns (model_or_None, fit_seconds, peak_rss_mb_or_None, ineligible_reason_or_None).
    """
    ctx = mp.get_context("spawn")  # matches macOS's default; explicit so Linux runs are identical
    result_queue: mp.Queue = ctx.Queue()
    process = ctx.Process(target=_fit_worker, args=(name, seed, n_jobs, x_train, result_queue))
    began = time.perf_counter()
    process.start()
    payload = None
    timed_out = False
    while True:
        elapsed = time.perf_counter() - began
        remaining = budget_seconds - elapsed
        if remaining <= 0:
            timed_out = True
            break
        # Reading the queue in short slices (not one blocking wait for the full
        # budget) is what lets us print progress — it's also what actually waits,
        # since joining the process first risks a deadlock if the child's put() is
        # blocked on pipe buffer space the parent hasn't drained yet.
        try:
            payload = result_queue.get(timeout=min(progress_interval_seconds, remaining))
            break
        except queue.Empty:
            print(f"[phase5b]   ... {name} {label} still running ({elapsed:.0f}s / {budget_seconds}s budget)", flush=True)
    fit_seconds = time.perf_counter() - began

    if timed_out:
        process.terminate()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
        process.join()
        return None, fit_seconds, None, f"fit exceeded the {budget_seconds}s budget; process terminated"

    process.join(timeout=30)
    if payload is None or not payload.get("ok"):
        reason = payload.get("error") if payload else f"worker exited without a result (exitcode={process.exitcode})"
        return None, fit_seconds, None, reason
    return payload["model"], fit_seconds, payload["peak_rss_mb"], None


@dataclass
class CandidateResult:
    name: str
    version: str
    parameters: dict[str, Any]
    eligible: bool
    ineligible_reason: str | None = None
    fit_seconds: float | None = None
    score_seconds: float | None = None
    batch_inference_p95_ms: float | None = None
    peak_rss_mb: float | None = None
    rss_warning: bool = False
    score_direction: str = "higher_is_more_suspicious"
    rank_stability_spearman: float | None = None
    review_workload: int | None = None
    precision_at_20: float | None = "unavailable"
    precision_at_50: float | None = "unavailable"
    benign_fp_per_1000: float | None = "unavailable"
    row_counts: dict[str, int] = field(default_factory=dict)
    limitations: list[str] = field(
        default_factory=lambda: ["Synthetic-fixture evaluation only — does not prove real-world detection accuracy."]
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return ordered[index]


def batch_inference_p95_ms(score_fn, matrix, batch_size: int = 100) -> float:
    latencies: list[float] = []
    for start in range(0, len(matrix), batch_size):
        batch = matrix[start : start + batch_size]
        began = time.perf_counter()
        score_fn(batch)
        latencies.append((time.perf_counter() - began) * 1000)
    return _percentile(latencies, 95)


def spearman(a: list[float], b: list[float]) -> float:
    """Stdlib-only Spearman rank correlation (no scipy.stats dependency for one metric)."""
    n = len(a)
    if n < 2:
        return 1.0

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                result[order[k]] = average_rank
            i = j + 1
        return result

    ra, rb = ranks(a), ranks(b)
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in ra)
    var_b = sum((y - mean_b) ** 2 for y in rb)
    if var_a == 0 or var_b == 0:
        return 1.0
    return cov / (var_a * var_b) ** 0.5


def evaluate_ranking(
    *,
    scores: list[float],
    keys: list[tuple[str, datetime]],
    split_mask: list[bool],
    review_positive_keys: set[tuple[str, datetime]],
    benign_keys: set[tuple[str, datetime]],
    review_positive_dropped: int,
    benign_dropped: int,
) -> dict[str, Any]:
    indexed = [(scores[i], keys[i]) for i in range(len(scores)) if split_mask[i]]
    indexed.sort(key=lambda item: item[0], reverse=True)
    ranked_keys = [key for _, key in indexed]

    def precision_at(k: int) -> Any:
        if not review_positive_keys:
            return "unavailable"
        top = ranked_keys[:k]
        hits = sum(1 for key in top if key in review_positive_keys)
        return hits / min(k, len(top)) if top else 0.0

    review_workload = REVIEW_BUDGET
    if benign_keys:
        top_budget = set(ranked_keys[:REVIEW_BUDGET])
        benign_hits = sum(1 for key in top_budget if key in benign_keys)
        benign_fp_per_1000 = (benign_hits / max(len(benign_keys), 1)) * 1000
    else:
        benign_fp_per_1000 = "unavailable"

    return {
        "review_workload": review_workload,
        "precision_at_20": precision_at(20),
        "precision_at_50": precision_at(50),
        "benign_fp_per_1000": benign_fp_per_1000,
        "review_positive_dropped": review_positive_dropped,
        "benign_dropped": benign_dropped,
    }


def run_rule_baseline(package: prepare.FeaturePackage) -> tuple[CandidateResult, list[float]]:
    began = time.perf_counter()
    scores = [
        1.0 / package.baseline_rank_by_key[key]
        if key in package.baseline_rank_by_key
        else 0.0
        for key in zip(
            [ref.removeprefix("address:") for ref in package.matrix.entity_refs],
            package.matrix.window_starts,
            strict=True,
        )
    ]
    elapsed = time.perf_counter() - began
    result = CandidateResult(
        name="phase4_rule_only_baseline",
        version="deterministic-v1",
        parameters={"note": "not an ML model; real FindingRecord.rank at the same (entity_ref, window) key"},
        eligible=True,
        fit_seconds=0.0,
        score_seconds=elapsed,
        batch_inference_p95_ms=0.0,
        peak_rss_mb=peak_rss_mb(),
    )
    return result, scores


def run_sklearn_candidate(
    *,
    name: str,
    version: str,
    parameters: dict[str, Any],
    x_train,
    x_all,
    seed: int,
    n_jobs: int,
    stability_check: bool = True,
    budget_seconds: int = FIT_BUDGET_SECONDS,
) -> tuple[CandidateResult, list[float] | None, Any]:
    """Returns (result, scores_or_None, fitted_model_or_None). The caller saves
    `fitted_model` directly as the artifact — this function never fits more than
    twice (main fit + one stability rerun), never a third time for saving."""
    result = CandidateResult(name=name, version=version, parameters=parameters, eligible=True)
    print(f"[phase5b]   {name}: starting main fit on {len(x_train)} training rows (budget {budget_seconds}s)...", flush=True)
    model, fit_seconds, child_peak_rss_mb, reason = fit_in_subprocess(
        name=name, seed=seed, n_jobs=n_jobs, x_train=x_train, budget_seconds=budget_seconds, label="main fit"
    )
    result.fit_seconds = fit_seconds
    if model is None:
        print(f"[phase5b]   {name}: ineligible after {fit_seconds:.1f}s — {reason}", flush=True)
        result.eligible = False
        result.ineligible_reason = reason
        return result, None, None
    print(f"[phase5b]   {name}: main fit done in {fit_seconds:.1f}s, scoring {len(x_all)} rows...", flush=True)

    began = time.perf_counter()
    raw_scores = -model.score_samples(x_all)
    score_seconds = time.perf_counter() - began
    print(f"[phase5b]   {name}: scoring done in {score_seconds:.1f}s", flush=True)

    result.score_seconds = score_seconds
    result.batch_inference_p95_ms = batch_inference_p95_ms(lambda batch: model.score_samples(batch), x_all)
    result.peak_rss_mb = child_peak_rss_mb
    result.rss_warning = child_peak_rss_mb is not None and child_peak_rss_mb > RSS_WARNING_MB

    if stability_check:
        print(f"[phase5b]   {name}: starting stability rerun (budget {budget_seconds}s)...", flush=True)
        rerun_model, rerun_fit_seconds, _rerun_rss, rerun_reason = fit_in_subprocess(
            name=name, seed=seed, n_jobs=n_jobs, x_train=x_train, budget_seconds=budget_seconds, label="stability rerun"
        )
        print(f"[phase5b]   {name}: stability rerun {'done' if rerun_model is not None else 'skipped'} in {rerun_fit_seconds:.1f}s", flush=True)
        if rerun_model is not None:
            rerun_scores = -rerun_model.score_samples(x_all)
            result.rank_stability_spearman = spearman(list(raw_scores), list(rerun_scores))
        else:
            result.limitations.append(f"stability rerun unavailable: {rerun_reason}")

    return result, list(raw_scores), model


def apply_selection_rule(
    results: list[CandidateResult], baseline_benign_fp: Any, baseline_precision_at_20: Any
) -> dict[str, Any]:
    eligible = [r for r in results if r.eligible and r.name != "phase4_rule_only_baseline"]
    gated: list[CandidateResult] = []
    gate_notes: dict[str, str] = {}
    for candidate in eligible:
        if candidate.rss_warning:
            gate_notes[candidate.name] = "exceeded RSS warning threshold"
            continue
        if isinstance(candidate.benign_fp_per_1000, (int, float)) and isinstance(baseline_benign_fp, (int, float)):
            margin = max(baseline_benign_fp * BENIGN_FP_MARGIN_MULTIPLIER, baseline_benign_fp + BENIGN_FP_MARGIN_ADDITIVE)
            if candidate.benign_fp_per_1000 > margin:
                gate_notes[candidate.name] = f"benign FP/1000 ({candidate.benign_fp_per_1000:.2f}) exceeded predeclared margin ({margin:.2f})"
                continue
        gated.append(candidate)

    if not gated:
        return {
            "selected_candidate": None,
            "status": "no_model_selected",
            "reason": "no candidate cleared the predeclared gates (budget/memory/benign-FP margin)",
            "gate_notes": gate_notes,
        }

    has_labels = any(isinstance(c.precision_at_20, (int, float)) for c in gated) and isinstance(
        baseline_precision_at_20, (int, float)
    )
    if has_labels:
        # A candidate must strictly beat the rule-only baseline's own validation
        # Precision@20 to be selected — a tie (including the degenerate 0.0 == 0.0
        # case when the label join finds nothing) means the model adds no
        # demonstrated value over the deterministic rules, so no model is selected.
        # Ties never win.
        beating_baseline = [
            c
            for c in gated
            if isinstance(c.precision_at_20, (int, float)) and c.precision_at_20 > baseline_precision_at_20
        ]
        if not beating_baseline:
            return {
                "selected_candidate": None,
                "status": "no_model_selected",
                "reason": (
                    f"no candidate's validation Precision@20 exceeded the rule-only baseline "
                    f"({baseline_precision_at_20:.4f}); ties never win"
                ),
                "gate_notes": gate_notes,
            }
        ranked = sorted(beating_baseline, key=lambda c: c.precision_at_20, reverse=True)
        chosen = ranked[0]
        basis = "best validation Precision@20 among candidates that strictly beat the rule-only baseline (ties never win)"
    else:
        ranked = sorted(
            gated,
            key=lambda c: (
                -(c.rank_stability_spearman or 0),
                c.score_seconds or 0,
                c.peak_rss_mb or 0,
            ),
        )
        chosen = ranked[0]
        basis = "labels unavailable — chosen provisionally on stability, latency, and memory only; real-world accuracy is unknown"

    return {
        "selected_candidate": chosen.name,
        # "selected" only when a real, label-based Precision@20 comparison decided it.
        # "selected_provisional" when the choice rests solely on stability/latency/memory
        # because no usable label join existed — a materially weaker claim that must be
        # visible in the status itself, not buried in a side field a caller might not check.
        "status": "selected" if has_labels else "selected_provisional",
        "selection_basis": basis,
        "real_world_accuracy": "unknown" if not has_labels else "not established — synthetic-fixture evaluation only",
        "gate_notes": gate_notes,
    }


def environment_report() -> dict[str, Any]:
    try:
        import sklearn

        sklearn_version = sklearn.__version__
    except ImportError:
        sklearn_version = None
    try:
        import numpy

        numpy_version = numpy.__version__
    except ImportError:
        numpy_version = None
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "sklearn_version": sklearn_version,
        "numpy_version": numpy_version,
        "cpu_count": os.cpu_count(),
        "tracex_ml_threads": thread_count(),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=REPO / "datasets" / "phase5a_100k")
    parser.add_argument("--output", type=Path, default=None, help="run directory; default is a fresh timestamped experiments/runs/<id>/")
    args = parser.parse_args()

    manifest_path = args.fixture / "fixture_manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(
            f"Phase 5A 100K fixture not found at {args.fixture}. Generate it first:\n"
            "  python3 fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson --verify"
        )

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output or (REPO / "experiments" / "runs" / run_id)
    (run_dir / "candidate_logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    fixture_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    print(f"[phase5b] preparing real feature package from {args.fixture} ...")
    package = prepare.prepare_feature_package(args.fixture, fixture_manifest_sha256=fixture_manifest_sha256)
    (run_dir / "feature_manifest.json").write_text(json.dumps(package.manifest, indent=2), encoding="utf-8")
    print(f"[phase5b] prepared {package.manifest['row_count']} rows, splits={package.manifest['split_counts']}")

    import numpy as np
    from sklearn.preprocessing import StandardScaler

    columns = package.matrix.columns
    raw = np.array([[row[c] for c in columns] for row in package.matrix.rows], dtype=float)
    splits = package.splits
    train_mask = [s == "train_reference" for s in splits]
    validation_mask = [s == "validation" for s in splits]
    holdout_mask = [s == "final_holdout" for s in splits]

    scaler = StandardScaler()
    scaler.fit(raw[train_mask])
    scaled = scaler.transform(raw)

    keys = [
        (ref.removeprefix("address:"), start)
        for ref, start in zip(package.matrix.entity_refs, package.matrix.window_starts, strict=True)
    ]
    review_positive_keys = package.label_positives.get("peeling_chain", set()) | package.label_positives.get("coinjoin_like", set())
    review_positive_dropped = package.label_dropped.get("peeling_chain", 0) + package.label_dropped.get("coinjoin_like", 0)
    benign_keys = package.label_positives.get("benign_ordinary_sequence", set()) | package.label_positives.get(
        "benign_ordinary_multi_output", set()
    )
    benign_dropped = package.label_dropped.get("benign_ordinary_sequence", 0) + package.label_dropped.get("benign_ordinary_multi_output", 0)

    def row_counts() -> dict[str, int]:
        return {
            "train_reference": sum(train_mask),
            "validation": sum(validation_mask),
            "final_holdout": sum(holdout_mask),
        }

    results: list[CandidateResult] = []
    all_scores: dict[str, list[float]] = {}

    print("[phase5b] running candidate 1/3: phase4_rule_only_baseline")
    baseline_result, baseline_scores = run_rule_baseline(package)
    baseline_metrics = evaluate_ranking(
        scores=baseline_scores, keys=keys, split_mask=validation_mask,
        review_positive_keys=review_positive_keys, benign_keys=benign_keys,
        review_positive_dropped=review_positive_dropped, benign_dropped=benign_dropped,
    )
    baseline_result.review_workload = baseline_metrics["review_workload"]
    baseline_result.precision_at_20 = baseline_metrics["precision_at_20"]
    baseline_result.precision_at_50 = baseline_metrics["precision_at_50"]
    baseline_result.benign_fp_per_1000 = baseline_metrics["benign_fp_per_1000"]
    baseline_result.row_counts = row_counts()
    results.append(baseline_result)
    all_scores[baseline_result.name] = baseline_scores

    candidate_specs = [
        (
            "isolation_forest",
            f"scikit-learn {__import__('sklearn').__version__}",
            {"random_state": 42, "n_jobs": thread_count(), "n_estimators": 100},
        ),
        (
            "local_outlier_factor_novelty",
            f"scikit-learn {__import__('sklearn').__version__}",
            {"novelty": True, "n_jobs": thread_count(), "n_neighbors": 20},
        ),
    ]
    for index, (name, version, parameters) in enumerate(candidate_specs, start=2):
        print(f"[phase5b] running candidate {index}/3: {name}")
        result, scores, model = run_sklearn_candidate(
            name=name, version=version, parameters=parameters,
            x_train=scaled[train_mask], x_all=scaled, seed=42, n_jobs=thread_count(),
        )
        result.row_counts = row_counts()
        if scores is not None:
            metrics = evaluate_ranking(
                scores=scores, keys=keys, split_mask=validation_mask,
                review_positive_keys=review_positive_keys, benign_keys=benign_keys,
                review_positive_dropped=review_positive_dropped, benign_dropped=benign_dropped,
            )
            result.review_workload = metrics["review_workload"]
            result.precision_at_20 = metrics["precision_at_20"]
            result.precision_at_50 = metrics["precision_at_50"]
            result.benign_fp_per_1000 = metrics["benign_fp_per_1000"]
            all_scores[name] = scores

            import joblib

            # The already-fitted model from run_sklearn_candidate's main fit — never
            # refit a third time just to produce the artifact.
            joblib.dump(model, run_dir / "artifacts" / f"{name}.joblib")
            (run_dir / "artifacts" / f"{name}.manifest.json").write_text(
                json.dumps(
                    {
                        "candidate": name, "version": version, "parameters": parameters,
                        "feature_schema_version": package.manifest["feature_schema_version"],
                        "feature_columns": list(columns), "feature_columns_sha256": package.manifest["feature_columns_sha256"],
                        "preprocessing": {"scaler": "StandardScaler", "fit_on": "train_reference"},
                        "scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
                        "python_version": platform.python_version(), "sklearn_version": __import__("sklearn").__version__,
                        "numpy_version": __import__("numpy").__version__, "training_seed": 42,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        results.append(result)
        (run_dir / "candidate_logs" / f"{name}.json").write_text(json.dumps(asdict(result), indent=2, default=str), encoding="utf-8")

    (run_dir / "candidate_logs" / "phase4_rule_only_baseline.json").write_text(
        json.dumps(asdict(baseline_result), indent=2, default=str), encoding="utf-8"
    )

    selection = apply_selection_rule(results, baseline_result.benign_fp_per_1000, baseline_result.precision_at_20)

    if selection["status"] in ("selected", "selected_provisional") and selection["selected_candidate"] in all_scores:
        chosen_name = selection["selected_candidate"]
        chosen_scores = all_scores[chosen_name]
        holdout_metrics = evaluate_ranking(
            scores=chosen_scores, keys=keys, split_mask=holdout_mask,
            review_positive_keys=review_positive_keys, benign_keys=benign_keys,
            review_positive_dropped=review_positive_dropped, benign_dropped=benign_dropped,
        )
        selection["final_holdout_metrics"] = holdout_metrics
        selection["final_holdout_evaluated_once"] = True

    (run_dir / "model_selection.json").write_text(json.dumps(selection, indent=2, default=str), encoding="utf-8")
    (run_dir / "environment.json").write_text(json.dumps(environment_report(), indent=2), encoding="utf-8")

    metrics_rows = ["candidate,eligible,fit_seconds,score_seconds,batch_inference_p95_ms,peak_rss_mb,precision_at_20,precision_at_50,benign_fp_per_1000,rank_stability_spearman"]
    for r in results:
        metrics_rows.append(
            f"{r.name},{r.eligible},{r.fit_seconds},{r.score_seconds},{r.batch_inference_p95_ms},{r.peak_rss_mb},"
            f"{r.precision_at_20},{r.precision_at_50},{r.benign_fp_per_1000},{r.rank_stability_spearman}"
        )
    (run_dir / "metrics.csv").write_text("\n".join(metrics_rows) + "\n", encoding="utf-8")

    report = {
        "run_id": run_id, "fixture": str(args.fixture), "fixture_manifest_sha256": fixture_manifest_sha256,
        "feature_manifest": package.manifest, "candidates": [asdict(r) for r in results], "selection": selection,
        "environment": environment_report(),
    }
    (run_dir / "comparison_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    lines = [
        f"# Phase 5B comparison report — {run_id}", "",
        "Synthetic-fixture evaluation only. Does not prove real-world detection accuracy.", "",
        f"Fixture: `{args.fixture}` (manifest sha256 `{fixture_manifest_sha256[:16]}…`)", "",
        f"Rows: {package.manifest['row_count']} (900s windows) — splits {package.manifest['split_counts']}", "",
        "## Candidates", "",
        "| Candidate | Eligible | Fit (s) | Score (s) | Batch p95 (ms) | Peak RSS (MB) | P@20 | P@50 | Benign FP/1000 | Stability |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {r.eligible} | {r.fit_seconds} | {r.score_seconds} | {r.batch_inference_p95_ms} | "
            f"{r.peak_rss_mb} | {r.precision_at_20} | {r.precision_at_50} | {r.benign_fp_per_1000} | {r.rank_stability_spearman} |"
        )
    lines += ["", "## Selection", "", f"```json\n{json.dumps(selection, indent=2, default=str)}\n```", ""]
    (run_dir / "comparison_report.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"[phase5b] done. selection: {selection['status']} ({selection.get('selected_candidate')})")
    print(f"[phase5b] report: {run_dir / 'comparison_report.md'}")


if __name__ == "__main__":
    main()
