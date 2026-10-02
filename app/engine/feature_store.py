"""Columnar storage for address-window feature rows.

Every committed snapshot produces about eight address-window feature rows per
transaction (15-minute, 1-hour and 24-hour windows for each address it pays).
At 100K transactions that was 834K JSON rows and ~3.9 GB of SQLite; at three
million transactions it would be ~25 million rows and over 100 GB -- more disk
and insert time than the whole rest of the pipeline. The rows are write-once
analytical facts (only the few a finding borrows are read back, and those are
copied into the finding), so they are written here as one zstd-compressed
Parquet file per snapshot in the evidence vault, with its SHA-256 recorded in
`feature_stores`. The row content is exactly what the database rows held.

Snapshots imported before this change still have `feature_records` rows; the
readers below serve both.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FeatureRecord, FeatureStoreRecord, GraphSnapshot, Snapshot

SCHEMA = pa.schema([
    ("feature_row_id", pa.string()),
    ("entity_ref", pa.string()),
    ("window_start", pa.timestamp("us", tz="UTC")),
    ("window_end", pa.timestamp("us", tz="UTC")),
    ("feature_schema_version", pa.dictionary(pa.int32(), pa.string())),
    ("feature_vector", pa.string()),
    ("source_refs", pa.string()),
])
_ROW_GROUP_ROWS = 50_000


def _safe(value: str) -> str:
    return value.replace("-", "").replace("/", "")


def relative_path(snapshot: Snapshot) -> Path:
    return Path(snapshot.case_id) / "features" / f"snapshot-{_safe(snapshot.id)}.parquet"


def _resolve(evidence_root: Path, relative: str | Path) -> Path:
    root = Path(evidence_root).resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise RuntimeError("feature store path escaped evidence root")
    return path


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class FeatureStoreWriter:
    """Appends feature rows to a temporary Parquet file; `publish` makes it the
    snapshot's immutable feature store and records it."""

    def __init__(
        self, evidence_root: Path, snapshot: Snapshot, graph: GraphSnapshot, coverage_json: str, *, part: bool = False,
    ) -> None:
        self.evidence_root = Path(evidence_root)
        self.snapshot = snapshot
        self.graph = graph
        self.coverage_json = coverage_json
        self.relative = relative_path(snapshot)
        self.target = _resolve(self.evidence_root, self.relative)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = self.target.with_name(f".{self.target.name}.{uuid.uuid4().hex}.part")
        # A part (one worker's share, see app.engine.bounded) is re-read once by
        # `append_part`, so it is written with a fast codec instead.
        options = {"compression": "lz4"} if part else {"compression": "zstd", "compression_level": 3}
        self._writer = pq.ParquetWriter(self.temporary, SCHEMA, **options)
        self._pending: list[dict[str, Any]] = []
        self.rows = 0
        self.schema_version: str | None = None

    def write(self, rows: list[dict[str, Any]]) -> None:
        self._pending.extend(rows)
        if len(self._pending) >= _ROW_GROUP_ROWS:
            self._flush()

    def _flush(self) -> None:
        rows = self._pending
        if not rows:
            return
        self._pending = []
        self.schema_version = self.schema_version or rows[0]["feature_schema_version"]
        table = pa.table({
            "feature_row_id": pa.array([row["id"] for row in rows], pa.string()),
            "entity_ref": pa.array([row["entity_ref"] for row in rows], pa.string()),
            "window_start": pa.array([row["window_start"] for row in rows], pa.timestamp("us", tz="UTC")),
            "window_end": pa.array([row["window_end"] for row in rows], pa.timestamp("us", tz="UTC")),
            "feature_schema_version": pa.array(
                [row["feature_schema_version"] for row in rows], pa.string()
            ).dictionary_encode(),
            "feature_vector": pa.array([row["feature_vector"] for row in rows], pa.string()),
            "source_refs": pa.array([row["source_refs"] for row in rows], pa.string()),
        }, schema=SCHEMA)
        self._writer.write_table(table)
        self.rows += len(rows)

    def close_part(self) -> tuple[Path, int, str | None]:
        """Finish a part file: (its path, rows written, feature schema version)."""
        self._flush()
        self._writer.close()
        return self.temporary, self.rows, self.schema_version

    def append_part(self, path: Path, rows: int, schema_version: str | None) -> None:
        """Copy a finished part's rows, in order, into this store, then delete it."""
        self._flush()
        try:
            for batch in pq.ParquetFile(path).iter_batches(batch_size=_ROW_GROUP_ROWS):
                self._writer.write_batch(batch)
        finally:
            Path(path).unlink(missing_ok=True)
        self.rows += rows
        self.schema_version = self.schema_version or schema_version

    def publish(self, session: Session, feature_schema_version: str) -> FeatureStoreRecord:
        self._flush()
        self._writer.close()
        with self.temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(self.temporary, self.target)
        record = FeatureStoreRecord(
            case_id=self.snapshot.case_id,
            snapshot_id=self.snapshot.id,
            graph_snapshot_id=self.graph.id,
            storage_relative_path=self.relative.as_posix(),
            sha256=_hash(self.target),
            row_count=self.rows,
            feature_schema_version=self.schema_version or feature_schema_version,
            coverage=json.loads(self.coverage_json),
        )
        session.add(record)
        session.flush()
        return record

    def abort(self) -> None:
        try:
            self._writer.close()
        finally:
            self.temporary.unlink(missing_ok=True)


def has_features(session: Session, snapshot_id: str) -> bool:
    return bool(
        session.scalar(select(FeatureStoreRecord.id).where(FeatureStoreRecord.snapshot_id == snapshot_id).limit(1))
        or session.scalar(select(FeatureRecord.id).where(FeatureRecord.snapshot_id == snapshot_id).limit(1))
    )


def feature_count(session: Session, *, case_id: str, snapshot_id: str | None = None) -> int:
    stores = select(func.coalesce(func.sum(FeatureStoreRecord.row_count), 0)).where(FeatureStoreRecord.case_id == case_id)
    legacy = select(func.count()).select_from(FeatureRecord).where(FeatureRecord.case_id == case_id)
    if snapshot_id:
        stores = stores.where(FeatureStoreRecord.snapshot_id == snapshot_id)
        legacy = legacy.where(FeatureRecord.snapshot_id == snapshot_id)
    return int(session.scalar(stores) or 0) + int(session.scalar(legacy) or 0)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def iter_feature_rows(
    session: Session, evidence_root: Path, *, case_id: str, snapshot_id: str | None = None,
    offset: int = 0, limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Export-shaped feature rows: legacy database rows first, then each stored
    snapshot file in snapshot order (rows within a file are in window order)."""
    skipped = 0
    produced = 0

    def emit(row: dict[str, Any]) -> bool:
        nonlocal skipped, produced
        if skipped < offset:
            skipped += 1
            return False
        produced += 1
        return True

    legacy = select(FeatureRecord).where(FeatureRecord.case_id == case_id)
    if snapshot_id:
        legacy = legacy.where(FeatureRecord.snapshot_id == snapshot_id)
    for row in session.scalars(
        legacy.order_by(FeatureRecord.snapshot_id, FeatureRecord.window_start, FeatureRecord.entity_ref)
        .execution_options(yield_per=5000)
    ):
        if limit is not None and produced >= limit:
            return
        view = {
            "feature_row_id": row.id, "entity_ref": row.entity_ref, "snapshot_id": row.snapshot_id,
            "graph_snapshot_id": row.graph_snapshot_id, "window_start": _iso(row.window_start),
            "window_end": _iso(row.window_end), "coverage": row.coverage, "source_refs": row.source_refs,
            "features": row.feature_vector,
        }
        if emit(view):
            yield view
    stores = select(FeatureStoreRecord).where(FeatureStoreRecord.case_id == case_id)
    if snapshot_id:
        stores = stores.where(FeatureStoreRecord.snapshot_id == snapshot_id)
    decode = json.JSONDecoder().decode
    for record in session.scalars(stores.order_by(FeatureStoreRecord.snapshot_id)):
        if offset - skipped >= record.row_count:
            skipped += record.row_count  # skip the whole file without reading it
            continue
        parquet = pq.ParquetFile(_resolve(evidence_root, record.storage_relative_path))
        for batch in parquet.iter_batches(batch_size=5000):
            columns = batch.to_pydict()
            for index in range(batch.num_rows):
                if limit is not None and produced >= limit:
                    return
                if skipped < offset:
                    skipped += 1
                    continue
                produced += 1
                yield {
                    "feature_row_id": columns["feature_row_id"][index],
                    "entity_ref": columns["entity_ref"][index],
                    "snapshot_id": record.snapshot_id,
                    "graph_snapshot_id": record.graph_snapshot_id,
                    "window_start": _iso(columns["window_start"][index]),
                    "window_end": _iso(columns["window_end"][index]),
                    "coverage": record.coverage,
                    "source_refs": decode(columns["source_refs"][index]),
                    "features": decode(columns["feature_vector"][index]),
                }


def store_path(session: Session, evidence_root: Path, snapshot_id: str) -> Path | None:
    record = session.scalar(select(FeatureStoreRecord).where(FeatureStoreRecord.snapshot_id == snapshot_id))
    return _resolve(evidence_root, record.storage_relative_path) if record else None


def rewrite_vectors(
    session: Session, evidence_root: Path, *, snapshot_id: str, entity_refs: set[str],
    transform: Callable[[str, datetime, dict[str, Any]], dict[str, Any] | None],
) -> int:
    """Streaming in-place update of the feature vectors of `entity_refs`
    (synthetic-seed proximity); returns rows changed. The file is rewritten
    one row group at a time and swapped in atomically."""
    record = session.scalar(select(FeatureStoreRecord).where(FeatureStoreRecord.snapshot_id == snapshot_id))
    if record is None or not entity_refs:
        return 0
    source = _resolve(evidence_root, record.storage_relative_path)
    temporary = source.with_name(f".{source.name}.{uuid.uuid4().hex}.part")
    parquet = pq.ParquetFile(source)
    writer = pq.ParquetWriter(temporary, parquet.schema_arrow, compression="zstd", compression_level=3)
    changed = 0
    decode = json.JSONDecoder().decode
    try:
        for batch in parquet.iter_batches(batch_size=_ROW_GROUP_ROWS):
            refs = batch.column("entity_ref").to_pylist()
            if not entity_refs.intersection(refs):
                writer.write_batch(batch)
                continue
            vectors = batch.column("feature_vector").to_pylist()
            starts = batch.column("window_start").to_pylist()
            for index, ref in enumerate(refs):
                if ref in entity_refs:
                    updated = transform(ref, starts[index], decode(vectors[index]))
                    if updated is not None:
                        vectors[index] = json.dumps(updated, sort_keys=True)
                        changed += 1
            columns = {name: batch.column(name) for name in batch.schema.names}
            columns["feature_vector"] = pa.array(vectors, pa.string())
            writer.write_batch(pa.record_batch(columns, schema=batch.schema))
        writer.close()
        os.replace(temporary, source)
    except Exception:
        writer.close()
        temporary.unlink(missing_ok=True)
        raise
    record.sha256 = _hash(source)
    return changed
