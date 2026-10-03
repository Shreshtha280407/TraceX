"""Independent exact canonical counts for generator NDJSON (truth never read)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def counts(source):
    seen = set()
    result = {"transactions": 0, "inputs": 0, "outputs": 0, "network_observations": 0, "quarantine": 0}
    source_rows = 0
    with source.open() as handle:
        for line in handle:
            if not line.strip():
                continue
            source_rows += 1
            row = json.loads(line)
            identity = bytes.fromhex(row["txid"])
            if identity in seen:
                result["quarantine"] += 1
                continue
            seen.add(identity)
            result["transactions"] += 1
            result["inputs"] += len(row["inputs"])
            result["outputs"] += len(row["outputs"])
            # The generator always contributes one observation row, including
            # explicit null endpoint metadata. Null is not a known endpoint.
            result["network_observations"] += 1
    return result, source_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result, source_rows = counts(args.source)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
    with args.source.open("rb") as handle:
        source_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    with args.output.with_suffix(".provenance.json").open("x") as handle:
        json.dump({"method": "independent raw generator row/array counting; no production parser or truth",
                   "source_sha256": source_hash, "source_rows": source_rows, "counts": result}, handle, indent=2)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
