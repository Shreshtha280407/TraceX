"""Prepare unfamiliar raw-prefix and missing-IP/prevout demo variants, label-free."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=10000)
    args = parser.parse_args()
    manifest = json.loads((args.source.parent / "dataset_manifest.json").read_text())
    with args.source.open("rb") as handle:
        checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    if checksum != manifest["files"][args.source.name]["sha256"]:
        raise ValueError("Demo parent source integrity mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    count = 0
    with args.source.open("rb") as source, (args.output / "unfamiliar.ndjson").open("xb") as original, \
            (args.output / "missing-ip-prevouts.ndjson").open("x") as variant:
        for line in source:
            if count >= args.rows:
                break
            original.write(line)
            row = json.loads(line)
            for key in ("src_ip", "dst_ip", "src_port", "dst_port", "geo_country", "asn", "observed_at"):
                row.pop(key, None)
            for item in row.get("inputs", []):
                item.pop("prev_txid", None)
                item.pop("prev_vout", None)
            variant.write(json.dumps(row, separators=(",", ":")) + "\n")
            count += 1
    files = {}
    for name in ("unfamiliar.ndjson", "missing-ip-prevouts.ndjson"):
        path = args.output / name
        with path.open("rb") as handle:
            files[name] = {"bytes": path.stat().st_size, "sha256": hashlib.file_digest(handle, "sha256").hexdigest()}
    report = {"synthetic": True, "source": str(args.source), "source_sha256": checksum, "rows": count,
              "truth_used": False, "selection": "raw first rows, no label or score selection", "files": files,
              "variant": "remove network fields and input outpoints; never fabricate references or locations"}
    with (args.output / "manifest.json").open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
