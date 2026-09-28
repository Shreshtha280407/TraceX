from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load("phase5b_prepare_features")
compare = _load("phase5b_compare")


def _synthetic_matrix(n_rows: int = 60, n_anomalies: int = 5) -> tuple[prepare.BuiltMatrix, np.ndarray]:
    """A small in-memory feature package — no 100K ingestion needed for this test."""
    rng = np.random.default_rng(42)
    columns = prepare.FEATURE_COLUMNS
    normal = rng.normal(loc=1.0, scale=0.3, size=(n_rows - n_anomalies, len(columns)))
    anomalous = rng.normal(loc=8.0, scale=0.5, size=(n_anomalies, len(columns)))
    raw = np.abs(np.vstack([normal, anomalous]))
    rows = [dict(zip(columns, values.tolist(), strict=True)) for values in raw]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    window_starts = [start + timedelta(minutes=15 * i) for i in range(n_rows)]
    entity_refs = [f"address:bcrt1qsynthetic{i:04d}" for i in range(n_rows)]
    matrix = prepare.BuiltMatrix(columns=columns, rows=rows, entity_refs=entity_refs, window_starts=window_starts)
    return matrix, raw


def test_sequential_execution_no_threading_or_multiprocessing_used() -> None:
    source = (REPO / "scripts" / "phase5b_compare.py").read_text(encoding="utf-8")
    for forbidden in ("import threading", "import multiprocessing", "concurrent.futures", "ThreadPoolExecutor", "ProcessPoolExecutor"):
        assert forbidden not in source, f"{forbidden!r} found — candidates must run one at a time, not concurrently"


def test_candidates_run_sequentially_and_produce_valid_results() -> None:
    from sklearn.ensemble import IsolationForest
    from sklearn.neighbors import LocalOutlierFactor

    _matrix, raw = _synthetic_matrix()
    train = raw[:40]

    order: list[str] = []
    for name, factory in (
        ("isolation_forest", lambda seed: IsolationForest(random_state=seed, n_estimators=20)),
        ("local_outlier_factor_novelty", lambda _seed: LocalOutlierFactor(novelty=True, n_neighbors=5)),
    ):
        order.append(name)  # a real orchestrator appends here only after the previous candidate fully returns
        result, scores = compare.run_sklearn_candidate(
            name=name, version="test", parameters={}, estimator_factory=factory, x_train=train, x_all=raw, seed=42
        )
        assert result.eligible
        assert result.fit_seconds is not None and result.fit_seconds >= 0
        assert result.score_seconds is not None
        assert result.peak_rss_mb is not None and result.peak_rss_mb > 0
        assert scores is not None and len(scores) == len(raw)
    assert order == ["isolation_forest", "local_outlier_factor_novelty"]


def test_rule_baseline_scores_flagged_rows_higher_than_unflagged() -> None:
    matrix, _ = _synthetic_matrix(n_rows=10, n_anomalies=0)
    key0 = (matrix.entity_refs[0].removeprefix("address:"), matrix.window_starts[0])
    package = prepare.FeaturePackage(
        matrix=matrix, splits=["validation"] * 10, label_positives={}, label_dropped={},
        baseline_rank_by_key={key0: 1}, manifest={},
    )
    result, scores = compare.run_rule_baseline(package)
    assert result.eligible
    assert scores[0] > 0
    assert all(score == 0 for score in scores[1:])


def test_evaluate_ranking_precision_and_benign_fp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compare, "REVIEW_BUDGET", 2)  # so "top budget" is a real subset of the 10 rows below
    keys = [(f"addr{i}", datetime(2026, 1, 1, tzinfo=UTC)) for i in range(10)]
    scores = [10 - i for i in range(10)]  # rank 0 highest score
    split_mask = [True] * 10
    review_positive_keys = {keys[0], keys[1]}  # both land in top-2
    benign_keys = {keys[9]}  # lands last, not in the top-2 review budget
    metrics = compare.evaluate_ranking(
        scores=scores, keys=keys, split_mask=split_mask, review_positive_keys=review_positive_keys,
        benign_keys=benign_keys, review_positive_dropped=0, benign_dropped=0,
    )
    assert metrics["precision_at_20"] == pytest.approx(2 / 10)  # only 10 rows exist, all in top 20
    assert metrics["benign_fp_per_1000"] == 0  # benign key wasn't in the reviewed top budget


def test_evaluate_ranking_reports_unavailable_when_no_positive_labels() -> None:
    keys = [(f"addr{i}", datetime(2026, 1, 1, tzinfo=UTC)) for i in range(5)]
    metrics = compare.evaluate_ranking(
        scores=[1, 2, 3, 4, 5], keys=keys, split_mask=[True] * 5, review_positive_keys=set(),
        benign_keys=set(), review_positive_dropped=3, benign_dropped=1,
    )
    assert metrics["precision_at_20"] == "unavailable"
    assert metrics["benign_fp_per_1000"] == "unavailable"
    assert metrics["review_positive_dropped"] == 3


def test_selection_records_no_model_selected_when_nothing_clears_gates() -> None:
    candidates = [
        compare.CandidateResult(name="isolation_forest", version="t", parameters={}, eligible=False, ineligible_reason="fit too slow"),
        compare.CandidateResult(name="local_outlier_factor_novelty", version="t", parameters={}, eligible=False, ineligible_reason="OOM"),
    ]
    selection = compare.apply_selection_rule(candidates, baseline_benign_fp=5.0)
    assert selection["status"] == "no_model_selected"
    assert selection["selected_candidate"] is None


def test_selection_chooses_best_validation_precision_when_labels_available() -> None:
    weaker = compare.CandidateResult(
        name="local_outlier_factor_novelty", version="t", parameters={}, eligible=True,
        peak_rss_mb=100, benign_fp_per_1000=4.0, precision_at_20=0.1,
    )
    stronger = compare.CandidateResult(
        name="isolation_forest", version="t", parameters={}, eligible=True,
        peak_rss_mb=100, benign_fp_per_1000=4.0, precision_at_20=0.4,
    )
    selection = compare.apply_selection_rule([weaker, stronger], baseline_benign_fp=5.0)
    assert selection["status"] == "selected"
    assert selection["selected_candidate"] == "isolation_forest"


def test_selection_falls_back_to_stability_when_labels_unavailable_and_flags_unknown_accuracy() -> None:
    less_stable = compare.CandidateResult(
        name="local_outlier_factor_novelty", version="t", parameters={}, eligible=True,
        peak_rss_mb=100, benign_fp_per_1000=4.0, precision_at_20="unavailable", score_seconds=1.0, rank_stability_spearman=0.5,
    )
    more_stable = compare.CandidateResult(
        name="isolation_forest", version="t", parameters={}, eligible=True,
        peak_rss_mb=100, benign_fp_per_1000=4.0, precision_at_20="unavailable", score_seconds=1.0, rank_stability_spearman=0.99,
    )
    selection = compare.apply_selection_rule([less_stable, more_stable], baseline_benign_fp=5.0)
    # "selected_provisional", not "selected" — labels were unavailable, so this must be
    # visibly distinct from a real label-based pick, not the same status under one name.
    assert selection["status"] == "selected_provisional"
    assert selection["selected_candidate"] == "isolation_forest"
    assert selection["real_world_accuracy"] == "unknown"


def test_selection_gates_out_excessive_benign_false_positive_rate() -> None:
    excessive = compare.CandidateResult(
        name="isolation_forest", version="t", parameters={}, eligible=True,
        peak_rss_mb=100, benign_fp_per_1000=100.0, precision_at_20=0.9,
    )
    selection = compare.apply_selection_rule([excessive], baseline_benign_fp=5.0)
    assert selection["status"] == "no_model_selected"
    assert "isolation_forest" in selection["gate_notes"]


def test_full_synthetic_run_writes_the_required_output_files(tmp_path: Path) -> None:
    """Exercises the same sequence scripts/phase5b_compare.py::main() performs
    (baseline + 2 candidates -> metrics -> selection -> report files), on a small
    synthetic package, and checks every required output file/key gets created."""
    from sklearn.ensemble import IsolationForest
    from sklearn.neighbors import LocalOutlierFactor
    from sklearn.preprocessing import StandardScaler

    matrix, raw = _synthetic_matrix()
    train_mask = [True] * 40 + [False] * 20
    validation_mask = [False] * 40 + [True] * 10 + [False] * 10
    scaler = StandardScaler().fit(raw[train_mask])
    scaled = scaler.transform(raw)
    keys = list(zip([ref.removeprefix("address:") for ref in matrix.entity_refs], matrix.window_starts, strict=True))

    results = []
    baseline_result, baseline_scores = compare.run_rule_baseline(
        prepare.FeaturePackage(matrix=matrix, splits=[], label_positives={}, label_dropped={}, baseline_rank_by_key={}, manifest={})
    )
    baseline_metrics = compare.evaluate_ranking(
        scores=baseline_scores, keys=keys, split_mask=validation_mask, review_positive_keys=set(),
        benign_keys=set(), review_positive_dropped=0, benign_dropped=0,
    )
    baseline_result.precision_at_20 = baseline_metrics["precision_at_20"]
    baseline_result.benign_fp_per_1000 = baseline_metrics["benign_fp_per_1000"]
    results.append(baseline_result)

    for name, factory in (
        ("isolation_forest", lambda seed: IsolationForest(random_state=seed, n_estimators=20)),
        ("local_outlier_factor_novelty", lambda _seed: LocalOutlierFactor(novelty=True, n_neighbors=5)),
    ):
        result, scores = compare.run_sklearn_candidate(
            name=name, version="test", parameters={}, estimator_factory=factory,
            x_train=scaled[train_mask], x_all=scaled, seed=42,
        )
        metrics = compare.evaluate_ranking(
            scores=scores, keys=keys, split_mask=validation_mask, review_positive_keys=set(),
            benign_keys=set(), review_positive_dropped=0, benign_dropped=0,
        )
        result.precision_at_20 = metrics["precision_at_20"]
        result.benign_fp_per_1000 = metrics["benign_fp_per_1000"]
        results.append(result)

    selection = compare.apply_selection_rule(results, baseline_result.benign_fp_per_1000)

    run_dir = tmp_path / "run"
    (run_dir / "candidate_logs").mkdir(parents=True)
    for result in results:
        (run_dir / "candidate_logs" / f"{result.name}.json").write_text(json.dumps(result.__dict__, default=str), encoding="utf-8")
    (run_dir / "model_selection.json").write_text(json.dumps(selection, default=str), encoding="utf-8")
    (run_dir / "environment.json").write_text(json.dumps(compare.environment_report()), encoding="utf-8")

    assert (run_dir / "model_selection.json").is_file()
    assert (run_dir / "environment.json").is_file()
    assert len(list((run_dir / "candidate_logs").glob("*.json"))) == 3
    written_selection = json.loads((run_dir / "model_selection.json").read_text(encoding="utf-8"))
    assert written_selection["status"] in ("selected", "selected_provisional", "no_model_selected")
