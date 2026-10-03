"""Prepare checksummed licensed Geo-IP build assets; never includes evidence/secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def verify_cache(source):
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("format_version") != 1 or not manifest.get("sources"):
        raise ValueError("Missing licensed compiled database provenance")
    sums = (source / "SHA256SUMS").read_text().splitlines()
    for line in sums:
        expected, name = line.split("  ", 1)
        path = source / name
        if path.parent != source or path.is_symlink() or not path.is_file():
            raise ValueError("invalid checksum inventory path")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"Geo-IP cache checksum mismatch: {name}")
    names = {line.split("  ", 1)[1] for line in sums}
    if names != {path.name for path in source.iterdir() if path.name != "SHA256SUMS"}:
        raise ValueError("Geo-IP checksum inventory is incomplete")
    return {"files_verified": len(names), "sources": manifest["sources"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("var/geoip/compiled"))
    parser.add_argument("--verify-existing", action="store_true", help="verify an existing build cache without overwriting it")
    args = parser.parse_args(argv)
    source = args.source.resolve(strict=True)
    if args.verify_existing:
        print(json.dumps(verify_cache(source)))
        return
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
