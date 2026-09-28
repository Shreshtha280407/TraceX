import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, EmptyState } from "../components/primitives";
import { api, ApiError, type GraphResponse, type Finding } from "../lib/api";
import { computeLayout } from "../graph/forceLayout";
import "./GraphExplorer.css";

const EDGE_COLOR: Record<string, string> = {
  SPENT_BY: "var(--danger)",
  CREATES_OUTPUT: "var(--info)",
  OBSERVED_TX: "var(--mustard)",
  LOCKED_TO: "var(--ink-mute)",
  POSSIBLE_COMMON_CONTROL: "var(--ink-mute)",
};

const WIDTH = 760;
const HEIGHT = 560;

export function GraphExplorer() {
  const { caseId } = useParams<{ caseId: string }>();
  const [searchParams] = useSearchParams();
  const [seedInput, setSeedInput] = useState(searchParams.get("seed") ?? "");
  const [seed, setSeed] = useState(searchParams.get("seed") ?? "");
  const [depth, setDepth] = useState(2);
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [openFindings, setOpenFindings] = useState<Finding[]>([]);

  useEffect(() => {
    if (!caseId) return;
    api.listFindings(caseId, 200, 0).then((r) => setOpenFindings(r.findings.filter((f) => f.status === "open")));
  }, [caseId]);

  useEffect(() => {
    if (!caseId || !seed) return;
    setError(null);
    setNotFound(false);
    api
      .getGraph(caseId, seed, depth, 200, 500)
      .then((result) => {
        setGraph(result);
        setSelectedId(result.nodes.find((n) => n.id === seed)?.id ?? result.nodes[0]?.id ?? null);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 409) setNotFound(true);
        else setError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend.");
      });
  }, [caseId, seed, depth]);

  const positions = useMemo(() => {
    if (!graph) return new Map();
    return computeLayout(
      graph.nodes.map((n) => n.id),
      graph.edges.map((e) => ({ from: e.from, to: e.to })),
      WIDTH,
      HEIGHT
    );
  }, [graph]);

  const flaggedIds = useMemo(() => new Set(openFindings.map((f) => f.entity_ref)), [openFindings]);
  const degreeOf = (id: string) => graph?.edges.filter((e) => e.from === id || e.to === id).length ?? 0;
  const selectedNode = graph?.nodes.find((n) => n.id === selectedId) ?? null;
  const selectedFinding = selectedNode ? openFindings.find((f) => f.entity_ref === selectedNode.id) : undefined;

  const coverage = graph?.coverage as
    | { missing_outpoint_inputs?: number; unknown_prior_output_inputs?: number; spend_lineage_complete?: boolean; value_violations?: number }
    | undefined;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>UTXO Graph Explorer</h1>
          <p className="subtitle">Transaction-as-node graph — bounded query {graph ? `· snapshot ${graph.snapshot_id}` : ""}</p>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="graph-layout">
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <NeoCard variant="neo-sm">
            <h2>Seed</h2>
            <div className="form-field">
              <label>Seed node ID</label>
              <input
                value={seedInput}
                onChange={(e) => setSeedInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && setSeed(seedInput.trim())}
                placeholder="address:bcrt1q… or tx:…"
              />
            </div>
            <button type="button" className="btn-ghost" onClick={() => setSeed(seedInput.trim())} disabled={!seedInput.trim()}>
              Load
            </button>
            <div className="form-field" style={{ marginTop: 14 }}>
              <label>Depth</label>
              <div className="pill-row">
                {[1, 2, 3].map((d) => (
                  <button key={d} type="button" className={`pill ${depth === d ? "active" : ""}`} onClick={() => setDepth(d)}>
                    {d}
                  </button>
                ))}
              </div>
            </div>
          </NeoCard>

          {graph && (
            <NeoCard variant="neo-sm">
              <h2>Counters</h2>
              <div className="kv-row"><span className="k">Nodes shown</span><span>{graph.nodes.length} / 200</span></div>
              <div className="kv-row"><span className="k">Edges shown</span><span>{graph.edges.length} / 500</span></div>
              <div className="kv-row"><span className="k">Truncated nodes</span><span>{graph.truncated_nodes}</span></div>
              <div className="kv-row"><span className="k">Truncated edges</span><span>{graph.truncated_edges}</span></div>
            </NeoCard>
          )}

          <NeoCard variant="neo-sm">
            <h2>Edge Legend</h2>
            <div className="legend-row"><span className="legend-swatch" style={{ background: EDGE_COLOR.SPENT_BY }} /> SPENT_BY</div>
            <div className="legend-row"><span className="legend-swatch" style={{ background: EDGE_COLOR.CREATES_OUTPUT }} /> CREATES</div>
            <div className="legend-row"><span className="legend-swatch" style={{ background: EDGE_COLOR.OBSERVED_TX }} /> OBSERVED_TX</div>
            <div className="legend-row"><span className="legend-swatch dashed" /> POSSIBLE_COMMON_CONTROL</div>
          </NeoCard>

          {coverage && coverage.spend_lineage_complete === false && (
            <div className="notice-banner coverage-note">
              No prevout IDs on {(coverage.missing_outpoint_inputs ?? 0) + (coverage.unknown_prior_output_inputs ?? 0)} source(s) —
              spend lineage is incomplete for this snapshot; edges fall back to participation-only where a spend link
              couldn't be verified.
            </div>
          )}
        </div>

        <NeoCard className="graph-canvas-wrap">
          {!seed ? (
            <EmptyState title="No seed selected" body="Enter a node ID (address:… or tx:…) and press Load, or open this page from a finding." />
          ) : notFound ? (
            <EmptyState title="Graph not built yet" body="No completed graph snapshot is available for this case — run an import first." />
          ) : !graph ? (
            <p className="coverage-note" style={{ padding: 20 }}>Loading…</p>
          ) : graph.nodes.length === 0 ? (
            <EmptyState title="Seed not found in this snapshot" body="That node ID doesn't appear in the current graph snapshot." />
          ) : (
            <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`}>
              {graph.edges.map((edge) => {
                const from = positions.get(edge.from);
                const to = positions.get(edge.to);
                if (!from || !to) return null;
                const dashed = edge.type === "POSSIBLE_COMMON_CONTROL";
                return (
                  <line
                    key={edge.id}
                    x1={from.x} y1={from.y} x2={to.x} y2={to.y}
                    stroke={EDGE_COLOR[edge.type] ?? "var(--ink-mute)"}
                    strokeWidth={1.4}
                    strokeDasharray={dashed ? "4 4" : undefined}
                    opacity={0.7}
                  />
                );
              })}
              {graph.nodes.map((node) => {
                const pos = positions.get(node.id);
                if (!pos) return null;
                const isSeed = node.id === seed;
                const isFlagged = flaggedIds.has(node.id);
                const cls = isFlagged ? "node-flag" : isSeed ? "node-seed" : "node-normal";
                return (
                  <g key={node.id} onClick={() => setSelectedId(node.id)} style={{ cursor: "pointer" }}>
                    <circle
                      cx={pos.x} cy={pos.y} r={isSeed ? 12 : 8}
                      className={`${cls} ${selectedId === node.id ? "node-selected" : ""}`}
                    />
                    <text x={pos.x} y={pos.y + 22} textAnchor="middle" fontSize={9} fill="var(--ink-mute)">
                      {node.label.slice(0, 10)}
                    </text>
                  </g>
                );
              })}
            </svg>
          )}
        </NeoCard>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <NeoCard variant="neo-sm">
            <h2>Selected Node</h2>
            {!selectedNode ? (
              <p className="coverage-note">Click a node to inspect it.</p>
            ) : (
              <>
                <p className="mono-id" style={{ wordBreak: "break-all" }}>{selectedNode.id}</p>
                {selectedFinding && <Badge tone="danger">{selectedFinding.finding_type}</Badge>}
                <div className="kv-row"><span className="k">Accounting check</span><span>{coverage?.value_violations === 0 ? "PASS" : coverage?.value_violations ? "FAIL" : "—"}</span></div>
                <div className="kv-row"><span className="k">Degree (local)</span><span>{degreeOf(selectedNode.id)}</span></div>
                <div className="kv-row"><span className="k">Block time</span><span>{String(selectedNode.attributes.block_time ?? "—")}</span></div>
                <div className="kv-row"><span className="k">Observer receive</span><span>—</span></div>
                <div className="kv-row"><span className="k">Ingestion time</span><span>—</span></div>
                <p className="coverage-note" style={{ marginTop: 10 }}>
                  {selectedFinding
                    ? `Flagged by ${selectedFinding.finding_type} — reasoning available on the Evidence Package for this finding.`
                    : "This node has no open finding attached in the current case."}
                </p>
              </>
            )}
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}
