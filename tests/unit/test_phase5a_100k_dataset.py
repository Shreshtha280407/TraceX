from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest

from app.engine.motifs.deterministic import (
    detect_coinjoin_like_transactions,
    detect_peeling_chains,
    propagate_synthetic_review_seeds,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "phase5a_100k"
REQUIRED_RAW_FIELDS = {
    "timestamp", "network", "txid", "input_addresses", "output_addresses", "input_amounts",
    "output_amounts", "src_ip", "dst_ip", "src_port", "dst_port", "geo_country", "asn",
    "fee", "script_type", "inputs", "outputs",
}


def _generator():
    spec = importlib.util.spec_from_file_location("phase5a_100k_generator_test", FIXTURE / "generate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory):
    generator = _generator()
    output = tmp_path_factory.mktemp("phase5a_100k") / "dataset"
    manifest = generator.generate(output, ("csv", "ndjson"))
    return generator, output, manifest


def _selected_facts(generator, output: Path, txids: set[str]):
    transactions = {row["txid"]: row for row in generator.ndjson_rows(output / "transactions.ndjson") if row["txid"] in txids}
    inputs = [row for row in generator.ndjson_rows(output / "inputs.ndjson") if row["txid"] in txids]
    outputs = [row for row in generator.ndjson_rows(output / "outputs.ndjson") if row["txid"] in txids]
    return transactions, inputs, outputs


def test_100k_canonical_determinism_and_manifest_hashes(generated, tmp_path: Path) -> None:
    generator, output, manifest = generated
    assert manifest["counts"]["canonical_transaction_count"] == 100_000
    assert manifest["counts"]["duplicate_candidates"] == 25
    second = generator.generate(tmp_path / "second", ("csv", "ndjson"), validate_output=False)
    assert manifest["generated_files"] == second["generated_files"]
    assert manifest["config_sha256"] == second["config_sha256"]
    schema = json.loads((FIXTURE / "fixture_manifest.schema.json").read_text(encoding="utf-8"))
    assert set(schema["required"]).issubset(manifest)
    assert (output / "fixture_manifest.json").is_file()


def test_csv_ndjson_raw_contract_utxo_and_duplicates(generated) -> None:
    generator, output, manifest = generated
    # generate() runs validate(), which checks references, no double-spends,
    # explicit null pairs, and exact satoshi conservation.
    with (output / "ingestion_rows.csv").open(encoding="utf-8", newline="") as handle:
        csv_rows = csv.DictReader(handle)
        canonical_txids: set[str] = set()
        count = 0
        for ndjson_row, csv_row in zip(generator.ndjson_rows(output / "ingestion_rows.ndjson"), csv_rows, strict=True):
            count += 1
            assert REQUIRED_RAW_FIELDS <= set(ndjson_row)
            assert (ndjson_row["txid"], ndjson_row["source_record_id"]) == (csv_row["txid"], csv_row["source_record_id"])
            assert "scenario" not in json.dumps(ndjson_row).lower()
            assert "group_id" not in json.dumps(ndjson_row).lower()
            canonical_txids.add(ndjson_row["txid"])
    assert count == 100_025
    assert len(canonical_txids) == 100_000
    assert manifest["invariants"]["no_double_spends"] is True
    assert manifest["invariants"]["fully_known_value_conservation"] is True
    unresolved = 0
    for row in generator.ndjson_rows(output / "inputs.ndjson"):
        unresolved += row["prev_txid"] is None and row["prev_vout"] is None
        assert (row["prev_txid"] is None) == (row["prev_vout"] is None)
    assert unresolved == 1000
    assert sum(1 for _ in generator.ndjson_rows(output / "duplicate_candidates.ndjson")) == 25


def test_split_truth_controls_and_network_observation_non_ownership(generated) -> None:
    generator, output, _ = generated
    truth = json.loads((output / "evaluation_truth.json").read_text(encoding="utf-8"))
    assert truth["evaluation_only"] is True and truth["do_not_ingest"] is True
    groups = truth["split_groups"]
    assert len(groups) >= 10
    assert len({group["group_id"] for group in groups}) == len(groups)
    assert [group["split"] for group in groups] == sorted(
        (group["split"] for group in groups), key={"train_reference": 0, "validation": 1, "final_holdout": 2}.get
    )
    assert sum(group["transaction_index_end_exclusive"] - group["transaction_index_start"] for group in groups if group["split"] == "final_holdout") >= 3000
    review_rows = sum(
        group["transaction_index_end_exclusive"] - group["transaction_index_start"]
        for group in groups if group["review_pattern"]
    )
    assert 0.05 <= review_rows / 100_000 <= 0.20
    assert 100_000 - review_rows >= 80_000
    observations = list(generator.ndjson_rows(output / "network_observations.ndjson"))
    assert observations and all("ownership" in row["capture_scope"] and row["txid"] for row in observations)
    assert all("address" not in row for row in observations)
    assert not [path for path in output.iterdir() if "model" in path.name.lower()]


def test_phase41_positive_negative_controls_and_seed_path(generated) -> None:
    generator, output, _ = generated
    truth = json.loads((output / "evaluation_truth.json").read_text(encoding="utf-8"))
    relevant = set(truth["expected_peeling_chain_transaction_ids"])
    relevant.update(truth["expected_coinjoin_like_transaction_ids"])
    relevant.update(truth["expected_benign_controls"]["ordinary_sequence_transaction_ids"])
    relevant.update(truth["expected_benign_controls"]["ordinary_multi_output_transaction_ids"])
    seed_indexes = (1000, 1020, 1021, 1022, 1040)
    config, _ = generator.load_config()
    relevant.update(generator.txid(config["seed"], index) for index in seed_indexes)
    transactions, inputs, outputs = _selected_facts(generator, output, relevant)
    peeling = detect_peeling_chains(transactions=transactions, inputs=inputs, outputs=outputs)
    assert tuple(truth["expected_peeling_chain_transaction_ids"]) in {tuple(item["transaction_ids"]) for item in peeling}
    ordinary = set(truth["expected_benign_controls"]["ordinary_sequence_transaction_ids"])
    assert all(not ordinary.issubset(item["transaction_ids"]) for item in peeling)
    coinjoin = detect_coinjoin_like_transactions(transactions=transactions, inputs=inputs, outputs=outputs)
    found = {item["transaction_id"] for item in coinjoin}
    assert set(truth["expected_coinjoin_like_transaction_ids"]).intersection(found)
    assert not set(truth["expected_benign_controls"]["ordinary_multi_output_transaction_ids"]).intersection(found)
    propagation = propagate_synthetic_review_seeds(
        seeds=[{"id": "synthetic-evaluation-seed", "seed_entity_ref": f"address:{truth['synthetic_review_seed_address']}", "seed_reason": "evaluation", "synthetic": True}],
        transactions=transactions,
        inputs=inputs,
        outputs=outputs,
    )
    by_entity = {item["entity_ref"]: item for item in propagation}
    target = by_entity[f"address:{truth['expected_two_hop_target_address']}"]
    assert target["risk_seed_distance"] == 2 and target["score"] > 0
    assert f"address:{truth['disconnected_address']}" not in by_entity
    assert all(not node.startswith(("endpoint:", "obs:")) for item in propagation for node in item["graph_path"]["nodes"])
