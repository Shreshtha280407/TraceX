"""Immutable, receipt-ready Parquet fragment publication."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class FragmentArtifact:
    record_type: str
    record_count: int
    sha256: str
    byte_size: int
    storage_relative_path: str


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _envelope(
    record_type: str,
    facts: list[dict[str, Any]],
    *,
    source_id: str,
    source_sha256: str,
    logical_record: int | None = None,
    canonical: list[str] | None = None,
) -> list[dict[str, Any]]:
    """`canonical`, when given, is each fact's canonical JSON already
    serialized (app.engine.canonical.batch), in the same order."""
    rows = []
    for position, fact in enumerate(facts):
        refs = fact.get("source_refs", [])
        locator = refs[0]["locator"] if refs else None
        rows.append(
            {
                "record_type": record_type,
                "case_id": fact.get("case_id"),
                "source_id": source_id,
                "source_sha256": source_sha256,
                "txid": fact.get("txid"),
                "logical_record": logical_record or _logical_record(locator),
                "source_locator": locator,
                "canonical_json": canonical[position] if canonical is not None
                else json.dumps(fact, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            }
        )
    return rows


def _logical_record(locator: str | None) -> int | None:
    if not locator:
        return None
    try:
        if locator.startswith("record:"):
            return int(locator.split(":", 1)[1])
        if locator.startswith("/"):
            return int(locator.rsplit("/", 1)[-1].strip("[]")) + 1
    except ValueError:
        return None
    return None


def _write_immutable(path: Path, rows: list[dict[str, Any]]) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    temporary = staging / f"{path.name}.{uuid.uuid4().hex}.part"
    try:
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, temporary, compression="zstd", use_dictionary=True, write_statistics=True)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        digest = _digest(temporary)
        size = temporary.stat().st_size
        if path.exists():
            if _digest(path) != digest:
                raise RuntimeError(f"immutable fragment conflict at {path}")
            temporary.unlink()
        else:
            os.replace(temporary, path)
        return digest, size
    finally:
        temporary.unlink(missing_ok=True)


def publish_batch(
    *,
    evidence_root: Path,
    case_id: str,
    source_sha256: str,
    source_id: str,
    logical_batch: int,
    facts: dict[str, list[dict[str, Any]]],
    canonical: dict[str, list[str]] | None = None,
) -> list[FragmentArtifact]:
    """Write a batch's non-empty fact groups before their DB receipts are committed.

    `canonical` optionally carries each group's facts already serialized; a
    group whose list does not match its facts one-to-one is serialized here."""
    base = evidence_root / case_id / "derived" / source_sha256 / f"batch-{logical_batch:08d}"
    artifacts = []
    for record_type, records in facts.items():
        if not records:
            continue
        serialized = (canonical or {}).get(record_type)
        rows = _envelope(
            record_type, records, source_id=source_id, source_sha256=source_sha256,
            canonical=serialized if serialized is not None and len(serialized) == len(records) else None,
        )
        relative = Path(case_id) / "derived" / source_sha256 / f"batch-{logical_batch:08d}" / f"{record_type}.parquet"
        digest, size = _write_immutable(base / f"{record_type}.parquet", rows)
        artifacts.append(FragmentArtifact(record_type, len(rows), digest, size, relative.as_posix()))
    return artifacts
