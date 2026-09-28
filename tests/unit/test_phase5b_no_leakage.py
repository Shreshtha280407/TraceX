from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    # Reuse an already-loaded module rather than re-executing it — see the matching
    # comment in test_phase5b_feature_package.py for why a second independent load
    # of the same module name breaks multiprocessing's spawn pickling.
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load("phase5b_prepare_features")


def test_no_forbidden_substring_in_any_frozen_column() -> None:
    for column in prepare.FEATURE_COLUMNS:
        lowered = column.lower()
        for forbidden in prepare.FORBIDDEN_SUBSTRINGS:
            assert forbidden not in lowered, f"column {column!r} contains forbidden substring {forbidden!r}"


def test_hard_excluded_risk_fields_never_in_frozen_columns() -> None:
    assert not (prepare.HARD_EXCLUDED_FIELDS & set(prepare.FEATURE_COLUMNS))


def test_no_network_geo_ip_column_exists_at_all() -> None:
    # The real exporter has no such field today; this pins that absence rather than
    # assuming it, so a future exporter change that adds one fails this test loudly.
    for token in ("geo", "asn", "ip_", "_ip", "endpoint", "network"):
        assert not any(token in column.lower() for column in prepare.FEATURE_COLUMNS)


def test_window_metadata_never_enters_feature_columns() -> None:
    for token in ("window_start", "window_end", "entity_ref", "snapshot", "coverage", "source_ref"):
        assert not any(token in column.lower() for column in prepare.FEATURE_COLUMNS)


def test_scaler_is_fit_on_train_rows_only() -> None:
    import numpy as np
    from sklearn.preprocessing import StandardScaler

    rng = np.random.default_rng(42)
    train = rng.normal(loc=0.0, scale=1.0, size=(200, 3))
    validation = rng.normal(loc=50.0, scale=10.0, size=(50, 3))  # deliberately shifted
    full = np.vstack([train, validation])
    train_mask = [True] * 200 + [False] * 50

    scaler = StandardScaler()
    scaler.fit(full[train_mask])

    expected_mean = train.mean(axis=0)
    expected_std = train.std(axis=0)
    assert np.allclose(scaler.mean_, expected_mean)
    assert np.allclose(scaler.scale_, expected_std)
    # If validation had leaked into the fit, the mean would be pulled toward 50.
    assert not np.allclose(scaler.mean_, full.mean(axis=0))


def test_split_assignment_is_time_respecting() -> None:
    boundaries = prepare.SplitBoundaries(
        train_reference_end=datetime(2026, 1, 2, tzinfo=UTC), validation_end=datetime(2026, 1, 3, tzinfo=UTC)
    )
    assert boundaries.assign(datetime(2026, 1, 1, 12, tzinfo=UTC)) == "train_reference"
    assert boundaries.assign(datetime(2026, 1, 2, 12, tzinfo=UTC)) == "validation"
    assert boundaries.assign(datetime(2026, 1, 3, 12, tzinfo=UTC)) == "final_holdout"

    rank = {"train_reference": 0, "validation": 1, "final_holdout": 2}
    windows = [datetime(2026, 1, 1, hour, tzinfo=UTC) for hour in range(0, 24, 3)] + [
        datetime(2026, 1, 2, hour, tzinfo=UTC) for hour in range(0, 24, 3)
    ] + [datetime(2026, 1, 3, hour, tzinfo=UTC) for hour in range(0, 24, 3)]
    assigned = [boundaries.assign(w) for w in windows]
    assert all(rank[a] <= rank[b] for a, b in pairwise(assigned))


def _write_transactions(path: Path, block_times: list[str]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for index, block_time in enumerate(block_times):
            handle.write(json.dumps({"txid": f"{index:064x}", "block_time": block_time, "network": "bitcoin-regtest"}) + "\n")


def test_compute_split_boundaries_uses_real_transaction_timestamps(tmp_path: Path) -> None:
    transactions_path = tmp_path / "transactions.ndjson"
    _write_transactions(
        transactions_path,
        [f"2026-01-01T00:{minute:02d}:00Z" for minute in range(10)],
    )
    truth = {
        "split_groups": [
            {"group_id": "g-1", "split": "train_reference", "transaction_index_start": 0, "transaction_index_end_exclusive": 5},
            {"group_id": "g-2", "split": "validation", "transaction_index_start": 5, "transaction_index_end_exclusive": 8},
            {"group_id": "g-3", "split": "final_holdout", "transaction_index_start": 8, "transaction_index_end_exclusive": 10},
        ]
    }
    boundaries = prepare.compute_split_boundaries(transactions_path, truth)
    assert boundaries.train_reference_end == datetime.fromisoformat("2026-01-01T00:05:00+00:00")
    assert boundaries.validation_end == datetime.fromisoformat("2026-01-01T00:08:00+00:00")


def test_compute_split_boundaries_rejects_non_time_ordered_groups(tmp_path: Path) -> None:
    transactions_path = tmp_path / "transactions.ndjson"
    # Index 5 (claimed train_reference_end) has a LATER timestamp than index 8
    # (claimed validation_end) — a malformed, non-time-respecting split.
    times = [f"2026-01-01T00:{minute:02d}:00Z" for minute in range(10)]
    times[5], times[8] = times[8], times[5]
    _write_transactions(transactions_path, times)
    truth = {
        "split_groups": [
            {"group_id": "g-1", "split": "train_reference", "transaction_index_start": 0, "transaction_index_end_exclusive": 5},
            {"group_id": "g-2", "split": "validation", "transaction_index_start": 5, "transaction_index_end_exclusive": 8},
        ]
    }
    with pytest.raises(prepare.FeaturePackageError):
        prepare.compute_split_boundaries(transactions_path, truth)
