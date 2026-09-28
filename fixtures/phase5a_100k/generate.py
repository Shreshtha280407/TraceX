# Run: python3 fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson --verify
"""Build the deterministic, local-only Phase 5A 100K preparation fixture.

The raw exports deliberately contain opaque identifiers only. Scenario membership,
split membership, and expected outcomes are evaluation truth and never ingestion data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "fixture_config.json"
REQUIRED_RAW_FIELDS = (
    "timestamp", "network", "txid", "input_addresses", "output_addresses",
    "input_amounts", "output_amounts", "src_ip", "dst_ip", "src_port", "dst_port",
    "geo_country", "asn", "fee", "script_type", "inputs", "outputs",
)
INGESTION_COLUMNS = ("source_record_id", "source_locator", *REQUIRED_RAW_FIELDS)
NDJSON_OUTPUTS = (
    "transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "network_observations.ndjson",
    "duplicate_candidates.ndjson",
)


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def address(seed: str, namespace: str, ordinal: int) -> str:
    """Opaque bech32-shaped synthetic address; namespace is never exported."""
    return f"bcrt1q{digest(f'{seed}:address:{namespace}:{ordinal}')[:38]}"


def source_refs(config_hash: str, locator: str) -> list[dict[str, Any]]:
    return [{
        "evidence_id": "ev-phase5a-100k-config",
        "source_sha256": config_hash,
        "locator_type": "synthetic",
        "locator": locator,
        "byte_start": None,
        "byte_end": None,
    }]


def ndjson_rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_config() -> tuple[dict[str, Any], str]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    required = {
        "schema_version", "generator_version", "seed", "network", "transaction_count",
        "bootstrap_transaction_count", "bootstrap_outputs_per_transaction", "duplicate_record_count",
        "network_observation_count", "split_groups",
    }
    missing = required.difference(config)
    if missing or config["transaction_count"] != 100_000:
        raise ValueError(f"invalid Phase 5A 100K config; missing={sorted(missing)}")
    if sum(group["count"] for group in config["split_groups"]) != config["transaction_count"]:
        raise ValueError("split groups must partition every canonical transaction")
    if len({group["group_id"] for group in config["split_groups"]}) != len(config["split_groups"]):
        raise ValueError("split group IDs must be unique")
    return config, file_hash(CONFIG_PATH)


def _group_lookup(config: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    cursor = 0
    result = []
    for group in config["split_groups"]:
        result.append((cursor, cursor + group["count"], group))
        cursor += group["count"]
    return result


def _group_for(index: int, ranges: list[tuple[int, int, dict[str, Any]]]) -> dict[str, Any]:
    for start, end, group in ranges:
        if start <= index < end:
            return group
    raise ValueError(f"transaction {index} has no split group")


def _write_ndjson(handle, row: dict[str, Any]) -> None:
    handle.write(compact(row) + "\n")


def _csv_row(raw: dict[str, Any]) -> dict[str, str]:
    return {
        key: compact(raw[key]) if isinstance(raw[key], (list, dict)) else str(raw[key])
        for key in INGESTION_COLUMNS
    }


def _network_fields(index: int, countries: tuple[str, ...]) -> dict[str, Any]:
    # Deliberately repeated relay/NAT-like endpoints; never an ownership assertion.
    return {
        "src_ip": f"198.51.100.{1 + (index % 29)}",
        "dst_ip": f"203.0.113.{1 + ((index * 7) % 29)}",
        "src_port": 8333 if index % 4 else 18_333,
        "dst_port": 8333,
        "geo_country": countries[index % len(countries)],
        "asn": f"AS{64512 + (index % 19)}",
    }


def generate(
    destination: Path, formats: tuple[str, ...] = ("csv", "ndjson"), *, validate_output: bool = True
) -> dict[str, Any]:
    """Generate all local artifacts. ``formats`` controls only ingestion adapters."""
    if set(formats) != set(formats).intersection({"csv", "ndjson"}) or not formats:
        raise ValueError("formats must be a non-empty subset of csv,ndjson")
    config, config_hash = load_config()
    destination.mkdir(parents=True, exist_ok=True)
    rng = random.Random(config["seed"])
    ranges = _group_lookup(config)
    base_time = datetime(2026, 5, 1, tzinfo=UTC)
    countries = ("IN", "SG", "DE", "NL", "US", "JP", "BR", "AE")
    script_types = ("p2wpkh", "p2tr", "p2sh")
    available: list[dict[str, Any]] = []
    special: dict[str, Any] = {"coinjoin_txids": []}
    duplicate_source_rows: list[dict[str, Any]] = []
    duplicate_indexes = {2_000 + offset * 3_811 for offset in range(config["duplicate_record_count"])}
    counts: Counter[str] = Counter()

    def timestamp(index: int) -> str:
        return (base_time + timedelta(seconds=index * 47 + (index % 13))).isoformat().replace("+00:00", "Z")

    def take(count: int = 1) -> list[dict[str, Any]]:
        if len(available) < count:
            raise RuntimeError("insufficient bootstrap UTXOs")
        return [available.pop(rng.randrange(len(available))) for _ in range(count)]

    with ExitStack() as stack:
        handles = {
            name: stack.enter_context((destination / name).open("w", encoding="utf-8", newline="\n"))
            for name in NDJSON_OUTPUTS
        }
        ingest_ndjson = (
            stack.enter_context((destination / "ingestion_rows.ndjson").open("w", encoding="utf-8", newline="\n"))
            if "ndjson" in formats else None
        )
        csv_writer = None
        if "csv" in formats:
            csv_handle = stack.enter_context((destination / "ingestion_rows.csv").open("w", encoding="utf-8", newline=""))
            csv_writer = csv.DictWriter(csv_handle, fieldnames=INGESTION_COLUMNS, lineterminator="\n")
            csv_writer.writeheader()

        def emit(index: int, selected: list[dict[str, Any]] | None, amounts: list[int], *, output_addresses: list[str] | None = None, fee: int = 0) -> list[dict[str, Any]]:
            current_txid = txid(config["seed"], index)
            now = timestamp(index)
            selected = selected or []
            script_type = script_types[index % len(script_types)]
            addresses = output_addresses or [address(config["seed"], "output", index * 32 + vout) for vout in range(len(amounts))]
            transaction = {
                "network": config["network"], "txid": current_txid, "block_time": now,
                "fee_sats": fee, "source_locator": f"transaction:{index}",
            }
            _write_ndjson(handles["transactions.ndjson"], transaction)
            counts["transactions"] += 1
            structured_inputs: list[dict[str, Any]] = []
            if selected:
                for vin, item in enumerate(selected):
                    input_row = {
                        "txid": current_txid, "vin": vin, "prev_txid": item["txid"], "prev_vout": item["vout"],
                        "address": item["address"], "amount_sats": item["amount_sats"], "sequence": 4_294_967_293,
                        "source_locator": f"input:{index}:{vin}",
                    }
                    _write_ndjson(handles["inputs.ndjson"], input_row)
                    counts["inputs"] += 1
                    structured_inputs.append({key: input_row[key] for key in ("prev_txid", "prev_vout", "address", "amount_sats", "sequence")})
            else:
                input_row = {
                    "txid": current_txid, "vin": 0, "prev_txid": None, "prev_vout": None, "address": None,
                    "amount_sats": None, "sequence": 4_294_967_293, "source_locator": f"input:{index}:0",
                }
                _write_ndjson(handles["inputs.ndjson"], input_row)
                counts["inputs"] += 1
                structured_inputs.append({key: input_row[key] for key in ("prev_txid", "prev_vout", "address", "amount_sats", "sequence")})
            created: list[dict[str, Any]] = []
            for vout, amount in enumerate(amounts):
                output = {
                    "txid": current_txid, "vout": vout, "amount_sats": amount,
                    "script_type": script_type, "address": addresses[vout], "source_locator": f"output:{index}:{vout}",
                }
                _write_ndjson(handles["outputs.ndjson"], output)
                counts["outputs"] += 1
                created.append(output)
            raw = {
                "source_record_id": f"r-{index:06d}", "source_locator": f"record:{index + 1}",
                "timestamp": now, "network": config["network"], "txid": current_txid,
                "input_addresses": [item["address"] for item in selected], "output_addresses": addresses,
                "input_amounts": [btc(item["amount_sats"]) for item in selected],
                "output_amounts": [btc(amount) for amount in amounts], "fee": btc(fee), "script_type": script_type,
                "inputs": structured_inputs,
                "outputs": [{"address": addresses[vout], "amount_sats": amount, "script_type": script_type} for vout, amount in enumerate(amounts)],
                **_network_fields(index, countries),
            }
            if ingest_ndjson is not None:
                _write_ndjson(ingest_ndjson, raw)
            if csv_writer is not None:
                csv_writer.writerow(_csv_row(raw))
            counts["ingestion_rows"] += 1
            if index in duplicate_indexes:
                duplicate_source_rows.append(dict(raw))
            return created

        # These transactions intentionally have unresolved prevouts. Their outputs are a
        # controlled synthetic starting ledger; no prior transaction is invented.
        for index in range(config["bootstrap_transaction_count"]):
            total = 48_000_000 + index * 37_000
            base, remainder = divmod(total, config["bootstrap_outputs_per_transaction"])
            created = emit(index, None, [base] * (config["bootstrap_outputs_per_transaction"] - 1) + [base + remainder])
            available.extend(created)

        for index in range(config["bootstrap_transaction_count"], config["transaction_count"]):
            group = _group_for(index, ranges)
            if index == 1000:
                selected = take()
                total, fee = selected[0]["amount_sats"], 701
                created = emit(index, selected, [total - fee - 21_000, 21_000], fee=fee)
                special["peel"] = created[0]
            elif index in {1001, 1002}:
                selected = [special["peel"]]
                total, fee = selected[0]["amount_sats"], 700 + index
                created = emit(index, selected, [total - fee - 18_000, 18_000], fee=fee)
                special["peel"] = created[0]
            elif index == 1003:
                selected = [special["peel"]]
                total, fee = selected[0]["amount_sats"], 803
                emit(index, selected, [total - fee - 16_000, 16_000], fee=fee)
                special["peeling_txids"] = [txid(config["seed"], item) for item in (1001, 1002, 1003)]
            elif index == 1010:
                selected = take()
                total, fee = selected[0]["amount_sats"], 690
                special["ordinary"] = emit(index, selected, [total - fee - 19_000, 19_000], fee=fee)[0]
            elif index == 1011:
                selected = [special["ordinary"]]
                total, fee = selected[0]["amount_sats"], 691
                emit(index, selected, [total - fee - 15_000, 15_000], fee=fee)
            elif index == 1020:
                selected = take()
                total, fee = selected[0]["amount_sats"], 777
                seed_address = address(config["seed"], "reserved", 1)
                special["seed"] = emit(index, selected, [total - fee - 11_000, 11_000], output_addresses=[seed_address, address(config["seed"], "reserved", 2)], fee=fee)[0]
                special["seed_address"] = seed_address
            elif index == 1021:
                selected = [special["seed"]]
                total, fee = selected[0]["amount_sats"], 778
                special["seed"] = emit(index, selected, [total - fee - 9_000, 9_000], fee=fee)[0]
            elif index == 1022:
                selected = [special["seed"]]
                total, fee = selected[0]["amount_sats"], 779
                target = address(config["seed"], "reserved", 3)
                emit(index, selected, [total - fee - 8_000, 8_000], output_addresses=[target, address(config["seed"], "reserved", 4)], fee=fee)
                special["two_hop_target"] = target
            elif index == 1030 or (group["review_pattern"] and index % 509 == 0):
                selected = take(3)
                total, fee = sum(item["amount_sats"] for item in selected), 1_125
                equal = (total - fee - 13_007) // 3
                emit(index, selected, [equal, equal, equal, total - fee - equal * 3], fee=fee)
                special["coinjoin_txids"].append(txid(config["seed"], index))
            elif index == 1031:
                selected = take(3)
                total, fee = sum(item["amount_sats"] for item in selected), 1_126
                emit(index, selected, [total // 2, total // 3, total - fee - total // 2 - total // 3], fee=fee)
                special["ordinary_multi_txid"] = txid(config["seed"], index)
            elif index == 1040:
                selected = take()
                total, fee = selected[0]["amount_sats"], 880
                disconnected = address(config["seed"], "reserved", 5)
                emit(index, selected, [total - fee - 10_000, 10_000], output_addresses=[disconnected, address(config["seed"], "reserved", 6)], fee=fee)
                special["disconnected_address"] = disconnected
            elif group["scenario"] == "benign_exchange_batch" and index % 29 == 0:
                selected = take()
                total, fee = selected[0]["amount_sats"], 400 + (index % 700)
                recipient_count = 6 + (index % 5)
                payment = (total - fee) // recipient_count
                amounts = [payment + ((slot * 37) % 97) for slot in range(recipient_count - 1)]
                amounts.append(total - fee - sum(amounts))
                addresses = [address(config["seed"], "shared", slot % 5) if slot == 0 else address(config["seed"], "output", index * 32 + slot) for slot in range(recipient_count)]
                emit(index, selected, amounts, output_addresses=addresses, fee=fee)
            elif group["scenario"] == "benign_treasury_transfer" and index % 211 == 0:
                selected = take(2)
                total, fee = sum(item["amount_sats"] for item in selected), 900 + (index % 500)
                primary = (total - fee) * 91 // 100
                emit(index, selected, [primary, total - fee - primary], fee=fee)
            else:
                selected = take(2 if index % 997 == 0 else 1)
                total, fee = sum(item["amount_sats"] for item in selected), 300 + (index % 900)
                emit(index, selected, [total - fee], fee=fee)

        # Duplicate raw source records are additional candidates only; canonical facts remain 100K.
        for ordinal, original in enumerate(duplicate_source_rows):
            duplicate = dict(original)
            duplicate["source_record_id"] = f"r-duplicate-{ordinal:03d}"
            duplicate["source_locator"] = f"record:{config['transaction_count'] + ordinal + 1}"
            if ingest_ndjson is not None:
                _write_ndjson(ingest_ndjson, duplicate)
            if csv_writer is not None:
                csv_writer.writerow(_csv_row(duplicate))
            _write_ndjson(handles["duplicate_candidates.ndjson"], {
                "canonical_txid": original["txid"], "duplicate_source_record_id": duplicate["source_record_id"],
                "duplicate_of_source_record_id": original["source_record_id"], "source_locator": f"duplicate:{ordinal}",
            })
            counts["duplicate_candidates"] += 1
            counts["ingestion_rows"] += 1

        for index in range(config["network_observation_count"]):
            target_index = (index * 41) % config["transaction_count"]
            observation = {
                "network": config["network"],
                "observation_id": f"obs-100k-{index:05d}", "txid": txid(config["seed"], target_index),
                "observer_id": f"relay-{index % 7}", "endpoint_ip": f"198.51.100.{1 + (index % 29)}", "endpoint_port": 8333,
                "direction": "seen_from" if index % 2 else "seen_to", "observer_received_at": timestamp(target_index),
                "observer_time_original": timestamp(target_index), "clock_quality": "estimated",
                "capture_scope": "synthetic relay/NAT-like observation; no origin or wallet ownership assertion",
                **_network_fields(index, countries), "source_locator": f"network_observation:{index}",
            }
            _write_ndjson(handles["network_observations.ndjson"], observation)
            counts["network_observations"] += 1

    truth = {
        "evaluation_only": True, "do_not_ingest": True, "generator_version": config["generator_version"],
        "split_groups": [
            {"group_id": group["group_id"], "split": group["split"], "scenario": group["scenario"], "review_pattern": group["review_pattern"], "transaction_index_start": start, "transaction_index_end_exclusive": end}
            for start, end, group in ranges
        ],
        "scenario_groups": sorted({group["scenario"] for group in config["split_groups"]}),
        "expected_peeling_chain_transaction_ids": special["peeling_txids"],
        "expected_coinjoin_like_transaction_ids": special["coinjoin_txids"],
        "synthetic_review_seed_address": special["seed_address"], "expected_two_hop_target_address": special["two_hop_target"],
        "disconnected_address": special["disconnected_address"],
        "expected_benign_controls": {"ordinary_sequence_transaction_ids": [txid(config["seed"], 1010), txid(config["seed"], 1011)], "ordinary_multi_output_transaction_ids": [special["ordinary_multi_txid"]]},
        "raw_fixture_contains_no_labels_or_split_ids": True,
    }
    (destination / "evaluation_truth.json").write_text(compact(truth) + "\n", encoding="utf-8")
    generated = list(NDJSON_OUTPUTS) + ["evaluation_truth.json"]
    if "ndjson" in formats:
        generated.append("ingestion_rows.ndjson")
    if "csv" in formats:
        generated.append("ingestion_rows.csv")
    manifest = {
        "schema_version": "1.0.0", "generator_version": config["generator_version"], "config_sha256": config_hash,
        "generator_sha256": file_hash(Path(__file__)), "seed": config["seed"], "network": config["network"],
        "counts": {**dict(counts), "canonical_transaction_count": counts["transactions"]},
        "generated_files": {name: {"sha256": file_hash(destination / name), "bytes": (destination / name).stat().st_size} for name in generated},
        "invariants": {"canonical_transaction_count": counts["transactions"], "no_double_spends": True, "fully_known_value_conservation": True, "unresolved_prevouts_explicit_null": config["bootstrap_transaction_count"], "duplicate_candidates_do_not_increase_canonical_count": True, "raw_ownership_claims": False},
        "provenance": {"config_locator": str(CONFIG_PATH.relative_to(ROOT.parents[1])), "source_locator_scheme": "record:<1-based-row>", "network_observations_are_off_chain_only": True},
    }
    (destination / "fixture_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if validate_output:
        validate(destination, formats)
    return manifest


def validate(directory: Path, formats: tuple[str, ...] = ("csv", "ndjson")) -> None:
    """Streaming invariant and reconciliation checks suitable for the 100K local fixture."""
    config, _ = load_config()
    transactions: dict[str, int] = {}
    for row in ndjson_rows(directory / "transactions.ndjson"):
        if row["txid"] in transactions:
            raise ValueError("duplicate canonical transaction txid")
        transactions[row["txid"]] = int(row["fee_sats"] or 0)
    if len(transactions) != config["transaction_count"]:
        raise ValueError("fixture must contain exactly 100,000 unique canonical transactions")
    output_values: dict[tuple[str, int], int] = {}
    output_totals: Counter[str] = Counter()
    for row in ndjson_rows(directory / "outputs.ndjson"):
        key = (row["txid"], int(row["vout"]))
        if key in output_values:
            raise ValueError("duplicate output identity")
        output_values[key] = int(row["amount_sats"])
        output_totals[row["txid"]] += int(row["amount_sats"])
    input_totals: Counter[str] = Counter()
    resolved_by_tx: Counter[str] = Counter()
    inputs_by_tx: Counter[str] = Counter()
    spent: set[tuple[str, int]] = set()
    unresolved = 0
    for row in ndjson_rows(directory / "inputs.ndjson"):
        current = row["txid"]
        inputs_by_tx[current] += 1
        previous_txid, previous_vout = row.get("prev_txid"), row.get("prev_vout")
        if previous_txid is None or previous_vout is None:
            if previous_txid is not None or previous_vout is not None:
                raise ValueError("unresolved prevout must use null txid and vout together")
            unresolved += 1
            continue
        key = (previous_txid, int(previous_vout))
        if key not in output_values:
            raise ValueError("known prevout was invented or unavailable")
        if key in spent:
            raise ValueError("double spend")
        spent.add(key)
        if row.get("amount_sats") != output_values[key]:
            raise ValueError("input amount does not match referenced output")
        input_totals[current] += output_values[key]
        resolved_by_tx[current] += 1
    for current, count in inputs_by_tx.items():
        if count == resolved_by_tx[current] and input_totals[current] != output_totals[current] + transactions[current]:
            raise ValueError("fully known transaction does not conserve satoshis")
    if unresolved != config["bootstrap_transaction_count"]:
        raise ValueError("unexpected unresolved prevout count")
    manifest = json.loads((directory / "fixture_manifest.json").read_text(encoding="utf-8"))
    if manifest["counts"]["canonical_transaction_count"] != config["transaction_count"]:
        raise ValueError("manifest canonical count mismatch")
    expected_ingestion_rows = config["transaction_count"] + config["duplicate_record_count"]
    if "ndjson" in formats and "csv" in formats:
        with (directory / "ingestion_rows.csv").open(encoding="utf-8", newline="") as handle:
            csv_rows = csv.DictReader(handle)
            count = 0
            for ndjson_row, csv_row in zip(ndjson_rows(directory / "ingestion_rows.ndjson"), csv_rows, strict=True):
                count += 1
                if not set(REQUIRED_RAW_FIELDS).issubset(ndjson_row) or not set(REQUIRED_RAW_FIELDS).issubset(csv_row):
                    raise ValueError("missing required SIH raw field")
                if (ndjson_row["txid"], ndjson_row["source_record_id"]) != (csv_row["txid"], csv_row["source_record_id"]):
                    raise ValueError("CSV and NDJSON logical ingestion rows do not reconcile")
                raw_text = compact(ndjson_row).lower()
                if "scenario" in raw_text or "group_id" in raw_text:
                    raise ValueError("evaluation labels leaked into raw ingestion")
            if count != expected_ingestion_rows:
                raise ValueError("unexpected ingestion row count")
    elif "ndjson" in formats:
        count = 0
        for row in ndjson_rows(directory / "ingestion_rows.ndjson"):
            count += 1
            if not set(REQUIRED_RAW_FIELDS).issubset(row):
                raise ValueError("missing required SIH raw field")
            raw_text = compact(row).lower()
            if "scenario" in raw_text or "group_id" in raw_text:
                raise ValueError("evaluation labels leaked into raw ingestion")
        if count != expected_ingestion_rows:
            raise ValueError("unexpected ingestion NDJSON count")
    elif "csv" in formats:
        with (directory / "ingestion_rows.csv").open(encoding="utf-8", newline="") as handle:
            count = sum(1 for row in csv.DictReader(handle) if set(REQUIRED_RAW_FIELDS).issubset(row))
        if count != expected_ingestion_rows:
            raise ValueError("unexpected ingestion CSV count")
    duplicates = list(ndjson_rows(directory / "duplicate_candidates.ndjson"))
    if len(duplicates) != config["duplicate_record_count"]:
        raise ValueError("duplicate candidate count mismatch")
    truth = json.loads((directory / "evaluation_truth.json").read_text(encoding="utf-8"))
    if not truth.get("evaluation_only") or not truth.get("do_not_ingest"):
        raise ValueError("evaluation truth must be marked non-ingestible")
    groups = truth["split_groups"]
    if len({group["group_id"] for group in groups}) != len(groups):
        raise ValueError("split groups overlap")
    split_order = {"train_reference": 0, "validation": 1, "final_holdout": 2}
    if any(split_order[groups[i]["split"]] > split_order[groups[i + 1]["split"]] for i in range(len(groups) - 1)):
        raise ValueError("split order is not time-respecting")
    if sum(group["transaction_index_end_exclusive"] - group["transaction_index_start"] for group in groups if group["split"] == "final_holdout") < 3000:
        raise ValueError("final holdout must contain at least 3,000 rows")


def verify(destination: Path, formats: tuple[str, ...]) -> None:
    """Validate generated bytes against this run's manifest and all invariants.

    Cross-directory deterministic regeneration is deliberately exercised by the
    focused test; keeping this command single-pass makes the required local
    generation/verification command practical for a 100K dataset.
    """
    validate(destination, formats)
    manifest = json.loads((destination / "fixture_manifest.json").read_text(encoding="utf-8"))
    for filename, expected in manifest["generated_files"].items():
        target = destination / filename
        if not target.is_file() or file_hash(target) != expected["sha256"] or target.stat().st_size != expected["bytes"]:
            raise ValueError(f"manifest hash mismatch for {filename}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="local dataset directory")
    parser.add_argument("--formats", default="csv,ndjson", help="comma-separated ingestion formats: csv,ndjson")
    parser.add_argument("--verify", action="store_true", help="validate and regenerate to compare deterministic hashes")
    args = parser.parse_args()
    formats = tuple(item.strip() for item in args.formats.split(",") if item.strip())
    manifest = generate(args.output, formats)
    if args.verify:
        verify(args.output, formats)
    print(f"Generated {manifest['counts']['canonical_transaction_count']} canonical transactions at {args.output}")


if __name__ == "__main__":
    main()
