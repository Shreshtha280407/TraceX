"""Prepare checksummed licensed Geo-IP build assets; never includes evidence/secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("var/geoip/compiled"))
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("format_version") != 1 or not manifest.get("sources"):
        raise ValueError("Missing licensed compiled database provenance")
    allowed = {"manifest.json", "asn_orgs.json"}
    allowed.update(f"{prefix}_{family}.{suffix}" for prefix in ("asn", "country")
                   for family in ("v4", "v6") for suffix in ("start", "end", "value", "org"))
    files = sorted(source.iterdir())
    if any(p.name not in allowed or not p.is_file() or p.is_symlink() for p in files):
        raise ValueError("Unexpected build input; only known compiled database files are allowed")
    destination = Path(__file__).resolve().parents[1] / "deploy/appliance/geoip-cache/compiled"
    shutil.copytree(source, destination)  # exclusive: never overwrites an existing cache
    checksums = []
    for path in files:
        with path.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        with (destination / path.name).open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != checksum:
                raise ValueError(f"Copied database integrity mismatch: {path.name}")
        checksums.append(f"{checksum}  {path.name}\n")
    with (destination / "SHA256SUMS").open("x") as handle:
        handle.writelines(checksums)
    print(json.dumps({"destination": str(destination), "files_verified": len(files), "sources": manifest["sources"]}))


if __name__ == "__main__":
    main()
