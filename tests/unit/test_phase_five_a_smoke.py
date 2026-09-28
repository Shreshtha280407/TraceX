from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.engine.motifs.deterministic import (
    detect_coinjoin_like_transactions,
    detect_peeling_chains,
    propagate_synthetic_review_seeds,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "ml_smoke_10k"
REQUIRED_RAW_FIELDS = {
    "timestamp", "network", "src_ip", "dst_ip", "src_port", "dst_port", "txid", "input_addresses",
    "output_addresses", "input_amounts", "output_amounts", "fee", "script_type", "geo_country", "asn", "inputs", "outputs",
}
PHASE41_FIELDS = {
    "peeling_chain_score", "peeling_chain_length", "peeling_chain_total_duration_sec", "peeling_chain_evidence_count",
    "coinjoin_like_score", "equal_output_count", "equal_output_value_sats", "coinjoin_like_evidence_count",
    "risk_propagation_score", "risk_seed_distance", "risk_path_evidence_count", "risk_seed_count",
}


def _generator():
    spec = importlib.util.spec_from_file_location("phase5a_generator_test", FIXTURE / "generate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _facts(generator, directory: Path):
    transactions = {row["txid"]: row for row in generator.read_rows(directory / "transactions.ndjson")}
    return transactions, generator.read_rows(directory / "inputs.ndjson"), generator.read_rows(directory / "outputs.ndjson")


def test_10k_fixture_is_deterministic_conserving_and_sih_compatible(tmp_path: Path) -> None:
    generator = _generator()
    first, second = tmp_path / "first", tmp_path / "second"
    manifest_a, manifest_b = generator.generate(first), generator.generate(second)
    generator.validate(first)
    assert manifest_a["counts"]["transactions"] == 10_000
    assert manifest_a["invariants"]["canonical_transaction_count"] == 10_000
    assert manifest_a["generated_files"] == manifest_b["generated_files"]
    rows = generator.read_rows(first / "ingestion_rows.ndjson")
    assert len(rows) == 10_007 and all(REQUIRED_RAW_FIELDS <= set(row) for row in rows)
    assert all("scenario" not in json.dumps(row).lower() for row in rows)


def test_10k_fixture_motif_controls_and_ip_exclusion(tmp_path: Path) -> None:
    generator = _generator()
    generator.generate(tmp_path)
    transactions, inputs, outputs = _facts(generator, tmp_path)
    truth = json.loads((tmp_path / "evaluation_truth.json").read_text(encoding="utf-8"))
    peeling = detect_peeling_chains(transactions=transactions, inputs=inputs, outputs=outputs)
    peeling_ids = {tuple(item["transaction_ids"]) for item in peeling}
    assert tuple(truth["expected_peeling_chain_transaction_ids"]) in peeling_ids
    controls = set(truth["expected_benign_controls"]["ordinary_sequence_transaction_ids"])
    assert all(not controls.issubset(set(item["transaction_ids"])) for item in peeling)
    coinjoin = detect_coinjoin_like_transactions(transactions=transactions, inputs=inputs, outputs=outputs)
    coinjoin_ids = {item["transaction_id"] for item in coinjoin}
    assert set(truth["expected_coinjoin_like_transaction_ids"]).issubset(coinjoin_ids)
    assert not set(truth["expected_benign_controls"]["ordinary_multi_output_transaction_ids"]).intersection(coinjoin_ids)
    propagation = propagate_synthetic_review_seeds(
        seeds=[{"id": "synthetic-test-seed", "seed_entity_ref": f"address:{truth['synthetic_review_seed_address']}", "seed_reason": "evaluation", "synthetic": True}],
        transactions=transactions,
        inputs=inputs,
        outputs=outputs,
    )
    by_entity = {item["entity_ref"]: item for item in propagation}
    target = by_entity[f"address:{truth['expected_two_hop_target_address']}"]
    assert target["risk_seed_distance"] == 2 and target["score"] > 0
    assert f"address:{truth['disconnected_address']}" not in by_entity
    assert all(not node.startswith(("endpoint:", "obs:")) for item in propagation for node in item["graph_path"]["nodes"])


def test_phase5a_feature_schema_contract_and_no_result_defaults() -> None:
    schema = json.loads((REPO / "feature_schema.json").read_text(encoding="utf-8"))
    assert PHASE41_FIELDS <= set(schema["properties"])
    # The Phase 4.1 API test covers exported rows; this fixture's disconnected
    # control above ensures a non-result remains absent from propagation.
