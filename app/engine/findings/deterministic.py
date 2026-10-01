"""Transparent Phase 4 motifs over receipt-approved snapshots; no model inference."""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Hashable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import bulk_insert_serialized
from app.engine.graph.builder import FactRecords, bulk_allocation, load_facts
from app.engine.motifs.deterministic import (
    detect_coinjoin_like_transactions,
    detect_peeling_chains,
    propagate_synthetic_review_seeds,
)
from app.events import append_event
from app.models import FeatureRecord, FindingRecord, GraphSnapshot, Snapshot, SyntheticReviewSeed
from app.resources import current_plan

RULE_VERSION = "deterministic-v1"
FEATURE_SCHEMA_VERSION = "phase4.1-feature-v1"
WINDOW_SECONDS = (15 * 60, 60 * 60, 24 * 60 * 60)
COLLECTION_MIN_SOURCE_TRANSACTIONS = 3
HUB_MIN_SOURCE_TRANSACTIONS = 4
RAPID_MIN_INBOUND_TRANSACTIONS = 3
RAPID_MAX_DELAY_SECONDS = 60 * 60


def _phase41_defaults() -> dict[str, Any]:
    """All frozen Phase 4.1 fields appear on every address-window row."""
    return {
        "peeling_chain_score": 0.0,
        "peeling_chain_length": 0,
        "peeling_chain_total_duration_sec": 0.0,
        "peeling_chain_evidence_count": 0,
        "coinjoin_like_score": 0.0,
        "equal_output_count": 0,
        "equal_output_value_sats": None,
        "coinjoin_like_evidence_count": 0,
        "risk_propagation_score": 0.0,
        "risk_seed_distance": None,
        "risk_path_evidence_count": 0,
        "risk_seed_count": 0,
    }


def _evidence_count(signal: dict[str, Any]) -> int:
    # Compact signal summaries (bounded path) carry the count, not the refs.
    return signal["evidence_count"] if "evidence_count" in signal else len(signal["evidence_refs"])


def signal_summary(signal: dict[str, Any]) -> dict[str, Any]:
    """Only what _signal_feature reads: what a window's feature vector needs from a detector signal."""
    keys = ("finding_type", "score", "hop_count", "total_duration_sec", "equal_output_count",
            "equal_output_value_sats", "risk_seed_distance", "risk_seed_count")
    return {**{key: signal[key] for key in keys if key in signal}, "evidence_count": len(signal["evidence_refs"])}


def _signal_feature(signal: dict[str, Any]) -> dict[str, Any]:
    """Translate a detector result into the eight frozen flat features."""
    values = _phase41_defaults()
    if signal["finding_type"] == "peeling_chain_candidate":
        values.update(
            {
                "peeling_chain_score": signal["score"],
                "peeling_chain_length": signal["hop_count"],
                "peeling_chain_total_duration_sec": signal["total_duration_sec"],
                "peeling_chain_evidence_count": _evidence_count(signal),
            }
        )
    elif signal["finding_type"] == "coinjoin_like_structure":
        values.update(
            {
                "coinjoin_like_score": signal["score"],
                "equal_output_count": signal["equal_output_count"],
                "equal_output_value_sats": signal["equal_output_value_sats"],
                "coinjoin_like_evidence_count": _evidence_count(signal),
            }
        )
    elif signal["finding_type"] == "synthetic_seed_proximity":
        values.update(
            {
                "risk_propagation_score": signal["score"],
                "risk_seed_distance": signal["risk_seed_distance"],
                "risk_path_evidence_count": _evidence_count(signal),
                "risk_seed_count": signal["risk_seed_count"],
            }
        )
    return values


def _merge_signal_features(current: dict[str, Any], signal: dict[str, Any]) -> None:
    """Use the strongest transparent result per detector family for a row."""
    incoming = _signal_feature(signal)
    if incoming["peeling_chain_score"] >= current["peeling_chain_score"]:
        for key in ("peeling_chain_score", "peeling_chain_length", "peeling_chain_total_duration_sec", "peeling_chain_evidence_count"):
            current[key] = incoming[key]
    if incoming["coinjoin_like_score"] >= current["coinjoin_like_score"]:
        for key in ("coinjoin_like_score", "equal_output_count", "equal_output_value_sats", "coinjoin_like_evidence_count"):
            current[key] = incoming[key]
    if incoming["risk_propagation_score"] >= current["risk_propagation_score"]:
        for key in ("risk_propagation_score", "risk_seed_distance", "risk_path_evidence_count", "risk_seed_count"):
            current[key] = incoming[key]


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


def _ref_key(reference: dict) -> Hashable:
    # Source refs are flat dicts of scalars, so a sorted item tuple identifies
    # one exactly; serializing each to JSON just to compare it cost ~15 s per
    # 100K-row snapshot. Anything unhashable falls back to the JSON form.
    key = tuple(sorted(reference.items()))
    try:
        hash(key)
    except TypeError:
        return json.dumps(reference, sort_keys=True)
    return key


def _dedupe_refs(facts: list[dict]) -> list[dict]:
    seen: set[Hashable] = set()
    result = []
    for fact in facts:
        for reference in fact.get("source_refs", []):
            key = _ref_key(reference)
            if key not in seen:
                seen.add(key)
                result.append(reference)
    return result


def _coverage(graph: GraphSnapshot, transactions: dict[str, dict]) -> dict:
    timed = sum(bool(_time(fact.get("block_time") or fact.get("source_timestamp"))) for fact in transactions.values())
    block_timed = sum(bool(fact.get("block_time")) for fact in transactions.values())
    return coverage_from_counts(graph, total=len(transactions), timed=timed, block_timed=block_timed)


def coverage_from_counts(graph: GraphSnapshot, *, total: int, timed: int, block_timed: int) -> dict:
    graph_coverage = graph.coverage or {}
    notes = []
    if graph_coverage.get("missing_outpoint_inputs"):
        notes.append("Some source inputs lack prev_txid/prev_vout; spend lineage is partial.")
    if graph_coverage.get("unknown_prior_output_inputs"):
        notes.append("Some supplied outpoints refer to outputs outside committed coverage.")
    if graph_coverage.get("double_spend_conflicts"):
        notes.append("Conflicting spenders were excluded from accepted spend edges.")
    if timed != total:
        notes.append("Transactions without a usable source or block timestamp are excluded from time-window features.")
    notes.append("Windows are bounded by the committed snapshot; activity outside supplied evidence is unknown.")
    return {
        "complete": bool(graph_coverage.get("spend_lineage_complete")),
        "spend_lineage_complete": bool(graph_coverage.get("spend_lineage_complete")),
        "time_coverage": {
            "transactions_total": total,
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


def _history_features(
    output_events: dict[tuple[str, int, datetime], list[dict]],
) -> dict[tuple[str, int, datetime], dict[str, Any]]:
    """Per-address, per-window-size baseline/surge features computed only from that
    address's own strictly-earlier observed windows in this committed snapshot.

    Never claims "dormancy" — the snapshot has no visibility before its own committed
    coverage, so a window with no earlier observed activity is `first_observed_activity`,
    not an assertion the address was previously inactive.
    """
    grouped: dict[tuple[str, int], list[datetime]] = defaultdict(list)
    for address, seconds, start in output_events:
        grouped[(address, seconds)].append(start)
    history: dict[tuple[str, int, datetime], dict[str, Any]] = {}
    for (address, seconds), starts in grouped.items():
        ordered = sorted(set(starts))
        running_count_total = 0
        running_value_total = 0
        for index, start in enumerate(ordered):
            key = (address, seconds, start)
            current_events = len({output["txid"] for output in output_events[key]})
            current_value = sum(output["amount_sats"] for output in output_events[key])
            if index == 0:
                history[key] = {
                    "first_observed_activity": True,
                    "prior_window_gap_seconds": None,
                    "baseline_in_event_count_mean": None,
                    "activity_surge_ratio": None,
                    "baseline_value_sats_mean": None,
                    "value_surge_ratio": None,
                }
            else:
                baseline_count = running_count_total / index
                baseline_value = running_value_total / index
                history[key] = {
                    "first_observed_activity": False,
                    "prior_window_gap_seconds": (start - ordered[index - 1]).total_seconds(),
                    "baseline_in_event_count_mean": baseline_count,
                    "activity_surge_ratio": (current_events / baseline_count) if baseline_count else None,
                    "baseline_value_sats_mean": baseline_value,
                    "value_surge_ratio": (current_value / baseline_value) if baseline_value else None,
                }
            running_count_total += current_events
            running_value_total += current_value
    return history


def _window_features(
    *,
    address: str,
    seconds: int,
    outputs: list[dict],
    transaction_times: dict[str, datetime | None],
    rapid_events: list[tuple[dict, dict, float]],
    outputs_by_tx: dict[str, list[dict]],
    inbound_txids: set[str] | None = None,
) -> dict[str, Any]:
    if inbound_txids is None:
        inbound_txids = {output["txid"] for output in outputs}
    inbound_times = [transaction_times[txid] for txid in inbound_txids if transaction_times.get(txid)]
    outgoing_txids = {tx_input["txid"] for tx_input, _, _ in rapid_events}
    outgoing_outputs = [output for txid in outgoing_txids for output in outputs_by_tx.get(txid, [])]
    # Only the size of the one-hop node set is reported: the address itself, its
    # distinct transactions and its distinct received outputs. The three kinds
    # carry different id prefixes in the graph, so they never collide and are
    # counted directly instead of formatting every id just to measure a set.
    component_node_count = (
        1 + len(inbound_txids | outgoing_txids) + len({(output["txid"], output["vout"]) for output in outputs})
    )
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
            "node_delta": component_node_count,
            "edge_delta": component_edge_delta,
            "resolved_spend_edge_delta": len(rapid_events),
        },
    }


_FEATURE_JSON_COLUMNS = frozenset({"feature_vector", "coverage", "source_refs"})
_FINDING_JSON_COLUMNS = frozenset(
    {"coverage", "feature_vector", "explanations", "benign_alternatives", "opposing_evidence", "source_refs"}
)


_WINDOW_LABEL = {15 * 60: "15-minute", 60 * 60: "1-hour", 24 * 60 * 60: "24-hour"}
_PATTERN_LIST_CAP = 500
_PATTERN_CHAIN_CAP = 25


def _group_peeling_patterns(
    chains: list[dict[str, Any]], transaction_times: dict[str, datetime | None]
) -> list[dict[str, Any]]:
    """Merge overlapping peeling-chain candidates into one reviewable pattern.

    The detector starts a walk at every spent outpoint, so a 7-hop chain also
    yields its 6-, 5-, 4- and 3-hop tails, and chains that converge on a shared
    transaction come out separately -- the same transactions then appeared in
    up to dozens of findings. Chains are connected when they share a
    transaction, or would be keyed to the same finding (same origin address and
    window). Each connected group becomes one finding: the longest chain is its
    readable path, its score is the strongest member's, and its graph path,
    transactions and evidence are the union of every member, so nothing a
    member showed is lost. Detector output and the feature vectors built from
    it are unchanged; only the review queue is grouped.
    """
    if not chains:
        return []
    parent = list(range(len(chains)))

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
    for index, chain in enumerate(chains):
        observed = transaction_times.get(chain["transaction_ids"][0])
        key = (chain["entity_ref"], _window(observed, WINDOW_SECONDS[0])[0] if observed else None)
        union(index, owner_by_key.setdefault(key, index))
        for txid in chain["transaction_ids"]:
            union(index, owner_by_tx.setdefault(txid, index))
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, chain in enumerate(chains):
        groups[find(index)].append(chain)

    patterns: list[dict[str, Any]] = []
    for members in groups.values():
        members.sort(key=lambda item: (-item["hop_count"], -item["score"], item["transaction_ids"]))
        primary = members[0]
        transaction_ids: dict[str, None] = {}
        nodes: dict[str, None] = {}
        edges: dict[str, None] = {}
        addresses: dict[str, None] = {}
        references: list[dict] = []
        seen_refs: set[Hashable] = set()
        for member in members:
            transaction_ids.update(dict.fromkeys(member["transaction_ids"]))
            nodes.update(dict.fromkeys(member["graph_path"]["nodes"]))
            edges.update(dict.fromkeys(member["graph_path"]["edge_ids"]))
            for step in member["steps"]:
                for address in (step.get("previous_address"), step.get("continuing_address")):
                    if address:
                        addresses[f"address:{address}"] = None
            for reference in member["evidence_refs"]:
                ref_key = _ref_key(reference)
                if ref_key not in seen_refs:
                    seen_refs.add(ref_key)
                    references.append(reference)
        merged = dict(primary)
        merged["score"] = max(member["score"] for member in members)
        merged["transaction_ids"] = list(transaction_ids)
        merged["evidence_refs"] = references
        merged["graph_path"] = {"nodes": list(nodes), "edge_ids": list(edges)}
        merged["coverage"] = {
            **primary["coverage"],
            "pattern_chain_count": len(members),
            "pattern_transaction_count": len(transaction_ids),
        }
        merged["pattern"] = {
            "chain_count": len(members),
            "transaction_count": len(transaction_ids),
            "address_count": len(addresses),
            "transaction_ids": [f"tx:{txid}" for txid in list(transaction_ids)[:_PATTERN_LIST_CAP]],
            "addresses": list(addresses)[:_PATTERN_LIST_CAP],
            "chains": [
                {
                    "entity_ref": member["entity_ref"],
                    "hop_count": member["hop_count"],
                    "score": member["score"],
                    "transaction_ids": member["transaction_ids"],
                }
                for member in members[:_PATTERN_CHAIN_CAP]
            ],
        }
        if len(members) > 1:
            merged["reason_codes"] = [*primary["reason_codes"], "merged_overlapping_chains"]
            merged["explanation"] = (
                f"{primary['explanation']} {len(members) - 1} overlapping chain candidate(s) sharing its transactions "
                f"or origin are merged into this one pattern ({len(transaction_ids)} transactions in total), so it is "
                "reviewed once."
            )
        patterns.append(merged)
    return patterns


def _uuid4_strings(batch: int = 4096) -> Iterator[str]:
    """Random (version 4) UUID strings, the same format as `str(uuid.uuid4())`.

    Draws entropy in blocks and formats the hex directly, which avoids
    building a UUID object per row -- measurable at ~850K ids per snapshot.
    """
    while True:
        block = bytearray(os.urandom(16 * batch))
        for offset in range(0, len(block), 16):
            block[offset + 6] = (block[offset + 6] & 0x0F) | 0x40  # version 4
            block[offset + 8] = (block[offset + 8] & 0x3F) | 0x80  # RFC 4122 variant
        text = block.hex()
        for start in range(0, len(text), 32):
            h = text[start : start + 32]
            yield f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _evidence_size(candidate: dict[str, Any]) -> int:
    return len({fact.get("txid") for fact in candidate["facts"]})


def _group_window_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse one address-window rule's nested windows into one finding per day.

    Windows are epoch-aligned, so each 15-minute and 1-hour window sits inside
    exactly one 24-hour window, and these rules only count events -- a burst
    that meets a rule in a short window meets it in every enclosing one too.
    The kept finding is the strongest window (most evidence on a tie, then the
    narrowest); the others are listed on it, with their evidence, rather than
    queued as separate reviews of the same activity.
    """
    groups: dict[tuple[str, str, datetime], list[dict[str, Any]]] = {}
    day = WINDOW_SECONDS[-1]
    for candidate in candidates:
        day_start = datetime.fromtimestamp(int(candidate["start"].timestamp()) // day * day, tz=UTC)
        groups.setdefault((candidate["entity_ref"], candidate["rule_id"], day_start), []).append(candidate)
    grouped: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) == 1:
            grouped.append(members[0])
            continue
        members.sort(
            key=lambda item: (-item["score"], -_evidence_size(item), (item["end"] - item["start"]).total_seconds())
        )
        primary = dict(members[0])
        windows = sorted(members, key=lambda item: ((item["end"] - item["start"]).total_seconds(), item["start"]))
        feature = dict(primary["feature"])
        feature["matched_windows"] = [
            {
                "window_seconds": int((item["end"] - item["start"]).total_seconds()),
                "window_start": item["start"].isoformat(),
                "window_end": item["end"].isoformat(),
                "score": item["score"],
            }
            for item in windows
        ]
        primary["feature"] = feature
        primary.pop("feature_json", None)
        labels = ", ".join(
            _WINDOW_LABEL.get(int((item["end"] - item["start"]).total_seconds()), "other") for item in windows if item is not members[0]
        )
        primary["explanations"] = [
            *primary["explanations"],
            (
                f"The same rule was also met in {len(members) - 1} other window(s) of this day ({labels}); "
                "they are merged into this finding so the activity is reviewed once."
            ),
        ]
        seen: set[int] = set()
        facts: list[dict] = []
        for member in members:
            for fact in member["facts"]:
                if id(fact) not in seen:
                    seen.add(id(fact))
                    facts.append(fact)
        primary["facts"] = facts
        grouped.append(primary)
    return grouped


def materialize_findings(
    session: Session, *, evidence_root, snapshot: Snapshot, graph: GraphSnapshot, records: FactRecords | None = None
) -> int:
    """Store deterministic address/window observations exactly once for a snapshot.

    `records` lets the ingestion pipeline pass facts it already decoded.
    """
    if session.scalar(select(FeatureRecord.id).where(FeatureRecord.snapshot_id == snapshot.id).limit(1)):
        return 0
    if records is None:
        records = load_facts(session, evidence_root, snapshot.id)
    with bulk_allocation():
        return _materialize(session, snapshot=snapshot, graph=graph, records=records)


def _materialize(session: Session, *, snapshot: Snapshot, graph: GraphSnapshot, records: FactRecords) -> int:
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
    signal_by_key: dict[tuple[str, int, datetime], list[dict[str, Any]]] = defaultdict(list)
    # Outpoints already present per window key: an output fact is identified by
    # (txid, vout), so this replaces a linear, dict-by-dict `in` scan of the
    # window's list -- quadratic on busy addresses.
    event_outpoints: dict[tuple[str, int, datetime], set[tuple[str, int]]] = {}

    def add_signal(address: str | None, observed: datetime | None, signal: dict[str, Any], output: dict[str, Any] | None = None) -> None:
        add_window_signal(output_events, event_outpoints, signal_by_key, address, observed, signal, output)

    peeling = detect_peeling_chains(transactions=transactions, inputs=records["inputs"], outputs=records["outputs"])
    coinjoin = detect_coinjoin_like_transactions(
        transactions=transactions, inputs=records["inputs"], outputs=records["outputs"]
    )
    seed_rows = list(
        session.scalars(
            select(SyntheticReviewSeed).where(
                SyntheticReviewSeed.case_id == snapshot.case_id, SyntheticReviewSeed.snapshot_id == snapshot.id
            )
        )
    )
    propagation = propagate_synthetic_review_seeds(
        seeds=[
            {"id": seed.id, "seed_entity_ref": seed.seed_entity_ref, "seed_reason": seed.seed_reason, "synthetic": seed.synthetic}
            for seed in seed_rows
        ],
        transactions=transactions,
        inputs=records["inputs"],
        outputs=records["outputs"],
    )
    for signal in peeling:
        origin = signal["output_ids"][0].removeprefix("out:").rsplit(":", 1)
        output = outputs_by_outpoint[(origin[0], int(origin[1]))]
        add_signal(output.get("address") or output.get("script_id"), transaction_times.get(signal["transaction_ids"][0]), signal, output)
    for signal in coinjoin:
        for output in outputs_by_tx.get(signal["transaction_id"], []):
            add_signal(output.get("address") or output.get("script_id"), transaction_times.get(signal["transaction_id"]), signal, output)
    outputs_by_address: dict[str, list[dict]] = defaultdict(list)
    if propagation:
        for item in records["outputs"]:
            outputs_by_address[item.get("address") or item.get("script_id")].append(item)
    for signal in propagation:
        address = signal["entity_ref"].removeprefix("address:")
        for output in outputs_by_address.get(address, []):
            add_signal(address, transaction_times.get(output["txid"]), signal, output)

    # Keys whose feature vector a detector finding borrows, resolved up front
    # so the address-window pass can stream every other feature row straight
    # to the database instead of holding ~800K of them in memory.
    peeling_patterns = _group_peeling_patterns(peeling, transaction_times)
    detector_keys: dict[int, tuple[str, int, datetime] | None] = {}
    for pattern in peeling_patterns:
        detector_keys[id(pattern)] = peeling_detector_key(pattern, transaction_times)
    for signal in coinjoin:
        detector_keys[id(signal)] = coinjoin_detector_key(
            signal, transaction_times, outputs_by_tx.get(signal["transaction_id"], [])
        )
    if propagation:
        earliest_key_by_address: dict[str, tuple[str, int, datetime]] = {}
        for feature_key in output_events:
            current = earliest_key_by_address.get(feature_key[0])
            if current is None or feature_key[2] < current[2]:
                earliest_key_by_address[feature_key[0]] = feature_key
        for signal in propagation:
            detector_keys[id(signal)] = earliest_key_by_address.get(signal["entity_ref"].removeprefix("address:"))
    needed_keys = {key for key in detector_keys.values() if key is not None}

    plan = current_plan()
    coverage_json = json.dumps(coverage)
    writer = FeatureRowWriter(session, snapshot=snapshot, graph=graph, coverage_json=coverage_json, chunk_rows=plan.insert_chunk_rows)
    window_candidates, kept_features = address_window_pass(
        output_events=output_events,
        rapid_events=rapid_events,
        signal_by_key=signal_by_key,
        transaction_times=transaction_times,
        outputs_by_tx=outputs_by_tx,
        needed_keys=needed_keys,
        writer=writer,
    )
    writer.flush()
    candidates.extend(window_candidates)

    # Detector finding records are separate from the address-window export but
    # share its snapshot, graph, source locators, coverage, and explanation.
    for signal in [*peeling_patterns, *coinjoin, *propagation]:
        key = detector_keys.get(id(signal))
        kept = kept_features.get(key) if key else None
        candidates.append(detector_candidate(signal, key, kept[0] if kept else None, transaction_times, coverage))
    del kept_features
    candidates = dedupe_and_rank(candidates)
    opposing_json = opposing_evidence_json()
    rows: list[dict[str, Any]] = []
    row_ids = _uuid4_strings()
    for rank, candidate in enumerate(candidates, 1):
        rows.append(
            finding_row(
                candidate, rank=rank, row_id=next(row_ids), snapshot=snapshot, graph=graph,
                coverage_json=coverage_json, opposing_json=opposing_json,
            )
        )
        if len(rows) >= plan.insert_chunk_rows:
            bulk_insert_serialized(session, FindingRecord, rows, _FINDING_JSON_COLUMNS)
            rows = []
    bulk_insert_serialized(session, FindingRecord, rows, _FINDING_JSON_COLUMNS)
    record_findings_event(session, snapshot, len(candidates))
    return len(candidates)


# --------------------------------------------------------------------------- #
# Building blocks shared by the in-memory pass above and the bounded-memory
# pass (app.engine.bounded), which runs them per address partition.
# --------------------------------------------------------------------------- #

def add_window_signal(
    output_events: dict[tuple[str, int, datetime], list[dict]],
    event_outpoints: dict[tuple[str, int, datetime], set[tuple[str, int]]],
    signal_by_key: dict[tuple[str, int, datetime], list[dict[str, Any]]],
    address: str | None,
    observed: datetime | None,
    signal: dict[str, Any],
    output: dict[str, Any] | None = None,
) -> None:
    """Attach a detector signal (and the output it keys on) to its windows."""
    if not address or not observed:
        return
    for seconds in WINDOW_SECONDS:
        start, _ = _window(observed, seconds)
        key = (str(address), seconds, start)
        if output is not None:
            present = event_outpoints.get(key)
            if present is None:
                present = event_outpoints[key] = {(item["txid"], item["vout"]) for item in output_events[key]}
            outpoint = (output["txid"], output["vout"])
            if outpoint not in present:
                # A detector can key a row to the verified spend time; preserve
                # the actual output fact rather than inventing a transfer.
                present.add(outpoint)
                output_events[key].append(output)
        signal_by_key[key].append(signal)


def peeling_detector_key(pattern: dict[str, Any], transaction_times: Mapping[str, datetime | None]) -> tuple[str, int, datetime] | None:
    observed = transaction_times.get(pattern["transaction_ids"][0])
    address = pattern["entity_ref"].removeprefix("address:")
    return (address, WINDOW_SECONDS[0], _window(observed, WINDOW_SECONDS[0])[0]) if observed else None


def coinjoin_detector_key(
    signal: dict[str, Any], transaction_times: Mapping[str, datetime | None], tx_outputs: list[dict]
) -> tuple[str, int, datetime] | None:
    """coinjoin has no single owning address (that's the point of the shape) — its
    entity_ref stays transaction-scoped, but the finding still borrows the real
    address-window feature vector computed for its first addressed output (that
    window always exists: the signal itself adds the output to it)."""
    observed = transaction_times.get(signal["transaction_id"])
    if not observed:
        return None
    window_start = _window(observed, WINDOW_SECONDS[0])[0]
    for output in tx_outputs:
        candidate_address = output.get("address") or output.get("script_id")
        if candidate_address:
            return (str(candidate_address), WINDOW_SECONDS[0], window_start)
    return None


class FeatureRowWriter:
    """Streams address-window feature rows to the database in bounded chunks.

    JSON columns are serialized here -- the shared coverage document exactly
    once -- and each source reference's JSON text is cached by identity while
    the facts holding it are alive; `end_pass()` drops that cache before the
    facts of a pass are released, so a recycled object id is never reused.
    """

    def __init__(self, session: Session, *, snapshot: Snapshot, graph: GraphSnapshot, coverage_json: str, chunk_rows: int) -> None:
        self._session = session
        self._snapshot = snapshot
        self._graph = graph
        self._coverage_json = coverage_json
        self._chunk_rows = chunk_rows
        self._rows: list[dict[str, Any]] = []
        self._ref_json: dict[int, str] = {}
        self._ids = _uuid4_strings()
        self.written = 0

    def _refs_json(self, references: list[dict]) -> str:
        parts = []
        for reference in references:
            encoded = self._ref_json.get(id(reference))
            if encoded is None:
                encoded = self._ref_json[id(reference)] = json.dumps(reference)
            parts.append(encoded)
        # Byte-identical to json.dumps(references) with its default separators.
        return f"[{', '.join(parts)}]"

    def add(self, address: str, seconds: int, start: datetime, feature_json: str, outputs: list[dict]) -> None:
        self._rows.append(
            {
                "id": next(self._ids),
                "case_id": self._snapshot.case_id,
                "snapshot_id": self._snapshot.id,
                "graph_snapshot_id": self._graph.id,
                "entity_ref": f"address:{address}",
                "window_start": start,
                "window_end": start + timedelta(seconds=seconds),
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "feature_vector": feature_json,
                "coverage": self._coverage_json,
                "source_refs": self._refs_json(_dedupe_refs(outputs)),
            }
        )
        if len(self._rows) >= self._chunk_rows:
            self.flush()

    def flush(self) -> None:
        if self._rows:
            bulk_insert_serialized(self._session, FeatureRecord, self._rows, _FEATURE_JSON_COLUMNS)
            self.written += len(self._rows)
            self._rows = []

    def end_pass(self) -> None:
        self.flush()
        self._ref_json.clear()


def address_window_pass(
    *,
    output_events: dict[tuple[str, int, datetime], list[dict]],
    rapid_events: Mapping[tuple[str, int, datetime], list[tuple[dict, dict, float]]],
    signal_by_key: Mapping[tuple[str, int, datetime], list[dict[str, Any]]],
    transaction_times: Mapping[str, datetime | None],
    outputs_by_tx: Mapping[str, list[dict]],
    needed_keys: set[tuple[str, int, datetime]],
    writer: FeatureRowWriter,
) -> tuple[list[dict[str, Any]], dict[tuple[str, int, datetime], tuple[dict[str, Any], str]]]:
    """Every address-window feature row plus the address-window rule candidates.

    Everything here is local to one address, so the bounded path can run it on
    one partition of addresses at a time and get exactly the same rows.
    Returns (grouped candidates in key order, features kept for detectors).
    """
    candidates: list[dict[str, Any]] = []
    thresholds = {
        "concentrated_collection_min_source_transactions": COLLECTION_MIN_SOURCE_TRANSACTIONS,
        "emerging_hub_min_source_transactions": HUB_MIN_SOURCE_TRANSACTIONS,
        "rapid_redistribution_min_inbound_transactions": RAPID_MIN_INBOUND_TRANSACTIONS,
        "rapid_redistribution_max_delay_seconds": RAPID_MAX_DELAY_SECONDS,
    }
    history = _history_features(output_events)
    kept_features: dict[tuple[str, int, datetime], tuple[dict[str, Any], str]] = {}
    for (address, seconds, start), outputs in output_events.items():
        key = (address, seconds, start)
        distinct_transactions = {output["txid"] for output in outputs}
        feature = _window_features(
            address=address,
            seconds=seconds,
            outputs=outputs,
            transaction_times=transaction_times,
            rapid_events=rapid_events.get(key, []),
            outputs_by_tx=outputs_by_tx,
            inbound_txids=distinct_transactions,
        )
        feature.update(history.pop(key))
        feature["rule_thresholds"] = dict(thresholds)
        feature.update(_phase41_defaults())
        for signal in signal_by_key.get(key, []):
            _merge_signal_features(feature, signal)
        feature_json = json.dumps(feature)
        writer.add(address, seconds, start, feature_json, outputs)
        if key in needed_keys:
            kept_features[key] = (feature, feature_json)
        if len(distinct_transactions) >= COLLECTION_MIN_SOURCE_TRANSACTIONS:
            candidates.append(
                {
                    "entity_ref": f"address:{address}",
                    "start": start,
                    "end": start + timedelta(seconds=seconds),
                    "rule_id": "concentrated_collection",
                    "score": float(min(100, 25 + 15 * len(distinct_transactions))),
                    "feature": feature,
                    "feature_json": feature_json,
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
                    "feature_json": feature_json,
                    "explanations": [
                        f"Address activity reached {len(distinct_transactions)} distinct source transactions in a bounded window."
                    ],
                    "alternatives": [
                        "A newly active exchange service, fundraiser, or payment processor can be a benign hub."
                    ],
                    "facts": outputs,
                }
            )
    del history
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
    # One review finding per address, rule and day: the same burst also meets
    # the rule in every enclosing window (15 min inside 1 h inside 24 h), which
    # used to put the same evidence in front of a reviewer up to three times.
    return _group_window_candidates(candidates), kept_features


def detector_candidate(
    signal: dict[str, Any],
    key: tuple[str, int, datetime] | None,
    kept_feature: dict[str, Any] | None,
    transaction_times: Mapping[str, datetime | None],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    if signal["finding_type"] == "peeling_chain_candidate":
        observed = transaction_times.get(signal["transaction_ids"][0])
    elif signal["finding_type"] == "coinjoin_like_structure":
        observed = transaction_times.get(signal["transaction_id"])
    else:
        observed = key[2] if key else None
    start = key[2] if key else (_window(observed, WINDOW_SECONDS[0])[0] if observed else datetime(1970, 1, 1, tzinfo=UTC))
    feature = dict(kept_feature) if kept_feature is not None else {"feature_contract_version": FEATURE_SCHEMA_VERSION, **_phase41_defaults()}
    feature.update(_signal_feature(signal))
    feature["detector_result"] = {
        "finding_type": signal["finding_type"], "reason_codes": signal["reason_codes"], "uncertainty": signal["uncertainty"],
        "explanation": signal["explanation"], "evidence_refs": signal["evidence_refs"], "graph_path": signal.get("graph_path"),
        # peeling_chain_candidate-only fields (None for other signal types): the
        # per-hop breakdown and its already-computed real signal components were
        # being thrown away here even though the detector fully computes them --
        # this is the one place a UI wanting hop-level detail can read them back.
        "steps": signal.get("steps"),
        "hop_count": signal.get("hop_count"),
        "total_duration_sec": signal.get("total_duration_sec"),
        "peel_ratio": signal.get("peel_ratio"),
        "velocity": signal.get("velocity"),
        "cluster_link": signal.get("cluster_link"),
        # The whole reviewable pattern (every merged chain), for the graph to
        # highlight as one unit; None for single-transaction detectors.
        "pattern": signal.get("pattern"),
    }
    return {
        "entity_ref": signal["entity_ref"], "start": start, "end": start + timedelta(seconds=WINDOW_SECONDS[0]),
        "rule_id": signal["finding_type"], "score": signal["score"], "feature": feature,
        "explanations": [signal["explanation"]],
        "alternatives": ["This deterministic review signal has benign explanations and does not establish ownership, origin, or wrongdoing."],
        "facts": [], "source_refs": signal["evidence_refs"], "coverage": {**coverage, **signal["coverage"]},
    }


def dedupe_and_rank(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unique (entity, window, rule) keys, highest score first.

    Two distinct detector signals (e.g. a peeling pattern and a synthetic-seed
    propagation) can resolve to the same address and window bucket, the
    table's unique key. Keep the highest-scoring candidate per key; ties keep
    whichever was constructed first. (Peeling chains sharing a key are already
    merged into one pattern by _group_peeling_patterns, so none is lost here.)
    """
    deduped: dict[tuple[str, datetime, datetime, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate["entity_ref"], candidate["start"], candidate["end"], candidate["rule_id"])
        current = deduped.get(key)
        if current is None or candidate["score"] > current["score"]:
            deduped[key] = candidate
    ranked = list(deduped.values())
    ranked.sort(key=lambda candidate: (-candidate["score"], candidate["rule_id"], candidate["entity_ref"]))
    return ranked


def opposing_evidence_json() -> str:
    return json.dumps(
        [
            {
                "kind": "coverage_limitation",
                "statement": (
                    "This committed snapshot contains no ownership attribution or independently "
                    "verified benign context; the pattern alone cannot establish either."
                ),
                "source_refs": [],
            }
        ]
    )


def finding_row(
    candidate: dict[str, Any], *, rank: int, row_id: str, snapshot: Snapshot, graph: GraphSnapshot,
    coverage_json: str, opposing_json: str,
) -> dict[str, Any]:
    candidate_coverage = candidate.get("coverage")
    return {
        "id": row_id,
        "case_id": snapshot.case_id,
        "snapshot_id": snapshot.id,
        "graph_snapshot_id": graph.id,
        "entity_ref": candidate["entity_ref"],
        "window_start": candidate["start"],
        "window_end": candidate["end"],
        "rule_id": candidate["rule_id"],
        "rule_version": RULE_VERSION,
        "claim": (
            f"Observed {candidate['rule_id']} pattern for {candidate['entity_ref']} in a committed "
            f"{candidate['feature'].get('window_seconds', WINDOW_SECONDS[0])}-second window; prioritize it for reviewer assessment."
        ),
        "finding_version": 1,
        "raw_score": candidate["score"],
        "rank": rank,
        "coverage": coverage_json if candidate_coverage is None else json.dumps(candidate_coverage),
        "feature_vector": candidate.get("feature_json") or json.dumps(candidate["feature"]),
        "feature_vector_hash": _hash(candidate["feature"]),
        "explanations": json.dumps(candidate["explanations"]),
        "benign_alternatives": json.dumps(candidate["alternatives"]),
        "opposing_evidence": opposing_json,
        "source_refs": json.dumps(
            candidate["source_refs"] if "source_refs" in candidate else _dedupe_refs(candidate["facts"])
        ),
        "status": "open",
    }


def record_findings_event(session: Session, snapshot: Snapshot, count: int) -> None:
    if count:
        append_event(
            session,
            case_id=snapshot.case_id,
            event_type="finding.updated",
            stage="findings_ready",
            payload={"snapshot_id": snapshot.id, "finding_count": count, "method": RULE_VERSION, "ml_enabled": False},
        )


def refresh_synthetic_seed_proximity(
    session: Session, *, evidence_root, snapshot: Snapshot, graph: GraphSnapshot
) -> int:
    """Materialize synthetic-seed context after an authorised seed is added.

    Seeds are intentionally supplied after a completed snapshot is available,
    so this updates only the frozen snapshot's feature rows and adds missing
    review findings. Existing score paths are never used to create graph edges.
    """
    records = load_facts(session, evidence_root, snapshot.id)
    transactions = {fact["txid"]: fact for fact in records["transactions"]}
    seeds = list(
        session.scalars(
            select(SyntheticReviewSeed).where(
                SyntheticReviewSeed.case_id == snapshot.case_id, SyntheticReviewSeed.snapshot_id == snapshot.id
            )
        )
    )
    signals = propagate_synthetic_review_seeds(
        seeds=[{"id": item.id, "seed_entity_ref": item.seed_entity_ref, "seed_reason": item.seed_reason, "synthetic": item.synthetic} for item in seeds],
        transactions=transactions, inputs=records["inputs"], outputs=records["outputs"],
    )
    rows = list(session.scalars(select(FeatureRecord).where(FeatureRecord.snapshot_id == snapshot.id)))
    updated = 0
    for signal in signals:
        for row in rows:
            if row.entity_ref != signal["entity_ref"]:
                continue
            vector = dict(row.feature_vector)
            _merge_signal_features(vector, signal)
            # Keep seed identity/path evidence on the case finding, not in the
            # Phase 5A address-window feature vector. The four flat risk fields
            # are evaluation metadata only and cannot carry a seed identity.
            row.feature_vector = vector
            updated += 1
        existing = session.scalar(
            select(FindingRecord.id).where(
                FindingRecord.snapshot_id == snapshot.id,
                FindingRecord.entity_ref == signal["entity_ref"],
                FindingRecord.rule_id == signal["finding_type"],
            ).limit(1)
        )
        if existing is not None:
            continue
        start = min((row.window_start for row in rows if row.entity_ref == signal["entity_ref"]), default=datetime(1970, 1, 1, tzinfo=UTC))
        feature = {"feature_contract_version": FEATURE_SCHEMA_VERSION, **_signal_feature(signal), "detector_result": {
            "finding_type": signal["finding_type"], "reason_codes": signal["reason_codes"], "uncertainty": signal["uncertainty"],
            "explanation": signal["explanation"], "evidence_refs": signal["evidence_refs"], "graph_path": signal["graph_path"],
        }}
        session.add(
            FindingRecord(
                case_id=snapshot.case_id, snapshot_id=snapshot.id, graph_snapshot_id=graph.id, entity_ref=signal["entity_ref"],
                window_start=start, window_end=start + timedelta(seconds=WINDOW_SECONDS[0]), rule_id=signal["finding_type"],
                rule_version=RULE_VERSION, claim="Observed synthetic evaluation seed proximity through verified UTXO relationships; requires review.",
                raw_score=signal["score"], rank=None, coverage={**_coverage(graph, transactions), **signal["coverage"]}, feature_vector=feature,
                feature_vector_hash=_hash(feature), explanations=[signal["explanation"]],
                benign_alternatives=["Synthetic review context is evaluation-only and is not an ownership or illicit-attribution claim."],
                opposing_evidence=[{"kind": "synthetic_context", "statement": "This score is produced only from an explicitly synthetic review seed.", "source_refs": []}],
                source_refs=signal["evidence_refs"], status="open",
            )
        )
    if signals:
        append_event(session, case_id=snapshot.case_id, event_type="finding.updated", stage="findings_ready", payload={"snapshot_id": snapshot.id, "finding_count": len(signals), "method": RULE_VERSION, "ml_enabled": False, "synthetic_seed_context": True})
    return updated
