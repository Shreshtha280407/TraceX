"""Deterministic, case-scoped graph analytics over an explicit edge snapshot."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime

import igraph
import leidenalg

from app.core.canonical import canonical_sha256
from app.modules.graph.intelligence.models import (
    AnalyticsResult,
    GraphEdgeSnapshot,
    MotifMatch,
)

ANALYTICS_VERSION = "phase5_deterministic_graph_analytics_v1"


def _validate(edges: tuple[GraphEdgeSnapshot, ...]) -> None:
    if len({edge.case_id for edge in edges}) > 1:
        raise ValueError("analytics edge snapshot spans multiple cases")


def _adjacency(edges: tuple[GraphEdgeSnapshot, ...]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        left, right = str(edge.left_id), str(edge.right_id)
        graph[left].add(right)
        graph[right].add(left)
    return graph


def _betweenness(graph: dict[str, set[str]]) -> dict[str, float]:
    """Deterministic unweighted Brandes betweenness for a case graph."""
    result = dict.fromkeys(graph, 0.0)
    for source in sorted(graph):
        stack: list[str] = []
        predecessors: dict[str, list[str]] = {node: [] for node in graph}
        paths = dict.fromkeys(graph, 0.0)
        paths[source] = 1.0
        distance = dict.fromkeys(graph, -1)
        distance[source] = 0
        queue = deque([source])
        while queue:
            current = queue.popleft()
            stack.append(current)
            for adjacent in sorted(graph[current]):
                if distance[adjacent] < 0:
                    queue.append(adjacent)
                    distance[adjacent] = distance[current] + 1
                if distance[adjacent] == distance[current] + 1:
                    paths[adjacent] += paths[current]
                    predecessors[adjacent].append(current)
        dependency = dict.fromkeys(graph, 0.0)
        while stack:
            current = stack.pop()
            if paths[current] > 0:
                ratio = (1.0 + dependency[current]) / paths[current]
                for predecessor in predecessors[current]:
                    dependency[predecessor] += paths[predecessor] * ratio
            if current != source:
                result[current] += dependency[current]
    return {node: score / 2.0 for node, score in result.items()}


def _components(graph: dict[str, set[str]]) -> dict[str, int]:
    components: dict[str, int] = {}
    component = 0
    for node in sorted(graph):
        if node in components:
            continue
        queue = deque([node])
        while queue:
            current = queue.popleft()
            if current in components:
                continue
            components[current] = component
            queue.extend(sorted(graph[current] - set(components)))
        component += 1
    return components


def _leiden(graph: dict[str, set[str]], *, seed: int) -> dict[str, int]:
    """Run seeded Leiden modularity partitioning over one case edge snapshot."""
    nodes = sorted(graph)
    by_node = {node: index for index, node in enumerate(nodes)}
    pairs = sorted(
        {
            tuple(sorted((by_node[left], by_node[right])))
            for left, neighbors in graph.items()
            for right in neighbors
        }
    )
    snapshot = igraph.Graph(n=len(nodes), edges=pairs, directed=False)
    partition = leidenalg.find_partition(snapshot, leidenalg.ModularityVertexPartition, seed=seed)
    communities: dict[str, int] = {}
    for community_id, members in enumerate(partition):
        for member in members:
            communities[nodes[member]] = community_id
    return communities


def detect_communication_transfer_movement_motifs(
    edges: tuple[GraphEdgeSnapshot, ...], *, window_seconds: int = 3_600
) -> tuple[MotifMatch, ...]:
    """Find ordered call -> transfer -> movement/meeting chains in one case.

    All three source-backed events must have bounded timestamps and share a
    participant through their edge endpoints.  A match is review guidance,
    never a conclusion about a real-world network or criminality.
    """
    _validate(edges)
    if not edges:
        return ()
    ordered = sorted(
        (edge for edge in edges if edge.event_start is not None and edge.event_end is not None),
        key=lambda edge: (edge.event_start, str(edge.event_id)),
    )
    calls = [edge for edge in ordered if edge.event_kind == "cdr_call"]
    transfers = [edge for edge in ordered if edge.event_kind == "financial_transaction"]
    movements = [edge for edge in ordered if edge.event_kind in {"movement", "meeting", "sighting"}]
    matches: list[MotifMatch] = []
    for call in calls:
        for transfer in transfers:
            if call.event_start is None or call.event_end is None or transfer.event_start is None:
                continue
            if not 0 <= (transfer.event_start - call.event_end).total_seconds() <= window_seconds:
                continue
            if not {call.left_id, call.right_id} & {transfer.left_id, transfer.right_id}:
                continue
            for movement in movements:
                if (
                    transfer.event_end is None
                    or movement.event_start is None
                    or movement.event_end is None
                ):
                    continue
                if (
                    not 0
                    <= (movement.event_start - transfer.event_end).total_seconds()
                    <= window_seconds
                ):
                    continue
                if not {transfer.left_id, transfer.right_id} & {
                    movement.left_id,
                    movement.right_id,
                }:
                    continue
                observations = tuple(
                    sorted(
                        set(call.evidence_observation_ids)
                        | set(transfer.evidence_observation_ids)
                        | set(movement.evidence_observation_ids),
                        key=str,
                    )
                )
                matches.append(
                    MotifMatch(
                        case_id=call.case_id,
                        motif_id=canonical_sha256(
                            {
                                "events": [
                                    str(call.event_id),
                                    str(transfer.event_id),
                                    str(movement.event_id),
                                ]
                            }
                        ),
                        event_ids=(call.event_id, transfer.event_id, movement.event_id),
                        supporting_observation_ids=observations,
                        temporal_window_start=call.event_start,
                        temporal_window_end=movement.event_end,
                        explanation=(
                            "Review-only evidence-backed temporal chain: communication, "
                            "then transfer, then movement/meeting."
                        ),
                    )
                )
    return tuple(matches)


def analyse(edges: tuple[GraphEdgeSnapshot, ...], *, seed: int = 0) -> tuple[AnalyticsResult, ...]:
    _validate(edges)
    if not edges:
        return ()
    case_id = edges[0].case_id
    graph = _adjacency(edges)
    nodes = sorted(graph)
    snapshot_hash = canonical_sha256(
        {"edges": {str(index): edge.model_dump(mode="json") for index, edge in enumerate(edges)}}
    )
    config_hash = canonical_sha256({"version": ANALYTICS_VERSION, "seed": seed})
    page_rank = {node: 1.0 / len(nodes) for node in nodes}
    for _ in range(30):
        page_rank = {
            node: 0.15 / len(nodes)
            + 0.85 * sum(page_rank[other] / len(graph[other]) for other in graph[node])
            for node in nodes
        }
    components = _components(graph)
    betweenness = _betweenness(graph)
    leiden = _leiden(graph, seed=seed)
    values: dict[str, float | int | str] = {
        f"pagerank:{node}": score for node, score in page_rank.items()
    }
    values.update({f"wcc:{node}": value for node, value in components.items()})
    values.update({f"betweenness:{node}": score for node, score in betweenness.items()})
    values.update({f"leiden:{node}": value for node, value in leiden.items()})
    return (
        AnalyticsResult(
            case_id=case_id,
            algorithm="pagerank_betweenness_wcc_leiden",
            configuration_hash=config_hash,
            graph_snapshot_hash=snapshot_hash,
            run_at=datetime.now(UTC),
            values=values,
        ),
    )
