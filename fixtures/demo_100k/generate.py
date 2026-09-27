"""Create and verify the deterministic TraceX Phase 0 fixture (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import tempfile
from collections.abc import Iterator
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


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_ndjson(path: Path, rows: Iterator[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(compact(row))
            handle.write("\n")
            count += 1
    return count


def txid(seed: str, kind: str, number: int) -> str:
    return digest_text(f"{seed}:{kind}:{number}")


def btc_text(sats: int) -> str:
    return f"{sats // 100_000_000}.{sats % 100_000_000:08d}"


def source_ref(config_hash: str, locator: str) -> list[dict[str, Any]]:
    return [{
        "evidence_id": "ev-demo-100k-config",
        "source_sha256": config_hash,
        "locator_type": "synthetic",
        "locator": locator,
        "byte_start": None,
        "byte_end": None,
    }]


def address_for(seed: str, tx_hash: str, vout: int, hidden: bool = False) -> str:
    namespace = "eval" if hidden else "addr"
    return f"bcrt1q{digest_text(f'{seed}:{namespace}:{tx_hash}:{vout}')[:38]}"


def load_config() -> tuple[dict[str, Any], str]:
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    config = json.loads(raw)
    required = {"generator_version", "seed", "case_id", "network", "transaction_count", "funding_outpoint_count", "duplicate_record_count", "network_observation_count"}
    missing = required.difference(config)
    if missing:
        raise ValueError(f"fixture config missing: {sorted(missing)}")
    if config["transaction_count"] != 100000:
        raise ValueError("Phase 0 fixture must contain exactly 100000 canonical transactions")
    return config, sha256_file(CONFIG_PATH)


def generate(destination: Path) -> dict[str, Any]:
    config, config_hash = load_config()
    destination.mkdir(parents=True, exist_ok=True)
    rng = random.Random(config["seed"])
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    source_sha = config_hash
    known_utxos: list[dict[str, Any]] = []
    funding_rows: list[dict[str, Any]] = []
    for index in range(config["funding_outpoint_count"]):
        funding_txid = txid(config["seed"], "funding", index)
        amount = 8_000_000 + (index * 37_000)
        row = {"fixture_kind": "preexisting_funding_outpoint", "case_id": config["case_id"], "network": config["network"], "txid": funding_txid, "vout": 0, "amount_sats": amount, "address": address_for(config["seed"], funding_txid, 0), "script_type": "p2wpkh", "source_refs": source_ref(source_sha, f"funding_outpoint:{index}")}
        funding_rows.append(row)
        known_utxos.append(row)

    transactions: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    ingestion_rows: list[dict[str, Any]] = []
    hidden_txids: list[str] = []
    hidden_addresses: set[str] = set()
    unresolved_txids: list[str] = []
    batch_txids: list[str] = []
    duplicate_rows: list[dict[str, Any]] = []

    def take_spendable(minimum_sats: int) -> dict[str, Any]:
        """Avoid selecting dust that cannot pay this fixture transaction's fee."""
        for _ in range(64):
            candidate = rng.randrange(len(known_utxos))
            if known_utxos[candidate]["amount_sats"] > minimum_sats:
                return known_utxos.pop(candidate)
        for candidate, output in enumerate(known_utxos):
            if output["amount_sats"] > minimum_sats:
                return known_utxos.pop(candidate)
        raise RuntimeError("fixture ran out of spendable UTXOs")

    for index in range(config["transaction_count"]):
        current_txid = txid(config["seed"], "transaction", index)
        is_hidden = 75_000 <= index < 75_240
        is_batch = index > 0 and index % 1000 == 0
        has_missing_prevout = index > 0 and index % 997 == 0
        fee = 250 + (index % 700)
        selected: list[dict[str, Any]] = []
        if has_missing_prevout:
            unresolved_txids.append(current_txid)
        else:
            input_count = 2 if is_batch else 1
            for _ in range(input_count):
                selected.append(take_spendable(fee))
        now = base_time + timedelta(seconds=index * 37)
        transaction = {
            "schema_version": "1.0.0", "case_id": config["case_id"], "network": config["network"], "txid": current_txid,
            "record_variant": "canonical", "block_hash": None, "block_height": None, "block_time": now.isoformat().replace("+00:00", "Z"),
            "block_time_original": now.isoformat().replace("+00:00", "Z"), "fee_sats": 250 + (index % 700), "confirmation_status": "unknown",
            "source_refs": source_ref(source_sha, f"transaction:{index}"),
        }
        transactions.append(transaction)
        if has_missing_prevout:
            inputs.append({"schema_version": "1.0.0", "case_id": config["case_id"], "network": config["network"], "txid": current_txid, "vin": 0, "prev_txid": None, "prev_vout": None, "address": None, "amount_sats": None, "sequence": 4294967293, "source_refs": source_ref(source_sha, f"input:{index}:0")})
            total_in = 250_000 + (index % 50_000)
        else:
            total_in = sum(item["amount_sats"] for item in selected)
            for vin, item in enumerate(selected):
                inputs.append({"schema_version": "1.0.0", "case_id": config["case_id"], "network": config["network"], "txid": current_txid, "vin": vin, "prev_txid": item["txid"], "prev_vout": item["vout"], "address": item["address"], "amount_sats": item["amount_sats"], "sequence": 4294967293, "source_refs": source_ref(source_sha, f"input:{index}:{vin}")})
        distributable = total_in - fee
        output_amounts: list[int]
        if is_batch:
            batch_txids.append(current_txid)
            recipient_count = 12
            payment = max(1, distributable // (recipient_count + 2))
            output_amounts = [payment] * recipient_count + [distributable - (payment * recipient_count)]
        else:
            payment = max(1, distributable * (35 + (index % 25)) // 100)
            output_amounts = [payment, distributable - payment]
        for vout, amount in enumerate(output_amounts):
            addr = address_for(config["seed"], current_txid, vout, hidden=is_hidden)
            output = {"schema_version": "1.0.0", "case_id": config["case_id"], "network": config["network"], "txid": current_txid, "vout": vout, "amount_sats": amount, "script_id": f"script:{addr}", "script_type": "p2wpkh", "address": addr, "source_refs": source_ref(source_sha, f"output:{index}:{vout}")}
            outputs.append(output)
            known_utxos.append(output)
            if is_hidden:
                hidden_addresses.add(addr)
        ingestion_rows.append({
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "network": config["network"],
            "txid": current_txid,
            "input_addresses": [] if has_missing_prevout else [item["address"] for item in selected],
            "input_amounts": [] if has_missing_prevout else [btc_text(item["amount_sats"]) for item in selected],
            "output_addresses": [address_for(config["seed"], current_txid, vout, hidden=is_hidden) for vout in range(len(output_amounts))],
            "output_amounts": [btc_text(amount) for amount in output_amounts],
            "fee": btc_text(fee),
            "script_type": "p2wpkh",
        })
        if is_hidden:
            hidden_txids.append(current_txid)
        if index % (config["transaction_count"] // config["duplicate_record_count"]) == 0:
            duplicate_rows.append({"fixture_kind": "duplicate_source_candidate", "canonical_txid": current_txid, "duplicate_of_locator": f"transaction:{index}", "duplicate_locator": f"duplicate_candidate:{len(duplicate_rows)}", "source_refs": source_ref(source_sha, f"duplicate_candidate:{len(duplicate_rows)}")})

    observations: list[dict[str, Any]] = []
    for index in range(config["network_observation_count"]):
        observed_txid = hidden_txids[index % len(hidden_txids)] if index < 160 else transactions[(index * 199) % len(transactions)]["txid"]
        observed_at = base_time + timedelta(seconds=((index * 199) % len(transactions)) * 37 + 3 + (index % 17))
        source_ip = f"198.51.100.{1 + (index % 19)}"
        destination_ip = f"203.0.113.{1 + ((index * 7) % 19)}"
        observations.append({"schema_version": "1.0.0", "case_id": config["case_id"], "network": config["network"], "observation_id": f"obs-demo-{index:06d}", "txid": observed_txid, "observer_id": f"observer-{index % 7}", "src_ip": source_ip, "dst_ip": destination_ip, "src_port": 8333, "dst_port": 8333, "endpoint_ip": source_ip, "endpoint_port": 8333, "direction": "seen_from" if index % 2 else "seen_to", "geo_country": "ZZ", "asn": f"AS{64500 + (index % 5)}", "observer_received_at": observed_at.isoformat().replace("+00:00", "Z"), "observer_time_original": observed_at.isoformat().replace("+00:00", "Z"), "clock_quality": "estimated" if index % 5 else "unknown", "capture_scope": "synthetic relay capture; no origin or ownership claim", "source_refs": source_ref(source_sha, f"network_observation:{index}")})

    counts = {
        "funding_outpoints": write_ndjson(destination / "funding_outpoints.ndjson", iter(funding_rows)),
        "transactions": write_ndjson(destination / "transactions.ndjson", iter(transactions)),
        "inputs": write_ndjson(destination / "inputs.ndjson", iter(inputs)),
        "outputs": write_ndjson(destination / "outputs.ndjson", iter(outputs)),
        "ingestion_rows": write_ndjson(destination / "ingestion_rows.ndjson", iter(ingestion_rows)),
        "network_observations": write_ndjson(destination / "network_observations.ndjson", iter(observations)),
        "duplicate_candidates": write_ndjson(destination / "duplicate_candidates.ndjson", iter(duplicate_rows)),
    }
    truth = {"evaluation_only": True, "do_not_ingest": True, "scenario_id": "opaque-eval-01", "transaction_ids": hidden_txids, "address_ids": sorted(hidden_addresses), "expected_properties": {"contains_exchange_batching": True, "contains_unresolved_outpoints": True, "network_observations_are_not_ownership_labels": True}}
    (destination / "evaluation_truth.json").write_text(compact(truth) + "\n", encoding="utf-8")
    manifest = {"schema_version": "1.0.0", "generator_version": config["generator_version"], "config_sha256": config_hash, "seed": config["seed"], "case_id": config["case_id"], "network": config["network"], "generated_files": {}, "counts": counts, "invariants": {"canonical_transaction_count": len(transactions), "full_coverage_transaction_count": len(transactions) - len(unresolved_txids), "unresolved_outpoint_transaction_count": len(unresolved_txids), "exchange_batch_transaction_count": len(batch_txids), "duplicate_candidate_count": len(duplicate_rows), "hidden_evaluation_transaction_count": len(hidden_txids), "hidden_evaluation_address_count": len(hidden_addresses), "all_fully_known_transactions_conserve_value": True, "ownership_labels_present_in_public_fixture": False}}
    for filename in GENERATED_FILES:
        target = destination / filename
        manifest["generated_files"][filename] = {"sha256": sha256_file(target), "bytes": target.stat().st_size}
    (destination / "fixture_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    validate(destination)
    return manifest


def read_ndjson(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path.name}:{number}: invalid JSON") from exc


def validate(directory: Path) -> None:
    manifest = json.loads((directory / "fixture_manifest.json").read_text(encoding="utf-8")) if (directory / "fixture_manifest.json").exists() else None
    txs = list(read_ndjson(directory / "transactions.ndjson"))
    inputs = list(read_ndjson(directory / "inputs.ndjson"))
    outputs = list(read_ndjson(directory / "outputs.ndjson"))
    funding = list(read_ndjson(directory / "funding_outpoints.ndjson"))
    if len(txs) != 100000 or len({row["txid"] for row in txs}) != 100000:
        raise ValueError("transactions must contain exactly 100000 unique txids")
    output_keys = [(row["txid"], row["vout"]) for row in outputs]
    if len(output_keys) != len(set(output_keys)):
        raise ValueError("output identities are not unique")
    known = {(row["txid"], row["vout"]): row["amount_sats"] for row in funding}
    known.update({(row["txid"], row["vout"]): row["amount_sats"] for row in outputs})
    by_tx_in: dict[str, list[dict[str, Any]]] = {}
    by_tx_out: dict[str, list[dict[str, Any]]] = {}
    for row in inputs:
        by_tx_in.setdefault(row["txid"], []).append(row)
    for row in outputs:
        by_tx_out.setdefault(row["txid"], []).append(row)
    spent: set[tuple[str, int]] = set()
    unresolved = 0
    for tx in txs:
        tx_inputs = by_tx_in[tx["txid"]]
        tx_outputs = by_tx_out[tx["txid"]]
        missing = any(row["prev_txid"] is None or row["prev_vout"] is None for row in tx_inputs)
        if missing:
            unresolved += 1
            continue
        refs = [(row["prev_txid"], row["prev_vout"]) for row in tx_inputs]
        if len(refs) != len(set(refs)) or any(ref not in known for ref in refs):
            raise ValueError(f"invalid prevout reference for {tx['txid']}")
        if spent.intersection(refs):
            raise ValueError(f"double spend in fixture for {tx['txid']}")
        spent.update(refs)
        if sum(known[ref] for ref in refs) <= sum(row["amount_sats"] for row in tx_outputs):
            raise ValueError(f"non-positive fee/value conservation failure for {tx['txid']}")
    if unresolved == 0:
        raise ValueError("fixture must include unresolved outpoints")
    if manifest is not None:
        for filename, details in manifest["generated_files"].items():
            if sha256_file(directory / filename) != details["sha256"]:
                raise ValueError(f"digest mismatch: {filename}")


def verify() -> None:
    expected = json.loads((ROOT / "fixture_manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="tracex-demo-100k-") as temporary:
        actual = generate(Path(temporary))
        if actual != expected:
            raise ValueError("regenerated manifest differs from the committed manifest")
    # Bulk NDJSON artifacts are intentionally generated locally rather than committed to Git.
    # Validate a local materialization too when one is present.
    if all((ROOT / filename).is_file() for filename in GENERATED_FILES):
        validate(ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT, help="destination directory (default: fixture directory)")
    parser.add_argument("--verify", action="store_true", help="regenerate to a temporary directory and compare manifests")
    args = parser.parse_args()
    if args.verify:
        if args.output != ROOT:
            raise SystemExit("--verify uses the checked-in fixture directory; omit --output")
        verify()
        print("fixture verification passed")
    else:
        manifest = generate(args.output)
        print(f"generated {manifest['counts']['transactions']} transactions in {args.output}")


if __name__ == "__main__":
    main()
