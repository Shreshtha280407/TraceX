"""Generate and verify the deterministic, stdlib-only Phase 5A 10K smoke fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import tempfile
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "fixture_config.json"
GENERATED_FILES = (
    "funding_outpoints.ndjson",
    "transactions.ndjson",
    "inputs.ndjson",
    "outputs.ndjson",
    "ingestion_rows.ndjson",
    "network_observations.ndjson",
    "duplicate_candidates.ndjson",
    "evaluation_truth.json",
)


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def btc(sats: int) -> str:
    return f"{sats // 100_000_000}.{sats % 100_000_000:08d}"


def txid(seed: str, index: int) -> str:
    return digest(f"{seed}:canonical-transaction:{index}")


def address(seed: str, tx_hash: str, vout: int) -> str:
    # Opaque deterministic values; no scenario identifier is encoded here.
    return f"bcrt1q{digest(f'{seed}:address:{tx_hash}:{vout}')[:38]}"


def source_ref(config_hash: str, locator: str) -> list[dict[str, Any]]:
    return [{"evidence_id": "ev-ml-smoke-config", "source_sha256": config_hash, "locator_type": "synthetic", "locator": locator, "byte_start": None, "byte_end": None}]


def write_rows(path: Path, rows: list[dict[str, Any]]) -> int:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(compact(row) + "\n")
    return len(rows)


def read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_config() -> tuple[dict[str, Any], str]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    required = {"generator_version", "seed", "network", "transaction_count", "bootstrap_transaction_count", "bootstrap_outputs_per_transaction", "duplicate_record_count", "network_observation_count"}
    missing = required.difference(config)
    if missing or config["transaction_count"] != 10_000:
        raise ValueError(f"invalid smoke fixture config; missing={sorted(missing)}")
    return config, file_hash(CONFIG_PATH)


def generate(destination: Path = ROOT) -> dict[str, Any]:
    config, config_hash = load_config()
    destination.mkdir(parents=True, exist_ok=True)
    rng = random.Random(config["seed"])
    base_time = datetime(2026, 4, 1, tzinfo=UTC)
    transactions: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    ingestion: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    funding: list[dict[str, Any]] = []
    available: list[dict[str, Any]] = []
    special: dict[str, Any] = {}

    countries = ("IN", "SG", "DE", "NL", "US", "JP", "BR")
    script_types = ("p2wpkh", "p2tr", "p2sh")

    def timestamp(index: int) -> str:
        return (base_time + timedelta(seconds=index * 43 + (index % 11))).isoformat().replace("+00:00", "Z")

    def network_fields(index: int) -> dict[str, Any]:
        # Repeated addresses deliberately model relay/NAT-like observations.
        return {"src_ip": f"198.51.100.{1 + (index % 17)}", "dst_ip": f"203.0.113.{1 + ((index * 5) % 17)}", "src_port": 8333 if index % 3 else 18_333, "dst_port": 8333, "geo_country": countries[index % len(countries)], "asn": f"AS{64512 + (index % 11)}"}

    def add_transaction(index: int, selected: list[dict[str, Any]] | None, amounts: list[int], addresses: list[str] | None = None, fee: int = 0) -> tuple[str, list[dict[str, Any]]]:
        current = txid(config["seed"], index)
        now = timestamp(index)
        selected = selected or []
        output_addresses = addresses or [address(config["seed"], current, vout) for vout in range(len(amounts))]
        script_type = script_types[index % len(script_types)]
        transaction = {"schema_version": "1.0.0", "case_id": "case-ml-smoke-10k", "network": config["network"], "txid": current, "record_variant": "canonical", "block_hash": None, "block_height": None, "block_time": now, "block_time_original": now, "fee_sats": fee, "confirmation_status": "unknown", "source_refs": source_ref(config_hash, f"transaction:{index}")}
        transactions.append(transaction)
        structured_inputs: list[dict[str, Any]] = []
        if selected:
            for vin, item in enumerate(selected):
                input_row = {"schema_version": "1.0.0", "case_id": "case-ml-smoke-10k", "network": config["network"], "txid": current, "vin": vin, "prev_txid": item["txid"], "prev_vout": item["vout"], "address": item["address"], "amount_sats": item["amount_sats"], "sequence": 4_294_967_293, "source_refs": source_ref(config_hash, f"input:{index}:{vin}")}
                inputs.append(input_row)
                structured_inputs.append({"prev_txid": item["txid"], "prev_vout": item["vout"], "address": item["address"], "amount_sats": item["amount_sats"], "sequence": 4_294_967_293})
        else:
            inputs.append({"schema_version": "1.0.0", "case_id": "case-ml-smoke-10k", "network": config["network"], "txid": current, "vin": 0, "prev_txid": None, "prev_vout": None, "address": None, "amount_sats": None, "sequence": 4_294_967_293, "source_refs": source_ref(config_hash, f"input:{index}:0")})
            structured_inputs.append({"prev_txid": None, "prev_vout": None, "address": None, "amount_sats": None, "sequence": 4_294_967_293})
        created: list[dict[str, Any]] = []
        for vout, amount in enumerate(amounts):
            row = {"schema_version": "1.0.0", "case_id": "case-ml-smoke-10k", "network": config["network"], "txid": current, "vout": vout, "amount_sats": amount, "script_id": f"script:{output_addresses[vout]}", "script_type": script_type, "address": output_addresses[vout], "source_refs": source_ref(config_hash, f"output:{index}:{vout}")}
            outputs.append(row)
            created.append(row)
        ingestion.append({"timestamp": now, "network": config["network"], "txid": current, "input_addresses": [item["address"] for item in selected], "input_amounts": [btc(item["amount_sats"]) for item in selected], "output_addresses": output_addresses, "output_amounts": [btc(amount) for amount in amounts], "fee": btc(fee), "script_type": script_type, "inputs": structured_inputs, "outputs": [{"address": output_addresses[vout], "amount_sats": amount, "script_type": script_type} for vout, amount in enumerate(amounts)], **network_fields(index)})
        return current, created

    # Bootstrap facts have deliberately unresolved inputs, but emit enough
    # one-time spendable UTXOs that ordinary rows never form accidental long chains.
    for index in range(config["bootstrap_transaction_count"]):
        total = 240_000_000 + index * 1_700_000
        amount = total // config["bootstrap_outputs_per_transaction"]
        remainder = total - amount * config["bootstrap_outputs_per_transaction"]
        _, created = add_transaction(index, None, [amount] * (config["bootstrap_outputs_per_transaction"] - 1) + [amount + remainder])
        available.extend(created)
        funding.extend(created)

    def take(count: int = 1) -> list[dict[str, Any]]:
        if len(available) < count:
            raise RuntimeError("insufficient synthetic bootstrap UTXOs")
        chosen = [available.pop(rng.randrange(len(available))) for _ in range(count)]
        return chosen

    for index in range(config["bootstrap_transaction_count"], config["transaction_count"]):
        # Special cases below are all kept out of the general one-hop flow.
        if index == 100:
            selected = take()
            total, fee = selected[0]["amount_sats"], 711
            _, created = add_transaction(index, selected, [total - fee - 23_000, 23_000], fee=fee)
            special["peel_origin"] = created[0]
        elif index in {101, 102}:
            selected = [special["peel_origin"]]
            total, fee = selected[0]["amount_sats"], 700 + index
            _, created = add_transaction(index, selected, [total - fee - 19_000, 19_000], fee=fee)
            special["peel_origin"] = created[0]
        elif index == 103:
            selected = [special["peel_origin"]]
            total, fee = selected[0]["amount_sats"], 803
            _, _ = add_transaction(index, selected, [total - fee - 17_000, 17_000], fee=fee)
            special["peeling_txids"] = [txid(config["seed"], value) for value in (101, 102, 103)]
        elif index == 110:
            selected = take()
            total, fee = selected[0]["amount_sats"], 690
            _, created = add_transaction(index, selected, [total - fee - 20_000, 20_000], fee=fee)
            special["ordinary_control"] = created[0]
        elif index == 111:
            selected = [special["ordinary_control"]]
            total, fee = selected[0]["amount_sats"], 691
            add_transaction(index, selected, [total - fee - 15_000, 15_000], fee=fee)
        elif index == 200:
            selected = take(3)
            total, fee = sum(item["amount_sats"] for item in selected), 1_125
            equal = (total - fee - 13_007) // 3
            add_transaction(index, selected, [equal, equal, equal, total - fee - equal * 3], fee=fee)
            special["coinjoin_txid"] = txid(config["seed"], index)
        elif index == 201:
            selected = take(3)
            total, fee = sum(item["amount_sats"] for item in selected), 1_126
            add_transaction(index, selected, [total // 2, total // 3, total - fee - total // 2 - total // 3], fee=fee)
            special["ordinary_multi_txid"] = txid(config["seed"], index)
        elif index == 300:
            selected = take()
            total, fee = selected[0]["amount_sats"], 777
            seed_address = address(config["seed"], txid(config["seed"], index), 0)
            _, created = add_transaction(index, selected, [total - fee - 11_000, 11_000], [seed_address, address(config["seed"], txid(config["seed"], index), 1)], fee)
            special["seed"] = created[0]
            special["seed_address"] = seed_address
        elif index == 301:
            selected = [special["seed"]]
            total, fee = selected[0]["amount_sats"], 778
            _, created = add_transaction(index, selected, [total - fee - 9_000, 9_000], fee=fee)
            special["seed"] = created[0]
        elif index == 302:
            selected = [special["seed"]]
            total, fee = selected[0]["amount_sats"], 779
            target_address = address(config["seed"], txid(config["seed"], index), 0)
            add_transaction(index, selected, [total - fee - 8_000, 8_000], [target_address, address(config["seed"], txid(config["seed"], index), 1)], fee)
            special["two_hop_target"] = target_address
        elif index == 400:
            selected = take()
            total, fee = selected[0]["amount_sats"], 880
            disconnected = address(config["seed"], txid(config["seed"], index), 0)
            add_transaction(index, selected, [total - fee - 10_000, 10_000], [disconnected, address(config["seed"], txid(config["seed"], index), 1)], fee)
            special["disconnected_address"] = disconnected
        elif index == 450:
            selected = take(2)
            total, fee = sum(item["amount_sats"] for item in selected), 913
            primary = (total - fee) * 91 // 100
            add_transaction(index, selected, [primary, total - fee - primary], fee=fee)
        else:
            selected = take(2 if index % 997 == 0 else 1)
            total, fee = sum(item["amount_sats"] for item in selected), 300 + (index % 900)
            if index % 500 == 0:
                recipients = 8 + (index % 5)
                payment = (total - fee) // (recipients + 1)
                amounts = [payment + ((slot * 37) % 100) for slot in range(recipients)]
                amounts.append(total - fee - sum(amounts))
            else:
                amounts = [total - fee]
            add_transaction(index, selected, amounts, fee=fee)

    # A separate observation export documents relay-like repeated IP captures;
    # raw rows also contain SIH-compatible observation fields for ingestion.
    for index in range(config["network_observation_count"]):
        tx_index = (index * 83) % config["transaction_count"]
        observed = timestamp(tx_index)
        observations.append({"schema_version": "1.0.0", "case_id": "case-ml-smoke-10k", "network": config["network"], "observation_id": f"obs-smoke-{index:05d}", "txid": txid(config["seed"], tx_index), "observer_id": f"relay-{index % 4}", **network_fields(index), "endpoint_ip": f"198.51.100.{1 + (index % 17)}", "endpoint_port": 8333, "direction": "seen_from" if index % 2 else "seen_to", "observer_received_at": observed, "observer_time_original": observed, "clock_quality": "estimated", "capture_scope": "synthetic relay/NAT-like observation; no origin or ownership assertion", "source_refs": source_ref(config_hash, f"network_observation:{index}")})

    duplicate_indexes = [1500 + step * 997 for step in range(config["duplicate_record_count"])]
    duplicates = [{"canonical_txid": txid(config["seed"], index), "duplicate_of_locator": f"record:{index + 1}", "duplicate_locator": f"record:{config['transaction_count'] + position + 1}", "source_refs": source_ref(config_hash, f"duplicate_candidate:{position}")} for position, index in enumerate(duplicate_indexes)]
    ingestion.extend(dict(ingestion[index]) for index in duplicate_indexes)

    counts = {"funding_outpoints": write_rows(destination / "funding_outpoints.ndjson", funding), "transactions": write_rows(destination / "transactions.ndjson", transactions), "inputs": write_rows(destination / "inputs.ndjson", inputs), "outputs": write_rows(destination / "outputs.ndjson", outputs), "ingestion_rows": write_rows(destination / "ingestion_rows.ndjson", ingestion), "network_observations": write_rows(destination / "network_observations.ndjson", observations), "duplicate_candidates": write_rows(destination / "duplicate_candidates.ndjson", duplicates)}
    truth = {"evaluation_only": True, "do_not_ingest": True, "scenario_groups": {"normal_low_activity": True, "benign_exchange_style_batches": True, "benign_treasury_transfer": True, "relay_nat_observations": True, "intentionally_unresolved_outpoints": True}, "expected_peeling_chain_transaction_ids": special["peeling_txids"], "expected_coinjoin_like_transaction_ids": [special["coinjoin_txid"]], "synthetic_review_seed_address": special["seed_address"], "expected_two_hop_target_address": special["two_hop_target"], "disconnected_address": special["disconnected_address"], "expected_benign_controls": {"ordinary_sequence_transaction_ids": [txid(config["seed"], 110), txid(config["seed"], 111)], "ordinary_multi_output_transaction_ids": [special["ordinary_multi_txid"]]}, "raw_fixture_contains_no_scenario_labels": True}
    (destination / "evaluation_truth.json").write_text(compact(truth) + "\n", encoding="utf-8")
    manifest = {"schema_version": "1.0.0", "generator_version": config["generator_version"], "config_sha256": config_hash, "seed": config["seed"], "network": config["network"], "generated_files": {}, "counts": counts, "invariants": {"canonical_transaction_count": len(transactions), "ingestion_row_count": len(ingestion), "duplicate_candidate_count": len(duplicates), "unresolved_outpoint_transaction_count": config["bootstrap_transaction_count"], "all_fully_known_transactions_conserve_value": True, "no_double_spends": True, "ownership_labels_present_in_raw_fixture": False}}
    for filename in GENERATED_FILES:
        target = destination / filename
        manifest["generated_files"][filename] = {"sha256": file_hash(target), "bytes": target.stat().st_size}
    (destination / "fixture_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validate(destination)
    return manifest


def validate(directory: Path = ROOT) -> None:
    config, _ = load_config()
    transactions, inputs, outputs = (read_rows(directory / name) for name in ("transactions.ndjson", "inputs.ndjson", "outputs.ndjson"))
    if len(transactions) != 10_000 or len({row["txid"] for row in transactions}) != 10_000:
        raise ValueError("fixture must contain exactly 10,000 unique canonical transactions")
    output_map = {(row["txid"], row["vout"]): row for row in outputs}
    if len(output_map) != len(outputs):
        raise ValueError("duplicate output identity")
    by_input: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inputs:
        by_input[row["txid"]].append(row)
    for row in outputs:
        by_output[row["txid"]].append(row)
    spent: set[tuple[str, int]] = set()
    for transaction in transactions:
        tx_inputs, tx_outputs = by_input[transaction["txid"]], by_output[transaction["txid"]]
        refs = [(item["prev_txid"], item["prev_vout"]) for item in tx_inputs]
        if any(left is None or right is None for left, right in refs):
            continue
        if len(refs) != len(set(refs)) or any(reference not in output_map for reference in refs) or spent.intersection(refs):
            raise ValueError(f"invalid or double-spent outpoint for {transaction['txid']}")
        spent.update(refs)
        if sum(output_map[reference]["amount_sats"] for reference in refs) != sum(item["amount_sats"] for item in tx_outputs) + transaction["fee_sats"]:
            raise ValueError(f"value conservation failed for {transaction['txid']}")
    rows = read_rows(directory / "ingestion_rows.ndjson")
    required = {"timestamp", "network", "src_ip", "dst_ip", "src_port", "dst_port", "txid", "input_addresses", "output_addresses", "input_amounts", "output_amounts", "fee", "script_type", "geo_country", "asn", "inputs", "outputs"}
    if any(not required.issubset(row) for row in rows):
        raise ValueError("ingestion rows lack required SIH/structured fields")
    manifest = json.loads((directory / "fixture_manifest.json").read_text(encoding="utf-8"))
    for filename in GENERATED_FILES:
        if manifest["generated_files"][filename]["sha256"] != file_hash(directory / filename):
            raise ValueError(f"manifest hash mismatch: {filename}")
    if manifest["counts"]["transactions"] != config["transaction_count"]:
        raise ValueError("manifest canonical count mismatch")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="regenerate in a temporary directory and compare deterministic hashes")
    args = parser.parse_args()
    if args.verify:
        validate(ROOT)
        with tempfile.TemporaryDirectory(prefix="tracex-ml-smoke-verify-") as temporary:
            regenerated = generate(Path(temporary))
            current = json.loads((ROOT / "fixture_manifest.json").read_text(encoding="utf-8"))
            if regenerated["generated_files"] != current["generated_files"]:
                raise SystemExit("fixture regeneration hash mismatch")
        print("Phase 5A 10K fixture verification passed")
    else:
        manifest = generate(ROOT)
        print(f"Generated {manifest['counts']['transactions']} canonical transactions")


if __name__ == "__main__":
    main()
