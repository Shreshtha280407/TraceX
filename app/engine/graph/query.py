"""Bounded, parameterized DuckDB neighbourhood queries."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import duckdb

from app.models import GraphSnapshot


class GraphQueryError(ValueError):
    pass


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


def _cursor(seed: str, next_depth: int) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps({"seed": seed, "next_depth": next_depth}, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def query_neighbourhood(
    *, evidence_root: Path, graph: GraphSnapshot, seed: str, depth: int, node_limit: int, edge_limit: int
) -> dict[str, Any]:
    if not 1 <= depth <= 5:
        raise GraphQueryError("depth must be in 1..5")
    if not 1 <= node_limit <= 1000 or not 1 <= edge_limit <= 3000:
        raise GraphQueryError("limits exceed graph safety caps")
    source = _node_id(seed)
    connection = duckdb.connect(str(_path(evidence_root, graph.storage_relative_path)), read_only=True)
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
        selected_nodes = {source}
        selected_edges: dict[str, tuple] = {}
        frontier = {source}
        truncated_nodes = 0
        truncated_edges = 0
        for _ in range(depth):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            values = list(frontier)
            candidates = connection.execute(
                f"SELECT edge_id, from_node, to_node, edge_type, attributes_json, uncertainty FROM edges WHERE from_node IN ({placeholders}) OR to_node IN ({placeholders})",
                values + values,
            ).fetchall()
            next_frontier: set[str] = set()
            for row in candidates:
                edge_id, from_node, to_node, *_ = row
                if edge_id in selected_edges:
                    continue
                if len(selected_edges) >= edge_limit:
                    truncated_edges += 1
                    continue
                new_nodes = {from_node, to_node}.difference(selected_nodes)
                allowed = max(0, node_limit - len(selected_nodes))
                if len(new_nodes) > allowed:
                    truncated_nodes += len(new_nodes)
                    continue
                selected_edges[edge_id] = row
                selected_nodes.update(new_nodes)
                next_frontier.update(new_nodes)
            frontier = next_frontier
        placeholders = ",".join("?" for _ in selected_nodes)
        node_rows = connection.execute(
            f"SELECT node_id, node_type, label, attributes_json FROM nodes WHERE node_id IN ({placeholders})",
            list(selected_nodes),
        ).fetchall()
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
            for edge_id, from_node, to_node, edge_type, attributes, uncertainty in selected_edges.values()
        ],
        "truncated_nodes": truncated_nodes,
        "truncated_edges": truncated_edges,
        "cursor": _cursor(seed, depth + 1) if (truncated_nodes or truncated_edges) and depth < 5 else None,
    }
