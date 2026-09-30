"""Receipt-approved UTXO graph snapshot builder. It never allocates inputs to outputs."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import append_event
from app.models import FragmentReceipt, GraphSnapshot, Snapshot


@dataclass(frozen=True)
class GraphBuildResult:
    graph_snapshot_id: str
    node_count: int
    edge_count: int
    coverage: dict[str, Any]


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _receipt_path(evidence_root: Path, relative_path: str) -> Path:
    root = evidence_root.resolve()
    path = (root / relative_path).resolve()
    if root not in path.parents:
        raise RuntimeError("fragment receipt escaped evidence root")
    return path


def _facts(session: Session, evidence_root: Path, snapshot_id: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {
        "transactions": [],
        "inputs": [],
        "outputs": [],
        "network_observations": [],
    }
    receipts = session.scalars(select(FragmentReceipt).where(FragmentReceipt.snapshot_id == snapshot_id))
    for receipt in receipts:
        if receipt.record_type not in grouped:
            continue
        table = pq.read_table(_receipt_path(evidence_root, receipt.storage_relative_path), columns=["canonical_json"])
        grouped[receipt.record_type].extend(json.loads(value) for value in table.column("canonical_json").to_pylist())
    return grouped


def _safe_name(value: str) -> str:
    return value.replace("-", "").replace("/", "")


def build_graph_snapshot(session: Session, *, evidence_root: Path, snapshot: Snapshot) -> GraphBuildResult:
    """Build one immutable DuckDB graph from committed fragments for a completed snapshot."""
    existing = session.scalar(select(GraphSnapshot).where(GraphSnapshot.snapshot_id == snapshot.id))
    if existing:
        return GraphBuildResult(existing.id, existing.node_count, existing.edge_count, existing.coverage)
    records = _facts(session, evidence_root, snapshot.id)
    root = evidence_root.resolve()
    relative = Path(snapshot.case_id) / "graphs" / f"snapshot-{_safe_name(snapshot.id)}.duckdb"
    target = root / relative
    staging = target.parent / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    temporary = staging / f"{target.name}.{uuid.uuid4().hex}.part"
    nodes: dict[str, tuple[str, str, dict[str, Any]]] = {}
    edges: dict[str, tuple[str, str, str, dict[str, Any], float | None]] = {}

    def node(node_id: str, node_type: str, label: str, attributes: dict[str, Any] | None = None) -> None:
        nodes.setdefault(node_id, (node_type, label, attributes or {}))

    def edge(
        from_id: str,
        to_id: str,
        edge_type: str,
        attributes: dict[str, Any] | None = None,
        uncertainty: float | None = None,
    ) -> None:
        edge_id = hashlib.sha256(f"{from_id}|{to_id}|{edge_type}".encode()).hexdigest()
        edges.setdefault(edge_id, (from_id, to_id, edge_type, attributes or {}, uncertainty))

    outputs: dict[tuple[str, int], dict[str, Any]] = {}
    for transaction in records["transactions"]:
        txid = transaction["txid"]
        node(
            f"tx:{txid}",
            "transaction",
            txid,
            {
                "network": transaction.get("network"),
                "block_time": transaction.get("block_time"),
                # block_time is null for unconfirmed/regtest data -- source_timestamp
                # (the ingested row's own timestamp) is real and always present, and is
                # what the peeling-chain detector already falls back to for its own
                # elapsed-time math. Surfacing it here is what the graph/UI needs to
                # stop showing a hardcoded placeholder for "when was this tx observed".
                "source_timestamp": transaction.get("source_timestamp"),
            },
        )
    for output in records["outputs"]:
        txid, vout = output["txid"], output["vout"]
        output_id = f"out:{txid}:{vout}"
        outputs[(txid, vout)] = output
        node(f"tx:{txid}", "transaction", txid)
        node(
            output_id,
            "output",
            f"{txid}:{vout}",
            {"amount_sats": output["amount_sats"], "script_type": output.get("script_type")},
        )
        edge(f"tx:{txid}", output_id, "CREATES_OUTPUT", {"vout": vout, "amount_sats": output["amount_sats"]})
        address = output.get("address") or output.get("script_id")
        if address:
            address_id = f"address:{address}"
            node(address_id, "address_or_script", str(address))
            edge(output_id, address_id, "LOCKED_TO")

    resolved_inputs = 0
    missing_outpoint_inputs = 0
    unknown_prior_output_inputs = 0
    double_spend_conflicts = 0
    spenders: dict[tuple[str, int], str] = {}
    known_value_checks = 0
    value_violations = 0
    inputs_by_tx: dict[str, list[dict[str, Any]]] = {}
    for tx_input in records["inputs"]:
        inputs_by_tx.setdefault(tx_input["txid"], []).append(tx_input)
        previous_txid, previous_vout = tx_input.get("prev_txid"), tx_input.get("prev_vout")
        if previous_txid is None or previous_vout is None:
            missing_outpoint_inputs += 1
            continue
        key = (previous_txid, previous_vout)
        output = outputs.get(key)
        if output is None:
            unknown_prior_output_inputs += 1
            continue
        spender = tx_input["txid"]
        if key in spenders and spenders[key] != spender:
            double_spend_conflicts += 1
            continue
        spenders[key] = spender
        resolved_inputs += 1
        edge(f"out:{previous_txid}:{previous_vout}", f"tx:{spender}", "SPENT_BY", {"vin": tx_input["vin"]})

    outputs_by_tx: dict[str, list[dict[str, Any]]] = {}
    for output in records["outputs"]:
        outputs_by_tx.setdefault(output["txid"], []).append(output)
    for txid, tx_inputs in inputs_by_tx.items():
        if all(
            item.get("prev_txid") is not None and (item["prev_txid"], item["prev_vout"]) in outputs
            for item in tx_inputs
        ):
            known_value_checks += 1
            input_value = sum(outputs[(item["prev_txid"], item["prev_vout"])]["amount_sats"] for item in tx_inputs)
            output_value = sum(item["amount_sats"] for item in outputs_by_tx.get(txid, []))
            if input_value < output_value:
                value_violations += 1

    for observation in records["network_observations"]:
        observation_id = f"obs:{observation['observation_id']}"
        node(
            observation_id,
            "network_observation",
            observation["observation_id"],
            {"clock_quality": observation.get("clock_quality")},
        )
        if observation.get("txid"):
            node(f"tx:{observation['txid']}", "transaction", observation["txid"])
            edge(observation_id, f"tx:{observation['txid']}", "OBSERVED_TX")
        for direction, address, port in (
            ("SEEN_FROM", observation.get("src_ip"), observation.get("src_port")),
            ("SEEN_TO", observation.get("dst_ip"), observation.get("dst_port")),
        ):
            if address:
                endpoint_id = f"endpoint:{address}:{port if port is not None else 'unknown'}"
                node(endpoint_id, "network_endpoint", f"{address}:{port if port is not None else '?'}")
                edge(observation_id, endpoint_id, direction)

    coverage = {
        "input_count": len(records["inputs"]),
        "resolved_spend_inputs": resolved_inputs,
        "missing_outpoint_inputs": missing_outpoint_inputs,
        "unknown_prior_output_inputs": unknown_prior_output_inputs,
        "double_spend_conflicts": double_spend_conflicts,
        "value_checks_with_complete_prevouts": known_value_checks,
        "value_violations": value_violations,
        "spend_lineage_complete": missing_outpoint_inputs == 0
        and unknown_prior_output_inputs == 0
        and double_spend_conflicts == 0,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(temporary))
    try:
        connection.execute(
            "CREATE TABLE nodes (node_id VARCHAR PRIMARY KEY, node_type VARCHAR, label VARCHAR, attributes_json VARCHAR)"
        )
        connection.execute(
            "CREATE TABLE edges (edge_id VARCHAR PRIMARY KEY, from_node VARCHAR, to_node VARCHAR, edge_type VARCHAR, attributes_json VARCHAR, uncertainty DOUBLE)"
        )
        if nodes:
            connection.register(
                "node_batch",
                pa.Table.from_pylist(
                    [
                        {
                            "node_id": node_id,
                            "node_type": kind,
                            "label": label,
                            "attributes_json": json.dumps(attrs, sort_keys=True),
                        }
                        for node_id, (kind, label, attrs) in nodes.items()
                    ]
                ),
            )
            connection.execute("INSERT INTO nodes SELECT * FROM node_batch")
            connection.unregister("node_batch")
        if edges:
            connection.register(
                "edge_batch",
                pa.Table.from_pylist(
                    [
                        {
                            "edge_id": edge_id,
                            "from_node": from_node,
                            "to_node": to_node,
                            "edge_type": edge_type,
                            "attributes_json": json.dumps(attrs, sort_keys=True),
                            "uncertainty": uncertainty,
                        }
                        for edge_id, (from_node, to_node, edge_type, attrs, uncertainty) in edges.items()
                    ]
                ),
            )
            connection.execute("INSERT INTO edges SELECT * FROM edge_batch")
            connection.unregister("edge_batch")
        connection.execute("CREATE INDEX nodes_node_id_idx ON nodes(node_id)")
        connection.execute("CREATE INDEX edges_from_idx ON edges(from_node)")
        connection.execute("CREATE INDEX edges_to_idx ON edges(to_node)")
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    digest = _hash(temporary)
    if target.exists():
        if _hash(target) != digest:
            raise RuntimeError("immutable graph snapshot conflict")
        temporary.unlink()
    else:
        os.replace(temporary, target)
    graph = GraphSnapshot(
        case_id=snapshot.case_id,
        snapshot_id=snapshot.id,
        storage_relative_path=relative.as_posix(),
        sha256=digest,
        node_count=len(nodes),
        edge_count=len(edges),
        coverage=coverage,
        state="complete",
    )
    session.add(graph)
    session.flush()
    append_event(
        session,
        case_id=snapshot.case_id,
        event_type="graph.delta_ready",
        stage="graph_ready",
        payload={
            "snapshot_id": snapshot.id,
            "graph_snapshot_id": graph.id,
            "nodes": len(nodes),
            "edges": len(edges),
            "coverage": coverage,
        },
    )
    return GraphBuildResult(graph.id, len(nodes), len(edges), coverage)
