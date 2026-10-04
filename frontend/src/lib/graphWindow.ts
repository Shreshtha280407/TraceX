import type { GraphResponse } from "./api";

export const MAX_LOADED_NODES = 500;
export const MAX_LOADED_EDGES = 1000;
export const RENDER_NODES = 100;

/** Match the graph API's seed normalization, not a label/ownership guess. */
export function graphSourceId(seed: string) {
  return /^[0-9a-f]{64}$/i.test(seed) ? `tx:${seed.toLowerCase()}` : seed;
}

export function mergeGraphPage(previous: GraphResponse | null, next: GraphResponse): GraphResponse {
  if (previous && previous.graph_snapshot_id !== next.graph_snapshot_id) throw new Error("Snapshot changed; reset the graph");
  return { ...next,
    nodes: [...new Map([...(previous?.nodes ?? []), ...next.nodes].map((node) => [node.id, node])).values()].slice(0, MAX_LOADED_NODES),
    edges: [...new Map([...(previous?.edges ?? []), ...next.edges].map((edge) => [edge.id, edge])).values()].slice(0, MAX_LOADED_EDGES) };
}

export function graphWindow(graph: GraphResponse, page: number) {
  const nodes = graph.nodes.slice(page * RENDER_NODES, (page + 1) * RENDER_NODES);
  const ids = new Set(nodes.map((node) => node.id));
  return { nodes, edges: graph.edges.filter((edge) => ids.has(edge.from) && ids.has(edge.to)),
    hiddenEdges: graph.edges.filter((edge) => !ids.has(edge.from) || !ids.has(edge.to)).length };
}
