import { useEffect, useRef, useState } from "react";
import { api, type GraphResponse } from "../lib/api";
import { NeoCard } from "./primitives";
import { graphWindow, mergeGraphPage, MAX_LOADED_EDGES, MAX_LOADED_NODES, RENDER_NODES } from "../lib/graphWindow";

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
  const [visualPage, setVisualPage] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const capped = !!graph && (graph.nodes.length >= MAX_LOADED_NODES || graph.edges.length >= MAX_LOADED_EDGES);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const next = await api.getGraph(caseId, seed, 2, pageSize, Math.min(3000, pageSize * 2), graph?.cursor ?? undefined,
        graph?.graph_snapshot_id ?? graphSnapshotId);
      if (!active.current) return;
      setGraph(mergeGraphPage(graph, next));
    } catch (err) { if (active.current) setError(String(err)); }
    finally { if (active.current) setLoading(false); }
  }

  return <NeoCard>
    <h2>Evidence neighborhood</h2>
    <p className="coverage-note">Transaction, output, address and relay associations within two steps. Network observations describe relay context.</p>
    {error && <p role="alert">{error}</p>}
    {graph && <p>{graph.nodes.length} nodes · {graph.edges.length} edges loaded · {graph.truncated_nodes} nodes and {graph.truncated_edges} edges remaining</p>}
    {graph && graph.nodes.length > 0 && <>
      <NeighborhoodVisual graph={graph} page={visualPage} selected={selected} onSelect={setSelected} />
      <button type="button" disabled={visualPage === 0} onClick={() => setVisualPage(visualPage - 1)}>Previous visual window</button>
      <button type="button" disabled={(visualPage + 1) * RENDER_NODES >= graph.nodes.length} onClick={() => setVisualPage(visualPage + 1)}>Next visual window</button>
      <p className="coverage-note">At most {RENDER_NODES} nodes are drawn per window; {MAX_LOADED_NODES} nodes/{MAX_LOADED_EDGES} edges retained. Edges across windows are not drawn, not assumed absent.</p>
      {selected && <pre>{JSON.stringify(graph.nodes.find((node) => node.id === selected), null, 2)}</pre>}
    </>}
    {graph && graph.nodes.length > 0 && <details>
      <summary>Inspect loaded nodes and associations</summary>
      <ul>{graphWindow(graph, visualPage).nodes.map((node) => <li key={node.id}>{node.type}: {node.label} <code>{node.id}</code></li>)}</ul>
      <ul>{graphWindow(graph, visualPage).edges
        .map((edge) => <li key={edge.id}><code>{edge.from}</code> → <code>{edge.to}</code> ({edge.type})</li>)}</ul>
    </details>}
    {(!graph || graph.cursor) && <button type="button" className="btn-secondary" disabled={loading || capped} onClick={() => void load()}>
      {loading ? "Loading…" : graph ? "Load more neighborhood" : "Load neighborhood"}
    </button>}
    {capped && <p role="status">Neighborhood display cap reached. Narrow the seed/page size or reset; the whole case is not reconstructed.</p>}
    {graph && <button type="button" disabled={loading} onClick={() => { setGraph(null); setVisualPage(0); setSelected(null); }}>Reset neighborhood</button>}
    {graph && !graph.nodes.length && <p className="coverage-note">No committed node matches this source in the selected snapshot.</p>}
  </NeoCard>;
}

function NeighborhoodVisual({ graph, page, selected, onSelect }: { graph: GraphResponse; page: number;
  selected: string | null; onSelect: (id: string) => void }) {
  const view = graphWindow(graph, page);
  const positions = new Map(view.nodes.map((node, index) => [node.id, { x: 80 + index % 5 * 170, y: 45 + Math.floor(index / 5) * 65 }]));
  return <div style={{ maxHeight: 420, overflow: "auto" }}>
    <p>{view.nodes.length} visual nodes · {view.edges.length} associations · {view.hiddenEdges} edges outside this window</p>
    <svg role="img" aria-label="Bounded evidence association graph" viewBox={`0 0 880 ${Math.max(120, Math.ceil(view.nodes.length / 5) * 65)}`}>
      {view.edges.map((edge) => { const from = positions.get(edge.from)!; const to = positions.get(edge.to)!;
        return <line key={edge.id} x1={from.x} y1={from.y} x2={to.x} y2={to.y} stroke="#8a8474"><title>{edge.type}: {edge.from} → {edge.to}</title></line>; })}
      {view.nodes.map((node) => { const point = positions.get(node.id)!;
        return <g key={node.id} role="button" tabIndex={0} aria-label={`Inspect ${node.id}`} onClick={() => onSelect(node.id)}
          onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onSelect(node.id); }}>
          <circle cx={point.x} cy={point.y} r={14} fill={selected === node.id ? "#e8a935" : "#5f8fa3"} />
          <text x={point.x} y={point.y + 28} textAnchor="middle" fill="currentColor" fontSize={10}>{node.type}: {node.label.slice(0, 18)}</text>
          <title>{node.id}</title>
        </g>; })}
    </svg>
  </div>;
}
