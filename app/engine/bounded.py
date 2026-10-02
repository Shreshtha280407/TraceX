"""Bounded-memory execution of the post-parse stages.

The in-memory path (graph builder, `materialize_findings`, the anomaly stack)
holds every committed fact of a snapshot as Python objects at once. That is
the fastest way to run when the machine has the room, and stays the default.
When `app.resources` estimates it would not fit, the pipeline runs the same
stages here instead:

* facts are staged once into an on-disk DuckDB work database (typed columns
  plus each fact's canonical JSON), which spills to disk under its own memory
  limit instead of growing the Python heap;
* set-wise work (spend resolution, coverage counts, peeling walks, de-duplication,
  ranking) is done there in SQL;
* everything that needs the per-row detector/feature logic streams through the
  very same Python functions the in-memory path uses -- on one chunk of
  chains, one chunk of transactions, or one partition of addresses at a time.

The result is identical: same graph content, same feature rows, same findings
and ranks, same anomaly scores (asserted by tests on both paths). Only the
amount of the snapshot resident at once changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import duckdb
import pyarrow as pa
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import bulk_insert_serialized
from app.engine.feature_store import has_features
from app.engine.findings import deterministic as det
from app.engine.graph.builder import (
    EDGES_TABLE_SQL,
    NODES_TABLE_SQL,
    GraphBuildResult,
    _attributes_json,
    _receipt_path,
    duckdb_config,
    graph_paths,
    publish_graph,
)
from app.engine.motifs.deterministic import (
    DEFAULT_MAX_PEELING_HOPS,
    DEFAULT_MIN_PEELING_LENGTH,
    build_peeling_signal,
    detect_coinjoin_like_transactions,
)
from app.engine.process_pool import ProcessPool
from app.models import FindingRecord, FragmentReceipt, GraphSnapshot, Snapshot, SyntheticReviewSeed
from app.resources import ResourcePlan, current_plan

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
#: Facts fetched per round trip when streaming a table back into Python.
_FETCH_ROWS = 20_000
#: Peeling chains / coinjoin candidates whose signals are built per chunk,
#: at most; scaled down with the memory budget (see _chain_chunk()).
_CHAIN_CHUNK = 4_000
#: Smallest peeling chunk handed to a worker process.
_MIN_PARALLEL_CHUNK = 250
_KINDS = ("transactions", "inputs", "outputs", "network_observations")


def _release_freed_memory() -> None:
    """Hand memory Python has already freed back to the OS (glibc only).

    The bounded path frees each chunk/partition before loading the next, but
    glibc keeps freed arenas mapped, so without this the process RSS would
    still climb to the sum of every chunk ever processed."""
    global _malloc_trim
    if _malloc_trim is None:
        try:
            import ctypes

            _malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
        except (OSError, AttributeError):
            _malloc_trim = False
    if _malloc_trim:
        _malloc_trim(0)


_malloc_trim = None


def _chain_chunk(plan: ResourcePlan) -> int:
    # ~2 chains per MB of budget: 2K chains at 1 GB, the full 4K from 2 GB up.
    return max(250, min(_CHAIN_CHUNK, plan.memory_budget_bytes // (512 * 1024)))


def _to_datetime(micros: int | None) -> datetime | None:
    # Exact inverse of the epoch-microsecond encoding below; tzinfo is UTC,
    # exactly what `_time()` returns for a parsed aware timestamp.
    return None if micros is None else _EPOCH + timedelta(microseconds=micros)


def _to_micros(value: datetime | None) -> int | None:
    if value is None:
        return None
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class _Clock:
    """Seconds per named section, for the log line a long stage ends with."""

    def __init__(self) -> None:
        self.laps: dict[str, float] = {}
        self._last = time.perf_counter()

    def lap(self, name: str | None = None) -> None:
        now = time.perf_counter()
        if name is not None:
            self.laps[name] = round(now - self._last, 1)
        self._last = now


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


class FactStore:
    """One snapshot's committed facts, staged in an on-disk DuckDB database."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        self.plan: ResourcePlan = current_plan()
        config = duckdb_config(work_dir / "spill")
        # Here DuckDB works alongside the Python chunk/partition, so it gets a
        # quarter of the budget rather than half.
        config["memory_limit"] = f"{max(256, self.plan.memory_budget_bytes // (4 << 20))}MB"
        # Each DuckDB thread keeps its own buffers: on a small budget fewer
        # threads keep the working set inside the limit (about one per 512 MB).
        config["threads"] = max(1, min(self.plan.cpu_count, self.plan.memory_budget_bytes // (512 << 20)))
        # Every read whose order matters says ORDER BY; not tracking insertion
        # order lets DuckDB stream and spill large results in less memory.
        config["preserve_insertion_order"] = False
        self._config = config
        self.path = work_dir / "facts.duckdb"
        self.con = duckdb.connect(str(self.path), config=config)
        self._owned = True

    @classmethod
    def attach(cls, path: Path, *, memory_limit_mb: int, plan: ResourcePlan, tables: tuple[str, ...]) -> FactStore:
        """A worker's read-only view of a staged store (see _address_partitions_parallel).

        The staged `tables` are reached through views over the attached file,
        so any number of worker processes can read it at once; everything the
        worker creates (`with_keys` tables, its outputs) goes to its own
        in-memory database."""
        store = cls.__new__(cls)
        store.work_dir = path.parent
        store.plan = plan
        store.path = path
        store._owned = False
        store._config = {
            "memory_limit": f"{memory_limit_mb}MB", "threads": 1, "preserve_insertion_order": False,
            "temp_directory": str(path.parent / "spill"),
        }
        store.con = duckdb.connect(":memory:", config=store._config)
        store.con.execute(f"ATTACH {_quote(path)} AS facts (READ_ONLY)")
        for table in tables:
            store.con.execute(f'CREATE VIEW "{table}" AS SELECT * FROM facts."{table}"')
        return store

    def suspend(self) -> None:
        """Checkpoint and release the file so worker processes can attach it."""
        self.con.execute("CHECKPOINT")
        self.con.close()

    def resume(self) -> None:
        self.con = duckdb.connect(str(self.path), config=self._config)

    # ---- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        try:
            self.con.close()
        finally:
            if self._owned:
                shutil.rmtree(self.work_dir, ignore_errors=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- staging -----------------------------------------------------------
    @classmethod
    def build(cls, session: Session, *, evidence_root: Path, snapshot: Snapshot) -> FactStore:
        work_root = evidence_root.resolve() / snapshot.case_id / ".work"
        # A worker that died mid-run leaves its work database behind; the
        # retry owns this snapshot now, so clear any such leftovers first.
        for stale in work_root.glob(f"snapshot-{snapshot.id}-*"):
            shutil.rmtree(stale, ignore_errors=True)
        store = cls(work_root / f"snapshot-{snapshot.id}-{uuid.uuid4().hex[:8]}")
        try:
            store._stage(session, evidence_root, snapshot.id)
        except Exception:
            store.close()
            raise
        return store

    def _stage(self, session: Session, evidence_root: Path, snapshot_id: str) -> None:
        receipts = session.scalars(
            select(FragmentReceipt)
            .where(FragmentReceipt.snapshot_id == snapshot_id)
            .order_by(FragmentReceipt.logical_batch, FragmentReceipt.record_type)
        )
        files: dict[str, list[tuple[Path, int]]] = {kind: [] for kind in _KINDS}
        for receipt in receipts:
            if receipt.record_type in files:
                files[receipt.record_type].append(
                    (_receipt_path(evidence_root, receipt.storage_relative_path), receipt.logical_batch)
                )
        con = self.con
        for kind in _KINDS:
            table = f"raw_{kind}"
            con.execute(f"CREATE TABLE {table} (seq BIGINT, j VARCHAR)")
            # One fragment per INSERT: streams a file at a time, so staging
            # never needs more than one fragment's worth of DuckDB memory.
            # seq reproduces load_facts' order exactly: by batch, then row.
            for path, batch in files[kind]:
                con.execute(
                    f"INSERT INTO {table} SELECT {int(batch)} * 4294967296 + file_row_number, canonical_json "
                    f"FROM read_parquet({_quote(path)}, file_row_number = true)"
                )
        con.execute(
            """
            CREATE TABLE tx AS SELECT seq, j ->> '$.txid' AS txid, j ->> '$.block_time' AS block_time,
                j ->> '$.source_timestamp' AS source_timestamp, j ->> '$.network' AS network,
                TRY_CAST(j ->> '$.fee_sats' AS BIGINT) AS fee_sats, j
            FROM raw_transactions
            """
        )
        con.execute(
            """
            CREATE TABLE outputs AS SELECT seq, j ->> '$.txid' AS txid, CAST(j ->> '$.vout' AS BIGINT) AS vout,
                TRY_CAST(j ->> '$.amount_sats' AS BIGINT) AS amount, j ->> '$.address' AS address,
                j ->> '$.script_id' AS script_id, j ->> '$.script_type' AS script_type,
                COALESCE(NULLIF(j ->> '$.address', ''), NULLIF(j ->> '$.script_id', '')) AS addr_key, j
            FROM raw_outputs
            """
        )
        con.execute(
            """
            CREATE TABLE inputs AS SELECT seq, j ->> '$.txid' AS txid, TRY_CAST(j ->> '$.vin' AS BIGINT) AS vin,
                j ->> '$.prev_txid' AS prev_txid, TRY_CAST(j ->> '$.prev_vout' AS BIGINT) AS prev_vout,
                j ->> '$.address' AS address, j
            FROM raw_inputs
            """
        )
        con.execute("CREATE TABLE observations AS SELECT seq, j FROM raw_network_observations")
        for kind in _KINDS:
            con.execute(f"DROP TABLE raw_{kind}")
        # Transaction times are parsed by the same Python `_time()` the
        # in-memory path uses (not by SQL), so edge cases parse identically.
        con.execute("CREATE TABLE tx_time (txid VARCHAR, us BIGINT)")
        self.transactions_total = 0
        self.timed = 0
        self.block_timed = 0
        for batch in self.stream("SELECT txid, block_time, source_timestamp FROM tx ORDER BY seq"):
            txids, block_times, sources = batch["txid"], batch["block_time"], batch["source_timestamp"]
            micros = [_to_micros(det._time(bt or st)) for bt, st in zip(block_times, sources, strict=True)]
            self.timed += sum(1 for value in micros if value is not None)
            self.block_timed += sum(1 for value in block_times if value)
            con.register("time_batch", pa.table({"txid": pa.array(txids, pa.string()), "us": pa.array(micros, pa.int64())}))
            con.execute("INSERT INTO tx_time SELECT * FROM time_batch")
            con.unregister("time_batch")
        self.transactions_total = con.execute("SELECT count(DISTINCT txid) FROM tx").fetchone()[0]
        self.record_count = con.execute("SELECT count(*) FROM tx").fetchone()[0]

    # ---- reading back ------------------------------------------------------
    def stream(self, sql: str, params: list | None = None, rows: int = _FETCH_ROWS) -> Iterator[dict[str, list]]:
        """Yield the query result as column dicts, one bounded batch at a time."""
        cursor = self.con.cursor()
        try:
            reader = cursor.execute(sql, params or []).to_arrow_reader(rows)
            for batch in reader:
                yield batch.to_pydict()
        finally:
            cursor.close()

    def rows(self, sql: str, params: list | None = None) -> Iterator[tuple]:
        """Result rows as tuples, by column position (names may repeat across joins)."""
        cursor = self.con.cursor()
        try:
            for batch in cursor.execute(sql, params or []).to_arrow_reader(_FETCH_ROWS):
                yield from zip(*(column.to_pylist() for column in batch.columns), strict=True)
        finally:
            cursor.close()

    def with_keys(self, name: str, columns: dict[str, pa.Array]) -> None:
        """(Re)create a small key table for joins. A real table, not a
        registered view: views are local to one connection, and reads stream
        through their own cursors."""
        self.con.register("_keys", pa.table(columns))
        self.con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _keys")
        self.con.unregister("_keys")

    # ---- ML -----------------------------------------------------------------
    def ml_facts(self):
        """The anomaly stack's integer-indexed arrays, built in DuckDB.

        Exactly what `app.ml.facts.facts_from_streams` builds from the same
        facts -- transactions ordered by (epoch second, txid); addresses and
        script types numbered by first appearance among outputs in commit
        order; an input's prevout resolved to its output position; the last
        spender (in commit order) of an output recorded as its spender -- but
        set-wise, so no per-row Python objects are created for millions of
        outputs. Asserted equal by tests on both execution paths.
        """
        import numpy as np

        from app.ml.facts import Facts

        con = self.con
        con.execute(
            """
            CREATE OR REPLACE TABLE ml_tx AS
            SELECT (row_number() OVER (ORDER BY tt.us // 1000000, t.txid) - 1)::INTEGER AS slot,
                   t.txid, tt.us // 1000000 AS t, COALESCE(t.fee_sats, 0) AS fee
            FROM tx t JOIN tx_time tt USING (txid) WHERE tt.us IS NOT NULL
            """
        )
        con.execute(
            """
            CREATE OR REPLACE TABLE ml_out AS
            WITH base AS (
                SELECT o.seq, m.slot AS tx, o.vout, o.amount, o.addr_key, COALESCE(o.script_type, '') AS script,
                       (row_number() OVER (ORDER BY o.seq) - 1)::INTEGER AS pos
                FROM outputs o JOIN ml_tx m USING (txid)),
                 addr_first AS (SELECT addr_key, min(pos) AS first FROM base WHERE addr_key IS NOT NULL GROUP BY 1),
                 addr_slot AS (SELECT addr_key, (row_number() OVER (ORDER BY first) - 1)::INTEGER AS slot
                               FROM addr_first),
                 script_first AS (SELECT script, min(pos) AS first FROM base GROUP BY 1),
                 script_slot AS (SELECT script, (row_number() OVER (ORDER BY first) - 1)::INTEGER AS slot
                                 FROM script_first)
            SELECT b.pos, b.tx, b.vout, b.amount, b.addr_key, COALESCE(a.slot, -1) AS addr, s.slot AS script
            FROM base b LEFT JOIN addr_slot a USING (addr_key) JOIN script_slot s USING (script)
            """
        )
        con.execute(
            """
            CREATE OR REPLACE TABLE ml_in AS
            WITH resolved AS (
                SELECT i.seq, m.slot AS tx, p.slot AS prev_tx, i.prev_vout
                FROM inputs i JOIN ml_tx m USING (txid) LEFT JOIN ml_tx p ON p.txid = i.prev_txid)
            -- Equality-only join (a NULL prev_vout simply finds no output), so
            -- DuckDB uses a hash join rather than a nested loop.
            SELECT r.seq, r.tx, COALESCE(o.pos, -1) AS prev
            FROM resolved r LEFT JOIN ml_out o ON o.tx = r.prev_tx AND o.vout = r.prev_vout
            """
        )

        def arrays(sql: str) -> list:
            table = con.execute(sql).to_arrow_reader(1 << 20).read_all()
            return [column.to_numpy(zero_copy_only=False) for column in table.columns]

        tx_table = con.execute("SELECT txid FROM ml_tx ORDER BY slot").to_arrow_reader(1 << 20).read_all()
        txids = tx_table.column(0).to_pylist()
        del tx_table
        tx_time, tx_fee = arrays("SELECT t, fee FROM ml_tx ORDER BY slot")
        out_tx, out_vout, out_value, out_addr, out_script = arrays(
            "SELECT tx, vout, amount, addr, script FROM ml_out ORDER BY pos"
        )
        in_tx, in_prev = arrays("SELECT tx, prev FROM ml_in ORDER BY seq")
        spent_by = np.full(out_tx.shape[0], -1, dtype=np.int32)
        spender_pos, spender_tx = arrays("SELECT prev, arg_max(tx, seq) FROM ml_in WHERE prev >= 0 GROUP BY prev")
        spent_by[spender_pos.astype(np.int64)] = spender_tx
        address_table = con.execute(
            "SELECT addr_key FROM (SELECT DISTINCT addr, addr_key FROM ml_out WHERE addr >= 0) ORDER BY addr"
        ).to_arrow_reader(1 << 20).read_all()
        addresses = address_table.column(0).to_pylist()
        del address_table
        for table in ("ml_tx", "ml_out", "ml_in"):
            con.execute(f"DROP TABLE {table}")
        return Facts(
            txids=txids, tx_index={txid: slot for slot, txid in enumerate(txids)},
            tx_time=tx_time.astype(np.int64), tx_fee=tx_fee.astype(np.int64),
            out_tx=out_tx.astype(np.int32), out_vout=out_vout.astype(np.int32),
            out_value=out_value.astype(np.int64), out_addr=out_addr.astype(np.int32),
            out_script=out_script.astype(np.int8), out_spent_by=spent_by,
            in_tx=in_tx.astype(np.int32), in_prev=in_prev.astype(np.int32),
            addresses=addresses, addr_index={address: slot for slot, address in enumerate(addresses)},
        )

    def network_observations(self) -> Iterator[dict]:
        for txid, src_ip, asn, country in self.rows(
            "SELECT j ->> '$.txid', j ->> '$.src_ip', j ->> '$.asn', j ->> '$.geo_country' FROM observations ORDER BY seq"
        ):
            yield {"txid": txid, "src_ip": src_ip, "asn": asn, "geo_country": country}

    def source_refs_by_txid(self, txids: list[str]) -> dict[str, list[dict]]:
        from app.ml.findings import _source_refs_by_txid

        if not txids:
            return {}
        self.with_keys("wanted_tx", {"txid": pa.array(sorted(set(txids)), pa.string())})
        records = {
            kind: [json.loads(j) for (j,) in self.rows(f"SELECT t.j FROM {table} t JOIN wanted_tx w USING (txid) ORDER BY t.seq")]
            for kind, table in (("transactions", "tx"), ("inputs", "inputs"), ("outputs", "outputs"))
        }
        return _source_refs_by_txid(records)


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #

def _edge_id(from_id: str, to_id: str, edge_type: str) -> str:
    return hashlib.sha256(f"{from_id}|{to_id}|{edge_type}".encode()).hexdigest()


class _Stage:
    """Buffered appender into a node/edge staging table, ordered by `ord`."""

    def __init__(self, con: duckdb.DuckDBPyConnection, table: str, columns: tuple[str, ...], types: tuple) -> None:
        self.con, self.table, self.columns, self.types = con, table, columns, types
        self.buffer: list[tuple] = []

    def add(self, *row) -> None:
        self.buffer.append(row)
        if len(self.buffer) >= 50_000:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        columns = list(zip(*self.buffer, strict=True))
        batch = pa.table({name: pa.array(values, kind) for name, values, kind in zip(self.columns, columns, self.types, strict=True)})
        self.con.register("stage_batch", batch)
        self.con.execute(f"INSERT INTO {self.table} SELECT * FROM stage_batch")
        self.con.unregister("stage_batch")
        self.buffer = []


def build_graph_bounded(session: Session, *, store: FactStore, evidence_root: Path, snapshot: Snapshot) -> GraphBuildResult:
    """Same nodes, edges and coverage as `build_graph_snapshot`, built through the store.

    The in-memory builder dedupes with first-insert-wins over a fixed order:
    every transaction, then every output (tx, output, address nodes; creates/
    locked edges), then every resolved spend, then every network observation.
    Each staged row carries that position as `ord`, and the final tables keep
    the first row per id -- the same rows the dicts would have kept.
    """
    existing = session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == snapshot.id))
    if existing:
        return GraphBuildResult(existing.id, existing.node_count, existing.edge_count, existing.coverage)
    con = store.con
    con.execute("CREATE TABLE n_stage (node_id VARCHAR, node_type VARCHAR, label VARCHAR, attributes_json VARCHAR, ord BIGINT)")
    con.execute(
        "CREATE TABLE e_stage (edge_id VARCHAR, from_node VARCHAR, to_node VARCHAR, edge_type VARCHAR, "
        "attributes_json VARCHAR, uncertainty DOUBLE, ord BIGINT)"
    )
    string, integer, double = pa.string(), pa.int64(), pa.float64()
    nodes = _Stage(con, "n_stage", ("node_id", "node_type", "label", "attributes_json", "ord"), (string, string, string, string, integer))
    edges = _Stage(
        con, "e_stage", ("edge_id", "from_node", "to_node", "edge_type", "attributes_json", "uncertainty", "ord"),
        (string, string, string, string, string, double, integer),
    )
    position = 0

    def next_position() -> int:
        nonlocal position
        position += 1
        return position

    for txid, network, block_time, source_timestamp in store.rows(
        "SELECT txid, network, block_time, source_timestamp FROM tx ORDER BY seq"
    ):
        nodes.add(
            f"tx:{txid}", "transaction", txid,
            _attributes_json({"network": network, "block_time": block_time, "source_timestamp": source_timestamp}),
            next_position(),
        )
    for txid, vout, amount, address, script_id, script_type in store.rows(
        "SELECT txid, vout, amount, address, script_id, script_type FROM outputs ORDER BY seq"
    ):
        output_id = f"out:{txid}:{vout}"
        nodes.add(f"tx:{txid}", "transaction", txid, "{}", next_position())
        nodes.add(output_id, "output", f"{txid}:{vout}", _attributes_json({"amount_sats": amount, "script_type": script_type}), next_position())
        edges.add(
            _edge_id(f"tx:{txid}", output_id, "CREATES_OUTPUT"), f"tx:{txid}", output_id, "CREATES_OUTPUT",
            _attributes_json({"vout": vout, "amount_sats": amount}), None, next_position(),
        )
        locked = address or script_id
        if locked:
            nodes.add(f"address:{locked}", "address_or_script", str(locked), "{}", next_position())
            edges.add(_edge_id(output_id, f"address:{locked}", "LOCKED_TO"), output_id, f"address:{locked}", "LOCKED_TO", "{}", None, next_position())
    # Spend resolution: an input resolves when its outpoint is a committed
    # output; the first input (in fact order) to spend an outpoint wins, later
    # inputs from another transaction are double-spend conflicts.
    con.execute(
        """
        CREATE TABLE matched AS
        SELECT i.seq, i.txid, i.vin, i.prev_txid, i.prev_vout
        FROM inputs i SEMI JOIN outputs o ON o.txid = i.prev_txid AND o.vout = i.prev_vout
        WHERE i.prev_txid IS NOT NULL AND i.prev_vout IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE TABLE resolution AS
        SELECT m.*, m.txid = f.spender AS resolved
        FROM matched m JOIN (
            SELECT prev_txid, prev_vout, arg_min(txid, seq) AS spender FROM matched GROUP BY ALL
        ) f USING (prev_txid, prev_vout)
        """
    )
    for prev_txid, prev_vout, txid, vin in store.rows(
        "SELECT prev_txid, prev_vout, txid, vin FROM resolution WHERE resolved ORDER BY seq"
    ):
        from_id = f"out:{prev_txid}:{prev_vout}"
        edges.add(_edge_id(from_id, f"tx:{txid}", "SPENT_BY"), from_id, f"tx:{txid}", "SPENT_BY", _attributes_json({"vin": vin}), None, next_position())
    for (raw,) in store.rows("SELECT j FROM observations ORDER BY seq"):
        observation = json.loads(raw)
        observation_id = f"obs:{observation['observation_id']}"
        nodes.add(observation_id, "network_observation", observation["observation_id"],
                  _attributes_json({"clock_quality": observation.get("clock_quality")}), next_position())
        if observation.get("txid"):
            nodes.add(f"tx:{observation['txid']}", "transaction", observation["txid"], "{}", next_position())
            edges.add(_edge_id(observation_id, f"tx:{observation['txid']}", "OBSERVED_TX"), observation_id,
                      f"tx:{observation['txid']}", "OBSERVED_TX", "{}", None, next_position())
        for direction, address, port in (
            ("SEEN_FROM", observation.get("src_ip"), observation.get("src_port")),
            ("SEEN_TO", observation.get("dst_ip"), observation.get("dst_port")),
        ):
            if address:
                endpoint_id = f"endpoint:{address}:{port if port is not None else 'unknown'}"
                nodes.add(endpoint_id, "network_endpoint", f"{address}:{port if port is not None else '?'}", "{}", next_position())
                edges.add(_edge_id(observation_id, endpoint_id, direction), observation_id, endpoint_id, direction, "{}", None, next_position())
    nodes.flush()
    edges.flush()

    counts = con.execute(
        """
        SELECT
            (SELECT count(*) FROM inputs),
            (SELECT count(*) FROM resolution WHERE resolved),
            (SELECT count(*) FROM inputs WHERE prev_txid IS NULL OR prev_vout IS NULL),
            (SELECT count(*) FROM inputs i WHERE prev_txid IS NOT NULL AND prev_vout IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM outputs o WHERE o.txid = i.prev_txid AND o.vout = i.prev_vout)),
            (SELECT count(*) FROM resolution WHERE NOT resolved)
        """
    ).fetchone()
    input_count, resolved_inputs, missing_outpoint_inputs, unknown_prior_output_inputs, double_spend_conflicts = counts
    # A transaction's value is checked when every one of its inputs points at
    # a committed output: inputs then sum those outputs' amounts.
    known_value_checks, value_violations = con.execute(
        """
        WITH per_input AS (
            SELECT i.txid, o.amount, (i.prev_txid IS NOT NULL AND o.txid IS NOT NULL) AS known
            FROM inputs i LEFT JOIN outputs o ON o.txid = i.prev_txid AND o.vout = i.prev_vout
        ), per_tx AS (
            SELECT txid, bool_and(known) AS complete, sum(amount) AS input_value FROM per_input GROUP BY txid
        ), out_value AS (SELECT txid, sum(amount) AS output_value FROM outputs GROUP BY txid)
        SELECT count(*), count(*) FILTER (WHERE input_value < COALESCE(output_value, 0))
        FROM per_tx LEFT JOIN out_value USING (txid) WHERE complete
        """
    ).fetchone()
    coverage = {
        "input_count": int(input_count),
        "resolved_spend_inputs": int(resolved_inputs),
        "missing_outpoint_inputs": int(missing_outpoint_inputs),
        "unknown_prior_output_inputs": int(unknown_prior_output_inputs),
        "double_spend_conflicts": int(double_spend_conflicts),
        "value_checks_with_complete_prevouts": int(known_value_checks),
        "value_violations": int(value_violations),
        "spend_lineage_complete": missing_outpoint_inputs == 0
        and unknown_prior_output_inputs == 0
        and double_spend_conflicts == 0,
    }
    paths = graph_paths(evidence_root, snapshot)
    con.execute(f"ATTACH {_quote(paths.temporary)} AS g")
    # The graph file is stored sorted (zone maps prune lookups), so keep the
    # ORDER BY's row order for these two inserts.
    con.execute("SET preserve_insertion_order = true")
    try:
        con.execute(NODES_TABLE_SQL.replace("CREATE TABLE nodes", "CREATE TABLE g.nodes"))
        con.execute(EDGES_TABLE_SQL.replace("CREATE TABLE edges", "CREATE TABLE g.edges"))
        con.execute(
            """
            INSERT INTO g.nodes SELECT node_id, node_type, label, attributes_json FROM n_stage
            QUALIFY row_number() OVER (PARTITION BY node_id ORDER BY ord) = 1 ORDER BY node_id
            """
        )
        con.execute(
            """
            INSERT INTO g.edges SELECT edge_id, from_node, to_node, edge_type, attributes_json, uncertainty FROM e_stage
            QUALIFY row_number() OVER (PARTITION BY edge_id ORDER BY ord) = 1 ORDER BY from_node, edge_id
            """
        )
        node_count = con.execute("SELECT count(*) FROM g.nodes").fetchone()[0]
        edge_count = con.execute("SELECT count(*) FROM g.edges").fetchone()[0]
        con.execute("CHECKPOINT g")
    finally:
        con.execute("DETACH g")
        con.execute("SET preserve_insertion_order = false")
    con.execute("DROP TABLE n_stage")
    con.execute("DROP TABLE e_stage")
    return publish_graph(session, snapshot=snapshot, paths=paths, node_count=node_count, edge_count=edge_count, coverage=coverage)


# --------------------------------------------------------------------------- #
# Deterministic findings and address-window features
# --------------------------------------------------------------------------- #

def _facts_by_tx(store: FactStore, table: str, txids: list[str]) -> tuple[dict[str, list[dict]], dict[int, dict]]:
    """Facts of `table` for these transactions, in fact order; plus by seq."""
    store.with_keys("chunk_tx", {"txid": pa.array(txids, pa.string())})
    grouped: dict[str, list[dict]] = defaultdict(list)
    by_seq: dict[int, dict] = {}
    for seq, raw in store.rows(f"SELECT t.seq, t.j FROM {table} t SEMI JOIN chunk_tx c ON c.txid = t.txid ORDER BY t.seq"):
        fact = json.loads(raw)
        grouped[fact["txid"]].append(fact)
        by_seq[seq] = fact
    return grouped, by_seq


def _times(store: FactStore, txids: list[str]) -> dict[str, datetime | None]:
    store.with_keys("time_tx", {"txid": pa.array(sorted(set(txids)), pa.string())})
    return {txid: _to_datetime(us) for txid, us in store.rows("SELECT t.txid, t.us FROM tx_time t SEMI JOIN time_tx k ON k.txid = t.txid")}


def _peeling(store: FactStore, max_hops: int, min_length: int) -> list[tuple[tuple[str, int], list[tuple[str, int | None]]]]:
    """Every emitted peeling walk, in the in-memory detector's emit order.

    Walks are computed in SQL from the hop table: an outpoint whose spend
    resolves to exactly one transaction (`resolved_spenders`) moves to that
    transaction's largest uniquely-spent output strictly smaller than itself.
    Returns [(start outpoint, [(spending txid, continuation vout or None)])].
    """
    con = store.con
    con.execute(
        """
        CREATE TABLE sp AS
        SELECT i.prev_txid AS txid, i.prev_vout AS vout, min(i.txid) AS spend_txid
        FROM inputs i SEMI JOIN outputs o ON o.txid = i.prev_txid AND o.vout = i.prev_vout
        WHERE i.prev_txid IS NOT NULL AND i.prev_vout IS NOT NULL
        GROUP BY 1, 2 HAVING count(DISTINCT i.txid) = 1
        """
    )
    con.execute(
        """
        CREATE TABLE hop AS
        SELECT s.txid, s.vout, s.spend_txid,
               first(c.vout ORDER BY c.amount DESC, c.vout) AS cont_vout
        FROM sp s
        JOIN outputs po ON po.txid = s.txid AND po.vout = s.vout
        LEFT JOIN (SELECT o.txid, o.vout, o.amount FROM outputs o SEMI JOIN sp ON sp.txid = o.txid AND sp.vout = o.vout) c
               ON c.txid = s.spend_txid AND c.amount < po.amount
        GROUP BY s.txid, s.vout, s.spend_txid
        """
    )
    con.execute(
        "CREATE TABLE walk_active AS SELECT txid AS s_txid, vout AS s_vout, txid AS c_txid, vout AS c_vout, "
        "[]::STRUCT(t VARCHAR, v BIGINT)[] AS hops FROM hop"
    )
    con.execute("CREATE TABLE walk_done (s_txid VARCHAR, s_vout BIGINT, hops STRUCT(t VARCHAR, v BIGINT)[])")
    for _ in range(max_hops):
        con.execute(
            "INSERT INTO walk_done SELECT s_txid, s_vout, hops FROM walk_active w "
            "ANTI JOIN hop h ON h.txid = w.c_txid AND h.vout = w.c_vout"
        )
        con.execute(
            """
            CREATE OR REPLACE TABLE walk_next AS
            SELECT w.s_txid, w.s_vout, h.spend_txid AS c_txid, h.cont_vout AS c_vout,
                   list_append(w.hops, {'t': h.spend_txid, 'v': h.cont_vout}) AS hops
            FROM walk_active w JOIN hop h ON h.txid = w.c_txid AND h.vout = w.c_vout
            """
        )
        con.execute("INSERT INTO walk_done SELECT s_txid, s_vout, hops FROM walk_next WHERE c_vout IS NULL")
        con.execute("CREATE OR REPLACE TABLE walk_active AS SELECT * FROM walk_next WHERE c_vout IS NOT NULL")
    con.execute("INSERT INTO walk_done SELECT s_txid, s_vout, hops FROM walk_active")
    con.execute(
        f"""
        CREATE TABLE chains AS
        SELECT s_txid, s_vout, hops FROM walk_done WHERE len(hops) >= {int(min_length)}
        QUALIFY row_number() OVER (PARTITION BY list_transform(hops, x -> x.t) ORDER BY s_txid, s_vout) = 1
        """
    )
    for table in ("walk_active", "walk_next", "walk_done"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    return [
        ((s_txid, s_vout), [(item["t"], item["v"]) for item in hops])
        for s_txid, s_vout, hops in store.rows("SELECT s_txid, s_vout, hops FROM chains ORDER BY s_txid, s_vout")
    ]


def _chain_clusters(store: FactStore, walks: list) -> list[list[int]]:
    """The same groups `_group_peeling_patterns` forms (shared tx, or same
    origin+window key), as lists of walk indices in root order."""
    starts = sorted({start for start, _ in walks})
    store.with_keys("starts", {"txid": pa.array([t for t, _ in starts], pa.string()), "vout": pa.array([v for _, v in starts], pa.int64())})
    origin = {
        (txid, vout): (address or script_id)
        for txid, vout, address, script_id in store.rows(
            "SELECT o.txid, o.vout, o.address, o.script_id FROM outputs o SEMI JOIN starts s ON s.txid = o.txid AND s.vout = o.vout"
        )
    }
    times = _times(store, [hops[0][0] for _, hops in walks])
    parent = list(range(len(walks)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    owner_by_tx: dict[str, int] = {}
    owner_by_key: dict[tuple[str, datetime | None], int] = {}
    for index, ((txid, vout), hops) in enumerate(walks):
        locked = origin.get((txid, vout))
        entity = f"address:{locked}" if locked else f"out:{txid}:{vout}"
        observed = times.get(hops[0][0])
        key = (entity, det._window(observed, det.WINDOW_SECONDS[0])[0] if observed else None)
        union(index, owner_by_key.setdefault(key, index))
        for spend_txid, _ in hops:
            union(index, owner_by_tx.setdefault(spend_txid, index))
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(walks)):
        groups[find(index)].append(index)
    return [groups[root] for root in sorted(groups)]


def _store_rows(store: FactStore, table: str, rows: list[tuple], columns: tuple[str, ...], types: tuple) -> None:
    if not rows:
        return
    values = list(zip(*rows, strict=True))
    store.con.register("rows_batch", pa.table({name: pa.array(col, kind) for name, col, kind in zip(columns, values, types, strict=True)}))
    store.con.execute(f"INSERT INTO {table} SELECT * FROM rows_batch")
    store.con.unregister("rows_batch")


_CONTRIB_COLUMNS = ("ord", "addr", "us", "out_seq", "summary")
_DET_COLUMNS = ("ord", "key_addr", "key_seconds", "key_us", "signal")


def materialize_findings_bounded(
    session: Session, *, store: FactStore, evidence_root: Path, snapshot: Snapshot, graph: GraphSnapshot
) -> int:
    """`materialize_findings` with only one chunk/partition of the snapshot resident."""
    if has_features(session, snapshot.id):
        return 0
    if session.scalar(select(SyntheticReviewSeed.id).where(SyntheticReviewSeed.snapshot_id == snapshot.id).limit(1)):
        # Seeds are added only to completed snapshots, so an import never has
        # one; should it happen, the exact in-memory path is the safe choice.
        from app.engine.graph.builder import load_facts

        return det.materialize_findings(
            session, evidence_root=evidence_root, snapshot=snapshot, graph=graph,
            records=load_facts(session, evidence_root, snapshot.id),
        )
    plan = store.plan
    # Worker pools release and reopen the store's connection: always use store.con.
    string, integer = pa.string(), pa.int64()
    store.con.execute(_CONTRIB_DDL)
    store.con.execute(_DET_DDL)
    workers = plan.worker_processes(records=store.record_count)
    chain_chunk = _chain_chunk(plan)

    clock = _Clock()
    # ---- peeling: walks in SQL, signals per chunk of whole clusters ---------
    walks = _peeling(store, DEFAULT_MAX_PEELING_HOPS, DEFAULT_MIN_PEELING_LENGTH)
    clusters = _chain_clusters(store, walks)
    # Smaller chunks when they are spread over workers, so all of them get work.
    per_chunk = chain_chunk if workers == 1 else max(_MIN_PARALLEL_CHUNK, min(chain_chunk, -(-len(walks) // (4 * workers))))
    chunks: list[list[tuple[int, Any]]] = []
    chunk: list[int] = []
    for cluster in clusters:
        chunk.extend(cluster)
        if len(chunk) >= per_chunk:
            chunks.append([(index, walks[index]) for index in sorted(chunk)])
            chunk = []
    if chunk:
        chunks.append([(index, walks[index]) for index in sorted(chunk)])
    walks.clear()
    clusters.clear()
    pattern_ord = 0
    if workers > 1 and len(chunks) > 1:
        results = _parallel_map(
            store, _peeling_job, [_job(store, workers, walks=chunk) for chunk in chunks], workers,
        )
        for result in results:
            store.con.execute(f"INSERT INTO contrib SELECT * FROM read_parquet({_quote(Path(result['contrib']))})")
            store.con.execute(
                "INSERT INTO det SELECT ord + ?, key_addr, key_seconds, key_us, signal "
                f"FROM read_parquet({_quote(Path(result['det']))})",
                [pattern_ord],
            )
            pattern_ord += result["count"]
        _remove_job_files(results)
    else:
        for chunk in chunks:
            pattern_ord += _peeling_chunk(store, chunk, pattern_base=pattern_ord)
            _release_freed_memory()
    chunks.clear()

    clock.lap("peeling")
    # ---- coinjoin-like: per transaction, prefiltered in SQL ------------------
    candidates = [txid for (txid,) in store.rows(
        """
        SELECT t.txid FROM (SELECT DISTINCT txid FROM tx) t
        JOIN (SELECT txid, count(*) AS n FROM inputs GROUP BY txid) i USING (txid)
        JOIN (SELECT txid, count(*) AS n FROM outputs GROUP BY txid) o USING (txid)
        WHERE i.n >= 3 AND o.n >= 3 ORDER BY t.txid
        """
    )]
    coinjoin_ord = 0
    contrib_base = 1 << 50
    for offset in range(0, len(candidates), chain_chunk):
        txids = candidates[offset : offset + chain_chunk]
        outputs_by_tx, outputs_by_seq = _facts_by_tx(store, "outputs", txids)
        inputs_by_tx, _ = _facts_by_tx(store, "inputs", txids)
        transactions_by_tx, _ = _facts_by_tx(store, "tx", txids)
        seq_of = {id(fact): seq for seq, fact in outputs_by_seq.items()}
        tx_times = _times(store, txids)
        signals = detect_coinjoin_like_transactions(
            transactions={txid: facts[-1] for txid, facts in transactions_by_tx.items()},
            inputs=[fact for txid in txids for fact in inputs_by_tx.get(txid, [])],
            outputs=[fact for txid in txids for fact in outputs_by_tx.get(txid, [])],
        )
        contrib_rows, det_rows = [], []
        for signal in signals:
            tx_outputs = outputs_by_tx.get(signal["transaction_id"], [])
            observed = tx_times.get(signal["transaction_id"])
            summary = json.dumps(det.signal_summary(signal))
            for position, output in enumerate(tx_outputs):
                locked = output.get("address") or output.get("script_id")
                if locked and observed:
                    contrib_rows.append(
                        (contrib_base + (coinjoin_ord << 20) + position, str(locked), _to_micros(observed), seq_of[id(output)], summary)
                    )
            key = det.coinjoin_detector_key(signal, tx_times, tx_outputs)
            det_rows.append((contrib_base + coinjoin_ord, *(_key_columns(key)), json.dumps(signal)))
            coinjoin_ord += 1
        _store_rows(store, "contrib", contrib_rows, _CONTRIB_COLUMNS, (integer, string, integer, integer, string))
        _store_rows(store, "det", det_rows, _DET_COLUMNS, (integer, string, integer, integer, string))
        _release_freed_memory()

    clock.lap("coinjoin")
    # ---- address-window features, one partition of addresses at a time -----
    coverage = det.coverage_from_counts(graph, total=store.transactions_total, timed=store.timed, block_timed=store.block_timed)
    coverage_json = json.dumps(coverage)
    writer = det.FeatureRowWriter(
        session, snapshot=snapshot, graph=graph, coverage_json=coverage_json, chunk_rows=plan.insert_chunk_rows,
        evidence_root=evidence_root,
    )
    store.con.execute(
        "CREATE TABLE needed AS SELECT DISTINCT key_addr AS addr, key_seconds AS seconds, key_us AS us "
        "FROM det WHERE key_addr IS NOT NULL"
    )
    store.con.execute(_KEPT_DDL)
    store.con.execute(_CAND_DDL)
    partitions = plan.partitions(records=store.record_count)
    workers = plan.worker_processes(records=store.record_count)
    opposing_json = det.opposing_evidence_json()
    position = 0
    try:
        if workers > 1:
            # Same partition function, each of `workers` processes holding one
            # partition of 1/workers the size, so the budget still holds.
            _address_partitions_parallel(
                store, partitions=partitions * workers, workers=workers, writer=writer, snapshot=snapshot, graph=graph,
                evidence_root=evidence_root, coverage_json=coverage_json, opposing_json=opposing_json,
            )
        else:
            for partition in range(partitions):
                position = _address_partition(
                    store, partition=partition, partitions=partitions, writer=writer, snapshot=snapshot, graph=graph,
                    coverage_json=coverage_json, opposing_json=opposing_json, position=position,
                )
                _release_freed_memory()
        writer.publish()
    except BaseException:
        writer.abort()
        raise

    clock.lap("address_windows")
    # ---- detector findings, borrowing their window's feature vector ---------
    # Ordinals are dense (patterns 0..n, coinjoin from contrib_base), so the
    # wide signal payloads are read a slice at a time and never sorted by
    # DuckDB as one result, which would need them all in its memory.
    slices = [
        (low, min(low + chain_chunk, first + count) - 1)
        for first, count in ((0, pattern_ord), (contrib_base, coinjoin_ord))
        for low in range(first, first + count, chain_chunk)
    ]
    detector_position = 1 << 50
    if workers > 1 and len(slices) > 1:
        jobs = [
            _job(store, workers, low=low, high=high, coverage=coverage, snapshot_id=snapshot.id, case_id=snapshot.case_id,
                 graph_id=graph.id, coverage_json=coverage_json, opposing_json=opposing_json)
            for low, high in slices
        ]
        results = _parallel_map(store, _detector_job, jobs, workers)
        for result in results:
            store.con.execute(
                "INSERT INTO cand SELECT score, rule_id, entity_ref, start_us, end_us, pos + ?, row "
                f"FROM read_parquet({_quote(Path(result['cand']))})",
                [detector_position],
            )
            detector_position += result["count"]
        _remove_job_files(results)
    else:
        for low, high in slices:
            detector_position += _detector_slice(
                store, low, high, position=detector_position, coverage=coverage, snapshot=snapshot, graph=graph,
                coverage_json=coverage_json, opposing_json=opposing_json,
            )
            _release_freed_memory()

    clock.lap("detector_findings")
    # ---- de-duplicate, rank and write (same order as dedupe_and_rank) -------
    total = 0
    ids = det._uuid4_strings()
    finding_rows: list[dict[str, Any]] = []
    # Rank on the narrow key columns only (window functions over the wide
    # row payloads would need it all in memory), then stream the payloads in
    # rank order through a join and an external sort.
    store.con.execute(
        """
        CREATE TABLE ranked AS
        SELECT pos, row_number() OVER (ORDER BY score DESC, rule_id, entity_ref, first_pos) AS rank FROM (
            SELECT pos, score, rule_id, entity_ref,
                   min(pos) OVER (PARTITION BY entity_ref, start_us, end_us, rule_id) AS first_pos,
                   row_number() OVER (PARTITION BY entity_ref, start_us, end_us, rule_id ORDER BY score DESC, pos) AS pick
            FROM cand
        ) WHERE pick = 1
        """
    )
    # Payloads are fetched a rank slice at a time and ordered here: sorting
    # the wide rows inside DuckDB would pin far more than its memory limit.
    ranked_total = store.con.execute("SELECT count(*) FROM ranked").fetchone()[0]
    for low in range(1, ranked_total + 1, plan.insert_chunk_rows):
        high = low + plan.insert_chunk_rows - 1
        by_rank = {
            rank: raw
            for rank, raw in store.rows(
                "SELECT r.rank, c.row FROM cand c JOIN ranked r USING (pos) WHERE r.rank BETWEEN ? AND ?", [low, high]
            )
        }
        for rank in range(low, min(high, ranked_total) + 1):
            row = json.loads(by_rank.pop(rank))
            total += 1
            row["id"] = next(ids)
            row["rank"] = rank
            row["window_start"] = _to_datetime(row.pop("window_start_us"))
            row["window_end"] = _to_datetime(row.pop("window_end_us"))
            finding_rows.append(row)
        bulk_insert_serialized(session, FindingRecord, finding_rows, det._FINDING_JSON_COLUMNS)
        finding_rows = []
        _release_freed_memory()
    bulk_insert_serialized(session, FindingRecord, finding_rows, det._FINDING_JSON_COLUMNS)
    det.record_findings_event(session, snapshot, total)
    clock.lap("rank_and_write")
    logger.info("bounded findings: %d findings, %d worker process(es), seconds %s", total, workers, clock.laps)
    return total


def _key_columns(key: tuple[str, int, datetime] | None) -> tuple:
    return (None, None, None) if key is None else (key[0], key[1], _to_micros(key[2]))


def _candidate_row(candidate, pos, snapshot, graph, coverage_json, opposing_json) -> tuple:
    row = det.finding_row(
        candidate, rank=0, row_id="", snapshot=snapshot, graph=graph, coverage_json=coverage_json, opposing_json=opposing_json,
    )
    row.pop("id")
    row.pop("rank")
    row["window_start_us"] = _to_micros(row.pop("window_start"))
    row["window_end_us"] = _to_micros(row.pop("window_end"))
    return (
        candidate["score"], candidate["rule_id"], candidate["entity_ref"],
        row["window_start_us"], row["window_end_us"], pos, json.dumps(row),
    )


_CONTRIB_DDL = "CREATE TABLE contrib (ord BIGINT, addr VARCHAR, us BIGINT, out_seq BIGINT, summary VARCHAR)"
_DET_DDL = "CREATE TABLE det (ord BIGINT, key_addr VARCHAR, key_seconds BIGINT, key_us BIGINT, signal VARCHAR)"


def _peeling_chunk(store: FactStore, walks: list[tuple[int, Any]], *, pattern_base: int) -> int:
    """Signals for one chunk of whole chain clusters ([(walk index, walk)]):
    their window contributions go to `contrib`, their merged patterns to `det`
    numbered from `pattern_base`. Returns the number of patterns."""
    string, integer = pa.string(), pa.int64()
    by_index = dict(walks)
    indices = [index for index, _ in walks]
    spend_txids = sorted({txid for index in indices for txid, _ in by_index[index][1]})
    outputs_by_tx, outputs_by_seq = _facts_by_tx(store, "outputs", spend_txids)
    inputs_by_tx, _ = _facts_by_tx(store, "inputs", spend_txids)
    transactions_by_tx, _ = _facts_by_tx(store, "tx", spend_txids)
    transactions = {txid: facts[-1] for txid, facts in transactions_by_tx.items()}
    starts = sorted({by_index[index][0] for index in indices})
    store.with_keys("starts", {"txid": pa.array([t for t, _ in starts], pa.string()), "vout": pa.array([v for _, v in starts], pa.int64())})
    start_facts = {
        (fact["txid"], fact["vout"]): (seq, fact)
        for seq, fact in ((seq, json.loads(raw)) for seq, raw in store.rows(
            "SELECT o.seq, o.j FROM outputs o SEMI JOIN starts s ON s.txid = o.txid AND s.vout = o.vout ORDER BY o.seq"
        ))
    }
    outputs_by_outpoint = {(fact["txid"], fact["vout"]): fact for facts in outputs_by_tx.values() for fact in facts}
    for outpoint, (_, fact) in start_facts.items():
        outputs_by_outpoint.setdefault(outpoint, fact)
    seq_of = {(fact["txid"], fact["vout"]): seq for seq, fact in outputs_by_seq.items()}
    for outpoint, (seq, _) in start_facts.items():
        seq_of.setdefault(outpoint, seq)
    store.with_keys("chunk_tx", {"txid": pa.array(spend_txids, pa.string())})
    spent = {(txid, vout) for txid, vout in store.rows("SELECT s.txid, s.vout FROM sp s SEMI JOIN chunk_tx c ON c.txid = s.txid")}
    tx_times = _times(store, spend_txids)
    signals: list[dict[str, Any]] = []
    contrib_rows: list[tuple] = []
    for index in indices:
        start, hops = by_index[index]
        walk = []
        outpoint = start
        for spend_txid, cont_vout in hops:
            continuation = outputs_by_outpoint[(spend_txid, cont_vout)] if cont_vout is not None else None
            walk.append((outpoint, spend_txid, continuation))
            outpoint = (spend_txid, cont_vout)
        signal = build_peeling_signal(
            start, walk, outputs_by_outpoint=outputs_by_outpoint, outputs_by_tx=outputs_by_tx,
            inputs_by_tx=inputs_by_tx, spenders=spent, transactions=transactions, tx_times=tx_times,
        )
        signals.append(signal)
        origin = outputs_by_outpoint[start]
        locked = origin.get("address") or origin.get("script_id")
        observed = tx_times.get(signal["transaction_ids"][0])
        if locked and observed:
            contrib_rows.append((index, str(locked), _to_micros(observed), seq_of[start], json.dumps(det.signal_summary(signal))))
    _store_rows(store, "contrib", contrib_rows, _CONTRIB_COLUMNS, (integer, string, integer, integer, string))
    det_rows = []
    for ordinal, pattern in enumerate(det._group_peeling_patterns(signals, tx_times), pattern_base):
        key = det.peeling_detector_key(pattern, tx_times)
        det_rows.append((ordinal, *(_key_columns(key)), json.dumps(pattern)))
    _store_rows(store, "det", det_rows, _DET_COLUMNS, (integer, string, integer, integer, string))
    return len(det_rows)


def _detector_slice(
    store: FactStore, low: int, high: int, *, position: int, coverage: dict, snapshot, graph, coverage_json: str,
    opposing_json: str,
) -> int:
    """Finding candidates for the detector signals with ordinals low..high,
    each borrowing its window's kept feature vector; positions from `position`."""
    rows = sorted(
        store.rows(
            "SELECT d.ord, d.key_addr, d.key_seconds, d.key_us, d.signal, k.feature FROM det d "
            "LEFT JOIN kept k ON k.addr = d.key_addr AND k.seconds = d.key_seconds AND k.us = d.key_us "
            "WHERE d.ord BETWEEN ? AND ?",
            [low, high],
        ),
        key=lambda row: row[0],
    )
    signals = [json.loads(row[4]) for row in rows]
    times = _times(store, [s["transaction_ids"][0] if s["finding_type"] == "peeling_chain_candidate" else s["transaction_id"] for s in signals])
    candidates = []
    for signal, (_, addr, seconds, us, _, feature) in zip(signals, rows, strict=True):
        key = (addr, seconds, _to_datetime(us)) if addr is not None else None
        candidate = det.detector_candidate(signal, key, json.loads(feature) if feature else None, times, coverage)
        candidates.append(_candidate_row(candidate, position + len(candidates), snapshot, graph, coverage_json, opposing_json))
    _store_rows(store, "cand", candidates, ("score", "rule_id", "entity_ref", "start_us", "end_us", "pos", "row"),
                (pa.float64(), pa.string(), pa.string(), pa.int64(), pa.int64(), pa.int64(), pa.string()))
    return len(candidates)


# --------------------------------------------------------------------------- #
# Worker processes. Peeling chunks, address partitions and detector slices are
# independent of one another, so on a machine with cores and memory to spare
# they run in a pool of processes that each attach the staged store read-only
# and write their rows to files; the parent merges them in job order with the
# same ordinals/positions the in-process loops assign, so the stored result is
# identical however many workers ran.
# --------------------------------------------------------------------------- #

def _job(store: FactStore, workers: int, **fields: Any) -> dict[str, Any]:
    out_dir = store.work_dir / "jobs"
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "db": str(store.path), "plan": store.plan, "out_dir": str(out_dir), "id": uuid.uuid4().hex,
        "memory_limit_mb": max(128, store.plan.memory_budget_bytes // (4 << 20) // workers), **fields,
    }


def _attach(job: dict[str, Any], tables: tuple[str, ...], local: tuple[str, ...]) -> FactStore:
    store = FactStore.attach(Path(job["db"]), memory_limit_mb=job["memory_limit_mb"], plan=job["plan"], tables=tables)
    for ddl in local:
        store.con.execute(ddl)
    return store


def _export(store: FactStore, job: dict[str, Any], table: str) -> str:
    path = Path(job["out_dir"]) / f"{table}-{job['id']}.parquet"
    store.con.execute(f"COPY {table} TO {_quote(path)} (FORMAT parquet)")
    return str(path)


def _parallel_map(store: FactStore, fn, jobs: list[dict[str, Any]], workers: int, on_result=None) -> list[dict[str, Any]]:
    """fn(job) for every job in `workers` processes, results in job order.
    The store's file is released meanwhile, so nothing may use `store.con`."""
    results: list[dict[str, Any]] = []
    store.suspend()
    try:
        with ProcessPool(min(workers, len(jobs))) as pool:
            for result in pool.imap(f"{__name__}:{fn.__name__}", jobs):
                if on_result is not None:
                    on_result(result)
                results.append(result)
    except BaseException:
        _remove_job_files(results)
        raise
    finally:
        store.resume()
    return results


def _remove_job_files(results: list[dict[str, Any]]) -> None:
    for result in results:
        for key in ("contrib", "det", "kept", "cand", "features"):
            if result.get(key):
                Path(result[key]).unlink(missing_ok=True)


def _peeling_job(job: dict[str, Any]) -> dict[str, Any]:
    store = _attach(job, ("outputs", "inputs", "tx", "sp", "tx_time"), (_CONTRIB_DDL, _DET_DDL))
    try:
        count = _peeling_chunk(store, job["walks"], pattern_base=0)
        return {"count": count, "contrib": _export(store, job, "contrib"), "det": _export(store, job, "det")}
    finally:
        store.con.close()


def _detector_job(job: dict[str, Any]) -> dict[str, Any]:
    store = _attach(job, ("det", "kept", "tx_time"), (_CAND_DDL,))
    try:
        count = _detector_slice(
            store, job["low"], job["high"], position=0, coverage=job["coverage"],
            snapshot=SimpleNamespace(id=job["snapshot_id"], case_id=job["case_id"]), graph=SimpleNamespace(id=job["graph_id"]),
            coverage_json=job["coverage_json"], opposing_json=job["opposing_json"],
        )
        return {"count": count, "cand": _export(store, job, "cand")}
    finally:
        store.con.close()


_CAND_DDL = "CREATE TABLE cand (score DOUBLE, rule_id VARCHAR, entity_ref VARCHAR, start_us BIGINT, end_us BIGINT, pos BIGINT, row VARCHAR)"
_KEPT_DDL = "CREATE TABLE kept (addr VARCHAR, seconds BIGINT, us BIGINT, feature VARCHAR)"


def _partition_job(job: dict[str, Any]) -> dict[str, Any]:
    """One address partition in a worker process; its outputs go to files."""
    store = _attach(job, ("outputs", "inputs", "tx_time", "contrib", "needed"), (_KEPT_DDL, _CAND_DDL))
    snapshot = SimpleNamespace(id=job["snapshot_id"], case_id=job["case_id"])
    graph = SimpleNamespace(id=job["graph_id"])
    try:
        writer = det.FeatureRowWriter(
            None, snapshot=snapshot, graph=graph, coverage_json=job["coverage_json"], chunk_rows=job["chunk_rows"],
            evidence_root=job["evidence_root"], part=True,
        )
        try:
            count = _address_partition(
                store, partition=job["partition"], partitions=job["partitions"], writer=writer, snapshot=snapshot,
                graph=graph, coverage_json=job["coverage_json"], opposing_json=job["opposing_json"], position=0,
            )
            features, feature_rows, schema_version = writer.close_part()
        except BaseException:
            writer.abort()
            raise
        return {
            "count": count, "features": str(features), "feature_rows": feature_rows, "schema_version": schema_version,
            "kept": _export(store, job, "kept"), "cand": _export(store, job, "cand"),
        }
    finally:
        store.con.close()


def _address_partitions_parallel(
    store: FactStore, *, partitions: int, workers: int, writer, snapshot, graph, evidence_root: Path,
    coverage_json: str, opposing_json: str,
) -> None:
    """`_address_partition` for every partition in worker processes. Feature
    rows are appended to the snapshot's store as partitions finish, in
    partition order; candidate positions are offset exactly as the in-process
    loop numbers them, so ranking ties resolve identically."""
    jobs = [
        _job(store, workers, partition=partition, partitions=partitions, snapshot_id=snapshot.id, case_id=snapshot.case_id,
             graph_id=graph.id, evidence_root=str(evidence_root), coverage_json=coverage_json,
             opposing_json=opposing_json, chunk_rows=store.plan.insert_chunk_rows)
        for partition in range(partitions)
    ]

    def append(result: dict[str, Any]) -> None:
        writer.append_part(Path(result["features"]), result["feature_rows"], result["schema_version"])
        result["features"] = None

    results = _parallel_map(store, _partition_job, jobs, workers, on_result=append)
    position = 0
    for result in results:
        store.con.execute(f"INSERT INTO kept SELECT * FROM read_parquet({_quote(Path(result['kept']))})")
        store.con.execute(
            "INSERT INTO cand SELECT score, rule_id, entity_ref, start_us, end_us, pos + ?, row "
            f"FROM read_parquet({_quote(Path(result['cand']))})",
            [position],
        )
        position += result["count"]
    _remove_job_files(results)


def _address_partition(
    store: FactStore, *, partition: int, partitions: int, writer, snapshot, graph, coverage_json, opposing_json, position: int,
) -> int:
    """One partition of addresses: exactly the in-memory address-window pass,
    over only the facts those addresses need."""
    con = store.con
    in_part = f"hash({{column}}) % {partitions} = {partition}"
    output_events: dict[tuple[str, int, datetime], list[dict]] = defaultdict(list)
    outputs_by_seq: dict[int, dict] = {}
    for seq, raw, addr_key, us in store.rows(
        f"""
        SELECT o.seq, o.j, o.addr_key, t.us FROM outputs o JOIN tx_time t USING (txid)
        WHERE o.addr_key IS NOT NULL AND t.us IS NOT NULL AND {in_part.format(column='o.addr_key')} ORDER BY o.seq
        """
    ):
        output = json.loads(raw)
        outputs_by_seq[seq] = output
        observed = _to_datetime(us)
        for seconds in det.WINDOW_SECONDS:
            start, _ = det._window(observed, seconds)
            output_events[(str(addr_key), seconds, start)].append(output)
    # Detector signals keyed on these addresses, in the in-memory call order.
    signal_by_key: dict[tuple[str, int, datetime], list[dict[str, Any]]] = defaultdict(list)
    event_outpoints: dict[tuple[str, int, datetime], set[tuple[str, int]]] = {}
    for addr, us, out_seq, out_raw, summary in store.rows(
        f"""
        SELECT c.addr, c.us, c.out_seq, o.j, c.summary FROM contrib c JOIN outputs o ON o.seq = c.out_seq
        WHERE {in_part.format(column='c.addr')} ORDER BY c.ord
        """
    ):
        output = outputs_by_seq.get(out_seq)
        if output is None:
            output = outputs_by_seq[out_seq] = json.loads(out_raw)
        det.add_window_signal(output_events, event_outpoints, signal_by_key, addr, _to_datetime(us), json.loads(summary), output)
    del event_outpoints
    rapid_events: dict[tuple[str, int, datetime], list[tuple[dict, dict, float]]] = defaultdict(list)
    outgoing: set[str] = set()
    for input_raw, previous_seq, previous_raw, address, spent_us, received_us in store.rows(
        f"""
        SELECT i.j, po.seq, po.j, po.address, ts.us, tr.us
        FROM inputs i
        JOIN outputs po ON po.txid = i.prev_txid AND po.vout = i.prev_vout
        JOIN tx_time ts ON ts.txid = i.txid
        JOIN tx_time tr ON tr.txid = i.prev_txid
        WHERE i.prev_txid IS NOT NULL AND i.prev_vout IS NOT NULL AND NULLIF(po.address, '') IS NOT NULL
          AND ts.us IS NOT NULL AND tr.us IS NOT NULL
          AND ts.us - tr.us BETWEEN 0 AND 3600000000 AND {in_part.format(column='po.address')}
        ORDER BY i.seq
        """
    ):
        tx_input = json.loads(input_raw)
        previous = outputs_by_seq.get(previous_seq)
        if previous is None:
            previous = outputs_by_seq[previous_seq] = json.loads(previous_raw)
        spent_at, received_at = _to_datetime(spent_us), _to_datetime(received_us)
        delay = (spent_at - received_at).total_seconds()
        if 0 <= delay <= 3600:
            outgoing.add(tx_input["txid"])
            for seconds in det.WINDOW_SECONDS:
                start, _ = det._window(spent_at, seconds)
                rapid_events[(str(address), seconds, start)].append((tx_input, previous, delay))
    outputs_by_tx, _ = _facts_by_tx(store, "outputs", sorted(outgoing)) if outgoing else ({}, {})
    inbound = {output["txid"] for outputs in output_events.values() for output in outputs}
    transaction_times = _times(store, sorted(inbound)) if inbound else {}
    needed = {
        (addr, seconds, _to_datetime(us))
        for addr, seconds, us in store.rows(f"SELECT addr, seconds, us FROM needed WHERE {in_part.format(column='addr')}")
    }
    candidates, kept = det.address_window_pass(
        output_events=output_events, rapid_events=rapid_events, signal_by_key=signal_by_key,
        transaction_times=transaction_times, outputs_by_tx=outputs_by_tx, needed_keys=needed, writer=writer,
    )
    writer.end_pass()
    _store_rows(
        store, "kept",
        [(key[0], key[1], _to_micros(key[2]), feature_json) for key, (_, feature_json) in kept.items()],
        ("addr", "seconds", "us", "feature"), (pa.string(), pa.int64(), pa.int64(), pa.string()),
    )
    rows = []
    for candidate in candidates:
        rows.append(_candidate_row(candidate, position, snapshot, graph, coverage_json, opposing_json))
        position += 1
    _store_rows(store, "cand", rows, ("score", "rule_id", "entity_ref", "start_us", "end_us", "pos", "row"),
                (pa.float64(), pa.string(), pa.string(), pa.int64(), pa.int64(), pa.int64(), pa.string()))
    del con
    return position
