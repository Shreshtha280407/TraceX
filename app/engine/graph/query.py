"""Bounded, parameterized DuckDB neighbourhood queries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import duckdb

from app.config import settings
from app.models import GraphSnapshot
from app.resources import current_plan


class GraphQueryError(ValueError):
    pass


class GraphNodeNotFound(GraphQueryError):
    pass


# Graph snapshots are immutable files, so one read-only DuckDB instance per
# file can serve every request: opening the database (catalog + index load)
# used to be paid on every single graph query. Each request takes its own
# cursor, which is DuckDB's thread-safe way to share one database instance.
_CONNECTION_CACHE_SIZE = 2
_connections: OrderedDict[str, duckdb.DuckDBPyConnection] = OrderedDict()
_connections_lock = threading.Lock()


def _cursor_for(path: Path) -> duckdb.DuckDBPyConnection:
    key = str(path)
    with _connections_lock:
        connection = _connections.get(key)
        if connection is None:
            budget_mb = max(64, min(256, current_plan().memory_budget_bytes // (8 << 20)))
            connection = duckdb.connect(key, read_only=True, config={"threads": 1, "memory_limit": f"{budget_mb}MB"})
            _connections[key] = connection
            while len(_connections) > _CONNECTION_CACHE_SIZE:
                # Dropped, not closed: a request on another thread may still hold
                # a cursor on it, and the instance is freed with its last cursor.
                _connections.popitem(last=False)
        else:
            _connections.move_to_end(key)
        return connection.cursor()


def _path(evidence_root: Path, relative_path: str) -> Path:
    root = evidence_root.resolve()
    value = (root / relative_path).resolve()
    if root not in value.parents:
        raise GraphQueryError("stored graph path escaped evidence root")
    return value


def _node_id(seed: str) -> str:
    if len(seed) == 64 and all(character in "0123456789abcdef" for character in seed.lower()):
        return f"tx:{seed.lower()}"
    return seed


def _cursor(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(settings.secret_key.encode(), b"graph-v2:" + raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + signature


def _decode_cursor(token, binding):
    try:
        if len(token) > 4096:
            raise ValueError("oversized")
        encoded, signature = token.split(".")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        expected = hmac.new(settings.secret_key.encode(), b"graph-v2:" + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        value = json.loads(raw)
        if any(value.get(key) != entry for key, entry in binding.items()):
            raise ValueError("scope")
        if any(type(value.get(key)) is not int or value[key] < 0 for key in ("nodes_offset", "edges_offset")):
            raise ValueError("offset")
        return value["nodes_offset"], value["edges_offset"]
    except (ValueError, TypeError, KeyError) as exc:
        raise GraphQueryError("invalid, stale or differently scoped graph cursor") from exc


def query_neighbourhood(
    *, evidence_root: Path, graph: GraphSnapshot, seed: str, depth: int, node_limit: int, edge_limit: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    if not 1 <= depth <= 5:
        raise GraphQueryError("depth must be in 1..5")
    if not 1 <= node_limit <= 1000 or not 1 <= edge_limit <= 3000:
        raise GraphQueryError("limits exceed graph safety caps")
    source = _node_id(seed)
    binding = {"version": 2, "case": graph.case_id, "graph": graph.id, "sha256": graph.sha256,
               "seed": source, "depth": depth, "node_limit": node_limit, "edge_limit": edge_limit}
    nodes_offset, edges_offset = _decode_cursor(cursor, binding) if cursor else (0, 0)
    connection = _cursor_for(_path(evidence_root, graph.storage_relative_path))
    try:
        if connection.execute("SELECT 1 FROM nodes WHERE node_id = ?", [source]).fetchone() is None:
            return {
                "snapshot_id": graph.snapshot_id,
                "graph_snapshot_id": graph.id,
                "coverage": graph.coverage,
                "nodes": [],
                "edges": [],
                "truncated_nodes": 0,
                "truncated_edges": 0,
                "cursor": None,
            }
        # Reachability remains in DuckDB; no whole-neighborhood Python dictionaries.
        # Nodes and edges have independent, stable pages. Endpoints may arrive on
        # later pages; clients merge by ID before drawing an edge.
        connection.execute("CREATE TEMP TABLE q_reach (node_id VARCHAR PRIMARY KEY, level INTEGER)")
        connection.execute("INSERT INTO q_reach VALUES (?, 0)", [source])
        for level in range(depth):
            connection.execute("CREATE OR REPLACE TEMP TABLE q_next AS SELECT DISTINCT node_id FROM ("
                "SELECT e.to_node AS node_id FROM edges e JOIN q_reach r ON r.node_id=e.from_node WHERE r.level=? "
                "UNION SELECT e.from_node FROM edges e JOIN q_reach r ON r.node_id=e.to_node WHERE r.level=?) "
                "WHERE node_id NOT IN (SELECT node_id FROM q_reach)", [level, level])
            connection.execute("INSERT INTO q_reach SELECT node_id, ? FROM q_next", [level + 1])
        connection.execute("CREATE TEMP VIEW q_edges AS SELECT e.* FROM edges e WHERE e.from_node IN "
                           "(SELECT node_id FROM q_reach WHERE level < " + str(depth) + ") OR e.to_node IN "
                           "(SELECT node_id FROM q_reach WHERE level < " + str(depth) + ")")
        total_nodes = connection.execute("SELECT count(*) FROM q_reach").fetchone()[0]
        total_edges = connection.execute("SELECT count(*) FROM q_edges").fetchone()[0]
        node_rows = connection.execute(
            "SELECT n.node_id, node_type, label, attributes_json FROM nodes n JOIN q_reach r USING (node_id) "
            "ORDER BY n.node_id != ?, n.node_id LIMIT ? OFFSET ?", [source, node_limit, nodes_offset],
        ).fetchall()
        edge_rows = connection.execute("SELECT edge_id, from_node, to_node, edge_type, attributes_json, uncertainty "
                                       "FROM q_edges ORDER BY edge_id LIMIT ? OFFSET ?", [edge_limit, edges_offset]).fetchall()
        nodes_offset += len(node_rows)
        edges_offset += len(edge_rows)
        truncated_nodes, truncated_edges = total_nodes - nodes_offset, total_edges - edges_offset
    finally:
        connection.close()
    return {
        "snapshot_id": graph.snapshot_id,
        "graph_snapshot_id": graph.id,
        "coverage": graph.coverage,
        "nodes": [
            {"id": node_id, "type": node_type, "label": label, "attributes": json.loads(attributes)}
            for node_id, node_type, label, attributes in node_rows
        ],
        "edges": [
            {
                "id": edge_id,
                "from": from_node,
                "to": to_node,
                "type": edge_type,
                "attributes": json.loads(attributes),
                "uncertainty": uncertainty,
            }
            for edge_id, from_node, to_node, edge_type, attributes, uncertainty in edge_rows
        ],
        "truncated_nodes": truncated_nodes,
        "truncated_edges": truncated_edges,
        "cursor": _cursor({**binding, "nodes_offset": nodes_offset, "edges_offset": edges_offset})
                  if truncated_nodes or truncated_edges else None,
        "continuation_contract": "graph-v2: independent stable node/edge pages; merge by ID; endpoints may follow",
    }


# ---------------------------------------------------------------------------
# Direct fund flow around one node ("set as source")
# ---------------------------------------------------------------------------

FLOW_SIDE_LIMIT_MAX = 200

_DEAD_END_ADDRESS = "No other transaction in this snapshot pays into or spends from this address."
_DEAD_END_TX = "Every input and output of this transaction belongs to the current source; there is nothing further to follow."
_DEAD_END_UNSPENT = "Unspent output with no address: these funds have not moved again in this snapshot."


def _in_query(cursor, sql: str, values: list[str], trailing: list | None = None, repeat: int = 1) -> list[tuple]:
    """Run `sql` with every `{placeholders}` bound to the whole `values` list.

    The list is passed as one LIST parameter and unnested, which DuckDB plans
    as a hash semi-join -- far cheaper than thousands of `?` placeholders for a
    hub address. `trailing` parameters are bound after the lists, so any `?`
    they fill must appear after the last `{placeholders}`.
    """
    if not values:
        return []
    statement = sql.format(placeholders="SELECT UNNEST(?)")
    return cursor.execute(statement, [list(values)] * repeat + (trailing or [])).fetchall()


def _creator_tx(output_id: str) -> str:
    # out:<txid>:<vout> -- a txid is 64 hex characters and never contains ':'.
    return f"tx:{output_id.split(':', 2)[1]}"


def _amount(attributes_json: str | None) -> int | None:
    if not attributes_json:
        return None
    value = json.loads(attributes_json).get("amount_sats")
    return int(value) if isinstance(value, int | float) else None


def _node_rows(cursor, node_ids: list[str]) -> dict[str, tuple[str, str, dict[str, Any]]]:
    rows = _in_query(
        cursor, "SELECT node_id, node_type, label, attributes_json FROM nodes WHERE node_id IN ({placeholders})", node_ids
    )
    return {node_id: (node_type, label, json.loads(attributes or "{}")) for node_id, node_type, label, attributes in rows}


def _address_onward_counts(cursor, addresses: list[str], exclude_tx: str) -> dict[str, int]:
    """Distinct transactions touching each address, not counting `exclude_tx`."""
    if not addresses:
        return {}
    rows = _in_query(
        cursor,
        """
        WITH o AS (
            SELECT from_node AS out_id, to_node AS address FROM edges
            WHERE edge_type = 'LOCKED_TO' AND to_node IN ({placeholders})
        ), t AS (
            SELECT address, 'tx:' || split_part(out_id, ':', 2) AS tx FROM o
            UNION
            SELECT o.address, s.to_node AS tx
            FROM o JOIN edges s ON s.from_node = o.out_id AND s.edge_type = 'SPENT_BY'
        )
        SELECT address, COUNT(*) FILTER (WHERE tx <> ?) FROM t GROUP BY address
        """,
        addresses,
        [exclude_tx],
    )
    return {address: int(count) for address, count in rows}


def _tx_onward_counts(cursor, txs: list[str], exclude_address: str) -> dict[str, int]:
    """Inputs plus outputs of each transaction that are not locked to `exclude_address`."""
    if not txs:
        return {}
    rows = _in_query(
        cursor,
        """
        WITH legs AS (
            SELECT s.to_node AS tx, s.from_node AS out_id FROM edges s
            WHERE s.edge_type = 'SPENT_BY' AND s.to_node IN ({placeholders})
            UNION ALL
            SELECT c.from_node AS tx, c.to_node AS out_id FROM edges c
            WHERE c.edge_type = 'CREATES_OUTPUT' AND c.from_node IN ({placeholders})
        )
        SELECT legs.tx, COUNT(*) FILTER (WHERE l.to_node IS NULL OR l.to_node <> ?)
        FROM legs LEFT JOIN edges l ON l.from_node = legs.out_id AND l.edge_type = 'LOCKED_TO'
        GROUP BY legs.tx
        """,
        txs,
        [exclude_address],
        repeat=2,
    )
    return {tx: int(count) for tx, count in rows}


def _counterparty(node_id: str, node_type: str, label: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "label": label,
        "amount_sats": 0,
        "utxo_count": 0,
        "spent_count": 0,
        "via": [],
        "timestamp": None,
        "source_id": node_id,
        "onward_count": 0,
        "selectable": True,
        "reason": None,
    }


def _add_amount(entry: dict[str, Any], amount: int | None) -> None:
    if amount is None or entry["amount_sats"] is None:
        entry["amount_sats"] = None
    else:
        entry["amount_sats"] += amount


def _tx_flow(cursor, tx_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Addresses whose outputs this transaction spent, and addresses it paid."""
    spent = [row[0] for row in cursor.execute(
        "SELECT from_node FROM edges WHERE to_node = ? AND edge_type = 'SPENT_BY'", [tx_id]
    ).fetchall()]
    created = cursor.execute(
        "SELECT to_node, attributes_json FROM edges WHERE from_node = ? AND edge_type = 'CREATES_OUTPUT'", [tx_id]
    ).fetchall()
    created_ids = [output_id for output_id, _ in created]
    locked: dict[str, str] = {}
    spender: dict[str, str] = {}
    for from_node, to_node, edge_type in _in_query(
        cursor,
        "SELECT from_node, to_node, edge_type FROM edges "
        "WHERE from_node IN ({placeholders}) AND edge_type IN ('LOCKED_TO', 'SPENT_BY')",
        spent + created_ids,
    ):
        (locked if edge_type == "LOCKED_TO" else spender)[from_node] = to_node
    spent_amounts = {
        node_id: attributes.get("amount_sats") for node_id, (_, _, attributes) in _node_rows(cursor, spent).items()
    }

    inputs: dict[str, dict[str, Any]] = {}
    for output_id in spent:
        address = locked.get(output_id)
        key = address or output_id
        entry = inputs.get(key)
        if entry is None:
            entry = _counterparty(key, "address_or_script" if address else "output", (address or output_id).split(":", 1)[1])
            if not address:
                # No address to follow: continue upstream through the funding tx.
                entry["source_id"] = _creator_tx(output_id)
                entry["onward_count"] = 1
            inputs[key] = entry
        entry["utxo_count"] += 1
        entry["spent_count"] += 1
        _add_amount(entry, spent_amounts.get(output_id))
        if len(entry["via"]) < 5:
            entry["via"].append(_creator_tx(output_id))

    outputs: dict[str, dict[str, Any]] = {}
    for output_id, attributes_json in created:
        address = locked.get(output_id)
        key = address or output_id
        entry = outputs.get(key)
        if entry is None:
            entry = _counterparty(key, "address_or_script" if address else "output", (address or output_id).split(":", 1)[1])
            if not address:
                next_tx = spender.get(output_id)
                entry["source_id"] = next_tx
                entry["onward_count"] = 1 if next_tx else 0
                if not next_tx:
                    entry["selectable"] = False
                    entry["reason"] = _DEAD_END_UNSPENT
            outputs[key] = entry
        entry["utxo_count"] += 1
        _add_amount(entry, _amount(attributes_json))
        if output_id in spender:
            entry["spent_count"] += 1
            if len(entry["via"]) < 5:
                entry["via"].append(spender[output_id])

    return list(inputs.values()), list(outputs.values())


def _mark_address_dead_ends(cursor, entries: list[dict[str, Any]], tx_id: str) -> None:
    addresses = sorted({entry["id"] for entry in entries if entry["type"] == "address_or_script"})
    onward = _address_onward_counts(cursor, addresses, tx_id)
    for entry in entries:
        if entry["type"] != "address_or_script":
            continue
        entry["onward_count"] = onward.get(entry["id"], 0)
        if entry["onward_count"] == 0:
            entry["selectable"] = False
            entry["reason"] = _DEAD_END_ADDRESS


def _address_flow(cursor, address_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Transactions that paid this address, and transactions that spent from it."""
    rows = cursor.execute(
        """
        SELECT o.from_node, n.attributes_json, s.to_node
        FROM edges o
        JOIN nodes n ON n.node_id = o.from_node
        LEFT JOIN edges s ON s.from_node = o.from_node AND s.edge_type = 'SPENT_BY'
        WHERE o.to_node = ? AND o.edge_type = 'LOCKED_TO'
        """,
        [address_id],
    ).fetchall()
    inputs: dict[str, dict[str, Any]] = {}
    outputs: dict[str, dict[str, Any]] = {}
    for output_id, attributes_json, spending_tx in rows:
        amount = _amount(attributes_json)
        funding_tx = _creator_tx(output_id)
        entry = inputs.setdefault(funding_tx, _counterparty(funding_tx, "transaction", funding_tx.split(":", 1)[1]))
        entry["utxo_count"] += 1
        _add_amount(entry, amount)
        if len(entry["via"]) < 5:
            entry["via"].append(output_id)
        if spending_tx:
            entry["spent_count"] += 1
            spent = outputs.setdefault(
                spending_tx, _counterparty(spending_tx, "transaction", spending_tx.split(":", 1)[1])
            )
            spent["utxo_count"] += 1
            spent["spent_count"] += 1
            _add_amount(spent, amount)
            if len(spent["via"]) < 5:
                spent["via"].append(output_id)
    return list(inputs.values()), list(outputs.values())


def _mark_tx_dead_ends(cursor, entries: list[dict[str, Any]], address_id: str) -> None:
    onward = _tx_onward_counts(cursor, sorted({entry["id"] for entry in entries}), address_id)
    for entry in entries:
        entry["onward_count"] = onward.get(entry["id"], 0)
        if entry["onward_count"] == 0:
            entry["selectable"] = False
            entry["reason"] = _DEAD_END_TX


def _ranked(entries: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int]:
    ordered = sorted(entries, key=lambda item: (item["amount_sats"] is None, -(item["amount_sats"] or 0), item["id"]))
    return ordered[:limit], max(0, len(ordered) - limit)


def query_flow(*, evidence_root: Path, graph: GraphSnapshot, node: str, limit: int = 40) -> dict[str, Any]:
    """Every direct input and output of one node, as counterparties with amounts.

    A transaction's counterparties are addresses (grouped over their UTXOs); an
    address's counterparties are the transactions that paid it or spent from
    it. Each counterparty says how much activity it has *beyond* this node, so
    a dead end is reported instead of offered as the next source.
    """
    if not 1 <= limit <= FLOW_SIDE_LIMIT_MAX:
        raise GraphQueryError(f"limit must be in 1..{FLOW_SIDE_LIMIT_MAX}")
    requested = _node_id(node.strip())
    cursor = _cursor_for(_path(evidence_root, graph.storage_relative_path))
    try:
        candidates = [requested] if ":" in requested else [requested, f"address:{requested}"]
        found = None
        for candidate in candidates:
            found = cursor.execute(
                "SELECT node_id, node_type, label, attributes_json FROM nodes WHERE node_id = ?", [candidate]
            ).fetchone()
            if found:
                break
        if found is None:
            raise GraphNodeNotFound(f"{node} is not in this case's graph snapshot")
        centre_id, centre_type, label, attributes_json = found
        resolved_from = None
        if centre_type == "output":
            # An output is one coin: its flow is its address's flow, or, with no
            # address, the flow of the transaction that created it.
            locked = cursor.execute(
                "SELECT to_node FROM edges WHERE from_node = ? AND edge_type = 'LOCKED_TO' LIMIT 1", [centre_id]
            ).fetchone()
            resolved_from = centre_id
            target = locked[0] if locked else _creator_tx(centre_id)
            centre_id, centre_type, label, attributes_json = cursor.execute(
                "SELECT node_id, node_type, label, attributes_json FROM nodes WHERE node_id = ?", [target]
            ).fetchone()
        if centre_type == "transaction":
            inputs, outputs = _tx_flow(cursor, centre_id)
        elif centre_type == "address_or_script":
            inputs, outputs = _address_flow(cursor, centre_id)
        else:
            raise GraphQueryError("only transactions, addresses and outputs can be a fund-flow source")
        shown_inputs, truncated_inputs = _ranked(inputs, limit)
        shown_outputs, truncated_outputs = _ranked(outputs, limit)
        # Onward activity is only needed for counterparties that are drawn, which
        # keeps a hub address with thousands of UTXOs to one bounded lookup.
        shown = [*shown_inputs, *shown_outputs]
        if centre_type == "address_or_script":
            _mark_tx_dead_ends(cursor, shown, centre_id)
            meta = _node_rows(cursor, [entry["id"] for entry in shown])
            for entry in shown:
                attributes = meta.get(entry["id"], (None, None, {}))[2]
                entry["timestamp"] = attributes.get("block_time") or attributes.get("source_timestamp")
        else:
            _mark_address_dead_ends(cursor, shown, centre_id)
    finally:
        cursor.close()
    attributes = json.loads(attributes_json or "{}")

    def total(entries: list[dict[str, Any]]) -> int | None:
        amounts = [entry["amount_sats"] for entry in entries]
        return None if any(amount is None for amount in amounts) else sum(amounts)

    has_flow = bool(inputs or outputs)
    centre_tx_count = len({entry["id"] for entry in (*inputs, *outputs)}) if centre_type == "address_or_script" else None
    return {
        "snapshot_id": graph.snapshot_id,
        "graph_snapshot_id": graph.id,
        "center": {
            "id": centre_id,
            "type": centre_type,
            "label": label,
            "attributes": attributes,
            "timestamp": attributes.get("block_time") or attributes.get("source_timestamp"),
            "resolved_from": resolved_from,
            "transaction_count": centre_tx_count,
            "selectable": has_flow,
            "reason": None if has_flow else "No committed transaction pays into or spends from this node in the snapshot.",
        },
        "inputs": shown_inputs,
        "outputs": shown_outputs,
        "totals": {
            "input_count": len(inputs),
            "output_count": len(outputs),
            "input_sats": total(inputs),
            "output_sats": total(outputs),
            # Counted over the counterparties shown (see `limit`).
            "dead_end_inputs": sum(1 for entry in shown_inputs if not entry["selectable"]),
            "dead_end_outputs": sum(1 for entry in shown_outputs if not entry["selectable"]),
        },
        "truncated_inputs": truncated_inputs,
        "truncated_outputs": truncated_outputs,
        "coverage": graph.coverage,
    }
