from __future__ import annotations

import numpy as np
import pytest

from app.ml import grains
from app.ml.facts import Facts, truncate_facts
from scripts.ml_controlled_study import causal_facts, cluster_interval, feature_groups, stress_facts


def test_research_features_ignore_future_prevouts_and_equal_time_peers():
    facts = Facts(txids=["a", "b", "c", "d"], tx_index={v: i for i, v in enumerate("abcd")},
        tx_time=np.array([10, 20, 20, 40]), tx_fee=np.zeros(4, dtype=np.int64),
        out_tx=np.arange(4, dtype=np.int32), out_vout=np.zeros(4, dtype=np.int32),
        out_value=np.array([100, 80, 70, 100000]), out_addr=np.zeros(4, dtype=np.int32),
        out_script=np.zeros(4, dtype=np.int8), out_spent_by=np.array([1, 2, -1, 0]),
        in_tx=np.array([0, 1, 2], dtype=np.int32), in_prev=np.array([3, 0, 1], dtype=np.int32),
        addresses=["address"], addr_index={"address": 0},
        tx_src_ip=np.array([0, 0, 0, 1]), tx_asn=np.array([0, 0, 0, 1]),
        tx_country=np.array([0, 0, 0, 1]), src_ips=["relay", "future"], asns=["1", "2"], countries=["AA", "BB"])

    def groups(source):
        source = causal_facts(source)
        table = grains.build_transaction_table(source)
        zero = np.zeros(source.transaction_count)
        return feature_groups(source, table, zero, zero)

    full, truncated = groups(facts), groups(truncate_facts(facts, 20))
    for name, (values, _) in truncated.items():
        np.testing.assert_allclose(values, full[name][0][:3], equal_nan=True)
    assert causal_facts(facts).in_prev.tolist() == [-1, 0, -1]
    assert full["causal_graph"][0][1, 7] == full["causal_graph"][0][2, 7] == 1
    assert full["prior_history"][0][1, 0] == pytest.approx(np.log(2))
    assert full["prior_history"][0][2, 0] == pytest.approx(np.log(2))
    missing = stress_facts(facts, "missing_network_prevouts")
    assert missing.tx_src_ip[0] == -1 and facts.tx_src_ip[0] == 0
    skewed = stress_facts(facts, "timing_endpoint_skew")
    assert np.all(skewed.tx_time % 60 == 0)


def test_numeric_streaming_handles_null_wallets_and_empty_tables():
    import duckdb

    from app.engine.analytics import _numeric_columns

    con = duckdb.connect()
    try:
        ids, wallets = _numeric_columns(con, "SELECT * FROM (VALUES (1, NULL), (2, 3)) t(id, wallet)",
                                       dtypes=[np.int64, np.float64])
        assert ids.tolist() == [1, 2]
        assert np.isnan(wallets[0]) and wallets[1] == 3
        assert _numeric_columns(con, "SELECT 1::BIGINT AS n WHERE false")[0].size == 0
    finally:
        con.close()


def test_sql_window_history_matches_reference_with_skew_and_signal_windows(tmp_path):
    from collections import defaultdict
    from datetime import UTC, datetime, timedelta

    from app.engine.bounded import FactStore, _address_history_sql, _to_micros
    from app.engine.findings import deterministic as det

    store = FactStore(tmp_path / "history")
    events = defaultdict(list)
    epoch = datetime(1969, 12, 31, 23, 50, 0, 900000, tzinfo=UTC)
    outputs, times = [], []
    for seq in range(90):
        txid = f"tx-{seq // 3}"
        observed = epoch + timedelta(seconds=(seq // 3) * 1000)
        output = {"txid": txid, "vout": seq % 3, "amount_sats": seq * 12345 + 1}
        outputs.append((seq, txid, "hot", output["amount_sats"]))
        if seq % 3 == 0:
            times.append((txid, _to_micros(observed)))
        for seconds in det.WINDOW_SECONDS:
            events[("hot", seconds, det._window(observed, seconds)[0])].append(output)
    # A signal can add a real output to a different time/address window.
    observed = epoch + timedelta(days=2)
    extra = {"txid": "tx-0", "vout": 0, "amount_sats": 1}
    det.add_window_signal(events, {}, defaultdict(list), "signal-only", observed, {}, extra)
    try:
        con = store.con
        con.execute("CREATE TABLE outputs(seq BIGINT, txid VARCHAR, addr_key VARCHAR, amount BIGINT)")
        con.executemany("INSERT INTO outputs VALUES (?, ?, ?, ?)", outputs)
        con.execute("CREATE TABLE tx_time(txid VARCHAR, us BIGINT)")
        con.executemany("INSERT INTO tx_time VALUES (?, ?)", times)
        con.execute("CREATE TABLE contrib(addr VARCHAR, us BIGINT, out_seq BIGINT)")
        con.executemany("INSERT INTO contrib VALUES (?, ?, ?)", [("signal-only", _to_micros(observed), 0)] * 2)
        assert _address_history_sql(store, partition=0, partitions=1) == det._history_features(events)
        combined = {}
        for partition in range(7):
            combined.update(_address_history_sql(store, partition=partition, partitions=7))
        assert combined == det._history_features(events)
    finally:
        store.close()


@pytest.mark.parametrize("partitions", [1, 7, 48])
def test_cached_rapid_spends_matches_original_join_and_restores_limits(tmp_path, partitions):
    import json

    import duckdb

    from app.engine.bounded import FactStore, _prepare_rapid_spends

    store = FactStore(tmp_path / "rapid-parity")
    try:
        con = store.con
        con.execute("CREATE TABLE inputs(seq BIGINT,j VARCHAR,txid VARCHAR,prev_txid VARCHAR,prev_vout BIGINT)")
        con.execute("CREATE TABLE outputs(seq BIGINT,j VARCHAR,txid VARCHAR,vout BIGINT,address VARCHAR)")
        con.execute("CREATE TABLE tx_time(txid VARCHAR,us BIGINT)")
        delays = [0, 3600000000, 3600000001, -1, None, 100, 100]
        for index, delay in enumerate(delays):
            address = "hot" if index < 5 else (None if index == 5 else "")
            raw = json.dumps({"txid": f"p{index}", "amount_sats": index + 1, "address": address})
            con.execute("INSERT INTO outputs VALUES (?,?,?,?,?)", [index, raw, f"p{index}", 0, address])
            con.execute("INSERT INTO tx_time VALUES (?,?)", [f"p{index}", -1000000])
            con.execute("INSERT INTO tx_time VALUES (?,?)", [f"s{index}", None if delay is None else -1000000 + delay])
            con.execute("INSERT INTO inputs VALUES (?,?,?,?,?)", [index, json.dumps({"txid": f"s{index}"}), f"s{index}", f"p{index}", 0])
        # Duplicate spender and missing/unresolvable prevouts stay exactly as
        # in the reference relation; caching must not infer or de-duplicate.
        con.execute("INSERT INTO inputs VALUES (7,'duplicate','s0','p0',0),(8,'missing','s0','unknown',0),"
                    "(9,'null','s0','p0',NULL)")
        reference = """
            SELECT i.seq,i.j,po.seq,po.j,po.address,ts.us,tr.us FROM inputs i
            JOIN outputs po ON po.txid=i.prev_txid AND po.vout=i.prev_vout
            JOIN tx_time ts ON ts.txid=i.txid JOIN tx_time tr ON tr.txid=i.prev_txid
            WHERE i.prev_txid IS NOT NULL AND i.prev_vout IS NOT NULL AND NULLIF(po.address,'') IS NOT NULL
              AND ts.us IS NOT NULL AND tr.us IS NOT NULL AND ts.us-tr.us BETWEEN 0 AND 3600000000
        """
        original = con.execute("SELECT current_setting('memory_limit'),current_setting('threads')").fetchone()
        _prepare_rapid_spends(store, partitions=partitions)
        assert con.execute("SELECT current_setting('memory_limit'),current_setting('threads')").fetchone() == original
        total = 0
        for partition in range(partitions):
            expected = con.execute(reference + f" AND hash(po.address)%{partitions}={partition} ORDER BY i.seq").fetchall()
            actual = con.execute("SELECT seq,input_raw,previous_seq,previous_raw,address,spent_us,received_us "
                                 "FROM rapid_spends WHERE bucket=? ORDER BY seq", [partition]).fetchall()
            assert actual == expected
            total += len(actual)
        assert total == 3
        with pytest.raises(duckdb.Error):
            _prepare_rapid_spends(store, partitions=partitions)  # Existing cache: native SQL failure.
        assert con.execute("SELECT current_setting('memory_limit'),current_setting('threads')").fetchone() == original
    finally:
        store.close()


def test_disk_admission_preserves_reserve_without_deleting_files(tmp_path, monkeypatch):
    import shutil

    from app import resources

    marker = tmp_path / "evidence"
    marker.write_bytes(b"immutable")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(1000, 900, 100))
    with pytest.raises(RuntimeError, match="disk admission failed"):
        resources.admit_disk_allocation("test", tmp_path, 80, reserve_bytes=30)
    resources.admit_disk_allocation("test", tmp_path, 70, reserve_bytes=30)
    assert marker.read_bytes() == b"immutable"


def test_cluster_bootstrap_resamples_whole_episodes_deterministically():
    score = np.asarray([.9, .8, .3, .2, .6, .5])
    truth = np.asarray([True, True, False, False, True, False])
    groups = np.asarray(["episode-a", "episode-a", "episode-b", "episode-b", "entity-c", "entity-c"])
    mask = np.ones(6, dtype=bool)
    first = cluster_interval(score, truth, mask, groups, 30)
    assert first == cluster_interval(score, truth, mask, groups, 30)
    assert first["clusters"] == 3 and first["replicates"] <= 30
    assert first["ap_95"][0] <= first["ap_95"][1]


def test_selection_rejects_final_outcomes_and_discrimination_regression():
    from scripts.freeze_research_selection import select_candidate

    def report(a, b):
        return {"results": {"motif": {"E4_xgboost": {"ap": a}}, "surge": {"E4_xgboost": {"ap": a}},
                "discrimination": {"E4_xgboost": {"ap": b}, "v2_deployed_procedure": {"ap": .9}}}}

    assert select_candidate([report(.85, .9), report(.82, .92)])[0] == "E4_xgboost"
    with pytest.raises(ValueError, match="No candidate"):
        select_candidate([report(.99, .7)])


def test_final_evaluation_requires_prior_reservation(tmp_path):
    from scripts.ml_controlled_study import run

    with pytest.raises(ValueError, match="pre-registered"):
        run([tmp_path], tmp_path / "result.json", final_candidate="E4_xgboost")
    assert not (tmp_path / ".reserved_final_evaluation.json").exists()


def test_completed_bucket_burst_has_no_future_warmup_leak():
    from types import SimpleNamespace

    from scripts.ml_controlled_study import causal_burst_raw

    times = np.asarray([0, 901, 1801, 2701, 3601, 4501, 5401, 6301, 6302, 6303])
    full = causal_burst_raw(SimpleNamespace(tx_time=times, transaction_count=len(times)), {"motif": np.ones(len(times))})
    truncated = causal_burst_raw(SimpleNamespace(tx_time=times[:3], transaction_count=3), {"motif": np.ones(3)})
    np.testing.assert_allclose(full[:3], truncated)


def test_local_if_sensitivity_is_bounded_and_does_not_change_features():
    from app.ml.findings import local_if_sensitivity
    from app.ml.grains import Table

    matrix = np.arange(300, dtype=np.float32).reshape(100, 3)
    original = matrix.copy()
    table = Table(matrix=matrix, columns=("a", "b", "c"))
    reference = np.arange(100) < 80
    results = local_if_sensitivity(table, reference, np.arange(100), limit=2)
    assert list(results) == [0, 1]
    assert set(results[0]["score_decrease_by_feature"]) == {"a", "b", "c"}
    np.testing.assert_array_equal(matrix, original)


def test_generator_counts_are_independent_and_reconcile_duplicates(tmp_path):
    import json

    from scripts.dataset_acceptance_counts import counts

    row = {"txid": "a" * 64, "inputs": [{}, {}], "outputs": [{}]}
    source = tmp_path / "source.ndjson"
    source.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    expected, total = counts(source)
    assert total == 2 and expected == {"transactions": 1, "inputs": 2, "outputs": 1,
                                     "network_observations": 1, "quarantine": 1}


def test_worker_admission_accounts_for_native_export_reserve(monkeypatch):
    from app.resources import current_plan

    monkeypatch.setenv("TRACEX_MEMORY_BUDGET_MB", "5120")
    monkeypatch.setenv("TRACEX_WORKERS", "8")
    plan = current_plan()
    assert plan.supported_workers() <= 4
    with pytest.raises(RuntimeError, match="worker admission failed"):
        plan.worker_processes(records=300000)
    monkeypatch.setenv("TRACEX_WORKERS", "4")
    assert plan.worker_processes(records=300000) == min(4, plan.cpu_count)


def test_reference_calibration_is_time_ordered_even_with_unsorted_source():
    from scripts.ml_controlled_study import reference_masks

    times = np.asarray([100, 10, 40, 20, 100, 30, 50, 60, 70, 80, 999])
    fit, calibration = reference_masks(times, times < 999)
    assert times[fit].max() < times[calibration].min()
    assert calibration[0] and calibration[4] and not fit[-1] and not calibration[-1]


@pytest.mark.parametrize("identity", ["confidence-v1", "../../.env", "bad/name"])
def test_calibration_diagnostics_cannot_reuse_active_identity_or_path(monkeypatch, tmp_path, identity):
    import sys

    from scripts import calibrate_confidence

    marker = tmp_path / ".env"
    marker.write_text("retained")
    monkeypatch.setattr(sys, "argv", ["calibrate", "--database", str(tmp_path / "absent.db"),
                                     "--calibration-id", identity])
    with pytest.raises(SystemExit) as error:
        calibrate_confidence.main()
    assert error.value.code == 2 and marker.read_text() == "retained"


def test_calibration_fit_cannot_write_active_registry(monkeypatch, tmp_path):
    import sys

    from scripts import calibrate_confidence

    active = calibrate_confidence.REPO / "app" / "engine" / "calibration" / "never-created-review.json"
    monkeypatch.setattr(sys, "argv", ["calibrate", "--database", str(tmp_path / "absent.db"),
                                     "--calibration-id", "new-diagnostic", "--output", str(active)])
    with pytest.raises(SystemExit) as error:
        calibrate_confidence.main()
    assert error.value.code == 2 and not active.exists()


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("phase", ["graph", "peeling"])
def test_graph_phase_uses_joint_budget_and_restores_detector_limits(monkeypatch, tmp_path, failure, phase):
    from app.engine import bounded

    monkeypatch.setenv("TRACEX_MEMORY_BUDGET_MB", "2048")
    store = bounded.FactStore(tmp_path / "phase-budget")
    original = store.con.execute("SELECT current_setting('memory_limit'), current_setting('threads')").fetchone()

    def graph(*args, **kwargs):
        # Read actual native settings, not a mock of the wrapper's SQL.
        configured = dict(store.con.execute("SELECT name,value FROM duckdb_settings() WHERE name IN ('memory_limit','threads')").fetchall())
        assert int(configured["threads"]) <= 4
        assert configured["memory_limit"] != original[0]
        if failure:
            raise RuntimeError("graph fixture failure")
        return "graph-complete"

    monkeypatch.setattr(bounded, "_build_graph_bounded" if phase == "graph" else "_peeling_sql", graph)
    def invoke():
        if phase == "graph":
            return bounded.build_graph_bounded(None, store=store, evidence_root=tmp_path, snapshot=None)
        return bounded._peeling(store, 8, 3)
    try:
        if failure:
            with pytest.raises(RuntimeError, match="graph fixture failure"):
                invoke()
        else:
            assert invoke() == "graph-complete"
        assert store.con.execute("SELECT current_setting('memory_limit'), current_setting('threads')").fetchone() == original
    finally:
        store.close()


def test_registered_research_budget_cannot_change_before_final_evaluation(tmp_path):
    import json

    from scripts.ml_controlled_study import run

    registration = tmp_path / "registration.json"
    registration.write_text(json.dumps({"max_iterations": 100}))
    output = tmp_path / "must-not-evaluate.json"
    with pytest.raises(ValueError, match="Iteration budget"):
        run([], output, max_iter=200, release_protocol=registration)
    assert not output.exists() and not output.with_suffix(".protocol.json").exists()


def test_final_contract_checks_validation_integrity_and_population(tmp_path):
    import hashlib
    import json

    from scripts.ml_controlled_study import release_manifest_sha256, validate_frozen_selection

    path = tmp_path / "validation.json"
    report = {"protocol": {"release_protocol_sha256": "registered",
                           "release_manifest_sha256": release_manifest_sha256()},
              "reports": [{"evaluation_split": "validation", "feature_contract_sha256": "frozen"}]}
    path.write_text(json.dumps(report))
    selection = {"protocol_sha256": "registered", "validation_report_sha256": {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()}}
    assert validate_frozen_selection(selection, "registered") == "frozen"
    report["reports"][0]["evaluation_split"] = "reserved_final_holdout"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="integrity mismatch"):
        validate_frozen_selection(selection, "registered")
    selection["validation_report_sha256"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="Only development validation"):
        validate_frozen_selection(selection, "registered")
