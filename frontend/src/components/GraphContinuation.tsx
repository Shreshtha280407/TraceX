import { useEffect, useRef, useState } from "react";
import { api, type GraphResponse } from "../lib/api";
import { NeoCard } from "./primitives";

export function GraphContinuation({ caseId, seed, graphSnapshotId }: { caseId: string; seed: string; graphSnapshotId?: string }) {
  const [pageSize, setPageSize] = useState(100);
  return <div>
    <label>Neighborhood page size <input aria-label="Neighborhood page size" type="number" min={1} max={1000}
      value={pageSize} onChange={(event) => {
        const next = Number(event.target.value);
        if (Number.isFinite(next)) setPageSize(Math.max(1, Math.min(1000, Math.floor(next))));
      }} /></label>
    <Neighborhood key={`${caseId}:${seed}:${graphSnapshotId ?? "latest"}:${pageSize}`} caseId={caseId} seed={seed}
      graphSnapshotId={graphSnapshotId} pageSize={pageSize} />
  </div>;
}

function Neighborhood({ caseId, seed, graphSnapshotId, pageSize }: { caseId: string; seed: string; graphSnapshotId?: string; pageSize: number }) {
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const next = await api.getGraph(caseId, seed, 2, pageSize, Math.min(3000, pageSize * 2), graph?.cursor ?? undefined,
        graph?.graph_snapshot_id ?? graphSnapshotId);
      if (!active.current) return;
      setGraph((previous) => ({ ...next,
        nodes: [...new Map([...(previous?.nodes ?? []), ...next.nodes].map((n) => [n.id, n])).values()],
        edges: [...new Map([...(previous?.edges ?? []), ...next.edges].map((e) => [e.id, e])).values()] }));
    } catch (err) { if (active.current) setError(String(err)); }
    finally { if (active.current) setLoading(false); }
  }

  return <NeoCard>
    <h2>Evidence neighborhood</h2>
    <p className="coverage-note">Transaction, output, address and relay associations within two steps. Network observations describe relay context.</p>
    {error && <p role="alert">{error}</p>}
    {graph && <p>{graph.nodes.length} nodes · {graph.edges.length} edges loaded · {graph.truncated_nodes} nodes and {graph.truncated_edges} edges remaining</p>}
    {graph && graph.nodes.length > 0 && <details>
      <summary>Inspect loaded nodes and associations</summary>
      <ul>{graph.nodes.map((node) => <li key={node.id}>{node.type}: {node.label} <code>{node.id}</code></li>)}</ul>
      <ul>{graph.edges.filter((edge) => graph.nodes.some((n) => n.id === edge.from) && graph.nodes.some((n) => n.id === edge.to))
        .map((edge) => <li key={edge.id}><code>{edge.from}</code> → <code>{edge.to}</code> ({edge.type})</li>)}</ul>
    </details>}
    {(!graph || graph.cursor) && <button type="button" className="btn-secondary" disabled={loading} onClick={() => void load()}>
      {loading ? "Loading…" : graph ? "Load more neighborhood" : "Load neighborhood"}
    </button>}
    {graph && !graph.nodes.length && <p className="coverage-note">No committed node matches this source in the selected snapshot.</p>}
  </NeoCard>;
}
