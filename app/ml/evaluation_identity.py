"""Stable source identities for offline evaluation; never inference features."""
import hashlib
import json
from pathlib import Path


def final_id(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


def fingerprint(directory):
    directory = Path(directory)
    inventory = {}
    for name in ("transactions", "inputs", "outputs", "network_observations", "ingestion_rows", "relay_events"):
        path = directory / (name + ".ndjson")
        if not path.is_file():
            if name in {"relay_events", "network_observations"}:
                continue
            raise ValueError(f"canonical evaluation source missing: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(1 << 20):
                digest.update(block)
        inventory[path.name] = {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}
    return {"source_sha256": hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(), "files": inventory}
