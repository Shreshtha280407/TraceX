"""Transparent Phase 4 motifs over receipt-approved snapshots; no model inference."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.graph.builder import _facts
from app.events import append_event
from app.models import FindingRecord, GraphSnapshot, Snapshot

RULE_VERSION = "deterministic-v1"
WINDOW_SECONDS = (15 * 60, 60 * 60, 24 * 60 * 60)
COLLECTION_MIN_SOURCE_TRANSACTIONS = 3
HUB_MIN_SOURCE_TRANSACTIONS = 4
RAPID_MIN_INBOUND_TRANSACTIONS = 3
RAPID_MAX_DELAY_SECONDS = 60 * 60


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _window(value: datetime, seconds: int) -> tuple[datetime, datetime]:
    start = datetime.fromtimestamp(int(value.timestamp()) // seconds * seconds, tz=UTC)
    return start, start + timedelta(seconds=seconds)


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _dedupe_refs(facts: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for fact in facts:
        for reference in fact.get("source_refs", []):
            key = json.dumps(reference, sort_keys=True)
            if key not in seen:
                seen.add(key)
                result.append(reference)
    return result


def _coverage(graph: GraphSnapshot, transactions: dict[str, dict]) -> dict:
    graph_coverage = graph.coverage or {}
    notes = []
    if graph_coverage.get("missing_outpoint_inputs"):
        notes.append("Some source inputs lack prev_txid/prev_vout; spend lineage is partial.")
    if graph_coverage.get("unknown_prior_output_inputs"):
        notes.append("Some supplied outpoints refer to outputs outside committed coverage.")
    if graph_coverage.get("double_spend_conflicts"):
        notes.append("Conflicting spenders were excluded from accepted spend edges.")
    timed = sum(bool(_time(fact.get("block_time") or fact.get("source_timestamp"))) for fact in transactions.values())
    block_timed = sum(bool(fact.get("block_time")) for fact in transactions.values())
    if timed != len(transactions):
        notes.append("Transactions without a usable source or block timestamp are excluded from time-window features.")
    notes.append("Windows are bounded by the committed snapshot; activity outside supplied evidence is unknown.")
    return {
        "complete": bool(graph_coverage.get("spend_lineage_complete")),
        "spend_lineage_complete": bool(graph_coverage.get("spend_lineage_complete")),
        "time_coverage": {
            "transactions_total": len(transactions),
            "transactions_with_usable_time": timed,
            "transactions_with_block_time": block_timed,
            "source_time_used_when_block_time_missing": timed - block_timed,
        },
        "window_boundary_incomplete": True,
        "notes": notes,
        "graph": graph_coverage,
    }


def _value_distribution(values: list[int]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "min_sats": ordered[0],
        "max_sats": ordered[-1],
        "median_sats": median(ordered),
        "total_sats": sum(ordered),
    }


def _inter_event_gaps(times: list[datetime]) -> dict[str, float | int | None]:
    ordered = sorted(set(times))
    gaps = [(later - earlier).total_seconds() for earlier, later in pairwise(ordered)]
    return {
        "count": len(gaps),
        "minimum_seconds": min(gaps) if gaps else None,
        "maximum_seconds": max(gaps) if gaps else None,
        "median_seconds": median(gaps) if gaps else None,
    }


def _window_features(
    *,
    address: str,
    seconds: int,
    outputs: list[dict],
    transaction_times: dict[str, datetime | None],
    rapid_events: list[tuple[dict, dict, float]],
    outputs_by_tx: dict[str, list[dict]],
) -> dict[str, Any]:
    inbound_txids = {output["txid"] for output in outputs}
    inbound_times = [transaction_times[txid] for txid in inbound_txids if transaction_times.get(txid)]
    outgoing_txids = {tx_input["txid"] for tx_input, _, _ in rapid_events}
    outgoing_outputs = [output for txid in outgoing_txids for output in outputs_by_tx.get(txid, [])]
    component_nodes = {
        f"address:{address}",
        *(f"tx:{txid}" for txid in inbound_txids | outgoing_txids),
        *(f"out:{output['txid']}:{output['vout']}" for output in outputs),
    }
    # This is a one-hop, in-window delta only; it is deliberately not a wallet/entity component claim.
    component_edge_delta = 2 * len(outputs) + len(rapid_events)
    return {
        "feature_contract_version": RULE_VERSION,
        "window_seconds": seconds,
        "in_event_count": len(inbound_txids),
        "out_event_count": len(outgoing_txids),
        "received_output_count": len(outputs),
        "outgoing_output_count": len(outgoing_outputs),
        "observed_counterparties": len(inbound_txids),
        "received_value_distribution_sats": _value_distribution([output["amount_sats"] for output in outputs]),
        "outgoing_value_distribution_sats": _value_distribution([output["amount_sats"] for output in outgoing_outputs])
        if outgoing_outputs
        else None,
        "inter_event_gaps_seconds": _inter_event_gaps(inbound_times),
        "bounded_component_change": {
            "scope": "one_hop_address_window",
            "node_delta": len(component_nodes),
            "edge_delta": component_edge_delta,
            "resolved_spend_edge_delta": len(rapid_events),
        },
    }


def materialize_findings(session: Session, *, evidence_root, snapshot: Snapshot, graph: GraphSnapshot) -> int:
    """Store deterministic address/window observations exactly once for a snapshot."""
    if session.scalar(select(FindingRecord.id).where(FindingRecord.snapshot_id == snapshot.id).limit(1)):
        return 0
    records = _facts(session, evidence_root, snapshot.id)
    transactions = {fact["txid"]: fact for fact in records["transactions"]}
    transaction_times = {
        txid: _time(fact.get("block_time") or fact.get("source_timestamp")) for txid, fact in transactions.items()
    }
    outputs_by_outpoint = {(fact["txid"], fact["vout"]): fact for fact in records["outputs"]}
    outputs_by_tx: dict[str, list[dict]] = defaultdict(list)
    for output in records["outputs"]:
        outputs_by_tx[output["txid"]].append(output)
    output_events: dict[tuple[str, int, datetime], list[dict]] = defaultdict(list)
    for output in records["outputs"]:
        address = output.get("address") or output.get("script_id")
        observed = transaction_times.get(output["txid"])
        if not address or not observed:
            continue
        for seconds in WINDOW_SECONDS:
            start, _ = _window(observed, seconds)
            output_events[(str(address), seconds, start)].append(output)
    rapid_events: dict[tuple[str, int, datetime], list[tuple[dict, dict, float]]] = defaultdict(list)
    for tx_input in records["inputs"]:
        if tx_input.get("prev_txid") is None or tx_input.get("prev_vout") is None:
            continue
        previous = outputs_by_outpoint.get((tx_input["prev_txid"], tx_input["prev_vout"]))
        spent_at = transaction_times.get(tx_input["txid"])
        received_at = transaction_times.get(tx_input["prev_txid"])
        address = previous.get("address") if previous else None
        if not previous or not address or not spent_at or not received_at:
            continue
        delay = (spent_at - received_at).total_seconds()
        if 0 <= delay <= 3600:
            for seconds in WINDOW_SECONDS:
                start, _ = _window(spent_at, seconds)
                rapid_events[(str(address), seconds, start)].append((tx_input, previous, delay))
    candidates: list[dict[str, Any]] = []
    coverage = _coverage(graph, transactions)
    for (address, seconds, start), outputs in output_events.items():
        distinct_transactions = {output["txid"] for output in outputs}
        rapid = rapid_events.get((address, seconds, start), [])
        feature = _window_features(
            address=address,
            seconds=seconds,
            outputs=outputs,
            transaction_times=transaction_times,
            rapid_events=rapid,
            outputs_by_tx=outputs_by_tx,
        )
        feature["rule_thresholds"] = {
            "concentrated_collection_min_source_transactions": COLLECTION_MIN_SOURCE_TRANSACTIONS,
            "emerging_hub_min_source_transactions": HUB_MIN_SOURCE_TRANSACTIONS,
            "rapid_redistribution_min_inbound_transactions": RAPID_MIN_INBOUND_TRANSACTIONS,
            "rapid_redistribution_max_delay_seconds": RAPID_MAX_DELAY_SECONDS,
        }
        if len(distinct_transactions) >= COLLECTION_MIN_SOURCE_TRANSACTIONS:
            candidates.append(
                {
                    "entity_ref": f"address:{address}",
                    "start": start,
                    "end": start + timedelta(seconds=seconds),
                    "rule_id": "concentrated_collection",
                    "score": float(min(100, 25 + 15 * len(distinct_transactions))),
                    "feature": feature,
                    "explanations": [
                        f"{len(outputs)} outputs from {len(distinct_transactions)} transactions reached this address in the {seconds}-second window."
                    ],
                    "alternatives": [
                        "Exchange deposit aggregation or a normal merchant collection flow can produce this pattern."
                    ],
                    "facts": outputs,
                }
            )
        if len(distinct_transactions) >= HUB_MIN_SOURCE_TRANSACTIONS:
            candidates.append(
                {
                    "entity_ref": f"address:{address}",
                    "start": start,
                    "end": start + timedelta(seconds=seconds),
                    "rule_id": "emerging_hub",
                    "score": float(min(100, 20 + 10 * len(distinct_transactions))),
                    "feature": feature,
                    "explanations": [
                        f"Address activity reached {len(distinct_transactions)} distinct source transactions in a bounded window."
                    ],
                    "alternatives": [
                        "A newly active exchange service, fundraiser, or payment processor can be a benign hub."
                    ],
                    "facts": outputs,
                }
            )
    for (address, seconds, start), events in rapid_events.items():
        inbound = output_events.get((address, seconds, start), [])
        inbound_transactions = {output["txid"] for output in inbound}
        if len(inbound_transactions) < RAPID_MIN_INBOUND_TRANSACTIONS:
            continue
        shortest_delay = min(item[2] for item in events)
        feature = _window_features(
            address=address,
            seconds=seconds,
            outputs=inbound,
            transaction_times=transaction_times,
            rapid_events=events,
            outputs_by_tx=outputs_by_tx,
        )
        feature.update(
            {
                "rule_thresholds": {
                    "rapid_redistribution_min_inbound_transactions": RAPID_MIN_INBOUND_TRANSACTIONS,
                    "rapid_redistribution_max_delay_seconds": RAPID_MAX_DELAY_SECONDS,
                },
                "verified_rapid_spend_count": len(events),
                "shortest_verified_delay_seconds": shortest_delay,
                "median_verified_delay_seconds": median(item[2] for item in events),
                "verified_prevout_links": len(events),
            }
        )
        candidates.append(
            {
                "entity_ref": f"address:{address}",
                "start": start,
                "end": start + timedelta(seconds=seconds),
                "rule_id": "rapid_redistribution",
                "score": float(min(100, 45 + 10 * len(events))),
                "feature": feature,
                "explanations": [
                    f"{len(events)} committed prevout(s) associated with this address were spent again within one hour; shortest delay was {shortest_delay:.0f} seconds."
                ],
                "alternatives": [
                    "Automated treasury sweeping, exchange hot-wallet operations, or change handling can be benign explanations."
                ],
                "facts": [fact for event in events for fact in event[:2]],
            }
        )
    candidates.sort(key=lambda candidate: (-candidate["score"], candidate["rule_id"], candidate["entity_ref"]))
    for rank, candidate in enumerate(candidates, 1):
        feature_hash = _hash(candidate["feature"])
        session.add(
            FindingRecord(
                case_id=snapshot.case_id,
                snapshot_id=snapshot.id,
                graph_snapshot_id=graph.id,
                entity_ref=candidate["entity_ref"],
                window_start=candidate["start"],
                window_end=candidate["end"],
                rule_id=candidate["rule_id"],
                rule_version=RULE_VERSION,
                claim=(
                    f"Observed {candidate['rule_id']} pattern for {candidate['entity_ref']} in a committed "
                    f"{candidate['feature']['window_seconds']}-second window; prioritize it for reviewer assessment."
                ),
                raw_score=candidate["score"],
                rank=rank,
                coverage=coverage,
                feature_vector=candidate["feature"],
                feature_vector_hash=feature_hash,
                explanations=candidate["explanations"],
                benign_alternatives=candidate["alternatives"],
                opposing_evidence=[
                    {
                        "kind": "coverage_limitation",
                        "statement": (
                            "This committed snapshot contains no ownership attribution or independently "
                            "verified benign context; the pattern alone cannot establish either."
                        ),
                        "source_refs": [],
                    }
                ],
                source_refs=_dedupe_refs(candidate["facts"]),
                status="open",
            )
        )
    if candidates:
        append_event(
            session,
            case_id=snapshot.case_id,
            event_type="finding.updated",
            stage="findings_ready",
            payload={
                "snapshot_id": snapshot.id,
                "finding_count": len(candidates),
                "method": RULE_VERSION,
                "ml_enabled": False,
            },
        )
    return len(candidates)
