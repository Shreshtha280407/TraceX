import { useEffect, useRef, useState } from "react";
import { api, type GraphResponse } from "../lib/api";
import { NeoCard } from "./primitives";
import { GraphIcon } from "./icons";
import { graphSourceId, graphWindow, mergeGraphPage, MAX_LOADED_EDGES, MAX_LOADED_NODES, RENDER_NODES } from "../lib/graphWindow";
import "./GraphContinuation.css";

export function GraphContinuation({ caseId, seed, graphSnapshotId }: { caseId: string; seed: string; graphSnapshotId?: string }) {
  const [pageSize, setPageSize] = useState(100);
  return <Neighborhood key={`${caseId}:${seed}:${graphSnapshotId ?? "latest"}:${pageSize}`} caseId={caseId} seed={seed}
    graphSnapshotId={graphSnapshotId} pageSize={pageSize} onPageSizeChange={setPageSize} />;
}

function Neighborhood({ caseId, seed, graphSnapshotId, pageSize, onPageSizeChange }: {
  caseId: string; seed: string; graphSnapshotId?: string; pageSize: number; onPageSizeChange: (value: number) => void;
}) {
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [visualPage, setVisualPage] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const capped = !!graph && (graph.nodes.length >= MAX_LOADED_NODES || graph.edges.length >= MAX_LOADED_EDGES);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const selectedNode = graph?.nodes.find((node) => node.id === selected);
  const windows = graph ? Math.ceil(graph.nodes.length / RENDER_NODES) : 0;
  const sourceId = graphSourceId(seed);
  const sourceIndex = graph?.nodes.findIndex((node) => node.id === sourceId) ?? -1;
  const sourceWindow = Math.floor(sourceIndex / RENDER_NODES);

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

  return <NeoCard variant="neo-sm" className="neighborhood-card">
    <div className="neighborhood-header">
      <div className="neighborhood-heading">
        <span className="neighborhood-icon" aria-hidden="true"><GraphIcon /></span>
        <div><h2>Evidence neighborhood</h2><p>Explore associations within two graph steps</p>
          <div className="neighborhood-source-label">Source <code title={sourceId}>{sourceId.length > 30 ? `${sourceId.slice(0, 18)}…${sourceId.slice(-8)}` : sourceId}</code></div>
        </div>
      </div>
      <div className="neighborhood-actions">
        <label className="neighborhood-size">Page size
          <input className="inline-control" aria-label="Neighborhood page size" type="number" min={1} max={1000}
            value={pageSize} disabled={loading} onChange={(event) => {
              const next = Number(event.target.value);
              if (Number.isFinite(next)) onPageSizeChange(Math.max(1, Math.min(1000, Math.floor(next))));
            }} />
        </label>
        {(!graph || graph.cursor) && <button type="button" className="btn-ghost neighborhood-load" disabled={loading || capped} onClick={() => void load()}>
          {loading ? "Loading…" : graph ? "Load more neighborhood" : "Load neighborhood"}
        </button>}
        {graph && <button type="button" className="btn-ghost neighborhood-reset" disabled={loading}
          onClick={() => { setGraph(null); setVisualPage(0); setSelected(null); }}>Reset neighborhood</button>}
      </div>
    </div>
    {error && <div className="error-banner neighborhood-error" role="alert">{error}</div>}
    {!graph && <div className="neighborhood-empty" aria-busy={loading}>
      <span className="neighborhood-icon" aria-hidden="true"><GraphIcon /></span>
      <div><strong>{loading ? "Loading source connections…" : "Explore this source’s connections"}</strong>
        <p>Load a bounded view of transactions, outputs, addresses and relays. Nothing is loaded until you request it.</p></div>
    </div>}
    {graph && <p className="neighborhood-summary" aria-live="polite">
      {graph.nodes.length} nodes · {graph.edges.length} edges loaded
      <span>{graph.truncated_nodes} nodes and {graph.truncated_edges} edges remaining</span>
    </p>}
    {graph && graph.nodes.length > 0 && <>
      <NeighborhoodVisual graph={graph} page={visualPage} sourceId={sourceId} selected={selected} onSelect={setSelected} />
      <div className="neighborhood-pagination">
        <span>Visual window {visualPage + 1} of {windows}</span>
        <div>
          {sourceIndex >= 0 && sourceWindow !== visualPage && <button type="button" className="btn-ghost neighborhood-load"
            onClick={() => setVisualPage(sourceWindow)}>Show source</button>}
          <button type="button" className="btn-ghost" disabled={visualPage === 0} onClick={() => setVisualPage(visualPage - 1)}>Previous visual window</button>
          <button type="button" className="btn-ghost" disabled={(visualPage + 1) * RENDER_NODES >= graph.nodes.length} onClick={() => setVisualPage(visualPage + 1)}>Next visual window</button>
        </div>
      </div>
      {selectedNode && <div className="neighborhood-selection">
        <div><strong>Selected {selectedNode.id === sourceId ? "source " : ""}{selectedNode.type.replaceAll("_", " ")}</strong>
          <button type="button" className="btn-ghost" onClick={() => setSelected(null)}>Clear selection</button></div>
        <code>{selectedNode.id}</code>
        <details><summary>View stored node details</summary><pre>{JSON.stringify(selectedNode, null, 2)}</pre></details>
      </div>}
    </>}
    {graph && graph.nodes.length > 0 && <details className="neighborhood-inspector">
      <summary>Inspect loaded nodes and associations</summary>
      <ul>{graphWindow(graph, visualPage).nodes.map((node) => <li key={node.id}>{node.type}: {node.label} <code>{node.id}</code></li>)}</ul>
      <ul>{graphWindow(graph, visualPage).edges
        .map((edge) => <li key={edge.id}><code>{edge.from}</code> → <code>{edge.to}</code> ({edge.type})</li>)}</ul>
    </details>}
    {capped && <p className="neighborhood-limit" role="status">Display cap reached. Narrow the source or reset to explore another bounded view.</p>}
    {graph && !graph.nodes.length && <p className="neighborhood-empty">No committed node matches this source in the selected snapshot.</p>}
    <p className="neighborhood-note">Up to {RENDER_NODES} nodes per visual window · {MAX_LOADED_NODES} nodes / {MAX_LOADED_EDGES} edges retained.
      {" "}Source is the exploration center, not a claim of funds’ origin. Cross-window edges are not drawn, not assumed absent. Relay associations do not establish ownership.</p>
  </NeoCard>;
}

function NeighborhoodVisual({ graph, page, sourceId, selected, onSelect }: { graph: GraphResponse; page: number; sourceId: string;
  selected: string | null; onSelect: (id: string) => void }) {
  const view = graphWindow(graph, page);
  const positions = new Map(view.nodes.map((node, index) => [node.id, { x: 80 + index % 5 * 170, y: 45 + Math.floor(index / 5) * 80 }]));
  return <div className="neighborhood-visual">
    <div className="neighborhood-visual-header">
      <span>{view.nodes.length} visual nodes · {view.edges.length} associations</span>
      <span>{view.hiddenEdges} edges outside this window</span>
    </div>
    <div className="neighborhood-canvas">
    <svg role="img" aria-label="Bounded evidence association graph" viewBox={`0 0 880 ${Math.max(150, Math.ceil(view.nodes.length / 5) * 80 + 28)}`}>
      {view.edges.map((edge) => { const from = positions.get(edge.from)!; const to = positions.get(edge.to)!;
        return <line key={edge.id} className="neighborhood-edge" x1={from.x} y1={from.y} x2={to.x} y2={to.y}><title>{edge.type}: {edge.from} → {edge.to}</title></line>; })}
      {view.nodes.map((node) => { const point = positions.get(node.id)!;
        const isSource = node.id === sourceId;
        return <g key={node.id} className={`neighborhood-node ${isSource ? "source" : ""} ${selected === node.id ? "selected" : ""}`} data-kind={node.type}
          role="button" tabIndex={0} aria-pressed={selected === node.id} aria-label={`${isSource ? "Source node; " : ""}Inspect ${node.id}`} onClick={() => onSelect(node.id)}
          onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(node.id); } }}>
          {isSource && <g className="neighborhood-source-badge" aria-hidden="true">
            <rect x={point.x - 28} y={point.y - 39} width={56} height={18} rx={6} />
            <text x={point.x} y={point.y - 27} textAnchor="middle">SOURCE</text>
          </g>}
          <circle cx={point.x} cy={point.y} r={isSource ? 18 : 14} />
          <text className="neighborhood-node-kind" x={point.x} y={point.y + 28} textAnchor="middle" fontSize={10}>{node.type.replaceAll("_", " ")}</text>
          <text className="neighborhood-node-label" x={point.x} y={point.y + 42} textAnchor="middle" fontSize={11}>
            {node.label.length > 20 ? `${node.label.slice(0, 9)}…${node.label.slice(-6)}` : node.label}
          </text>
          <title>{node.type}: {node.label} ({node.id})</title>
        </g>; })}
    </svg>
    </div>
  </div>;
}
