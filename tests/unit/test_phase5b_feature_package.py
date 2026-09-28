from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses' type resolution needs the module registered first
    spec.loader.exec_module(module)
    return module


prepare = _load("phase5b_prepare_features")


def _complete_features(**overrides) -> dict:
    base = {
        "in_event_count": 3, "out_event_count": 1, "received_output_count": 3, "outgoing_output_count": 1,
        "observed_counterparties": 3,
        "received_value_distribution_sats": {"min_sats": 100, "max_sats": 300, "median_sats": 200, "total_sats": 600},
        "outgoing_value_distribution_sats": {"min_sats": 50, "max_sats": 50, "median_sats": 50, "total_sats": 50},
        "inter_event_gaps_seconds": {"count": 2, "minimum_seconds": 10.0, "maximum_seconds": 20.0, "median_seconds": 15.0},
        "bounded_component_change": {"scope": "one_hop_address_window", "node_delta": 4, "edge_delta": 6, "resolved_spend_edge_delta": 1},
        "first_observed_activity": False,
        "prior_window_gap_seconds": 3600.0, "baseline_in_event_count_mean": 2.0, "activity_surge_ratio": 1.5,
        "baseline_value_sats_mean": 400.0, "value_surge_ratio": 1.5,
        "peeling_chain_score": 0.0, "peeling_chain_length": 0, "peeling_chain_total_duration_sec": 0.0,
        "peeling_chain_evidence_count": 0,
        "coinjoin_like_score": 0.0, "equal_output_count": 0, "equal_output_value_sats": None,
        "coinjoin_like_evidence_count": 0,
    }
    base.update(overrides)
    return base


def test_flatten_produces_exact_frozen_column_order() -> None:
    flat = prepare.flatten_feature_row(_complete_features())
    assert list(flat.keys()) == list(prepare.FEATURE_COLUMNS)
    assert len(prepare.FEATURE_COLUMNS) == 37


def test_feature_columns_hash_is_deterministic_and_derived() -> None:
    first = prepare.feature_columns_sha256()
    second = prepare.feature_columns_sha256()
    assert first == second
    assert first == hashlib.sha256(",".join(prepare.FEATURE_COLUMNS).encode("utf-8")).hexdigest()


def test_first_observed_activity_imputes_neutral_ratios_not_zero() -> None:
    flat = prepare.flatten_feature_row(
        _complete_features(
            first_observed_activity=True, prior_window_gap_seconds=None, baseline_in_event_count_mean=None,
            activity_surge_ratio=None, baseline_value_sats_mean=None, value_surge_ratio=None,
        )
    )
    assert flat["first_observed_activity"] == 1.0
    assert flat["activity_surge_ratio"] == 1.0  # neutral "no change", not 0
    assert flat["value_surge_ratio"] == 1.0
    assert flat["prior_window_gap_seconds"] == 0.0
    assert flat["baseline_in_event_count_mean"] == 0.0


def test_independent_missingness_indicators() -> None:
    no_outgoing = prepare.flatten_feature_row(_complete_features(outgoing_value_distribution_sats=None))
    assert no_outgoing["outgoing_value_missing"] == 1.0
    assert no_outgoing["outgoing_value_total_sats"] == 0.0

    single_event = prepare.flatten_feature_row(
        _complete_features(inter_event_gaps_seconds={"count": 1, "minimum_seconds": None, "maximum_seconds": None, "median_seconds": None})
    )
    assert single_event["inter_event_gap_missing"] == 1.0
    assert single_event["inter_event_gap_min_seconds"] == 0.0

    no_equal_output = prepare.flatten_feature_row(_complete_features(equal_output_value_sats=None))
    assert no_equal_output["equal_output_value_missing"] == 1.0
    assert no_equal_output["equal_output_value_sats"] == 0.0


@pytest.mark.parametrize(
    "missing_key",
    ["received_value_distribution_sats", "inter_event_gaps_seconds", "bounded_component_change", "first_observed_activity", "in_event_count"],
)
def test_malformed_rows_are_rejected_not_guessed(missing_key: str) -> None:
    features = _complete_features()
    del features[missing_key]
    with pytest.raises(prepare.FeaturePackageError):
        prepare.flatten_feature_row(features)


def _export_row(*, window_seconds: int, features: dict, entity_ref: str = "address:bcrt1qtest") -> dict:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime.fromtimestamp(start.timestamp() + window_seconds, tz=UTC)
    return {"entity_ref": entity_ref, "window_start": start.isoformat(), "window_end": end.isoformat(), "features": features}


def test_build_feature_matrix_filters_to_900s_and_counts_rejections() -> None:
    rows = [
        _export_row(window_seconds=900, features=_complete_features()),
        _export_row(window_seconds=3600, features=_complete_features()),  # filtered out, wrong window size
        _export_row(window_seconds=900, features={"in_event_count": 1}),  # malformed, rejected and counted
    ]
    matrix = prepare.build_feature_matrix(rows)
    assert len(matrix.rows) == 1
    assert matrix.rejected_incomplete == 1
    assert matrix.columns == prepare.FEATURE_COLUMNS


def test_build_feature_matrix_refuses_an_empty_result() -> None:
    rows = [_export_row(window_seconds=3600, features=_complete_features())]
    with pytest.raises(prepare.FeaturePackageError):
        prepare.build_feature_matrix(rows)
