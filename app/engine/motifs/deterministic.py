"""Deterministic, evidence-first Bitcoin transaction structure motifs.

These helpers deliberately operate on resolved prevouts only.  They do not
allocate an input to an output, infer wallet control, or use network/IP facts.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Container, Mapping
from datetime import UTC, datetime
from itertools import pairwise
from statistics import median
from typing import Any

DEFAULT_MAX_PEELING_HOPS = 8
DEFAULT_MIN_PEELING_LENGTH = 3
DEFAULT_MIN_COINJOIN_INPUTS = 3
DEFAULT_MIN_COINJOIN_OUTPUTS = 3
DEFAULT_MIN_EQUAL_OUTPUTS = 3
DEFAULT_EQUAL_OUTPUT_TOLERANCE_SATS = 0
DEFAULT_RISK_MAX_DEPTH = 2
DEFAULT_RISK_DECAY = 0.60


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def edge_id(from_node: str, to_node: str, edge_type: str) -> str:
    """Match the immutable graph builder's deterministic edge identity."""
    return hashlib.sha256(f"{from_node}|{to_node}|{edge_type}".encode()).hexdigest()


def dedupe_refs(*facts: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    references: list[dict[str, Any]] = []
    for fact in facts:
        for reference in fact.get("source_refs", []):
            key = repr(sorted(reference.items()))
            if key not in seen:
                seen.add(key)
                references.append(reference)
    return references


def resolved_spenders(inputs: list[dict[str, Any]], outputs: dict[tuple[str, int], dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """Return only unique, supplied-prevout spend links.

    A conflicting spender is excluded rather than selected.  This mirrors the
    graph builder's policy and keeps every motif rooted in a verified UTXO link.
    """
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for tx_input in inputs:
        prev_txid, prev_vout = tx_input.get("prev_txid"), tx_input.get("prev_vout")
        if prev_txid is not None and prev_vout is not None and (prev_txid, prev_vout) in outputs:
            grouped[(prev_txid, prev_vout)].append(tx_input)
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for outpoint, candidates in grouped.items():
        txids = {candidate["txid"] for candidate in candidates}
        if len(txids) == 1:
            # Multiple rows for the same input are not expected, but selecting
            # the lowest vin is deterministic and does not change the spender.
            result[outpoint] = min(candidates, key=lambda item: item["vin"])
    return result


def address_of_output(output: dict[str, Any] | None) -> str | None:
    if output is None:
        return None
    value = output.get("address") or output.get("script_id")
    return str(value) if value else None


def _hop_detail(
    spending_txid: str,
    previous: dict[str, Any],
    continuation: dict[str, Any] | None,
    *,
    outputs_by_tx: Mapping[str, list[dict[str, Any]]],
    inputs_by_tx: Mapping[str, list[dict[str, Any]]],
    spenders: Container[tuple[str, int]],
) -> dict[str, Any]:
    """Per-hop context a reviewer needs to read the chain without re-querying
    the graph: which addresses each hop moved between, the transaction's own
    in/out arity, the sibling outputs actually peeled off at this hop, and the
    co-spent input addresses behind the common-input-ownership signal."""
    tx_inputs = inputs_by_tx.get(spending_txid, [])
    tx_outputs = outputs_by_tx.get(spending_txid, [])
    continuation_key = (continuation["txid"], continuation["vout"]) if continuation else None
    peels = [
        {
            "output_id": f"out:{item['txid']}:{item['vout']}",
            "address": address_of_output(item),
            "amount_sats": item["amount_sats"],
            # "spent" here means a uniquely-resolved onward spend exists in
            # this snapshot -- an unspent peel is a real terminal output.
            "is_spent": (item["txid"], item["vout"]) in spenders,
        }
        for item in sorted(tx_outputs, key=lambda o: (-o["amount_sats"], o["vout"]))
        if (item["txid"], item["vout"]) != continuation_key
    ]
    co_spend_addresses = sorted({addr for item in tx_inputs if (addr := item.get("address"))})
    return {
        "previous_address": address_of_output(previous),
        "previous_script_type": previous.get("script_type"),
        "continuing_address": address_of_output(continuation),
        "continuing_script_type": continuation.get("script_type") if continuation else None,
        "input_count": len(tx_inputs),
        "output_count": len(tx_outputs),
        # Bounded so a wide transaction can't bloat the stored finding row.
        "peel_outputs": peels[:4],
        "peel_output_total": len(peels),
        "co_spend_addresses": co_spend_addresses[:6],
        "co_spend_input_address_count": len(co_spend_addresses),
    }


#: One walk: (outpoint, spending txid, continuation output or None) per hop.
PeelingWalk = list[tuple[tuple[str, int], str, "dict[str, Any] | None"]]


def build_peeling_signal(
    start_outpoint: tuple[str, int],
    walk: PeelingWalk,
    *,
    outputs_by_outpoint: Mapping[tuple[str, int], dict[str, Any]],
    outputs_by_tx: Mapping[str, list[dict[str, Any]]],
    inputs_by_tx: Mapping[str, list[dict[str, Any]]],
    spenders: Container[tuple[str, int]],
    transactions: Mapping[str, dict[str, Any]],
    tx_times: Mapping[str, datetime | None],
    max_hops: int = DEFAULT_MAX_PEELING_HOPS,
) -> dict[str, Any]:
    """The full reviewable signal for one emitted walk.

    Shared by the in-memory detector and the bounded-memory path, which hands
    it lookups restricted to the walk's own transactions -- so both produce
    the same signal from the same facts.
    """
    def hop_detail(spending_txid: str, previous: dict[str, Any], continuation: dict[str, Any] | None) -> dict[str, Any]:
        return _hop_detail(
            spending_txid, previous, continuation,
            outputs_by_tx=outputs_by_tx, inputs_by_tx=inputs_by_tx, spenders=spenders,
        )

    steps: list[dict[str, Any]] = []
    for outpoint, spending_txid, continuation in walk:
        previous = outputs_by_outpoint[outpoint]
        if continuation is None:
            # The current output's spend is itself a verified transaction
            # in the chain.  Do not nominate an unspent terminal output as
            # a continuation; only preceding continuation outputs were
            # selected because their later spend is real.
            steps.append(
                {
                    "previous_output_id": f"out:{previous['txid']}:{previous['vout']}",
                    "spending_transaction_id": spending_txid,
                    "continuing_output_id": None,
                    "previous_value_sats": previous["amount_sats"],
                    "continuing_value_sats": None,
                    "timestamp": (tx_times.get(spending_txid).isoformat() if tx_times.get(spending_txid) else None),
                    "edge_ids": [edge_id(f"out:{previous['txid']}:{previous['vout']}", f"tx:{spending_txid}", "SPENT_BY")],
                    **hop_detail(spending_txid, previous, None),
                }
            )
            break
        steps.append(
            {
                "previous_output_id": f"out:{previous['txid']}:{previous['vout']}",
                "spending_transaction_id": spending_txid,
                "continuing_output_id": f"out:{continuation['txid']}:{continuation['vout']}",
                "previous_value_sats": previous["amount_sats"],
                "continuing_value_sats": continuation["amount_sats"],
                "timestamp": (tx_times.get(spending_txid).isoformat() if tx_times.get(spending_txid) else None),
                "edge_ids": [
                    edge_id(f"out:{previous['txid']}:{previous['vout']}", f"tx:{spending_txid}", "SPENT_BY"),
                    edge_id(f"tx:{spending_txid}", f"out:{continuation['txid']}:{continuation['vout']}", "CREATES_OUTPUT"),
                ],
                **hop_detail(spending_txid, previous, continuation),
            }
        )
    txids = tuple(step["spending_transaction_id"] for step in steps)
    known_times = [tx_times[txid] for txid in txids if tx_times.get(txid)]
    duration = (max(known_times) - min(known_times)).total_seconds() if len(known_times) >= 2 else 0.0
    reductions = [
        1 - (step["continuing_value_sats"] / step["previous_value_sats"])
        for step in steps
        if step["previous_value_sats"] > 0 and step["continuing_value_sats"] is not None
    ]
    regularity = sum(1 for value in reductions if value > 0) / len(steps)
    gaps = [
        (later - earlier).total_seconds() for earlier, later in pairwise(known_times) if later >= earlier
    ]
    time_continuity = (1 / (1 + (median(gaps) / 86400))) if gaps else 0.0
    evidence_facts: list[dict[str, Any]] = []
    for step in steps:
        previous_txid, previous_vout = step["previous_output_id"].removeprefix("out:").rsplit(":", 1)
        evidence_facts.extend([outputs_by_outpoint[(previous_txid, int(previous_vout))], transactions[step["spending_transaction_id"]]])
        if step["continuing_output_id"]:
            continuation_txid, continuation_vout = step["continuing_output_id"].removeprefix("out:").rsplit(":", 1)
            evidence_facts.append(outputs_by_outpoint[(continuation_txid, int(continuation_vout))])
    refs = dedupe_refs(*evidence_facts)
    evidence_coverage = min(1.0, len(refs) / max(1, len(steps)))
    score = max(0.0, min(1.0, (min(1.0, len(steps) / max_hops) + regularity + time_continuity + evidence_coverage) / 4))
    # Real, deterministic common-input-ownership signal (see _hop_detail):
    # fraction of hops whose spending tx combines >=2 distinct input addresses
    # in one signature set. Kept alongside score's own components rather than
    # recomputed downstream from raw steps -- one source of truth per signal.
    cluster_link_ratio = sum(1 for step in steps if step["co_spend_input_address_count"] >= 2) / len(steps)
    start_output = outputs_by_outpoint[start_outpoint]
    return (
        {
            "finding_type": "peeling_chain_candidate",
            "entity_ref": f"address:{start_output.get('address') or start_output.get('script_id')}"
            if (start_output.get("address") or start_output.get("script_id"))
            else f"out:{start_output['txid']}:{start_output['vout']}",
            "transaction_ids": list(txids),
            "output_ids": [steps[0]["previous_output_id"]] + [step["continuing_output_id"] for step in steps if step["continuing_output_id"]],
            "steps": steps,
            "hop_count": len(steps),
            "total_duration_sec": float(duration),
            "peel_ratio": regularity,
            "velocity": time_continuity,
            "cluster_link": cluster_link_ratio,
            "score": score,
            "coverage": {
                "verified_utxo_hops": len(steps),
                "timestamps_available": len(known_times),
                "evidence_coverage": evidence_coverage,
            },
            "uncertainty": {
                "time_continuity_unavailable": len(known_times) < 2,
                "scope": "Only supplied and uniquely resolved UTXO spends were examined.",
            },
            "reason_codes": ["verified_prevout_chain", "strict_output_reduction", "spent_continuation_output"],
            "explanation": (
                f"{len(steps)} sequential verified UTXO spends have a strictly smaller, later-spent continuation output. "
                "This is a review signal and does not establish laundering or common ownership."
            ),
            "evidence_refs": refs,
            "graph_path": {
                "nodes": [node for step in steps for node in (step["previous_output_id"], f"tx:{step['spending_transaction_id']}") if node]
                + [step["continuing_output_id"] for step in steps if step["continuing_output_id"]],
                "edge_ids": [edge for step in steps for edge in step["edge_ids"]],
            },
        }
    )


def peeling_hop(
    outpoint: tuple[str, int],
    *,
    spenders: Mapping[tuple[str, int], dict[str, Any]],
    outputs_by_outpoint: Mapping[tuple[str, int], dict[str, Any]],
    outputs_by_tx: Mapping[str, list[dict[str, Any]]],
    ranked_cache: dict[str, list[dict[str, Any]]],
) -> tuple[str, dict[str, Any] | None] | None:
    """The hop taken from an outpoint: (spending txid, continuation or None),
    or None when the outpoint ends the walk. Continuation selection: the
    largest uniquely-spent output of the spending transaction strictly smaller
    than the spent one (ties by vout)."""
    spending_input = spenders.get(outpoint)
    previous = outputs_by_outpoint.get(outpoint)
    if spending_input is None or previous is None:
        return None
    spending_txid = spending_input["txid"]
    ranked = ranked_cache.get(spending_txid)
    if ranked is None:
        ranked = ranked_cache[spending_txid] = sorted(
            (item for item in outputs_by_tx.get(spending_txid, []) if (item["txid"], item["vout"]) in spenders),
            key=lambda item: (-item["amount_sats"], item["vout"]),
        )
    continuation = next((item for item in ranked if item["amount_sats"] < previous["amount_sats"]), None)
    return (spending_txid, continuation)


def detect_peeling_chains(
    *,
    transactions: dict[str, dict[str, Any]],
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    max_hops: int = DEFAULT_MAX_PEELING_HOPS,
    min_chain_length: int = DEFAULT_MIN_PEELING_LENGTH,
) -> list[dict[str, Any]]:
    """Find bounded, sequential spends with an actually-spent continuation.

    For a spending transaction, the continuation selection is transparent:
    choose the largest output strictly smaller than the prior output that is
    itself uniquely spent in the snapshot (ties resolve by vout).  This is a
    structural candidate selection, never a change or ownership assertion.
    """
    if max_hops < 1 or min_chain_length < 1:
        raise ValueError("peeling depth and length must be positive")
    outputs_by_outpoint = {(output["txid"], output["vout"]): output for output in outputs}
    outputs_by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for output in outputs:
        outputs_by_tx[output["txid"]].append(output)
    inputs_by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tx_input in inputs:
        inputs_by_tx[tx_input["txid"]].append(tx_input)
    spenders = resolved_spenders(inputs, outputs_by_outpoint)
    tx_times = {
        txid: _time(fact.get("block_time") or fact.get("source_timestamp")) for txid, fact in transactions.items()
    }
    results: list[dict[str, Any]] = []
    emitted: set[tuple[str, ...]] = set()
    # The hop taken from an outpoint depends only on that outpoint, and chains
    # started from neighbouring outpoints share their suffixes, so each hop is
    # resolved once and memoised.
    hop_cache: dict[tuple[str, int], tuple[str, dict[str, Any] | None] | None] = {}
    ranked_cache: dict[str, list[dict[str, Any]]] = {}

    def _next_hop(outpoint: tuple[str, int]) -> tuple[str, dict[str, Any] | None] | None:
        if outpoint not in hop_cache:
            hop_cache[outpoint] = peeling_hop(
                outpoint, spenders=spenders, outputs_by_outpoint=outputs_by_outpoint,
                outputs_by_tx=outputs_by_tx, ranked_cache=ranked_cache,
            )
        return hop_cache[outpoint]

    for start_outpoint in sorted(spenders):
        # Walk first, cheaply; per-hop detail is only built for a chain that is
        # long enough and not already emitted (most walks are neither).
        walk: PeelingWalk = []
        current = start_outpoint
        for _ in range(max_hops):
            hop = _next_hop(current)
            if hop is None:
                break
            walk.append((current, hop[0], hop[1]))
            if hop[1] is None:
                break
            current = (hop[1]["txid"], hop[1]["vout"])
        if len(walk) < min_chain_length:
            continue
        txids = tuple(spending_txid for _, spending_txid, _ in walk)
        if txids in emitted:
            continue
        emitted.add(txids)
        results.append(
            build_peeling_signal(
                start_outpoint, walk, outputs_by_outpoint=outputs_by_outpoint, outputs_by_tx=outputs_by_tx,
                inputs_by_tx=inputs_by_tx, spenders=spenders, transactions=transactions, tx_times=tx_times,
                max_hops=max_hops,
            )
        )
    return results


def detect_coinjoin_like_transactions(
    *,
    transactions: dict[str, dict[str, Any]],
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    min_inputs: int = DEFAULT_MIN_COINJOIN_INPUTS,
    min_outputs: int = DEFAULT_MIN_COINJOIN_OUTPUTS,
    min_equal_outputs: int = DEFAULT_MIN_EQUAL_OUTPUTS,
    equality_tolerance_sats: int = DEFAULT_EQUAL_OUTPUT_TOLERANCE_SATS,
) -> list[dict[str, Any]]:
    """Report observable equal-output structure without calling it a CoinJoin."""
    if min_inputs < 1 or min_outputs < 1 or min_equal_outputs < 1 or equality_tolerance_sats < 0:
        raise ValueError("coinjoin thresholds must be non-negative and counts positive")
    inputs_by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    outputs_by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in inputs:
        inputs_by_tx[item["txid"]].append(item)
    for item in outputs:
        outputs_by_tx[item["txid"]].append(item)
    findings: list[dict[str, Any]] = []
    for txid, transaction in sorted(transactions.items()):
        tx_inputs, tx_outputs = inputs_by_tx.get(txid, []), sorted(outputs_by_tx.get(txid, []), key=lambda item: (item["amount_sats"], item["vout"]))
        if len(tx_inputs) < min_inputs or len(tx_outputs) < min_outputs:
            continue
        groups: list[list[dict[str, Any]]] = []
        for output in tx_outputs:
            fitting = [group for group in groups if output["amount_sats"] - group[0]["amount_sats"] <= equality_tolerance_sats]
            if fitting:
                fitting[0].append(output)
            else:
                groups.append([output])
        qualifying = [group for group in groups if len(group) >= min_equal_outputs]
        if not qualifying:
            continue
        equal_group = min(qualifying, key=lambda group: (-len(group), median(item["amount_sats"] for item in group), group[0]["vout"]))
        equal_value = int(median(item["amount_sats"] for item in equal_group))
        refs = dedupe_refs(transaction, *tx_inputs, *tx_outputs)
        input_factor = min(1.0, len(tx_inputs) / min_inputs)
        output_factor = min(1.0, len(tx_outputs) / min_outputs)
        equal_factor = min(1.0, len(equal_group) / min_equal_outputs)
        coverage = min(1.0, len(refs) / max(1, len(tx_inputs) + len(tx_outputs) + 1))
        score = max(0.0, min(1.0, (input_factor + output_factor + equal_factor + coverage) / 4))
        findings.append(
            {
                "finding_type": "coinjoin_like_structure",
                "entity_ref": f"tx:{txid}",
                "transaction_id": txid,
                "input_count": len(tx_inputs),
                "output_count": len(tx_outputs),
                "equal_output_count": len(equal_group),
                "equal_output_value_sats": equal_value,
                "fee_sats": transaction.get("fee_sats"),
                "score": score,
                "coverage": {"transaction_inputs_observed": len(tx_inputs), "transaction_outputs_observed": len(tx_outputs), "evidence_coverage": coverage},
                "uncertainty": {"change_outputs_not_inferred": True, "scope": "Only observable transaction shape and values were evaluated."},
                "reason_codes": ["minimum_input_count", "minimum_output_count", "equal_output_group"],
                "explanation": (
                    f"Transaction has {len(tx_inputs)} inputs, {len(tx_outputs)} outputs, and {len(equal_group)} output values "
                    f"within {equality_tolerance_sats} satoshis. This is a structural review signal, not proof of CoinJoin, mixing, or common ownership."
                ),
                "evidence_refs": refs,
                "graph_path": {"nodes": [f"tx:{txid}"] + [f"out:{item['txid']}:{item['vout']}" for item in equal_group], "edge_ids": [edge_id(f"tx:{txid}", f"out:{item['txid']}:{item['vout']}", "CREATES_OUTPUT") for item in equal_group]},
            }
        )
    return findings


def propagate_synthetic_review_seeds(
    *,
    seeds: list[dict[str, Any]],
    transactions: dict[str, dict[str, Any]],
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    max_depth: int = DEFAULT_RISK_MAX_DEPTH,
    decay: float = DEFAULT_RISK_DECAY,
) -> list[dict[str, Any]]:
    """Bounded downstream context from explicitly synthetic review seeds.

    One hop is a uniquely verified output spend followed by the spending
    transaction's real outputs. Network observations are intentionally absent.
    """
    if max_depth < 0 or not 0 <= decay <= 1:
        raise ValueError("risk max_depth must be non-negative and decay must be in 0..1")
    outputs_by_outpoint = {(output["txid"], output["vout"]): output for output in outputs}
    outputs_by_address: dict[str, list[dict[str, Any]]] = defaultdict(list)
    outputs_by_tx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for output in outputs:
        address = output.get("address") or output.get("script_id")
        if address:
            outputs_by_address[str(address)].append(output)
        outputs_by_tx[output["txid"]].append(output)
    spenders = resolved_spenders(inputs, outputs_by_outpoint)
    best: dict[str, dict[str, Any]] = {}
    support: dict[str, set[str]] = defaultdict(set)
    for seed in seeds:
        if not seed.get("synthetic", True):
            continue
        entity_ref = seed.get("seed_entity_ref") or seed.get("seed_address_id")
        address = str(entity_ref).removeprefix("address:")
        seed_id = str(seed["id"])
        direct = {
            "entity_ref": f"address:{address}", "seed_id": seed_id, "seed_reason": seed["seed_reason"], "distance": 0,
            "score": 1.0, "path_nodes": [f"address:{address}"], "edge_ids": [], "facts": [], "direct_seed": True,
        }
        frontier = [(output, direct) for output in outputs_by_address.get(address, [])]
        candidates = [direct]
        for distance in range(1, max_depth + 1):
            next_frontier = []
            for current_output, prior in frontier:
                spending_input = spenders.get((current_output["txid"], current_output["vout"]))
                if spending_input is None:
                    continue
                txid = spending_input["txid"]
                for next_output in outputs_by_tx.get(txid, []):
                    target_address = next_output.get("address") or next_output.get("script_id")
                    if not target_address:
                        continue
                    path = {
                        "entity_ref": f"address:{target_address}", "seed_id": seed_id, "seed_reason": seed["seed_reason"],
                        "distance": distance, "score": decay**distance,
                        "path_nodes": prior["path_nodes"] + [f"out:{current_output['txid']}:{current_output['vout']}", f"tx:{txid}", f"out:{next_output['txid']}:{next_output['vout']}", f"address:{target_address}"],
                        "edge_ids": prior["edge_ids"] + [
                            edge_id(f"out:{current_output['txid']}:{current_output['vout']}", f"tx:{txid}", "SPENT_BY"),
                            edge_id(f"tx:{txid}", f"out:{next_output['txid']}:{next_output['vout']}", "CREATES_OUTPUT"),
                            edge_id(f"out:{next_output['txid']}:{next_output['vout']}", f"address:{target_address}", "LOCKED_TO"),
                        ],
                        "facts": prior["facts"] + [current_output, spending_input, transactions[txid], next_output], "direct_seed": False,
                    }
                    candidates.append(path)
                    next_frontier.append((next_output, path))
            frontier = next_frontier
        for candidate in candidates:
            entity = candidate["entity_ref"]
            support[entity].add(seed_id)
            old = best.get(entity)
            if old is None or candidate["score"] > old["score"] or (candidate["score"] == old["score"] and candidate["distance"] < old["distance"]):
                best[entity] = candidate
    results = []
    for entity, item in sorted(best.items()):
        refs = dedupe_refs(*item["facts"])
        seed_ref = {"synthetic_review_seed_id": item["seed_id"], "seed_reason": item["seed_reason"], "synthetic": True}
        results.append(
            {
                "finding_type": "synthetic_seed_proximity", "entity_ref": entity, "score": item["score"],
                "risk_seed_distance": item["distance"], "risk_seed_count": len(support[entity]), "direct_seed": item["direct_seed"],
                "coverage": {"verified_utxo_path": True, "path_hops": item["distance"], "evidence_coverage": 1.0 if item["facts"] or item["direct_seed"] else 0.0},
                "uncertainty": {"synthetic_evaluation_only": True, "scope": "No ownership or illicit-attribution claim is made."},
                "reason_codes": ["synthetic_review_seed", "verified_utxo_path"] + (["direct_synthetic_seed"] if item["direct_seed"] else ["decayed_path_score"]),
                "explanation": (
                    "This address is the explicitly synthetic review seed." if item["direct_seed"] else
                    f"This address is {item['distance']} verified UTXO hop(s) from an explicitly synthetic review seed; score uses {decay:g} decay per hop."
                ),
                "evidence_refs": refs + [seed_ref], "graph_path": {"nodes": item["path_nodes"], "edge_ids": item["edge_ids"]},
            }
        )
    return results
