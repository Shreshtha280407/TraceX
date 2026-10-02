"""Replicate the labelled 100K fixture into a realistic multi-million-row stress set.

    uv run python scripts/scale_fixture.py --copies 10 --output datasets/scaled/rows_1m.ndjson
    uv run python scripts/scale_fixture.py --copies 30 --output datasets/scaled/rows_3m.ndjson

Unlike the linear-chain throughput generator in phase6_throughput.py, every
copy keeps the full structure of generator v2: motif episodes (peeling chains,
CoinJoin-like transactions, surges), benign near-misses, batch payouts,
consolidations, unresolved outpoints, duplicate rows and network fields. Copy k
gets its own txids and addresses (a keyed hash of the originals, so spends
still resolve within the copy) and is shifted forward in time by k times the
fixture's span, so the copies form one continuous, time-ordered history.

This is a load test for the pipeline, not a new labelled dataset: copies are
structurally identical, so detection quality must still be measured on the
original fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _txid(value: str, copy: int) -> str:
    if copy == 0:
        return value
    return hashlib.sha256(f"{copy}:{value}".encode()).hexdigest()


def _address(value: str | None, copy: int) -> str | None:
    if not value or copy == 0:
        return value
    digest = hashlib.blake2b(f"{copy}:{value}".encode(), digest_size=20).hexdigest()
    prefix = value[: value.find("1") + 1] if "1" in value[:6] else value[:4]
    return f"{prefix}{digest}"


def _shift(value: str | None, offset: timedelta) -> str | None:
    if not value or not offset:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return (parsed + offset).astimezone(UTC).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, default=REPO / "datasets" / "phase5a_100k" / "ingestion_rows.ndjson")
    parser.add_argument("--copies", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.source.open(encoding="utf-8") if line.strip()]
    times = [datetime.fromisoformat(row["timestamp"]) for row in rows if row.get("timestamp")]
    span = (max(times) - min(times)) + timedelta(hours=1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with args.output.open("w", encoding="utf-8") as handle:
        for copy in range(args.copies):
            offset = span * copy
            for row in rows:
                value = dict(row)
                value["txid"] = _txid(row["txid"], copy)
                for key in ("timestamp", "block_time", "observed_at"):
                    if key in value:
                        value[key] = _shift(value[key], offset)
                value["inputs"] = [
                    {**item, "address": _address(item.get("address"), copy),
                     "prev_txid": _txid(item["prev_txid"], copy) if item.get("prev_txid") else item.get("prev_txid")}
                    for item in row.get("inputs") or []
                ]
                value["outputs"] = [{**item, "address": _address(item.get("address"), copy)}
                                    for item in row.get("outputs") or []]
                value["input_addresses"] = [_address(item, copy) for item in row.get("input_addresses") or []]
                value["output_addresses"] = [_address(item, copy) for item in row.get("output_addresses") or []]
                if "source_record_id" in value:
                    value["source_record_id"] = f"{row['source_record_id']}-c{copy}"
                handle.write(json.dumps(value, separators=(",", ":")) + "\n")
                written += 1
    print(f"wrote {written} rows ({args.copies} copies) to {args.output} ({args.output.stat().st_size / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
