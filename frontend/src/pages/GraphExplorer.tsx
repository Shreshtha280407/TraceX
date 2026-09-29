import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, EmptyState, FilterPills } from "../components/primitives";
import { api, ApiError, ML_RULE_VERSION, type GraphResponse, type Finding } from "../lib/api";
import { computeLayout, type LayoutPoint } from "../graph/forceLayout";
import "./GraphExplorer.css";

type BrowseFilterId = "all" | "deterministic" | "ml";

const EDGE_COLOR: Record<string, string> = {
  SPENT_BY: "var(--danger)",
  CREATES_OUTPUT: "var(--info)",
  OBSERVED_TX: "var(--mustard)",
  LOCKED_TO: "var(--ink-mute)",
  POSSIBLE_COMMON_CONTROL: "var(--ink-mute)",
};

const EDGE_MARKER: Record<string, string> = {
  SPENT_BY: "url(#arrow-input)",
  CREATES_OUTPUT: "url(#arrow-output)",
  OBSERVED_TX: "url(#arrow-observed)",
};

// Widescreen viewBox so the canvas genuinely fills the middle column instead of
// letterboxing inside a near-square SVG aspect ratio.
const WIDTH = 1040;
const HEIGHT = 600;
// Backend hard-clamps depth to 1..5 (app/api/routes.py) -- each extra BFS hop
// multiplies the frontier before node_limit/edge_limit cut it off, so this isn't
// an arbitrary UI choice, it's the real ceiling the API enforces.
const MAX_DEPTH = 5;
const ZOOM_MIN_W = WIDTH * 0.22;
const ZOOM_MAX_W = WIDTH * 2.4;

type ViewBox = { x: number; y: number; w: number; h: number };
const DEFAULT_VIEWBOX: ViewBox = { x: 0, y: 0, w: WIDTH, h: HEIGHT };

/** Short, collision-resistant label for canvas text. Output-node labels carry
 * their disambiguating ":<vout>" as a suffix, so a naive front slice(0, n) always
 * cuts it off and makes a transaction and every one of its own outputs render
 * identically. Keep a short head plus whatever suffix distinguishes the id. */
function shortLabel(label: string): string {
  if (label.length <= 14) return label;
  const suffixMatch = label.match(/:(\d+)$/);
  if (suffixMatch) {
    const base = label.slice(0, label.length - suffixMatch[0].length);
    return `${base.slice(0, 6)}…:${suffixMatch[1]}`;
  }
  return `${label.slice(0, 6)}…${label.slice(-4)}`;
}

export function GraphExplorer() {
  const { caseId } = useParams<{ caseId: string }>();
  const [searchParams] = useSearchParams();
  const [seedInput, setSeedInput] = useState(searchParams.get("seed") ?? "");
  const [seed, setSeed] = useState(searchParams.get("seed") ?? "");
  const [seedHistory, setSeedHistory] = useState<string[]>([]);
  const [depth, setDepth] = useState(2);
  const [customDepthOpen, setCustomDepthOpen] = useState(false);
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [browseFilter, setBrowseFilter] = useState<BrowseFilterId>("all");
  const [comboOpen, setComboOpen] = useState(false);
  const [viewBox, setViewBox] = useState<ViewBox>(DEFAULT_VIEWBOX);
  const [isDragging, setIsDragging] = useState(false);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragState = useRef({ active: false, lastX: 0, lastY: 0 });
  const unhoverTimer = useRef<number | null>(null);

  /** The "set as source" chip floats a few px away from the node it belongs to, so
   * moving the pointer from one to the other briefly leaves both elements' hit areas.
   * Clearing hoveredId on that exact mouseleave made the chip disappear mid-move,
   * before a click could land on it. A short cancellable delay bridges the gap. */
  function hoverNode(id: string) {
    if (unhoverTimer.current) { window.clearTimeout(unhoverTimer.current); unhoverTimer.current = null; }
    setHoveredId(id);
  }
  function scheduleUnhover(id: string) {
    if (unhoverTimer.current) window.clearTimeout(unhoverTimer.current);
    unhoverTimer.current = window.setTimeout(() => {
      setHoveredId((h) => (h === id ? null : h));
      unhoverTimer.current = null;
    }, 250);
  }

  useEffect(() => {
    if (!caseId) return;
    api.listFindings(caseId, 200, 0).then((r) => setFindings(r.findings));
  }, [caseId]);

  const openFindings = useMemo(() => findings.filter((f) => f.status === "open"), [findings]);

  // Local UI state (seed, loaded graph, browse query) does not automatically
  // clear when :caseId changes on this route -- React Router updates the param
  // without remounting the component. Skip the very first run (caseId going
  // from undefined to its initial value) so a seed passed in via ?seed= from
  // another page survives the initial load; only a genuine case switch clears it.
  const previousCaseId = useRef(caseId);
  useEffect(() => {
    if (previousCaseId.current !== undefined && previousCaseId.current !== caseId) {
      setSeed("");
      setSeedInput("");
      setSeedHistory([]);
      setGraph(null);
      setBrowseFilter("all");
      setComboOpen(false);
      setCustomDepthOpen(false);
    }
    previousCaseId.current = caseId;
  }, [caseId]);

  const browseResults = useMemo(() => {
    let list = findings;
    if (browseFilter === "deterministic") list = list.filter((f) => f.rule_version !== ML_RULE_VERSION);
    else if (browseFilter === "ml") list = list.filter((f) => f.rule_version === ML_RULE_VERSION);
    if (seedInput.trim()) {
      const q = seedInput.trim().toLowerCase();
      list = list.filter((f) => f.entity_ref.toLowerCase().includes(q));
    }
    const byRef = new Map<string, Finding>();
    for (const f of list) {
      const existing = byRef.get(f.entity_ref);
      if (!existing || (f.rank ?? Infinity) < (existing.rank ?? Infinity)) byRef.set(f.entity_ref, f);
    }
    return [...byRef.values()].sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity)).slice(0, 30);
  }, [findings, browseFilter, seedInput]);

  /** Every user-driven jump to a new seed (combobox pick, Load, Enter, or the
   * hover "Set as source" action on a node) funnels through here so the back
   * arrow always has an accurate trail to undo through. */
  function goToSeed(next: string) {
    const trimmed = next.trim();
    if (!trimmed) return;
    setSeedHistory((h) => (seed && seed !== trimmed ? [...h, seed] : h));
    setSeedInput(trimmed);
    setSeed(trimmed);
    setComboOpen(false);
  }

  function goBack() {
    setSeedHistory((h) => {
      if (h.length === 0) return h;
      const prev = h[h.length - 1];
      setSeedInput(prev);
      setSeed(prev);
      return h.slice(0, -1);
    });
  }

  function onComboBlur(event: React.FocusEvent<HTMLDivElement>) {
    if (!event.currentTarget.contains(event.relatedTarget)) setComboOpen(false);
  }

  useEffect(() => {
    if (!caseId || !seed) return;
    setError(null);
    setNotFound(false);
    api
      .getGraph(caseId, seed, depth, 200, 500)
      .then((result) => {
        setGraph(result);
        setSelectedId(result.nodes.find((n) => n.id === seed)?.id ?? result.nodes[0]?.id ?? null);
        setViewBox(DEFAULT_VIEWBOX);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 409) setNotFound(true);
        else setError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend.");
      });
  }, [caseId, seed, depth]);

  const positions = useMemo(() => {
    if (!graph) return new Map<string, LayoutPoint>();
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
  const hoveredPos = hoveredId ? positions.get(hoveredId) : null;

  const coverage = graph?.coverage as
    | { missing_outpoint_inputs?: number; unknown_prior_output_inputs?: number; spend_lineage_complete?: boolean; value_violations?: number }
    | undefined;

  function zoomBy(factor: number) {
    setViewBox((vb) => {
      const newW = Math.min(ZOOM_MAX_W, Math.max(ZOOM_MIN_W, vb.w * factor));
      const newH = newW * (HEIGHT / WIDTH);
      const cx = vb.x + vb.w / 2;
      const cy = vb.y + vb.h / 2;
      return { x: cx - newW / 2, y: cy - newH / 2, w: newW, h: newH };
    });
  }
  const resetView = () => setViewBox(DEFAULT_VIEWBOX);

  function onSvgPointerDown(e: React.PointerEvent<SVGSVGElement>) {
    if (e.target !== e.currentTarget) return; // only pan when grabbing empty canvas, not a node/edge
    dragState.current = { active: true, lastX: e.clientX, lastY: e.clientY };
    setIsDragging(true);
  }
  function onSvgPointerMove(e: React.PointerEvent<SVGSVGElement>) {
    if (!dragState.current.active || !svgRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const scaleX = viewBox.w / rect.width;
    const scaleY = viewBox.h / rect.height;
    const dx = (e.clientX - dragState.current.lastX) * scaleX;
    const dy = (e.clientY - dragState.current.lastY) * scaleY;
    dragState.current.lastX = e.clientX;
    dragState.current.lastY = e.clientY;
    setViewBox((vb) => ({ ...vb, x: vb.x - dx, y: vb.y - dy }));
  }
  function endDrag() {
    dragState.current.active = false;
    setIsDragging(false);
  }

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
            <div className="combobox" onBlur={onComboBlur}>
              <div className="form-field">
                <label>Search or enter an address / txid</label>
                <input
                  value={seedInput}
                  onChange={(e) => setSeedInput(e.target.value)}
                  onFocus={() => setComboOpen(true)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") goToSeed(seedInput);
                    else if (e.key === "Escape") setComboOpen(false);
                  }}
                  placeholder="type to search this case's findings, or paste address:… / tx:…"
                />
              </div>
              {comboOpen && (
                <div className="combobox-panel neo-inset">
                  <FilterPills
                    active={browseFilter}
                    onChange={setBrowseFilter}
                    options={[
                      { id: "all", label: "All" },
                      { id: "deterministic", label: "Deterministic" },
                      { id: "ml", label: "ML-Flagged" },
                    ]}
                  />
                  {browseResults.length === 0 ? (
                    <p className="coverage-note" style={{ marginTop: 8 }}>No matching findings in this case.</p>
                  ) : (
                    <div className="browse-list">
                      {browseResults.map((f) => (
                        <button
                          key={f.entity_ref}
                          type="button"
                          className={`browse-item ${seed === f.entity_ref ? "active" : ""}`}
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() => goToSeed(f.entity_ref)}
                        >
                          <span className="mono-id">{f.entity_ref}</span>
                          <Badge tone={f.rule_version === ML_RULE_VERSION ? "ml" : "deterministic"}>
                            {f.rule_version === ML_RULE_VERSION ? "ml" : "det"}
                          </Badge>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
            <div className="form-field" style={{ marginTop: 14 }}>
              <label>Depth</label>
              <div className="pill-row">
                {[1, 2, 3].map((d) => (
                  <button
                    key={d}
                    type="button"
                    className={`pill ${depth === d && !customDepthOpen ? "active" : ""}`}
                    onClick={() => { setDepth(d); setCustomDepthOpen(false); }}
                  >
                    {d}
                  </button>
                ))}
                <button
                  type="button"
                  className={`pill ${customDepthOpen || depth > 3 ? "active" : ""}`}
                  onClick={() => setCustomDepthOpen(true)}
                >
                  Custom
                </button>
              </div>
              {customDepthOpen && (
                <div className="depth-custom-row">
                  <input
                    type="number"
                    min={1}
                    max={MAX_DEPTH}
                    value={depth}
                    onChange={(e) => {
                      const n = Math.round(Number(e.target.value));
                      if (!Number.isFinite(n)) return;
                      setDepth(Math.min(MAX_DEPTH, Math.max(1, n)));
                    }}
                  />
                  <span className="coverage-note">
                    Capped at {MAX_DEPTH} — each extra hop multiplies how many nodes/edges get pulled in and how long the
                    layout takes to settle.
                  </span>
                </div>
              )}
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
            <div className="legend-row">
              <span className="legend-swatch" style={{ background: EDGE_COLOR.SPENT_BY }} />
              Input <span className="legend-sub">spent by this tx</span>
            </div>
            <div className="legend-row">
              <span className="legend-swatch" style={{ background: EDGE_COLOR.CREATES_OUTPUT }} />
              Output <span className="legend-sub">created by this tx</span>
            </div>
            <div className="legend-row">
              <span className="legend-swatch" style={{ background: EDGE_COLOR.OBSERVED_TX }} />
              Observed <span className="legend-sub">network capture</span>
            </div>
            <div className="legend-row"><span className="legend-swatch dashed" /> Possible common control</div>
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
            <>
              <div className="graph-toolbar graph-toolbar-right">
                {seedHistory.length > 0 && (
                  <button type="button" className="graph-toolbar-back" onClick={goBack} title="Undo — return to the previous source">
                    ← Undo
                  </button>
                )}
                <button type="button" onClick={() => zoomBy(0.8)} title="Zoom in">+</button>
                <button type="button" onClick={() => zoomBy(1.25)} title="Zoom out">−</button>
                <button type="button" onClick={resetView} title="Reset view">⟲</button>
              </div>
              <svg
                ref={svgRef}
                viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
                className={isDragging ? "dragging" : ""}
                onPointerDown={onSvgPointerDown}
                onPointerMove={onSvgPointerMove}
                onPointerUp={endDrag}
                onPointerLeave={endDrag}
              >
                <defs>
                  <marker id="arrow-input" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="5.5" markerHeight="5.5" orient="auto-start-reverse">
                    <path d="M0,0 L10,5 L0,10 z" fill="var(--danger)" />
                  </marker>
                  <marker id="arrow-output" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="5.5" markerHeight="5.5" orient="auto-start-reverse">
                    <path d="M0,0 L10,5 L0,10 z" fill="var(--info)" />
                  </marker>
                  <marker id="arrow-observed" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="5.5" markerHeight="5.5" orient="auto-start-reverse">
                    <path d="M0,0 L10,5 L0,10 z" fill="var(--mustard)" />
                  </marker>
                  <radialGradient id="node-sheen" cx="35%" cy="30%" r="70%">
                    <stop offset="0%" stopColor="rgba(255,255,255,0.55)" />
                    <stop offset="100%" stopColor="rgba(255,255,255,0)" />
                  </radialGradient>
                </defs>

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
                      strokeWidth={1.5}
                      strokeDasharray={dashed ? "4 4" : undefined}
                      markerEnd={EDGE_MARKER[edge.type]}
                      opacity={0.8}
                    />
                  );
                })}
                {graph.nodes.map((node) => {
                  const pos = positions.get(node.id);
                  if (!pos) return null;
                  const isSeed = node.id === seed;
                  const isFlagged = flaggedIds.has(node.id);
                  const cls = isFlagged ? "node-flag" : isSeed ? "node-seed" : "node-normal";
                  const degree = degreeOf(node.id);
                  const radius = isSeed ? 14 : Math.max(6, Math.min(13, 5 + Math.sqrt(degree)));
                  return (
                    <g
                      key={node.id}
                      onClick={() => setSelectedId(node.id)}
                      onMouseEnter={() => hoverNode(node.id)}
                      onMouseLeave={() => scheduleUnhover(node.id)}
                      style={{ cursor: "pointer" }}
                    >
                      {isSeed && <circle cx={pos.x} cy={pos.y} r={radius + 5} className="node-seed-ring" />}
                      <circle
                        cx={pos.x} cy={pos.y} r={radius}
                        className={`graph-node-circle ${cls} ${selectedId === node.id ? "node-selected" : ""}`}
                      />
                      <circle cx={pos.x} cy={pos.y} r={radius} fill="url(#node-sheen)" pointerEvents="none" />
                      <text x={pos.x} y={pos.y + radius + 13} textAnchor="middle" fontSize={9} fill="var(--ink-mute)">
                        {shortLabel(node.label)}
                      </text>
                    </g>
                  );
                })}

                {hoveredId && hoveredPos && hoveredId !== seed && (
                  <g
                    className="node-action-chip"
                    transform={`translate(${Math.min(hoveredPos.x + 12, viewBox.x + viewBox.w - 98)}, ${hoveredPos.y - 18})`}
                    onMouseEnter={() => hoverNode(hoveredId)}
                    onMouseLeave={() => scheduleUnhover(hoveredId)}
                    onClick={(e) => { e.stopPropagation(); goToSeed(hoveredId); }}
                  >
                    <rect className="chip-hit-area" x={-8} y={-16} width={108} height={32} rx={14} />
                    <rect className="chip-bg" x={0} y={-11} width={92} height={22} rx={11} />
                    <text x={46} y={4} textAnchor="middle">Set as source</text>
                  </g>
                )}
              </svg>
            </>
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
                <p className="coverage-note" style={{ marginTop: 10 }}>Hover a node on the canvas to set it as the new source.</p>
              </>
            )}
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}
