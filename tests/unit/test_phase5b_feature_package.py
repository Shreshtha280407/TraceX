from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    # Reuse an already-loaded module rather than re-executing it: another test file
    # in this same pytest process may have already called _load(name), and a second
    # independent load rebinds sys.modules[name] to a different object — then
    # multiprocessing's spawn pickling of a module-level function fails with
    # "it's not the same object as <module>.<func>" because the two module
    # instances' functions aren't identical, even though they're byte-identical.
    if name in sys.modules:
        return sys.modules[name]
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


compare = _load("phase5b_compare")


def _tx(prefix: str, number: int) -> str:
    return f"{prefix}{number:060x}"


def _source_timestamp_only_records() -> tuple[dict, dict, set]:
    """Real ingestion commonly carries a fact's time as source_timestamp, not
    block_time (see app/engine/findings/deterministic.py::_coverage's own
    source_time_used_when_block_time_missing counter). Every transaction fact here
    has ONLY source_timestamp — no block_time key at all — to prove the label join
    survives that, not just the happy path where block_time is always present."""
    transactions: list[dict] = []
    inputs: list[dict] = []
    outputs: list[dict] = []
    feature_keys: set[tuple[str, object]] = set()
    base = datetime(2026, 1, 1, tzinfo=UTC)

    def stamp(offset_seconds: int) -> str:
        return (base + timedelta(seconds=offset_seconds)).isoformat()

    def add_tx(txid: str, offset_seconds: int) -> None:
        transactions.append({"txid": txid, "source_timestamp": stamp(offset_seconds), "network": "bitcoin-regtest"})

    def window_key(address: str, offset_seconds: int) -> tuple[str, object]:
        return (address, prepare.window_bucket_start(base + timedelta(seconds=offset_seconds)))

    # --- one verified 3-hop peeling chain: seed -> hop1(+pay1) -> hop2(+pay2) -> hop3(+pay3) ---
    peeling_txids = [_tx("peel", i) for i in range(4)]
    amounts = [100_000, 90_000, 80_000, 70_000]
    addresses = ["seed", "hop1", "hop2", "hop3"]
    for index, amount in enumerate(amounts):
        add_tx(peeling_txids[index], index * 60)
        outputs.append({"txid": peeling_txids[index], "vout": 0, "address": addresses[index], "amount_sats": amount})
        if index:
            inputs.append({"txid": peeling_txids[index], "vin": 0, "prev_txid": peeling_txids[index - 1], "prev_vout": 0})
    peeling_expected_txids = peeling_txids[1:]  # detect_peeling_chains reports the 3 verified hop txids
    feature_keys.add(window_key("seed", 60))  # the chain's finding keys off its origin address + first-hop time

    # --- 35 distinct coinjoin-like transactions: 3 inputs, 3 equal-valued outputs + 1 differing ---
    coinjoin_txids = []
    for i in range(35):
        txid = _tx("cjtx", i)
        coinjoin_txids.append(txid)
        offset = 10_000 + i * 60
        add_tx(txid, offset)
        for vin in range(3):
            inputs.append({"txid": txid, "vin": vin})
        equal_address = f"bcrt1qcj{i:04d}"
        outputs.append({"txid": txid, "vout": 0, "address": equal_address, "amount_sats": 50_000})
        outputs.append({"txid": txid, "vout": 1, "address": f"bcrt1qcjb{i:04d}", "amount_sats": 50_000})
        outputs.append({"txid": txid, "vout": 2, "address": f"bcrt1qcjc{i:04d}", "amount_sats": 50_000})
        outputs.append({"txid": txid, "vout": 3, "address": f"bcrt1qcjfee{i:04d}", "amount_sats": 1_000})
        feature_keys.add(window_key(equal_address, offset))  # resolve_via_own_outputs picks the first output

    # --- 3 ordinary benign-control transactions (no peeling/coinjoin shape) ---
    benign_txids = []
    for i in range(3):
        txid = _tx("benign", i)
        benign_txids.append(txid)
        offset = 50_000 + i * 60
        add_tx(txid, offset)
        address = f"bcrt1qbenign{i:04d}"
        outputs.append({"txid": txid, "vout": 0, "address": address, "amount_sats": 25_000})
        outputs.append({"txid": txid, "vout": 1, "address": f"bcrt1qbenignchange{i:04d}", "amount_sats": 5_000})
        feature_keys.add(window_key(address, offset))

    records = {"transactions": transactions, "inputs": inputs, "outputs": outputs}
    truth = {
        "expected_peeling_chain_transaction_ids": peeling_expected_txids,
        "expected_coinjoin_like_transaction_ids": coinjoin_txids,
        "expected_benign_controls": {"ordinary_sequence_transaction_ids": benign_txids, "ordinary_multi_output_transaction_ids": []},
    }
    return records, truth, feature_keys


def test_label_join_survives_source_timestamp_only_records_regression() -> None:
    """Regression test for the bug where transaction_times only read block_time:
    every real label got dropped on ingested facts that carry source_timestamp
    instead — not because labels are sparse, but because the join silently found
    no usable time for any transaction and dropped everything."""
    records, truth, feature_keys = _source_timestamp_only_records()
    # Prove every transaction fact really has no block_time at all — this exercises
    # the fallback branch, not the happy path.
    assert all("block_time" not in t for t in records["transactions"])

    result = prepare.resolve_label_positives(records=records, truth=truth, feature_keys=feature_keys)

    # Peeling: all 3 labeled hop txids resolve (0 dropped). They collapse to ONE
    # address-window key by design — a peeling-chain finding keys off the chain's
    # origin address once, exactly like materialize_findings represents it — so
    # this is the correct, non-buggy shape, not a partial failure.
    assert result.dropped_counts["peeling_chain"] == 0
    assert len(result.positive_keys["peeling_chain"]) == 1

    # CoinJoin: 35 distinct transactions -> 35 distinct resolved keys, 0 dropped.
    assert result.dropped_counts["coinjoin_like"] == 0
    assert len(result.positive_keys["coinjoin_like"]) == 35

    # Benign controls: 3 distinct transactions -> 3 distinct resolved keys, 0 dropped.
    assert result.dropped_counts["benign_ordinary_sequence"] == 0
    assert len(result.positive_keys["benign_ordinary_sequence"]) == 3

    # And with real positives resolved, the metrics that were "unavailable" before
    # the fix now come back as real numbers.
    review_positive_keys = result.positive_keys["peeling_chain"] | result.positive_keys["coinjoin_like"]
    benign_keys = result.positive_keys["benign_ordinary_sequence"]
    all_keys = list(review_positive_keys | benign_keys) + [("bcrt1qother", "2026-01-01T00:00:00+00:00")]
    scores = [1.0] * len(all_keys)
    metrics = compare.evaluate_ranking(
        scores=scores, keys=all_keys, split_mask=[True] * len(all_keys),
        review_positive_keys=review_positive_keys, benign_keys=benign_keys,
        review_positive_dropped=0, benign_dropped=0,
    )
    assert metrics["precision_at_20"] != "unavailable"
    assert metrics["precision_at_50"] != "unavailable"
    assert metrics["benign_fp_per_1000"] != "unavailable"
