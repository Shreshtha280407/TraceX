"""Entity clustering, network correlation, graph embeddings and risk propagation.

This is the PS's "cluster entities / correlate network-layer observations /
propagate risk from seed wallets" layer. It runs once per completed snapshot,
after the graph, the deterministic findings and the anomaly stack, and writes
one derived DuckDB file next to the (unchanged) graph snapshot.

Everything set-wise is SQL in DuckDB, which spills to disk under its memory
limit, so the same code serves the in-memory and the bounded execution modes;
only the arrays handed to the union-find, the embedding and the propagation
(a few int32/int64 columns per output) are held in Python.

What is computed
----------------
* **Entity clusters** -- the common-input-ownership heuristic: addresses spent
  together as inputs of one transaction are proposed as one entity. Inputs of
  CoinJoin-shaped transactions (>= 3 inputs and >= 3 equal-valued outputs)
  are *not* merged, because collaborative transactions break the heuristic.
  A cluster is a reviewable proposition backed by the listed linking
  transactions, never an ownership finding.
* **Geo-IP enrichment** -- every observed endpoint IP is classified (public /
  private / documentation / ...) and, when public, looked up in the installed
  offline Geo-IP database (app.engine.geoip); the source-reported country/ASN
  is then marked verified, contradicted or unverifiable.
* **Network <-> blockchain correlation** -- for each wallet (entity cluster or
  single address) with enough observed spends, how concentrated its spends are
  on one relay endpoint / ASN versus that endpoint's base rate in the snapshot
  (exact binomial tail, Bonferroni-corrected across wallets). Significant
  concentrations are written as `network-correlation-v1` findings.
* **Value-flow arrays** -- output-level (UTXO-exact) flow tables used by the
  on-demand risk propagation below.
* **Graph embeddings** -- a spectral embedding (truncated SVD of the degree-
  normalised wallet x transaction incidence matrix) of every wallet active in
  at least two transactions, used for "behaviourally similar wallet" search.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.engine import geoip
from app.engine.findings.deterministic import _time
from app.engine.graph.builder import duckdb_config
from app.events import append_event
from app.models import AnalyticsRevision, AnalyticsSnapshot, FindingRecord, GraphSnapshot, RiskRun, RiskSeed, Snapshot
from app.resources import admit_global_allocation, current_plan

logger = logging.getLogger(__name__)

ANALYTICS_VERSION = "entity-network-v1"
NETWORK_RULE_VERSION = "network-correlation-v1"
RISK_METHOD_VERSION = "utxo-haircut-propagation-v1"
EMBEDDING_DIMENSIONS = 16

#: CoinJoin-shaped transactions are excluded from input clustering.
MIXING_MIN_INPUTS = 3
MIXING_MIN_EQUAL_OUTPUTS = 3
#: A wallet needs this many observed spends before its relay concentration is tested.
CORRELATION_MIN_SPENDS = 5
CORRELATION_MIN_SHARE = 0.6
#: Family-wise false-alarm rate for the concentration tests (Bonferroni).
CORRELATION_ALPHA = 1e-3
#: Reported geo/ASN on a public IP must disagree on at least this many observations.
GEO_MISMATCH_MIN_OBSERVATIONS = 3

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _micros(value: str | None) -> int | None:
    parsed = _time(value)
    if parsed is None:
        return None
    delta = parsed - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _from_micros(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=int(value))


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _safe_name(value: str) -> str:
    return value.replace("-", "").replace("/", "")


def analytics_relative_path(snapshot: Snapshot) -> Path:
    return Path(snapshot.case_id) / "analytics" / f"snapshot-{_safe_name(snapshot.id)}.duckdb"


def _resolve(evidence_root: Path, relative: str | Path) -> Path:
    root = evidence_root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise RuntimeError("analytics path escaped evidence root")
    return path


@dataclass(frozen=True)
class AnalyticsResult:
    analytics_snapshot_id: str | None
    summary: dict[str, Any]
    network_finding_count: int


# --------------------------------------------------------------------------- #
# Base relations
# --------------------------------------------------------------------------- #


def _materialize(con: duckdb.DuckDBPyConnection, name: str, table: pa.Table) -> None:
    # Copied into DuckDB once: a registered Arrow table is re-scanned (single
    # threaded) by every query that reads it.
    con.register(f"_{name}", table)
    con.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM _{name}")
    con.unregister(f"_{name}")


def _register_records(con: duckdb.DuckDBPyConnection, records: dict[str, list[dict]]) -> None:
    """Expose the in-memory facts as the a_* relations (in-memory mode)."""
    transactions = records["transactions"]
    _materialize(con, "a_tx", pa.table({
        "txid": pa.array([t["txid"] for t in transactions], pa.string()),
        "us": pa.array([_micros(t.get("block_time") or t.get("source_timestamp")) for t in transactions], pa.int64()),
        "block_us": pa.array([_micros(t.get("block_time")) for t in transactions], pa.int64()),
    }))
    outputs = records["outputs"]
    _materialize(con, "a_out", pa.table({
        "txid": pa.array([o["txid"] for o in outputs], pa.string()),
        "vout": pa.array([o["vout"] for o in outputs], pa.int64()),
        "addr_key": pa.array([o.get("address") or o.get("script_id") or None for o in outputs], pa.string()),
        "amount": pa.array([o.get("amount_sats") for o in outputs], pa.int64()),
    }))
    inputs = records["inputs"]
    _materialize(con, "a_in", pa.table({
        "txid": pa.array([i["txid"] for i in inputs], pa.string()),
        "vin": pa.array([i.get("vin") for i in inputs], pa.int64()),
        "prev_txid": pa.array([i.get("prev_txid") for i in inputs], pa.string()),
        "prev_vout": pa.array([i.get("prev_vout") for i in inputs], pa.int64()),
        "address": pa.array([i.get("address") or None for i in inputs], pa.string()),
        "amount": pa.array([i.get("amount_sats") for i in inputs], pa.int64()),
    }))
    observations = records["network_observations"]

    def column(name: str) -> pa.Array:
        return pa.array([None if o.get(name) in (None, "") else str(o.get(name)) for o in observations], pa.string())

    _materialize(con, "a_obs", pa.table({
        "txid": column("txid"),
        "src_ip": column("src_ip"),
        "dst_ip": column("dst_ip"),
        "src_port": pa.array([o.get("src_port") for o in observations], pa.int64()),
        "dst_port": pa.array([o.get("dst_port") for o in observations], pa.int64()),
        "geo_country": column("geo_country"),
        "asn": column("asn"),
        "observed_us": pa.array([_micros(o.get("observer_received_at")) for o in observations], pa.int64()),
    }))


def _views_over_store(con: duckdb.DuckDBPyConnection) -> None:
    """Expose a bounded-mode FactStore's typed tables as the a_* relations."""
    con.execute(
        "CREATE OR REPLACE TEMP VIEW a_tx AS SELECT t.txid, tt.us, NULL::BIGINT AS block_us "
        "FROM tx t JOIN tx_time tt USING (txid)"
    )
    con.execute("CREATE OR REPLACE TEMP VIEW a_out AS SELECT txid, vout, addr_key, amount FROM outputs")
    con.execute(
        "CREATE OR REPLACE TEMP VIEW a_in AS SELECT txid, vin, prev_txid, prev_vout, NULLIF(address, '') AS address, "
        "amount FROM inputs"
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP VIEW a_obs AS SELECT
            NULLIF(txid, '') AS txid, NULLIF(src_ip, '') AS src_ip,
            NULLIF(dst_ip, '') AS dst_ip, src_port, dst_port, NULLIF(geo_country, '') AS geo_country,
            NULLIF(asn, '') AS asn, NULL::BIGINT AS observed_us
        FROM observations
        """
    )


def _store_block_times(con: duckdb.DuckDBPyConnection) -> None:
    """Bounded mode: observer times and block times parsed by the same Python
    `_time()` as the in-memory path (only for rows that carry them)."""
    con.execute("CREATE OR REPLACE TABLE obs_time (seq BIGINT, observed_us BIGINT)")
    reader = con.execute(
        "SELECT seq, observer_received_at FROM observations WHERE observer_received_at IS NOT NULL"
    ).to_arrow_reader(50_000)
    write = con.cursor()
    for batch in reader:
        seqs, values = batch.column(0).to_pylist(), batch.column(1).to_pylist()
        write.register("_ot", pa.table({"seq": pa.array(seqs, pa.int64()),
                                         "observed_us": pa.array([_micros(v) for v in values], pa.int64())}))
        write.execute("INSERT INTO obs_time SELECT * FROM _ot")
        write.unregister("_ot")
    con.execute("DROP VIEW IF EXISTS a_obs")
    con.execute(
        """
        CREATE OR REPLACE TABLE a_obs AS SELECT
            NULLIF(o.txid, '') AS txid, NULLIF(o.src_ip, '') AS src_ip,
            NULLIF(o.dst_ip, '') AS dst_ip, o.src_port, o.dst_port, NULLIF(o.geo_country, '') AS geo_country,
            NULLIF(o.asn, '') AS asn, ot.observed_us
        FROM observations o LEFT JOIN obs_time ot USING (seq)
        """
    )
    con.execute("CREATE OR REPLACE TABLE block_time (txid VARCHAR, block_us BIGINT)")
    reader = con.execute("SELECT txid, block_time FROM tx WHERE block_time IS NOT NULL").to_arrow_reader(50_000)
    for batch in reader:
        txids, values = batch.column(0).to_pylist(), batch.column(1).to_pylist()
        write.register("_bt", pa.table({"txid": pa.array(txids, pa.string()),
                                         "block_us": pa.array([_micros(v) for v in values], pa.int64())}))
        write.execute("INSERT INTO block_time SELECT * FROM _bt")
        write.unregister("_bt")
    write.close()
    con.execute(
        "CREATE OR REPLACE TEMP VIEW a_tx AS SELECT t.txid, tt.us, bt.block_us "
        "FROM tx t JOIN tx_time tt USING (txid) LEFT JOIN block_time bt USING (txid)"
    )


# --------------------------------------------------------------------------- #
# Union-find
# --------------------------------------------------------------------------- #


def _numeric_columns(con, sql: str, *, dtypes=None):
    """Stream Arrow batches into one admitted numeric allocation, never read_all."""
    count = con.execute(f"SELECT count(*) FROM ({sql}) q").fetchone()[0]
    reader = con.execute(sql).to_arrow_reader(20_000)
    try:
        import numpy as np
    except ImportError:
        values = [[] for _ in reader.schema]
        for batch in reader:
            for target, column in zip(values, batch.columns, strict=True):
                target.extend(column.to_pylist())
        return values
    values = [np.empty(count, dtype=dtype) for dtype in (dtypes or [np.int64] * len(reader.schema))]
    offset = 0
    for batch in reader:
        for target, column in zip(values, batch.columns, strict=True):
            target[offset:offset + batch.num_rows] = column.to_numpy(zero_copy_only=False)
        offset += batch.num_rows
    return values


def _components(count: int, roots: list[int] | Any, members: list[int] | Any) -> list[int] | Any:
    """Component label per id = the smallest id in its component.

    Uses scipy's connected components when available (the `ml` extra), else a
    pure-Python union-find; both return the identical labelling."""
    try:
        import numpy as np
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components
    except ImportError:
        parent = list(range(count))

        def find(item: int) -> int:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for left, right in zip(roots, members, strict=True):
            a, b = find(left), find(right)
            if a != b:
                if a < b:
                    parent[b] = a
                else:
                    parent[a] = b
        return [find(item) for item in range(count)]
    if count == 0:
        return np.zeros(0, dtype=np.int64)
    roots = np.asarray(roots, dtype=np.int64)
    members = np.asarray(members, dtype=np.int64)
    graph = coo_matrix((np.ones(roots.size, dtype=np.int8), (roots, members)), shape=(count, count))
    _, labels = connected_components(graph, directed=False)
    smallest = np.full(labels.max() + 1 if count else 0, count, dtype=np.int64)
    np.minimum.at(smallest, labels, np.arange(count, dtype=np.int64))
    return smallest[labels]


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), computed in log space."""
    if k <= 0:
        return 1.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    log_p, log_q = math.log(p), math.log1p(-p)
    log_terms = [
        math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q
        for i in range(k, n + 1)
    ]
    peak = max(log_terms)
    return min(1.0, math.exp(peak) * sum(math.exp(term - peak) for term in log_terms))


def _asn_number(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip().upper().removeprefix("AS")
    return int(text) if text.isdigit() else None


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_analytics(
    session: Session,
    *,
    evidence_root: Path,
    snapshot: Snapshot,
    graph: GraphSnapshot,
    records: dict[str, list[dict]] | None = None,
    store=None,
    geoip_dir: Path | None = None,
    refresh: bool = False,
) -> AnalyticsResult:
    existing = latest_analytics(session, snapshot.case_id, snapshot.id)
    if existing and not refresh:
        count = session.scalar(
            select(func.count()).select_from(FindingRecord).where(
                FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == NETWORK_RULE_VERSION
            )
        ) or 0
        return AnalyticsResult(existing.id, existing.summary, count)
    if records is None and store is None:
        from app.engine.graph.builder import load_facts

        records = load_facts(session, evidence_root, snapshot.id)

    revision = (session.scalar(select(func.max(AnalyticsRevision.revision)).where(
        AnalyticsRevision.snapshot_id == snapshot.id)) or 0) + 1 if existing else 0
    relative = analytics_relative_path(snapshot)
    if revision:
        relative = relative.with_name(f"{relative.stem}-revision-{revision}.duckdb")
    target = _resolve(evidence_root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / ".staging"
    staging.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    temporary = staging / f"{target.name}.{token}.part"
    embedding_temporary = staging / f"{target.stem}.{token}.embeddings.npy"
    plan = current_plan()
    if store is not None:
        nt, no, ni = store.con.execute("SELECT (SELECT count(*) FROM tx), (SELECT count(*) FROM outputs), "
                                      "(SELECT count(*) FROM inputs)").fetchone()
    else:
        nt, no, ni = (len(records.get(kind, [])) for kind in ("transactions", "outputs", "inputs"))
    memory_estimate = int(nt * 256 + no * 192 + ni * 96)
    admit_global_allocation("entity/network analytics", memory_estimate, plan=plan)

    if store is not None:
        con = store.con
        owns_connection = False
        _views_over_store(con)
        _store_block_times(con)
    else:
        config = duckdb_config(staging / "spill")
        config["preserve_insertion_order"] = False
        con = duckdb.connect(config=config)
        owns_connection = True
        _register_records(con, records)
    try:
        con.execute(f"ATTACH {_quote(temporary)} AS an")
        summary = _compute(con, plan=plan, geoip_dir=geoip_dir, embedding_path=embedding_temporary)
        summary["global_memory_estimate_bytes"] = memory_estimate
        summary["execution_mode"] = "bounded" if store is not None else "memory"
        summary["revision"] = revision
        summary["supersedes_analytics_id"] = existing.id if existing else None
        summary["finding_refresh_policy"] = "append new natural keys; preserve prior findings, reviews and confidence"
        con.execute("CREATE TABLE an.meta (key VARCHAR, value VARCHAR)")
        con.execute("INSERT INTO an.meta VALUES ('analytics_version', ?), ('summary', ?)",
                    [ANALYTICS_VERSION, json.dumps(summary, sort_keys=True)])
        network_rows = _network_finding_rows(con)
        con.execute("DETACH an")
    finally:
        for name in ("in_addr", "out_ids", "tx_ids", "linking", "link_addr", "addr_ids", "wallet_spend", "obs_tx",
                     "ip_geo", "obs_time", "block_time", "a_obs"):
            try:
                con.execute(f"DROP TABLE IF EXISTS {name}")
            except duckdb.Error:
                pass
        if owns_connection:
            con.close()

    with temporary.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    os.replace(temporary, target)
    embedding_target = target.with_suffix(".embeddings.npy")
    if embedding_temporary.exists():
        os.replace(embedding_temporary, embedding_target)
    shutil.rmtree(staging / "spill", ignore_errors=True)

    record_type = AnalyticsRevision if revision else AnalyticsSnapshot
    record = record_type(
        case_id=snapshot.case_id,
        snapshot_id=snapshot.id,
        graph_snapshot_id=graph.id,
        storage_relative_path=relative.as_posix(),
        sha256=digest,
        analytics_version=ANALYTICS_VERSION,
        summary=dict(summary),
        state="complete",
        **({"revision": revision} if revision else {}),
    )
    session.add(record)
    session.flush()
    added = _write_network_findings(
        session, snapshot=snapshot, graph=graph, rows=network_rows, records=records, store=store, summary=summary
    )
    written = session.scalar(select(func.count()).select_from(FindingRecord).where(
        FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == NETWORK_RULE_VERSION)) or 0
    summary["network_findings"] = written
    summary["new_network_findings"] = added
    record.summary = dict(summary)
    append_event(
        session,
        case_id=snapshot.case_id,
        event_type="analytics.ready",
        stage="analytics",
        payload={"snapshot_id": snapshot.id, "analytics_snapshot_id": record.id, **{
            key: summary.get(key) for key in (
                "entity_count", "clustered_addresses", "network_findings", "geoip_installed", "embedded_wallets",
            )
        }},
    )
    return AnalyticsResult(record.id, summary, written)


def _compute(con: duckdb.DuckDBPyConnection, *, plan, geoip_dir: Path | None, embedding_path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"analytics_version": ANALYTICS_VERSION}
    timings: dict[str, float] = {}
    clock = [time.perf_counter()]

    def mark(name: str) -> None:
        now = time.perf_counter()
        timings[name] = round(now - clock[0], 3)
        clock[0] = now

    # ---- dense ids ---------------------------------------------------------
    con.execute(
        "CREATE TEMP TABLE tx_ids AS SELECT txid, (row_number() OVER (ORDER BY txid) - 1)::INTEGER AS tid, us, block_us "
        "FROM a_tx"
    )
    con.execute(
        """
        CREATE TEMP TABLE out_ids AS SELECT (row_number() OVER (ORDER BY t.tid, o.vout) - 1)::INTEGER AS oid,
            o.txid, o.vout, o.addr_key, COALESCE(o.amount, 0) AS amount, t.tid
        FROM a_out o JOIN tx_ids t USING (txid)
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE in_addr AS SELECT i.txid, t.tid, COALESCE(i.address, o.addr_key) AS addr,
            COALESCE(o.amount, i.amount) AS amount, o.oid AS prev_oid
        FROM a_in i JOIN tx_ids t USING (txid)
        LEFT JOIN out_ids o ON o.txid = i.prev_txid AND o.vout = i.prev_vout
        """
    )

    mark("dense_ids")
    # ---- common-input-ownership clustering --------------------------------
    con.execute(
        f"""
        CREATE TEMP TABLE linking AS
        WITH ins AS (SELECT txid, count(*) AS n_in, count(DISTINCT addr) AS n_addr FROM in_addr GROUP BY txid),
             outs AS (SELECT txid, max(c) AS max_equal FROM (
                        SELECT txid, amount, count(*) AS c FROM out_ids GROUP BY txid, amount) GROUP BY txid)
        SELECT ins.txid, ins.n_addr,
               (ins.n_in >= {MIXING_MIN_INPUTS} AND COALESCE(outs.max_equal, 0) >= {MIXING_MIN_EQUAL_OUTPUTS}) AS mixing
        FROM ins LEFT JOIN outs USING (txid) WHERE ins.n_addr >= 2
        """
    )
    summary["multi_input_transactions"], summary["mixing_excluded_transactions"] = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE mixing) FROM linking"
    ).fetchone()
    con.execute(
        "CREATE TEMP TABLE link_addr AS SELECT DISTINCT i.txid, i.addr FROM in_addr i JOIN linking l USING (txid) "
        "WHERE NOT l.mixing AND i.addr IS NOT NULL"
    )
    con.execute(
        "CREATE TEMP TABLE addr_ids AS SELECT addr, (row_number() OVER (ORDER BY addr) - 1)::INTEGER AS id "
        "FROM (SELECT DISTINCT addr FROM link_addr)"
    )
    address_count = con.execute("SELECT count(*) FROM addr_ids").fetchone()[0]
    roots, members = _numeric_columns(con,
        "SELECT min(a.id) OVER (PARTITION BY l.txid) AS root, a.id FROM link_addr l JOIN addr_ids a USING (addr)"
    )
    labels = _components(address_count, roots, members)
    del roots, members
    con.register("_labels", pa.table({
        "id": pa.array(range(address_count), pa.int32()),
        "root": pa.array(labels, pa.int32()) if not _has_numpy() else pa.array(labels.astype("int32")),
    }))
    con.execute(
        """
        CREATE TABLE an.address_entity AS
        WITH labelled AS (SELECT a.addr, a.id, l.root FROM addr_ids a JOIN _labels l USING (id)),
             roots AS (SELECT l.root, a.addr AS root_addr FROM (SELECT DISTINCT root FROM labelled) l
                       JOIN addr_ids a ON a.id = l.root)
        SELECT labelled.addr AS address, 'E-' || substr(md5(roots.root_addr), 1, 12) AS entity_id
        FROM labelled JOIN roots USING (root)
        ORDER BY address
        """
    )
    con.unregister("_labels")
    con.execute(
        "CREATE TABLE an.entity_links AS SELECT ae.entity_id, l.txid, count(*)::INTEGER AS input_addresses "
        "FROM link_addr l JOIN an.address_entity ae ON ae.address = l.addr GROUP BY ae.entity_id, l.txid "
        "ORDER BY ae.entity_id, l.txid"
    )
    con.execute(
        """
        CREATE TABLE an.entities AS
        WITH members AS (SELECT entity_id, count(*)::INTEGER AS address_count, min(address) AS root_address
                         FROM an.address_entity GROUP BY entity_id),
             links AS (SELECT entity_id, count(*)::INTEGER AS linking_tx_count FROM an.entity_links GROUP BY entity_id),
             recv AS (SELECT ae.entity_id, count(DISTINCT o.tid)::INTEGER AS received_tx_count,
                             sum(o.amount)::BIGINT AS received_sats, min(t.us) AS first_us, max(t.us) AS last_us
                      FROM out_ids o JOIN an.address_entity ae ON ae.address = o.addr_key
                      JOIN tx_ids t ON t.tid = o.tid GROUP BY ae.entity_id),
             sent AS (SELECT ae.entity_id, count(DISTINCT i.tid)::INTEGER AS spent_tx_count,
                             sum(i.amount)::BIGINT AS sent_sats, min(t.us) AS first_us, max(t.us) AS last_us
                      FROM in_addr i JOIN an.address_entity ae ON ae.address = i.addr
                      JOIN tx_ids t ON t.tid = i.tid GROUP BY ae.entity_id)
        SELECT m.entity_id, m.root_address, m.address_count, COALESCE(l.linking_tx_count, 0) AS linking_tx_count,
               COALESCE(r.received_tx_count, 0) AS received_tx_count, COALESCE(r.received_sats, 0) AS received_sats,
               COALESCE(s.spent_tx_count, 0) AS spent_tx_count, COALESCE(s.sent_sats, 0) AS sent_sats,
               least(r.first_us, s.first_us) AS first_seen_us, greatest(r.last_us, s.last_us) AS last_seen_us
        FROM members m LEFT JOIN links l USING (entity_id) LEFT JOIN recv r USING (entity_id)
        LEFT JOIN sent s USING (entity_id)
        ORDER BY m.address_count DESC, m.entity_id
        """
    )
    entity_count, clustered, largest = con.execute(
        "SELECT count(*), COALESCE(sum(address_count), 0), COALESCE(max(address_count), 0) FROM an.entities"
    ).fetchone()
    summary.update({
        "entity_count": int(entity_count),
        "clustered_addresses": int(clustered),
        "largest_entity_addresses": int(largest),
        "clustering_method": "common-input-ownership (CoinJoin-shaped inputs excluded)",
    })

    mark("clustering")
    # ---- wallets and value flows -------------------------------------------
    con.execute(
        """
        CREATE TABLE an.wallets AS
        WITH keys AS (
            SELECT DISTINCT COALESCE(ae.entity_id, a.addr) AS wallet,
                   CASE WHEN ae.entity_id IS NULL THEN 'address' ELSE 'entity' END AS kind
            FROM (SELECT addr_key AS addr FROM out_ids WHERE addr_key IS NOT NULL
                  UNION SELECT addr FROM in_addr WHERE addr IS NOT NULL) a
            LEFT JOIN an.address_entity ae ON ae.address = a.addr)
        SELECT (row_number() OVER (ORDER BY wallet) - 1)::INTEGER AS wid, wallet, kind FROM keys ORDER BY wid
        """
    )
    con.execute(
        """
        CREATE TABLE an.flow_out AS
        SELECT o.oid, o.tid, w.wid, o.amount FROM out_ids o
        LEFT JOIN an.address_entity ae ON ae.address = o.addr_key
        LEFT JOIN an.wallets w ON w.wallet = COALESCE(ae.entity_id, o.addr_key)
        ORDER BY o.oid
        """
    )
    con.execute(
        """
        CREATE TABLE an.flow_in AS
        SELECT i.tid, i.prev_oid AS oid, w.wid, COALESCE(i.amount, 0) AS amount FROM in_addr i
        LEFT JOIN an.address_entity ae ON ae.address = i.addr
        LEFT JOIN an.wallets w ON w.wallet = COALESCE(ae.entity_id, i.addr)
        ORDER BY i.tid
        """
    )
    con.execute("CREATE TABLE an.tx_index AS SELECT tid, txid, us FROM tx_ids ORDER BY tid")
    summary["wallet_count"] = con.execute("SELECT count(*) FROM an.wallets").fetchone()[0]

    mark("flows")
    # ---- Geo-IP enrichment --------------------------------------------------
    summary.update(_geo_enrichment(con, geoip_dir))
    mark("geoip")

    # ---- network <-> wallet correlation ------------------------------------
    summary.update(_network_correlation(con))
    mark("network_correlation")

    # ---- graph embeddings ----------------------------------------------------
    summary.update(_embeddings(con, embedding_path))
    mark("embeddings")
    summary["timings_s"] = timings
    return summary


def _has_numpy() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        return False
    return True


def _geo_enrichment(con: duckdb.DuckDBPyConnection, geoip_dir: Path | None) -> dict[str, Any]:
    database = geoip.open_database(geoip_dir)
    ips = [row[0] for row in con.execute(
        "SELECT DISTINCT ip FROM (SELECT src_ip AS ip FROM a_obs UNION SELECT dst_ip FROM a_obs) WHERE ip IS NOT NULL"
    ).fetchall()]
    results = []
    for ip in ips:
        if database is not None:
            results.append(database.lookup(ip))
        else:
            scope, address = geoip.ip_scope(ip)
            results.append(geoip.GeoIPResult(ip, scope, address.version if address else None))
    con.register("_geo", pa.table({
        "ip": pa.array([r.ip for r in results], pa.string()),
        "scope": pa.array([r.scope for r in results], pa.string()),
        "ip_version": pa.array([r.version for r in results], pa.int32()),
        "db_country": pa.array([r.country for r in results], pa.string()),
        "db_asn": pa.array([r.asn for r in results], pa.int64()),
        "db_as_org": pa.array([r.as_org for r in results], pa.string()),
    }))
    con.execute("CREATE TEMP TABLE ip_geo AS SELECT * FROM _geo")
    con.unregister("_geo")
    installed = database is not None
    con.execute(
        f"""
        CREATE TABLE an.endpoint_geo AS
        WITH country_votes AS (
            SELECT ip, value FROM (
                SELECT src_ip AS ip, geo_country AS value,
                       row_number() OVER (PARTITION BY src_ip ORDER BY count(*) DESC, geo_country) AS r
                FROM a_obs WHERE src_ip IS NOT NULL AND geo_country IS NOT NULL GROUP BY src_ip, geo_country)
            WHERE r = 1),
             asn_votes AS (
            SELECT ip, value FROM (
                SELECT src_ip AS ip, asn AS value,
                       row_number() OVER (PARTITION BY src_ip ORDER BY count(*) DESC, asn) AS r
                FROM a_obs WHERE src_ip IS NOT NULL AND asn IS NOT NULL GROUP BY src_ip, asn)
            WHERE r = 1),
             src AS (
            SELECT src_ip AS ip, count(*)::INTEGER AS observations, count(DISTINCT txid)::INTEGER AS transactions,
                   any_value(cv.value) AS reported_country, count(DISTINCT geo_country)::INTEGER AS reported_countries,
                   any_value(av.value) AS reported_asn, count(DISTINCT asn)::INTEGER AS reported_asns,
                   count(DISTINCT src_port)::INTEGER AS ports,
                   median((o.observed_us - t.block_us) / 1e6) AS median_relay_latency_s,
                   min(o.observed_us) AS first_observed_us, max(o.observed_us) AS last_observed_us
            FROM a_obs o LEFT JOIN tx_ids t USING (txid)
            LEFT JOIN country_votes cv ON cv.ip = o.src_ip LEFT JOIN asn_votes av ON av.ip = o.src_ip
            WHERE src_ip IS NOT NULL GROUP BY src_ip),
             dst AS (SELECT dst_ip AS ip, count(*)::INTEGER AS observations FROM a_obs WHERE dst_ip IS NOT NULL
                     GROUP BY dst_ip)
        SELECT g.ip, g.scope, g.ip_version, g.db_country, g.db_asn, g.db_as_org,
               COALESCE(src.observations, 0) AS src_observations, COALESCE(dst.observations, 0) AS dst_observations,
               COALESCE(src.transactions, 0) AS transactions, src.reported_country, src.reported_countries,
               src.reported_asn, src.reported_asns, src.ports, src.median_relay_latency_s,
               src.first_observed_us, src.last_observed_us,
               CASE WHEN src.reported_country IS NULL THEN 'not_reported'
                    WHEN g.scope <> 'public' THEN 'unverifiable_non_public'
                    WHEN NOT {installed} THEN 'no_geoip_database'
                    WHEN g.db_country IS NULL THEN 'not_in_database'
                    WHEN upper(src.reported_country) = g.db_country THEN 'verified'
                    ELSE 'mismatch' END AS country_check,
               CASE WHEN src.reported_asn IS NULL THEN 'not_reported'
                    WHEN g.scope <> 'public' THEN 'unverifiable_non_public'
                    WHEN NOT {installed} THEN 'no_geoip_database'
                    WHEN g.db_asn IS NULL THEN 'not_in_database'
                    WHEN TRY_CAST(regexp_replace(upper(src.reported_asn), '^AS', '') AS BIGINT) = g.db_asn
                        THEN 'verified'
                    ELSE 'mismatch' END AS asn_check
        FROM ip_geo g LEFT JOIN src USING (ip) LEFT JOIN dst USING (ip)
        ORDER BY g.ip
        """
    )
    checks = dict(con.execute(
        "SELECT country_check, sum(src_observations) FROM an.endpoint_geo WHERE src_observations > 0 GROUP BY 1"
    ).fetchall())
    asn_checks = dict(con.execute(
        "SELECT asn_check, sum(src_observations) FROM an.endpoint_geo WHERE src_observations > 0 GROUP BY 1"
    ).fetchall())
    scopes = dict(con.execute("SELECT scope, count(*) FROM an.endpoint_geo GROUP BY 1").fetchall())
    return {
        "geoip_installed": installed,
        "geoip_sources": [
            {key: source.get(key) for key in ("file", "kind", "licence", "attribution", "sha256")}
            for source in (database.manifest.get("sources", []) if database else [])
        ],
        "geoip_compiled_at": database.manifest.get("compiled_at") if database else None,
        "endpoint_ips": len(ips),
        "endpoint_scopes": {key: int(value) for key, value in scopes.items()},
        "observation_country_checks": {key: int(value or 0) for key, value in checks.items()},
        "observation_asn_checks": {key: int(value or 0) for key, value in asn_checks.items()},
    }


def _network_correlation(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    con.execute(
        "CREATE TEMP TABLE wallet_spend AS SELECT DISTINCT COALESCE(ae.entity_id, i.addr) AS wallet, i.txid "
        "FROM in_addr i LEFT JOIN an.address_entity ae ON ae.address = i.addr WHERE i.addr IS NOT NULL"
    )
    con.execute(
        """
        CREATE TEMP TABLE obs_tx AS
        SELECT DISTINCT o.txid, o.src_ip AS ip,
               COALESCE('AS' || g.db_asn::VARCHAR, upper(o.asn)) AS asn
        FROM a_obs o LEFT JOIN ip_geo g ON g.ip = o.src_ip
        WHERE o.txid IS NOT NULL AND o.src_ip IS NOT NULL
        """
    )
    observed_transactions = con.execute("SELECT count(DISTINCT txid) FROM obs_tx").fetchone()[0]
    findings: list[tuple] = []
    tested_total = 0
    for dimension, column in (("endpoint", "ip"), ("asn", "asn")):
        rows = con.execute(
            f"""
            WITH base AS (SELECT {column} AS key, count(DISTINCT txid)::DOUBLE / ? AS p
                          FROM obs_tx WHERE {column} IS NOT NULL GROUP BY 1),
                 spends AS (SELECT w.wallet, o.{column} AS key, count(DISTINCT w.txid) AS n
                            FROM wallet_spend w JOIN obs_tx o USING (txid) WHERE o.{column} IS NOT NULL
                            GROUP BY 1, 2),
                 totals AS (SELECT wallet, sum(n) AS t, count(*) AS keys FROM spends GROUP BY wallet),
                 top AS (SELECT wallet, arg_max(key, n) AS key, max(n) AS n FROM spends GROUP BY wallet)
            SELECT top.wallet, top.key, top.n::INTEGER, totals.t::INTEGER, totals.keys::INTEGER, base.p
            FROM top JOIN totals USING (wallet) JOIN base USING (key)
            WHERE totals.t >= {CORRELATION_MIN_SPENDS}
            """,
            [max(observed_transactions, 1)],
        ).fetchall()
        tested = len(rows)
        tested_total += tested
        for wallet, key, n, total, keys, p in rows:
            if n / total < CORRELATION_MIN_SHARE:
                continue
            p_value = binomial_tail(n, total, p)
            adjusted = min(1.0, p_value * max(tested, 1))
            if adjusted < CORRELATION_ALPHA:
                findings.append((dimension, wallet, key, n, total, keys, p, p_value, adjusted))
    con.execute(
        """
        CREATE TABLE an.network_correlations (dimension VARCHAR, wallet VARCHAR, key VARCHAR, spends_on_key INTEGER,
            observed_spends INTEGER, distinct_keys INTEGER, base_rate DOUBLE, p_value DOUBLE, adjusted_p_value DOUBLE)
        """
    )
    if findings:
        con.executemany("INSERT INTO an.network_correlations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", findings)
    # Top relay endpoints per wallet, for the entity view.
    con.execute(
        """
        CREATE TABLE an.wallet_endpoints AS
        SELECT wallet, ip, n AS spends FROM (
            SELECT w.wallet, o.ip, count(DISTINCT w.txid)::INTEGER AS n,
                   row_number() OVER (PARTITION BY w.wallet ORDER BY count(DISTINCT w.txid) DESC, o.ip) AS r
            FROM wallet_spend w JOIN obs_tx o USING (txid) GROUP BY w.wallet, o.ip)
        WHERE r <= 5 ORDER BY wallet, spends DESC
        """
    )
    coherence = _relay_flow_coherence(con, observed_transactions)
    mismatches = con.execute(
        f"""
        SELECT ip, scope, db_country, db_asn, db_as_org, reported_country, reported_asn, src_observations,
               country_check, asn_check
        FROM an.endpoint_geo
        WHERE (country_check = 'mismatch' OR asn_check = 'mismatch')
          AND src_observations >= {GEO_MISMATCH_MIN_OBSERVATIONS}
        ORDER BY src_observations DESC, ip
        """
    ).fetchall()
    con.execute(
        "CREATE TABLE an.geo_mismatches (ip VARCHAR, scope VARCHAR, db_country VARCHAR, db_asn BIGINT, "
        "db_as_org VARCHAR, reported_country VARCHAR, reported_asn VARCHAR, observations INTEGER, "
        "country_check VARCHAR, asn_check VARCHAR)"
    )
    if mismatches:
        con.executemany("INSERT INTO an.geo_mismatches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", mismatches)
    return {
        "observed_transactions": int(observed_transactions),
        "correlation_wallets_tested": int(tested_total),
        "relay_concentrations": len(findings),
        "relay_coherent_endpoints": coherence,
        "geo_mismatch_endpoints": len(mismatches),
    }


def _relay_flow_coherence(con: duckdb.DuckDBPyConnection, observed_transactions: int) -> int:
    """Endpoints that relay transactions spending outputs of transactions they
    also relayed, far more often than their share of traffic explains.

    One operator's own node broadcasting its own chain of spends (peel hops,
    consolidation rounds, layered re-spends) produces exactly this, however
    many single-use addresses the chain moves through -- which per-wallet
    concentration cannot see. Under independence, a relayed transaction with m
    observed parent transactions has P = 1 - (1 - p)^m of any parent sharing its
    endpoint (p = the endpoint's share of observed transactions); the count of
    coherent children is tested with a binomial tail at the mean P, Bonferroni-
    corrected across the endpoints tested.
    """
    rows = con.execute(
        """
        WITH parents AS (
            SELECT DISTINCT i.txid AS child, pt.txid AS parent
            FROM in_addr i JOIN out_ids o ON o.oid = i.prev_oid JOIN tx_ids pt ON pt.tid = o.tid),
             base AS (SELECT ip, count(DISTINCT txid)::DOUBLE / ? AS p FROM obs_tx GROUP BY ip),
             links AS (
            SELECT c.ip, p.child, p.parent, (pp.ip = c.ip) AS same
            FROM parents p JOIN obs_tx c ON c.txid = p.child JOIN obs_tx pp ON pp.txid = p.parent),
             per_child AS (
            SELECT ip, child, bool_or(same) AS coherent, count(DISTINCT parent) AS m FROM links GROUP BY ip, child)
        SELECT pc.ip, count(*)::INTEGER AS n, count(*) FILTER (WHERE coherent)::INTEGER AS k,
               avg(1 - pow(1 - b.p, pc.m)) AS q, b.p
        FROM per_child pc JOIN base b USING (ip)
        GROUP BY pc.ip, b.p HAVING count(*) >= 3
        """,
        [max(observed_transactions, 1)],
    ).fetchall()
    tested = len(rows)
    flagged = []
    for ip, n, k, q, p in rows:
        if k < 3 or k / n < 0.5:
            continue
        p_value = binomial_tail(k, n, min(max(q, 1e-12), 1.0))
        adjusted = min(1.0, p_value * max(tested, 1))
        if adjusted < CORRELATION_ALPHA:
            flagged.append((ip, n, k, q, p, p_value, adjusted))
    con.execute(
        "CREATE TABLE an.relay_coherence (ip VARCHAR, relayed_children INTEGER, coherent_children INTEGER, "
        "expected_share DOUBLE, base_rate DOUBLE, p_value DOUBLE, adjusted_p_value DOUBLE)"
    )
    if flagged:
        con.executemany("INSERT INTO an.relay_coherence VALUES (?, ?, ?, ?, ?, ?, ?)", flagged)
    return len(flagged)


def _embeddings(con: duckdb.DuckDBPyConnection, path: Path) -> dict[str, Any]:
    """Spectral embedding of wallets active in >= 2 transactions."""
    try:
        import numpy as np
        from scipy.sparse import coo_matrix, diags
        from sklearn.utils.extmath import randomized_svd
    except ImportError:
        return {"embedded_wallets": 0, "embedding_status": "unavailable (install the ml extra)"}
    wallets, transactions = _numeric_columns(con,
        """
        WITH touches AS (
            SELECT wid, tid FROM an.flow_out WHERE wid IS NOT NULL
            UNION SELECT wid, tid FROM an.flow_in WHERE wid IS NOT NULL),
             active AS (SELECT wid FROM touches GROUP BY wid HAVING count(*) >= 2)
        SELECT t.wid, t.tid FROM touches t JOIN active USING (wid)
        """
    )
    if wallets.size == 0:
        return {"embedded_wallets": 0, "embedding_status": "no wallet active in two transactions"}
    wallet_ids, rows = np.unique(wallets, return_inverse=True)
    tx_ids, cols = np.unique(transactions, return_inverse=True)
    matrix = coo_matrix(
        (np.ones(rows.size, dtype=np.float32), (rows, cols)), shape=(wallet_ids.size, tx_ids.size)
    ).tocsr()
    matrix.data[:] = 1.0  # duplicate touches count once
    row_degree = np.asarray(matrix.sum(axis=1)).ravel()
    col_degree = np.asarray(matrix.sum(axis=0)).ravel()
    normalised = diags(1.0 / np.sqrt(np.maximum(row_degree, 1))) @ matrix @ diags(1.0 / np.sqrt(np.maximum(col_degree, 1)))
    dimensions = int(min(EMBEDDING_DIMENSIONS, max(1, min(normalised.shape) - 1)))
    left, singular, _ = randomized_svd(normalised, n_components=dimensions, n_iter=4, random_state=42)
    embedding = (left * singular).astype(np.float32)
    norms = np.linalg.norm(embedding, axis=1, keepdims=True)
    embedding /= np.maximum(norms, 1e-12)
    np.save(path, embedding)
    con.register("_emb", pa.table({"row": pa.array(np.arange(wallet_ids.size, dtype=np.int32)),
                                   "wid": pa.array(wallet_ids.astype(np.int32))}))
    con.execute("CREATE TABLE an.embedding_rows AS SELECT * FROM _emb ORDER BY row")
    con.unregister("_emb")
    return {
        "embedded_wallets": int(wallet_ids.size),
        "embedding_dimensions": dimensions,
        "embedding_method": "truncated SVD of the degree-normalised wallet x transaction incidence matrix",
        "embedding_status": "complete",
    }


# --------------------------------------------------------------------------- #
# Network correlation findings
# --------------------------------------------------------------------------- #


def _network_finding_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    correlations = con.execute(
        "SELECT dimension, wallet, key, spends_on_key, observed_spends, distinct_keys, base_rate, p_value, "
        "adjusted_p_value FROM an.network_correlations ORDER BY adjusted_p_value, wallet, dimension LIMIT 500"
    ).fetchall()
    for dimension, wallet, key, n, total, keys, base_rate, p_value, adjusted in correlations:
        txs = con.execute(
            f"""
            SELECT DISTINCT w.txid, t.us FROM wallet_spend w JOIN obs_tx o USING (txid) JOIN tx_ids t USING (txid)
            WHERE w.wallet = ? AND o.{'ip' if dimension == 'endpoint' else 'asn'} = ? ORDER BY t.us NULLS LAST, w.txid
            """,
            [wallet, key],
        ).fetchall()
        geo = con.execute(
            "SELECT scope, db_country, db_asn, db_as_org, reported_country, reported_asn, country_check, asn_check "
            "FROM an.endpoint_geo WHERE ip = ?", [key]
        ).fetchone() if dimension == "endpoint" else None
        entity = con.execute(
            "SELECT address_count FROM an.entities WHERE entity_id = ?", [wallet]
        ).fetchone()
        rows.append({
            "kind": "concentration", "dimension": dimension, "wallet": wallet, "key": key, "n": n, "total": total,
            "keys": keys, "base_rate": base_rate, "p_value": p_value, "adjusted": adjusted,
            "txids": [txid for txid, _ in txs], "times": [us for _, us in txs if us is not None],
            "geo": geo, "entity_addresses": entity[0] if entity else None,
        })
    for ip, n, k, expected, base_rate, p_value, adjusted in con.execute(
        "SELECT * FROM an.relay_coherence ORDER BY adjusted_p_value, ip LIMIT 500"
    ).fetchall():
        txs = con.execute(
            """
            SELECT DISTINCT o.txid, t.us FROM obs_tx o JOIN tx_ids t USING (txid)
            WHERE o.ip = ? ORDER BY t.us NULLS LAST, o.txid LIMIT 200
            """,
            [ip],
        ).fetchall()
        geo = con.execute(
            "SELECT scope, db_country, db_asn, db_as_org, reported_country, reported_asn, country_check, asn_check "
            "FROM an.endpoint_geo WHERE ip = ?", [ip]
        ).fetchone()
        rows.append({
            "kind": "coherence", "ip": ip, "n": n, "k": k, "expected": expected, "base_rate": base_rate,
            "p_value": p_value, "adjusted": adjusted, "geo": geo,
            "txids": [txid for txid, _ in txs], "times": [us for _, us in txs if us is not None],
        })
    for ip, scope, db_country, db_asn, db_org, country, asn, observations, country_check, asn_check in con.execute(
        "SELECT * FROM an.geo_mismatches ORDER BY observations DESC, ip LIMIT 500"
    ).fetchall():
        txs = con.execute(
            "SELECT DISTINCT o.txid, t.us FROM a_obs o JOIN tx_ids t USING (txid) WHERE o.src_ip = ? "
            "ORDER BY t.us NULLS LAST, o.txid LIMIT 200", [ip]
        ).fetchall()
        rows.append({
            "kind": "geo_mismatch", "ip": ip, "scope": scope, "db_country": db_country, "db_asn": db_asn,
            "db_org": db_org, "reported_country": country, "reported_asn": asn, "observations": observations,
            "country_check": country_check, "asn_check": asn_check,
            "txids": [txid for txid, _ in txs], "times": [us for _, us in txs if us is not None],
        })
    return rows


def _refs_for(txids: list[str], records, store) -> list[dict]:
    wanted = txids[:25]
    if store is not None:
        by_txid = store.source_refs_by_txid(wanted)
    else:
        wanted_set = set(wanted)
        by_txid = {}
        for transaction in records["transactions"]:
            if transaction["txid"] in wanted_set:
                by_txid[transaction["txid"]] = transaction.get("source_refs") or []
    refs: list[dict] = []
    seen: set[str] = set()
    for txid in wanted:
        for ref in by_txid.get(txid, []):
            key = json.dumps(ref, sort_keys=True)
            if key not in seen:
                seen.add(key)
                refs.append(ref)
    return refs


def _write_network_findings(
    session: Session, *, snapshot: Snapshot, graph: GraphSnapshot, rows: list[dict], records, store,
    summary: dict[str, Any],
) -> int:
    if not rows:
        return 0
    def key(entity, start, end, rule):
        return (entity, start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC),
                end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC), rule)

    existing_keys = {key(*values) for values in session.execute(select(
        FindingRecord.entity_ref, FindingRecord.window_start, FindingRecord.window_end, FindingRecord.rule_id
    ).where(FindingRecord.snapshot_id == snapshot.id, FindingRecord.rule_version == NETWORK_RULE_VERSION))}
    fallback = snapshot.created_at or datetime.now(UTC)
    coverage = {
        "analytics_version": ANALYTICS_VERSION,
        "observed_transactions": summary.get("observed_transactions"),
        "wallets_tested": summary.get("correlation_wallets_tested"),
        "geoip_installed": summary.get("geoip_installed"),
        "geoip_sources": summary.get("geoip_sources"),
        "multiple_testing": f"Bonferroni across tested wallets, family-wise alpha {CORRELATION_ALPHA}",
    }
    prepared = []
    for row in rows:
        start = _from_micros(min(row["times"])) if row["times"] else fallback
        end = _from_micros(max(row["times"])) if row["times"] else fallback
        if end <= start:
            end = start + timedelta(seconds=1)
        refs = _refs_for(row["txids"], records, store)
        if row["kind"] == "concentration":
            wallet = row["wallet"]
            is_entity = wallet.startswith("E-") and row["entity_addresses"] is not None
            entity_ref = f"entity:{wallet}" if is_entity else f"address:{wallet}"
            what = (f"Entity cluster {wallet} ({row['entity_addresses']} addresses)" if is_entity
                    else f"Address {wallet}")
            dimension_text = "relay endpoint" if row["dimension"] == "endpoint" else "relay network (ASN)"
            share = row["n"] / row["total"]
            score = min(50.0, -math.log10(max(row["adjusted"], 1e-50)))
            geo = row.get("geo")
            geo_text = ""
            if geo:
                scope, db_country, db_asn, db_org, reported_country, reported_asn, _, _ = geo
                if scope == "public" and (db_country or db_asn):
                    geo_text = (f" Offline Geo-IP places {row['key']} in {db_country or 'an unknown country'}"
                                f"{f' (AS{db_asn} {db_org})' if db_asn else ''}.")
                else:
                    geo_text = (f" {row['key']} is a {scope} address, so its reported location "
                                f"({reported_country or '?'}/{reported_asn or '?'}) cannot be verified.")
            claim = (
                f"{what}: {row['n']} of its {row['total']} observed spends were first seen from {dimension_text} "
                f"{row['key']} ({share:.0%}), against a {row['base_rate']:.2%} base rate for that {dimension_text} "
                f"in this snapshot (Bonferroni-adjusted p = {row['adjusted']:.2g})."
            )
            feature_vector = {
                "feature_contract_version": NETWORK_RULE_VERSION,
                "dimension": row["dimension"], "wallet": wallet, "key": row["key"],
                "spends_on_key": row["n"], "observed_spends": row["total"], "distinct_keys": row["keys"],
                "share": share, "base_rate": row["base_rate"], "p_value": row["p_value"],
                "adjusted_p_value": row["adjusted"], "transactions": row["txids"][:200],
                "geo": dict(zip(("scope", "db_country", "db_asn", "db_as_org", "reported_country", "reported_asn",
                                 "country_check", "asn_check"), geo, strict=True)) if geo else None,
            }
            rule_id = "relay_endpoint_concentration" if row["dimension"] == "endpoint" else "relay_asn_concentration"
            explanations = [claim + geo_text, (
                "Correlates the network layer (which peer relayed each spend) with the blockchain layer "
                "(which wallet spent). It links observations; it does not identify who operated either."
            )]
            benign = [
                "A wallet service, exchange or custodian broadcasting from its own node.",
                "The observer's vantage point: one well-connected peer relays most traffic it sees.",
                "Shared NAT/VPN egress or a hosting provider used by many unrelated users.",
            ]
            opposing = [f"{row['total'] - row['n']} observed spend(s) of this wallet came from other endpoints."]
        elif row["kind"] == "coherence":
            entity_ref = f"endpoint:{row['ip']}"
            score = min(50.0, -math.log10(max(row["adjusted"], 1e-50)))
            claim = (
                f"Endpoint {row['ip']} relayed {row['n']} transaction(s) whose parent transactions were also observed; "
                f"{row['k']} of them spend outputs of a transaction this same endpoint relayed, against "
                f"{row['expected']:.2%} expected from its {row['base_rate']:.2%} share of relayed traffic "
                f"(Bonferroni-adjusted p = {row['adjusted']:.2g})."
            )
            geo = row.get("geo")
            feature_vector = {
                "feature_contract_version": NETWORK_RULE_VERSION, "ip": row["ip"], "relayed_children": row["n"],
                "coherent_children": row["k"], "expected_share": row["expected"], "base_rate": row["base_rate"],
                "p_value": row["p_value"], "adjusted_p_value": row["adjusted"], "transactions": row["txids"][:200],
                "geo": dict(zip(("scope", "db_country", "db_asn", "db_as_org", "reported_country", "reported_asn",
                                 "country_check", "asn_check"), geo, strict=True)) if geo else None,
            }
            rule_id = "relay_flow_coherence"
            explanations = [claim, (
                "A node that relays a chain of transactions each spending the previous one is typically the node "
                "of whoever controls that chain (e.g. a peeling chain or layered re-spends), even when every hop "
                "uses a fresh address. It correlates observations; it does not identify the operator."
            )]
            benign = ["A wallet service or exchange broadcasting its own customers' chained withdrawals.",
                      "An observer that only sees a few peers, so one peer relays most chains it sees."]
            opposing = [f"{row['n'] - row['k']} relayed child transaction(s) had parents relayed elsewhere."]
        else:
            entity_ref = f"endpoint:{row['ip']}"
            score = float(row["observations"])
            org_text = f" ({row['db_org']})" if row["db_org"] else ""
            claim = (
                f"Endpoint {row['ip']}: the dataset reports {row['reported_country'] or '?'} / "
                f"{row['reported_asn'] or '?'} on {row['observations']} observation(s), but the offline Geo-IP "
                f"database assigns {row['db_country'] or '?'} / {('AS' + str(row['db_asn'])) if row['db_asn'] else '?'}"
                f"{org_text}."
            )
            feature_vector = {
                "feature_contract_version": NETWORK_RULE_VERSION, "ip": row["ip"], "scope": row["scope"],
                "db_country": row["db_country"], "db_asn": row["db_asn"], "db_as_org": row["db_org"],
                "reported_country": row["reported_country"], "reported_asn": row["reported_asn"],
                "observations": row["observations"], "country_check": row["country_check"],
                "asn_check": row["asn_check"], "transactions": row["txids"][:200],
            }
            rule_id = "reported_geo_mismatch"
            explanations = [claim, (
                "Reported network metadata that contradicts an independent Geo-IP source can mean "
                "spoofed, stale or mis-joined observations; review before relying on it."
            )]
            benign = ["The Geo-IP database may be stale for re-assigned or anycast ranges.",
                      "The source may report the observer's location rather than the peer's."]
            opposing = []
        feature_hash = hashlib.sha256(json.dumps(feature_vector, sort_keys=True).encode()).hexdigest()
        prepared.append((score, entity_ref, start, end, rule_id, claim, feature_vector, feature_hash,
                         explanations, benign, opposing, refs))
    prepared.sort(key=lambda item: (-item[0], item[1], item[4]))
    added = 0
    for rank, (score, entity_ref, start, end, rule_id, claim, feature_vector, feature_hash, explanations,
               benign, opposing, refs) in enumerate(prepared, 1):
        natural_key = key(entity_ref, start, end, rule_id)
        if natural_key in existing_keys:
            continue
        existing_keys.add(natural_key)
        added += 1
        session.add(FindingRecord(
            case_id=snapshot.case_id, snapshot_id=snapshot.id, graph_snapshot_id=graph.id, entity_ref=entity_ref,
            window_start=start, window_end=end, rule_id=rule_id, rule_version=NETWORK_RULE_VERSION, claim=claim,
            raw_score=float(score), rank=rank, coverage=coverage, feature_vector=feature_vector,
            feature_vector_hash=feature_hash, explanations=explanations, benign_alternatives=benign,
            opposing_evidence=opposing, source_refs=refs,
        ))
    session.flush()
    return added


# --------------------------------------------------------------------------- #
# Read side
# --------------------------------------------------------------------------- #

_CACHE_SIZE = 2
_connections: OrderedDict[str, duckdb.DuckDBPyConnection] = OrderedDict()
_embeddings_cache: OrderedDict[str, Any] = OrderedDict()
_lock = threading.Lock()


def latest_analytics(session: Session, case_id: str, snapshot_id: str | None = None) -> AnalyticsSnapshot | AnalyticsRevision | None:
    query = select(AnalyticsSnapshot).where(AnalyticsSnapshot.case_id == case_id, AnalyticsSnapshot.state == "complete")
    if snapshot_id:
        query = query.where(AnalyticsSnapshot.snapshot_id == snapshot_id)
    original = session.scalar(query.order_by(AnalyticsSnapshot.created_at.desc(), AnalyticsSnapshot.id).limit(1))
    if original is None:
        return None
    # Choose the latest source snapshot first, then its latest revision. A
    # refresh of an older source must not hide a newer imported snapshot.
    revision = session.scalar(select(AnalyticsRevision).where(
        AnalyticsRevision.snapshot_id == original.snapshot_id, AnalyticsRevision.case_id == case_id,
        AnalyticsRevision.state == "complete").order_by(AnalyticsRevision.revision.desc()).limit(1))
    return revision or original


def cursor_for(evidence_root: Path, record: AnalyticsSnapshot) -> duckdb.DuckDBPyConnection:
    path = str(_resolve(evidence_root, record.storage_relative_path))
    with _lock:
        connection = _connections.get(path)
        if connection is None:
            budget_mb = max(64, min(256, current_plan().memory_budget_bytes // (8 << 20)))
            connection = duckdb.connect(path, read_only=True, config={"threads": 1, "memory_limit": f"{budget_mb}MB"})
            _connections[path] = connection
            while len(_connections) > _CACHE_SIZE:
                _connections.popitem(last=False)
        else:
            _connections.move_to_end(path)
        return connection.cursor()


def _rows(cursor, sql: str, params: list | None = None) -> list[dict[str, Any]]:
    result = cursor.execute(sql, params or [])
    names = [column[0] for column in result.description]
    return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def _entity_view(row: dict[str, Any]) -> dict[str, Any]:
    view = dict(row)
    view["first_seen"] = _from_micros(view.pop("first_seen_us", None))
    view["last_seen"] = _from_micros(view.pop("last_seen_us", None))
    for key in ("first_seen", "last_seen"):
        view[key] = view[key].isoformat() if view[key] else None
    return view


def wallet_for_addresses(cursor, addresses: list[str]) -> dict[str, str]:
    if not addresses:
        return {}
    placeholders = ",".join("?" for _ in addresses)
    return dict(cursor.execute(
        f"SELECT address, entity_id FROM address_entity WHERE address IN ({placeholders})", addresses
    ).fetchall())


def list_entities(cursor, *, limit: int, offset: int, min_addresses: int = 2) -> dict[str, Any]:
    total = cursor.execute("SELECT count(*) FROM entities WHERE address_count >= ?", [min_addresses]).fetchone()[0]
    rows = _rows(cursor, "SELECT * FROM entities WHERE address_count >= ? ORDER BY address_count DESC, entity_id "
                         "LIMIT ? OFFSET ?", [min_addresses, limit, offset])
    return {"total": total, "entities": [_entity_view(row) for row in rows]}


def entity_detail(cursor, wallet: str, *, address_limit: int = 200, link_limit: int = 100) -> dict[str, Any] | None:
    """An entity cluster (E-...) or, for an unclustered address, the address itself."""
    if not wallet.startswith("E-"):
        entity = cursor.execute("SELECT entity_id FROM address_entity WHERE address = ?", [wallet]).fetchone()
        if entity:
            wallet = entity[0]
    rows = _rows(cursor, "SELECT * FROM entities WHERE entity_id = ?", [wallet])
    endpoints = _rows(cursor, "SELECT w.ip, w.spends, g.scope, g.db_country, g.db_asn, g.db_as_org, "
                              "g.reported_country, g.reported_asn, g.country_check, g.asn_check "
                              "FROM wallet_endpoints w LEFT JOIN endpoint_geo g USING (ip) WHERE w.wallet = ? "
                              "ORDER BY w.spends DESC, w.ip", [wallet])
    correlations = _rows(cursor, "SELECT * FROM network_correlations WHERE wallet = ? ORDER BY adjusted_p_value",
                         [wallet])
    if not rows:
        known = cursor.execute("SELECT kind FROM wallets WHERE wallet = ?", [wallet]).fetchone()
        if not known:
            return None
        return {"wallet": wallet, "kind": "address", "entity": None, "addresses": [wallet], "address_total": 1,
                "linking_transactions": [], "linking_total": 0, "relay_endpoints": endpoints,
                "network_correlations": correlations}
    addresses = [row[0] for row in cursor.execute(
        "SELECT address FROM address_entity WHERE entity_id = ? ORDER BY address LIMIT ?", [wallet, address_limit]
    ).fetchall()]
    links = _rows(cursor, "SELECT l.txid, l.input_addresses, t.us FROM entity_links l LEFT JOIN tx_index t USING (txid) "
                          "WHERE l.entity_id = ? ORDER BY t.us NULLS LAST, l.txid LIMIT ?", [wallet, link_limit])
    for link in links:
        moment = _from_micros(link.pop("us"))
        link["time"] = moment.isoformat() if moment else None
    entity = _entity_view(rows[0])
    return {
        "wallet": wallet, "kind": "entity", "entity": entity, "addresses": addresses,
        "address_total": entity["address_count"], "linking_transactions": links,
        "linking_total": entity["linking_tx_count"], "relay_endpoints": endpoints,
        "network_correlations": correlations,
        "basis": "common-input-ownership: every address here was spent as an input together with another member "
                 "in at least one listed transaction. A proposition for review, not proof of common control.",
    }


def endpoint_view(cursor, ip: str) -> dict[str, Any] | None:
    rows = _rows(cursor, "SELECT * FROM endpoint_geo WHERE ip = ?", [ip])
    if not rows:
        return None
    view = rows[0]
    for key in ("first_observed_us", "last_observed_us"):
        moment = _from_micros(view.pop(key))
        view[key.replace("_us", "")] = moment.isoformat() if moment else None
    view["wallets"] = _rows(cursor, "SELECT wallet, spends FROM wallet_endpoints WHERE ip = ? ORDER BY spends DESC, "
                                    "wallet LIMIT 50", [ip])
    return view


def network_summary(cursor) -> dict[str, Any]:
    summary = json.loads(cursor.execute("SELECT value FROM meta WHERE key = 'summary'").fetchone()[0])
    top_endpoints = _rows(cursor, "SELECT ip, scope, transactions, src_observations, db_country, db_asn, db_as_org, "
                                  "reported_country, reported_asn, country_check, asn_check, median_relay_latency_s "
                                  "FROM endpoint_geo ORDER BY transactions DESC, ip LIMIT 25")
    countries = _rows(cursor, "SELECT COALESCE(db_country, reported_country, '??') AS country, "
                              "sum(src_observations)::BIGINT AS observations FROM endpoint_geo GROUP BY 1 "
                              "ORDER BY observations DESC LIMIT 20")
    return {"summary": summary, "top_endpoints": top_endpoints, "countries": countries}


def _embedding_matrix(evidence_root: Path, record: AnalyticsSnapshot):
    import numpy as np

    path = _resolve(evidence_root, record.storage_relative_path).with_suffix(".embeddings.npy")
    key = str(path)
    with _lock:
        cached = _embeddings_cache.get(key)
        if cached is not None:
            _embeddings_cache.move_to_end(key)
            return cached
    if not path.exists():
        return None
    matrix = np.load(path, mmap_mode="r")
    with _lock:
        _embeddings_cache[key] = matrix
        while len(_embeddings_cache) > 4:
            _embeddings_cache.popitem(last=False)
    return matrix


def similar_wallets(evidence_root: Path, record: AnalyticsSnapshot, wallet: str, *, limit: int = 10) -> dict[str, Any]:
    try:
        import numpy as np
    except ImportError:
        return {"wallet": wallet, "status": "unavailable", "similar": []}
    cursor = cursor_for(evidence_root, record)
    if not wallet.startswith("E-"):
        entity = cursor.execute("SELECT entity_id FROM address_entity WHERE address = ?", [wallet]).fetchone()
        if entity:
            wallet = entity[0]
    row = cursor.execute(
        "SELECT e.row FROM wallets w JOIN embedding_rows e USING (wid) WHERE w.wallet = ?", [wallet]
    ).fetchone() if _table_exists(cursor, "embedding_rows") else None
    matrix = _embedding_matrix(evidence_root, record)
    if row is None or matrix is None:
        return {"wallet": wallet, "status": "not_embedded (active in fewer than two transactions)", "similar": []}
    vector = np.asarray(matrix[row[0]], dtype=np.float32)
    scores = np.asarray(matrix @ vector)
    scores[row[0]] = -np.inf
    count = min(limit, scores.size - 1)
    if count <= 0:
        return {"wallet": wallet, "status": "complete", "similar": []}
    top = np.argpartition(-scores, count - 1)[:count]
    top = top[np.argsort(-scores[top], kind="stable")]
    rows = [int(item) for item in top]
    placeholders = ",".join("?" for _ in rows)
    keys = dict(cursor.execute(
        f"SELECT e.row, w.wallet FROM embedding_rows e JOIN wallets w USING (wid) WHERE e.row IN ({placeholders})",
        rows,
    ).fetchall())
    return {
        "wallet": wallet,
        "status": "complete",
        "method": "cosine similarity of spectral graph embeddings",
        "similar": [{"wallet": keys[item], "similarity": float(scores[item])} for item in rows if item in keys],
    }


def _table_exists(cursor, name: str) -> bool:
    return bool(cursor.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0])


# --------------------------------------------------------------------------- #
# Risk propagation
# --------------------------------------------------------------------------- #

RISK_DECAY = 0.85
RISK_MAX_HOPS = 8
RISK_TOP = 2000


def propagate_risk(
    session: Session, *, evidence_root: Path, record: AnalyticsSnapshot, case_id: str, created_by: str | None,
    decay: float = RISK_DECAY, max_hops: int = RISK_MAX_HOPS,
) -> RiskRun:
    """Propagate seed risk over the UTXO value-flow graph, both directions.

    Downstream ("received funds from a seed"): every output a seed wallet
    spends carries the seed's weight; a transaction's taint is the
    value-weighted average taint of the outputs it spends (the haircut
    method), and every output it creates inherits that taint times `decay`
    per hop. A wallet's downstream risk is the value-weighted taint of what
    it received, and `tainted_received_sats` is the tainted value itself.

    Upstream ("sent funds to a seed"): the mirror image -- a transaction's
    upstream score is the value share of its outputs going to risky wallets,
    passed back to the wallets that funded it.

    Risk = 1 - (1 - downstream)(1 - upstream). Seeds are fixed at their weight.
    """
    import numpy as np
    from scipy.sparse import csr_matrix

    seeds = list(session.scalars(select(RiskSeed).where(RiskSeed.case_id == case_id).order_by(RiskSeed.created_at)))
    cursor = cursor_for(evidence_root, record)
    wallet_count = cursor.execute("SELECT count(*) FROM wallets").fetchone()[0]
    tx_count = cursor.execute("SELECT count(*) FROM tx_index").fetchone()[0]
    flow_count = cursor.execute("SELECT (SELECT count(*) FROM flow_out) + (SELECT count(*) FROM flow_in)").fetchone()[0]
    try:
        admit_global_allocation("risk propagation", wallet_count * 256 + tx_count * 192 + flow_count * 128)
    except RuntimeError:
        cursor.close()
        raise
    seed_vector = np.zeros(wallet_count, dtype=np.float64)
    resolved: list[dict[str, Any]] = []
    for seed in seeds:
        ref = seed.wallet_ref.removeprefix("address:").removeprefix("entity:")
        wallet = ref
        if not ref.startswith("E-"):
            entity = cursor.execute("SELECT entity_id FROM address_entity WHERE address = ?", [ref]).fetchone()
            if entity:
                wallet = entity[0]
        row = cursor.execute("SELECT wid FROM wallets WHERE wallet = ?", [wallet]).fetchone()
        resolved.append({"seed_id": seed.id, "wallet_ref": seed.wallet_ref, "wallet": wallet if row else None,
                         "label": seed.label, "weight": seed.weight, "found": bool(row)})
        if row:
            seed_vector[row[0]] = max(seed_vector[row[0]], float(seed.weight))

    oid, out_tid, out_wid, out_amount = _numeric_columns(
        cursor, "SELECT oid, tid, wid, amount FROM flow_out ORDER BY oid",
        dtypes=[np.int64, np.int64, np.float64, np.float64])
    out_amount = np.maximum(out_amount, 0.0)
    out_count = oid.size
    has_wallet = ~np.isnan(out_wid.astype(np.float64)) if out_wid.dtype.kind == "f" else np.ones(out_count, bool)
    out_wid_int = np.where(has_wallet, np.nan_to_num(out_wid.astype(np.float64), nan=0), 0).astype(np.int64)
    in_tid, in_oid = _numeric_columns(cursor, "SELECT tid, oid FROM flow_in WHERE oid IS NOT NULL")

    # spend[t, o] = value share of tx t's resolved inputs that output o supplies.
    spent_value = out_amount[in_oid]
    totals = np.bincount(in_tid, weights=spent_value, minlength=tx_count)
    share = np.divide(spent_value, totals[in_tid], out=np.zeros_like(spent_value), where=totals[in_tid] > 0)
    spend = csr_matrix((share, (in_tid, in_oid)), shape=(tx_count, out_count))
    # creates[o, t] = 1 when output o is created by tx t.
    creates = csr_matrix((np.ones(out_count), (oid, out_tid)), shape=(out_count, tx_count))
    # own[o, w] = 1 when output o is locked to wallet w.
    own = csr_matrix((has_wallet.astype(np.float64), (oid, out_wid_int)), shape=(out_count, wallet_count))

    seed_outputs = own @ seed_vector  # outputs locked to a seed carry its weight
    downstream = seed_outputs.copy()
    for _ in range(max_hops):
        tx_taint = spend @ downstream
        updated = np.maximum(seed_outputs, decay * (creates @ tx_taint))
        if np.allclose(updated, downstream):
            break
        downstream = updated
    received = own.T @ out_amount
    tainted_received = own.T @ (downstream * out_amount)
    down_wallet = np.divide(tainted_received, received, out=np.zeros(wallet_count), where=received > 0)

    # Upstream: share of each tx's created value that reaches risky wallets,
    # handed back to the outputs that funded it (each output has one spender).
    created_value = creates.T @ out_amount
    spender = np.full(out_count, -1, dtype=np.int64)
    spender[in_oid] = in_tid
    spent_mask = spender >= 0
    upstream_out = seed_outputs.copy()
    for _ in range(max_hops):
        risky_value = creates.T @ (upstream_out * out_amount)
        tx_up = np.divide(risky_value, created_value, out=np.zeros(tx_count), where=created_value > 0)
        handed_back = np.zeros(out_count)
        handed_back[spent_mask] = decay * tx_up[spender[spent_mask]]
        updated = np.maximum(seed_outputs, handed_back)
        if np.allclose(updated, upstream_out):
            break
        upstream_out = updated
    spent_amount = out_amount * spent_mask
    spent_by_wallet = own.T @ spent_amount
    upstream_value = own.T @ (spent_amount * upstream_out)
    up_wallet = np.divide(upstream_value, spent_by_wallet, out=np.zeros(wallet_count), where=spent_by_wallet > 0)

    down_wallet = np.maximum(down_wallet, seed_vector)
    up_wallet = np.maximum(up_wallet, seed_vector)
    combined = 1.0 - (1.0 - np.clip(down_wallet, 0, 1)) * (1.0 - np.clip(up_wallet, 0, 1))
    order = np.argsort(-combined, kind="stable")
    order = order[combined[order] > 1e-6][:RISK_TOP]
    keys: dict[int, tuple[str, str]] = {}
    for start in range(0, order.size, 1000):
        chunk = [int(item) for item in order[start:start + 1000]]
        placeholders = ",".join("?" for _ in chunk)
        keys.update({
            wid: (wallet, kind)
            for wid, wallet, kind in cursor.execute(
                f"SELECT wid, wallet, kind FROM wallets WHERE wid IN ({placeholders})", chunk
            ).fetchall()
        })
    scores = []
    for wid in order:
        wid = int(wid)
        wallet, kind = keys.get(wid, (str(wid), "unknown"))
        scores.append({
            "wallet": wallet, "kind": kind, "risk": round(float(combined[wid]), 6),
            "downstream": round(float(down_wallet[wid]), 6), "upstream": round(float(up_wallet[wid]), 6),
            "tainted_received_sats": round(float(tainted_received[wid])),
            "received_sats": round(float(received[wid])),
            "is_seed": bool(seed_vector[wid] > 0),
        })
    run = RiskRun(
        case_id=case_id,
        snapshot_id=record.snapshot_id,
        method_version=RISK_METHOD_VERSION,
        parameters={"decay": decay, "max_hops": max_hops, "top": RISK_TOP,
                    "analytics_snapshot_id": record.id, "analytics_sha256": record.sha256},
        seeds=resolved,
        summary={
            "wallets": int(wallet_count),
            "wallets_with_risk": int((combined > 1e-6).sum()),
            "wallets_above_0_5": int((combined >= 0.5).sum()),
            "wallets_above_0_1": int((combined >= 0.1).sum()),
            "seeds_found": sum(1 for item in resolved if item["found"]),
            "seeds_total": len(resolved),
            "method": "value-weighted (haircut) taint over the UTXO flow graph, downstream and upstream, "
                      f"decay {decay} per hop, at most {max_hops} hops",
        },
        scores=scores,
        created_by=created_by,
    )
    session.add(run)
    session.flush()
    return run
