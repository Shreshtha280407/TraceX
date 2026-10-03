import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, EmptyState, FilterPills } from "../components/primitives";
import { FundFlowGraph, ZOOM_STEPS, type GraphNodeInfo } from "../components/FundFlowGraph";
import { SourceFlowGraph, type FlowSide } from "../components/SourceFlowGraph";
import { GraphContinuation } from "../components/GraphContinuation";
import { api, ApiError, type Finding, type FlowCounterparty, type FlowResponse, type PathSignals } from "../lib/api";
import "./GraphExplorer.css";

// Entities / Clusters / Temporal were removed rather than left as dead tabs:
// entity attribution has no data source in this build, while clustering and
// temporal information are already rendered inside Fund flow itself (dashed
// co-spend cluster boxes and the elapsed timeline), so separate empty views
// would have been navigation noise pointing at capability that is already here.
type TabId = "fund-flow" | "suspicious-path";

const TABS: { id: TabId; label: string }[] = [
  { id: "fund-flow", label: "Fund flow" },
  { id: "suspicious-path", label: "Suspicious path" },
];

// Only these rule_ids ever carry a graph_path (app/engine/motifs/deterministic.py).
// Everything else is a plain address/window observation with no path to draw.
const PATH_RULE_IDS = ["peeling_chain_candidate", "coinjoin_like_structure", "synthetic_seed_proximity"];

const FINDING_TYPE_LABEL: Record<string, string> = {
  peeling_chain_candidate: "Peeling chain",
  coinjoin_like_structure: "Equal-output structuring",
  synthetic_seed_proximity: "Synthetic seed proximity",
};

function humanizeFindingType(ruleId: string): string {
  return FINDING_TYPE_LABEL[ruleId] ?? ruleId.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function humanizeReasonCode(code: string): string {
  return code.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Risk comes from the detector's OWN score, not the case-wide rank percentile.
 * Rank orders every rule type together even though their raw_score scales
 * genuinely differ, which produced the incoherent "Confidence 1.00 / LOW
 * PRIORITY" pairing on a maximal-confidence chain. */
/** Calibrated probabilities are never 0 or 1; say so at the extremes. */
function formatConfidence(value: number): string {
  if (value >= 0.995) return ">99%";
  if (value > 0 && value < 0.005) return "<1%";
  return `${Math.round(value * 100)}%`;
}

function riskTier(score: number): { label: string; tone: "danger" | "warning" | "muted" } {
  if (score >= 0.75) return { label: "HIGH RISK", tone: "danger" };
  if (score >= 0.5) return { label: "MEDIUM RISK", tone: "warning" };
  return { label: "LOW RISK", tone: "muted" };
}

function btc(satoshis: number | null | undefined): string {
  if (satoshis === null || satoshis === undefined) return "—";
  const v = satoshis / 1e8;
  if (v > 0 && v < 0.01) return `${v.toFixed(6).replace(/0+$/, "").replace(/\.$/, "")} BTC`;
  return `${v.toFixed(2)} BTC`;
}

function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || totalSeconds <= 0) return "—";
  const s = Math.round(totalSeconds);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} hr ${m % 60} min` : `${h} hr`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}

function shortId(id: string): string {
  const bare = id.includes(":") ? id.split(":").slice(1).join(":") : id;
  return bare.length <= 14 ? bare : `${bare.slice(0, 6)}…${bare.slice(-4)}`;
}

function nodeKind(type: string): string {
  if (type === "transaction") return "Transaction";
  if (type === "output") return "Output";
  return "Address";
}

function stampUtc(iso: string | null | undefined): string {
  if (!iso) return "not available";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "not available" : `${d.toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

type TrailEntry = { id: string; label: string; type: string };

function pathCode(finding: Finding): string {
  return `P-${String(finding.rank ?? 0).padStart(2, "0")}`;
}

function SignalBar({ label, value }: { label: string; value: number | null | undefined }) {
  const pct = value === null || value === undefined ? null : Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="ff-signal">
      <span className="ff-signal-label">{label}</span>
      <span className="ff-signal-track">
        <span className="ff-signal-fill" style={{ width: `${pct ?? 0}%`, opacity: pct === null ? 0.25 : 1 }} />
      </span>
      <span className="ff-signal-value">{pct === null ? "n/a" : (pct / 100).toFixed(2)}</span>
    </div>
  );
}

export function GraphExplorer() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [pathFindings, setPathFindings] = useState<Finding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>("fund-flow");
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  const [signals, setSignals] = useState<PathSignals | null>(null);
  const [pinned, setPinned] = useState<Set<string>>(new Set());
  const [unresolvedSeed, setUnresolvedSeed] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [zoomIdx, setZoomIdx] = useState(3);
  const [node, setNode] = useState<GraphNodeInfo | null>(null);
  const [sourceNodeId, setSourceNodeId] = useState<string | null>(null);
  const [sourceNote, setSourceNote] = useState<string | null>(null);
  const [sourceNoteTone, setSourceNoteTone] = useState<"info" | "danger">("info");
  // Source-centred flow view ("set as source"): the node's direct inputs and
  // outputs, fetched live from the case graph snapshot.
  const [flow, setFlow] = useState<FlowResponse | null>(null);
  const [flowLoading, setFlowLoading] = useState(false);
  const [trail, setTrail] = useState<TrailEntry[]>([]);
  const [flowPick, setFlowPick] = useState<{ c: FlowCounterparty; side: FlowSide } | null>(null);
  const [traceQuery, setTraceQuery] = useState("");
  // Only the latest "set as source" request may land: re-centring twice in a
  // row must not let a slower, older response overwrite the newer source.
  const flowTicket = useRef(0);

  useEffect(() => {
    if (!caseId) return;
    setFindings(null);
    setSelectedFindingId(null);
    setFlow(null);
    setTrail([]);
    setFlowPick(null);
    setSourceNote(null);
    api
      .listFindings(caseId, 200, 0)
      .then((r) => setFindings(r.findings))
      .catch((err) => setError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend."));
  }, [caseId]);

  // `findings` is the top-200 by case-wide rank, but rank is one ordering across
  // every rule type and their raw_score scales genuinely differ -- a real peeling
  // chain can sit far past position 200. Fetch path-shaped rule_ids directly.
  useEffect(() => {
    if (!caseId) return;
    setPathFindings(null);
    api
      .listFindings(caseId, 200, 0, PATH_RULE_IDS)
      .then((r) =>
        setPathFindings(
          r.findings.filter((f) => f.graph_path && f.status === "open").sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity))
        )
      )
      .catch(() => setPathFindings([]));
  }, [caseId]);

  // Other pages link here with ?seed=<entity_ref>, which may name a finding that
  // has no path at all. Resolve once both fetches land; otherwise open on the
  // top-ranked real path so Fund Flow is never blank for no reason.
  useEffect(() => {
    if (!findings || !pathFindings || selectedFindingId) return;
    const seed = searchParams.get("seed");
    if (seed) {
      const match = [...pathFindings, ...findings].find((f) => f.entity_ref === seed);
      if (match?.graph_path) {
        setSelectedFindingId(match.finding_id);
        return;
      }
      if (match) {
        setUnresolvedSeed(seed);
        return;
      }
    }
    if (pathFindings.length > 0) setSelectedFindingId(pathFindings[0].finding_id);
  }, [findings, pathFindings, searchParams, selectedFindingId]);

  const selected = useMemo(
    () => pathFindings?.find((f) => f.finding_id === selectedFindingId) ?? findings?.find((f) => f.finding_id === selectedFindingId) ?? null,
    [pathFindings, findings, selectedFindingId]
  );

  useEffect(() => {
    setSignals(null);
    setNode(null);
    if (!selected || selected.rule_id !== "peeling_chain_candidate") return;
    api.getPathSignals(selected.finding_id).then(setSignals).catch(() => undefined);
  }, [selected]);

  const filteredPaths = useMemo(() => {
    const list = pathFindings ?? [];
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter(
      (f) =>
        f.entity_ref.toLowerCase().includes(q) ||
        pathCode(f).toLowerCase().includes(q) ||
        humanizeFindingType(f.rule_id).toLowerCase().includes(q) ||
        (f.steps ?? []).some((s) => s.spending_transaction_id.toLowerCase().includes(q))
    );
  }, [pathFindings, query]);

  function selectPath(findingId: string) {
    setSelectedFindingId(findingId);
    setUnresolvedSeed(null);
    setSourceNote(null);
    setFlow(null);
    setTrail([]);
    setFlowPick(null);
    setActiveTab("fund-flow");
  }

  function refuse(message: string) {
    setSourceNote(message);
    setSourceNoteTone("danger");
  }

  /** Re-root the canvas on a node: fetch its direct inputs and outputs and draw
   * it in the centre. A node with no transactions in or out of it is refused
   * with a highlighted reason instead of being "selected" as an empty view. */
  async function setSource(nodeId: string, opts: { fromTrail?: number } = {}) {
    if (!caseId) return;
    const ticket = ++flowTicket.current;
    setFlowLoading(true);
    try {
      const result = await api.getGraphFlow(caseId, nodeId, 40, selected?.graph_snapshot_id);
      if (ticket !== flowTicket.current) return;
      if (!result.center.selectable) {
        refuse(
          `${shortId(nodeId)} can't be selected as source — ${result.center.reason ?? "it has no further transactions in this snapshot."}`
        );
        return;
      }
      const entry = { id: result.center.id, label: result.center.label, type: result.center.type };
      setTrail((t) => {
        if (opts.fromTrail !== undefined) return t.slice(0, opts.fromTrail + 1);
        const existing = t.findIndex((e) => e.id === entry.id);
        return existing >= 0 ? t.slice(0, existing + 1) : [...t, entry];
      });
      setFlow(result);
      setFlowPick(null);
      setNode(null);
      setSourceNodeId(result.center.id);
      setSourceNoteTone("info");
      const resolved = result.center.resolved_from ? ` (output ${shortId(result.center.resolved_from)} resolved to its ${nodeKind(result.center.type).toLowerCase()})` : "";
      setSourceNote(
        `Source set to ${nodeKind(result.center.type).toLowerCase()} ${shortId(result.center.id)}${resolved} — ` +
          `${result.totals.input_count} direct input(s), ${result.totals.output_count} direct output(s).`
      );
    } catch (err) {
      if (ticket !== flowTicket.current) return;
      if (err instanceof ApiError && err.status === 404) {
        refuse(`${shortId(nodeId)} can't be selected as source — it is not in this case's graph snapshot.`);
      } else if (err instanceof ApiError && err.status === 409) {
        refuse("No completed graph snapshot is available for this case yet — finish an import first.");
      } else {
        refuse(err instanceof ApiError ? `Could not load the flow: ${String(err.detail)}` : "Could not reach the TraceX backend.");
      }
    } finally {
      if (ticket === flowTicket.current) setFlowLoading(false);
    }
  }

  /** Counterparties arrive already classified: a dead end is refused with the
   * backend's own reason, never re-queried as an empty source. */
  function setSourceFromFlow(c: FlowCounterparty) {
    if (!c.selectable || !c.source_id) {
      refuse(`${shortId(c.id)} can't be selected as source — ${c.reason ?? "it has no further transactions from it."}`);
      return;
    }
    void setSource(c.source_id);
  }

  function traceSubmit(event: React.FormEvent) {
    event.preventDefault();
    const value = traceQuery.trim();
    if (value) void setSource(value);
  }

  function backToPath() {
    flowTicket.current += 1;
    setFlowLoading(false);
    setFlow(null);
    setFlowPick(null);
    setTrail([]);
    setSourceNote(null);
  }

  function togglePin(findingId: string) {
    setPinned((p) => {
      const next = new Set(p);
      if (next.has(findingId)) next.delete(findingId);
      else next.add(findingId);
      return next;
    });
  }

  // Every graph node of the selected flagged pattern (all merged chains), so
  // the flow view can paint the whole pattern red when you walk through it.
  const patternNodes = useMemo(() => {
    const ids = new Set<string>();
    if (!selected) return ids;
    selected.graph_path?.nodes.forEach((id) => ids.add(id));
    selected.pattern?.transaction_ids.forEach((id) => ids.add(id));
    selected.pattern?.addresses.forEach((id) => ids.add(id));
    for (const step of selected.steps ?? []) {
      ids.add(`tx:${step.spending_transaction_id}`);
      if (step.previous_address) ids.add(`address:${step.previous_address}`);
      if (step.continuing_address) ids.add(`address:${step.continuing_address}`);
    }
    if (selected.entity_ref) ids.add(selected.entity_ref);
    return ids;
  }, [selected]);

  const risk = selected ? riskTier(selected.score) : null;
  const steps = selected?.steps ?? [];
  const hasSteps = steps.length > 0;
  const timeUnavailable = !!(selected?.uncertainty as { time_continuity_unavailable?: boolean } | undefined)?.time_continuity_unavailable;
  const startSats = steps[0]?.previous_value_sats ?? null;
  const endSats = steps.length ? steps[steps.length - 1].continuing_value_sats ?? steps[steps.length - 1].previous_value_sats : null;
  const clusterHops = steps.filter((s) => s.co_spend_input_address_count >= 2).length;

  const picker = (
    <NeoCard variant="neo-sm" className="ff-picker">
      <h2>Paths</h2>
      <input
        className="ff-search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search txid, address, P-code…"
        aria-label="Search paths"
      />
      {pathFindings === null ? (
        <p className="coverage-note">Loading…</p>
      ) : filteredPaths.length === 0 ? (
        <p className="coverage-note">{query ? "No path matches that search." : "No path-shaped findings in this case."}</p>
      ) : (
        <div className="ff-picker-list">
          {filteredPaths.map((f) => {
            const r = riskTier(f.score);
            return (
              <button
                key={f.finding_id}
                type="button"
                className={`ff-picker-row ${selectedFindingId === f.finding_id ? "active" : ""}`}
                onClick={() => selectPath(f.finding_id)}
              >
                <span className="ff-picker-code">{pathCode(f)}</span>
                <span className="mono-id ff-picker-ref">{shortId(f.entity_ref)}</span>
                <span className="ff-picker-meta">
                  {f.hop_count ?? "?"} hops · <span className={`ff-dot tone-${r.tone}`} />
                </span>
              </button>
            );
          })}
        </div>
      )}
      <p className="coverage-note ff-picker-count">
        {pathFindings ? `${filteredPaths.length} of ${pathFindings.length} path(s)` : ""}
      </p>
    </NeoCard>
  );

  return (
    <Shell>
      <div className="page-header ff-header">
        <div>
          <h1>UTXO Graph Explorer</h1>
          <p className="subtitle">
            Fund-flow view · case {caseId?.slice(0, 8)} · {selected ? `snapshot ${selected.snapshot_id.slice(0, 8)}…` : "no path selected"} · bounded query
          </p>
        </div>
        {selected && (
          <div className="ff-counters">
            <span>Hops <b>{selected.hop_count ?? "—"}</b></span>
            <span>Evidence <b>{selected.evidence_refs.length}</b></span>
            <span>Pattern score <b>{selected.score.toFixed(2)}</b></span>
            {selected.confidence?.value !== undefined && selected.confidence?.value !== null && (
              <span>Confidence <b>{formatConfidence(selected.confidence.value)}</b></span>
            )}
          </div>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}

      <FilterPills active={activeTab} onChange={setActiveTab} options={TABS} />

      {activeTab === "suspicious-path" && (
        <NeoCard className="path-list-card">
          <h2>Suspicious paths in this case</h2>
          <p className="coverage-note">
            Every pattern below is a structural review signal — a real, verified sequence of UTXO spends or an
            equal-output shape. None of it asserts ownership, laundering, or wrongdoing on its own.
          </p>
          {pathFindings === null ? (
            <p className="coverage-note">Loading…</p>
          ) : pathFindings.length === 0 ? (
            <EmptyState title="No path-shaped findings yet" body="Peeling-chain or equal-output patterns appear here once the deterministic detectors flag one in this case." />
          ) : (
            <div className="path-list">
              {pathFindings.map((f) => {
                const r = riskTier(f.score);
                return (
                  <button key={f.finding_id} type="button" className={`path-list-row ${selectedFindingId === f.finding_id ? "active" : ""}`} onClick={() => selectPath(f.finding_id)}>
                    <div className="path-list-main">
                      <span className="path-list-type">{pathCode(f)} · {humanizeFindingType(f.rule_id)}</span>
                      <span className="mono-id">{shortId(f.entity_ref)}</span>
                    </div>
                    <div className="path-list-meta">
                      {f.hop_count !== null && <span>{f.hop_count} hops</span>}
                      <span>{formatDuration(f.total_duration_sec)}</span>
                      <Badge tone={r.tone}>{r.label}</Badge>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </NeoCard>
      )}

      {activeTab === "fund-flow" && (
        <div className="ff-layout">
          {picker}

          <div className="ff-canvas-wrap">
            {caseId && (sourceNodeId || unresolvedSeed || selected?.entity_ref) && <GraphContinuation key={`${caseId}:${sourceNodeId ?? unresolvedSeed ?? selected?.entity_ref}`}
              caseId={caseId} seed={sourceNodeId ?? unresolvedSeed ?? selected!.entity_ref} graphSnapshotId={selected?.graph_snapshot_id} />}
            <div className="ff-toolbar">
              <button type="button" onClick={() => setZoomIdx((i) => Math.max(0, i - 1))} disabled={zoomIdx === 0} title="Zoom out">−</button>
              <span className="ff-zoom-label">{Math.round(ZOOM_STEPS[zoomIdx] * 100)}%</span>
              <button type="button" onClick={() => setZoomIdx((i) => Math.min(ZOOM_STEPS.length - 1, i + 1))} disabled={zoomIdx === ZOOM_STEPS.length - 1} title="Zoom in">+</button>
              <button type="button" onClick={() => setZoomIdx(3)} title="Reset zoom">⟲</button>
              {flow && (
                <button type="button" className="ff-tool-text" onClick={backToPath} title="Back to the detected path">
                  ← {selected ? pathCode(selected) : "Path"}
                </button>
              )}
              <form className="ff-trace" onSubmit={traceSubmit}>
                <input
                  value={traceQuery}
                  onChange={(e) => setTraceQuery(e.target.value)}
                  placeholder="Trace a txid or address…"
                  aria-label="Trace a txid or address as the source"
                />
                <button type="submit" className="ff-tool-text" disabled={!traceQuery.trim() || flowLoading}>Trace</button>
              </form>
              <span className="ff-hint">
                {flow ? "Tap for detail · double-click or hover → “Set as source”" : "Tap a node for detail · hover for “Set as source”"}
              </span>
            </div>
            {flow && trail.length > 0 && (
              <nav className="ff-trail" aria-label="Source history">
                {trail.map((entry, index) => (
                  <span key={entry.id} className="ff-trail-item">
                    {index > 0 && <span className="ff-trail-sep">→</span>}
                    <button
                      type="button"
                      className={index === trail.length - 1 ? "current" : ""}
                      disabled={index === trail.length - 1}
                      onClick={() => void setSource(entry.id, { fromTrail: index })}
                      title={entry.id}
                    >
                      {entry.type === "transaction" ? "TX" : "ADDR"} {shortId(entry.id)}
                    </button>
                  </span>
                ))}
              </nav>
            )}
            {sourceNote && (
              <div className={`ff-note ${sourceNoteTone === "danger" ? "ff-note-danger" : ""}`} role={sourceNoteTone === "danger" ? "alert" : "status"}>
                {sourceNoteTone === "danger" && <span className="ff-note-icon">✕</span>}
                {sourceNote}
                {flow && sourceNoteTone !== "danger" && (
                  <button type="button" className="ff-note-clear" onClick={backToPath}>clear source</button>
                )}
                {!flow && sourceNodeId && sourceNoteTone !== "danger" && (
                  <button type="button" className="ff-note-clear" onClick={() => { setSourceNodeId(null); setSourceNote(null); }}>
                    clear source
                  </button>
                )}
                {sourceNoteTone === "danger" && (
                  <button type="button" className="ff-note-clear" onClick={() => setSourceNote(null)}>dismiss</button>
                )}
              </div>
            )}
            {flowLoading && <div className="ff-loading" role="status"><span className="spinner" /> Loading direct inputs and outputs…</div>}
            {flow ? (
              <SourceFlowGraph
                flow={flow}
                zoom={ZOOM_STEPS[zoomIdx]}
                selectedId={flowPick?.c.id ?? null}
                onSelect={(c, side) => setFlowPick({ c, side })}
                onSelectCenter={() => setFlowPick(null)}
                onSetSource={setSourceFromFlow}
                patternNodes={patternNodes}
              />
            ) : unresolvedSeed ? (
              <NeoCard>
                <EmptyState
                  title="This finding has no fund-flow path"
                  body={`"${unresolvedSeed}" is a real finding, but its type is a plain address/window observation rather than a UTXO chain, so there is no path to draw. Trace it above to see its direct inputs and outputs, or open its Evidence Package.`}
                />
              </NeoCard>
            ) : pathFindings === null ? (
              <NeoCard><p className="coverage-note">Loading…</p></NeoCard>
            ) : !selected ? (
              <NeoCard><EmptyState title="No suspicious path selected" body="Pick one from the Paths list, or trace any txid or address above." /></NeoCard>
            ) : !hasSteps ? (
              <NeoCard>
                <EmptyState
                  title="Per-hop detail unavailable for this finding"
                  body="This finding was materialized before per-hop steps were persisted, or its pattern type has no hop sequence. Re-run ingestion for this case to populate it."
                />
              </NeoCard>
            ) : (
              <FundFlowGraph
                finding={selected}
                signals={signals}
                zoom={ZOOM_STEPS[zoomIdx]}
                selectedNodeId={node?.id ?? null}
                sourceNodeId={sourceNodeId}
                onSelectNode={setNode}
                onSetSource={(id) => void setSource(id)}
              />
            )}
          </div>

          <aside className="ff-panel">
            {flow && flowPick ? (
              <>
                <div className="ff-panel-head">
                  <div className="ff-panel-label">{flowPick.side === "input" ? "DIRECT INPUT" : "DIRECT OUTPUT"}</div>
                  <button type="button" className="ff-close" onClick={() => setFlowPick(null)} aria-label="Back to source detail">×</button>
                </div>
                <h2 className="ff-panel-title">{nodeKind(flowPick.c.type)}</h2>
                <div className="ff-kv"><span>Id</span><b className="ff-kv-long mono-id">{flowPick.c.id}</b></div>
                <div className="ff-kv">
                  <span>{flowPick.side === "input" ? "Flowed in" : "Flowed out"}</span>
                  <b>{flowPick.c.amount_sats === null ? "amount unknown" : btc(flowPick.c.amount_sats)}</b>
                </div>
                <div className="ff-kv"><span>UTXOs</span><b>{flowPick.c.utxo_count}</b></div>
                {flowPick.side === "output" && flowPick.c.type !== "transaction" && (
                  <div className="ff-kv"><span>Spent onward</span><b>{flowPick.c.spent_count} of {flowPick.c.utxo_count}</b></div>
                )}
                {flowPick.c.timestamp && <div className="ff-kv"><span>Observed at</span><b>{stampUtc(flowPick.c.timestamp)}</b></div>}
                <div className="ff-kv">
                  <span>Beyond this source</span>
                  <b>
                    {flowPick.c.type === "address_or_script"
                      ? `${flowPick.c.onward_count} other transaction(s)`
                      : flowPick.c.type === "transaction"
                        ? `${flowPick.c.onward_count} other input/output leg(s)`
                        : flowPick.c.selectable ? "continues" : "nothing"}
                  </b>
                </div>
                {flowPick.c.via.length > 0 && (
                  <>
                    <div className="ff-panel-label ff-section">{flowPick.c.type === "transaction" ? "VIA UTXOS" : "VIA TRANSACTIONS"}</div>
                    <div className="ff-evidence-sub mono-id">{flowPick.c.via.map((v) => shortId(v)).join(" · ")}</div>
                  </>
                )}
                {!flowPick.c.selectable && (
                  <div className="ff-deadend" role="note">
                    <b>Can't be selected as source.</b> {flowPick.c.reason ?? "It has no further transactions from it."}
                  </div>
                )}
                <div className="ff-actions">
                  <button
                    type="button"
                    className="ff-btn ff-btn-primary"
                    disabled={!flowPick.c.selectable}
                    title={flowPick.c.selectable ? "Re-centre the graph on this node" : flowPick.c.reason ?? undefined}
                    onClick={() => setSourceFromFlow(flowPick.c)}
                  >
                    Set as source
                  </button>
                  <button type="button" className="ff-btn" onClick={() => setFlowPick(null)}>Back to source</button>
                </div>
              </>
            ) : flow ? (
              <>
                <div className="ff-panel-label">SOURCE</div>
                <h2 className="ff-panel-title">{nodeKind(flow.center.type)}</h2>
                <p className="ff-panel-sub mono-id ff-kv-long" style={{ maxWidth: "none", textAlign: "left" }}>{flow.center.label}</p>
                {flow.center.resolved_from && (
                  <p className="coverage-note">Requested output {shortId(flow.center.resolved_from)} — shown through its {nodeKind(flow.center.type).toLowerCase()}.</p>
                )}
                <div className="ff-kv"><span>Direct inputs</span><b>{flow.totals.input_count}</b></div>
                <div className="ff-kv"><span>Total in</span><b>{flow.totals.input_sats === null ? "partly unknown" : btc(flow.totals.input_sats)}</b></div>
                <div className="ff-kv"><span>Direct outputs</span><b>{flow.totals.output_count}</b></div>
                <div className="ff-kv"><span>Total out</span><b>{flow.totals.output_sats === null ? "partly unknown" : btc(flow.totals.output_sats)}</b></div>
                {flow.center.type === "transaction" &&
                  flow.totals.input_sats !== null &&
                  flow.totals.output_sats !== null &&
                  flow.totals.input_sats >= flow.totals.output_sats &&
                  flow.totals.input_count > 0 && (
                    <div className="ff-kv"><span>Implied fee</span><b>{btc(flow.totals.input_sats - flow.totals.output_sats)}</b></div>
                  )}
                {flow.center.type === "address_or_script" && (
                  <div className="ff-kv"><span>Transactions</span><b>{flow.center.transaction_count ?? 0}</b></div>
                )}
                <div className="ff-kv"><span>Observed at</span><b>{stampUtc(flow.center.timestamp)}</b></div>
                {(flow.totals.dead_end_inputs > 0 || flow.totals.dead_end_outputs > 0) && (
                  <div className="ff-deadend" role="note">
                    <b>{flow.totals.dead_end_inputs + flow.totals.dead_end_outputs} dead end(s)</b> shown in orange have no further
                    transactions and can't be selected as source.
                  </div>
                )}
                {(flow.truncated_inputs > 0 || flow.truncated_outputs > 0) && (
                  <p className="coverage-note">
                    Showing the 40 largest per side; {flow.truncated_inputs + flow.truncated_outputs} smaller counterparties are not drawn.
                  </p>
                )}
                <p className="coverage-note ff-section">
                  Only committed, uniquely resolved spends appear. An input whose previous output is outside the imported data
                  has no arrow — absence here is not evidence of absence on-chain.
                </p>
                <div className="ff-actions">
                  <button type="button" className="ff-btn ff-btn-primary" onClick={backToPath}>
                    {selected ? `Back to ${pathCode(selected)}` : "Close flow"}
                  </button>
                </div>
                <p className="ff-footnote">
                  Arrow weight follows the amount moved. Blue = funds in, amber = funds out, red = the flagged
                  pattern{selected ? ` ${pathCode(selected)}` : ""} or a node with its own open finding, orange dashed = dead end.
                </p>
              </>
            ) : node ? (
              <>
                <div className="ff-panel-head">
                  <div className="ff-panel-label">NODE</div>
                  <button type="button" className="ff-close" onClick={() => setNode(null)} aria-label="Back to path detail">×</button>
                </div>
                <h2 className="ff-panel-title">{node.title}</h2>
                {node.rows.map((r) => (
                  <div className="ff-kv" key={r.k}>
                    <span>{r.k}</span>
                    <b className={r.v.length > 24 ? "ff-kv-long mono-id" : ""}>{r.v}</b>
                  </div>
                ))}
                <p className="coverage-note ff-section">{node.note}</p>
                <div className="ff-actions">
                  <button type="button" className="ff-btn ff-btn-primary" onClick={() => void setSource(node.id)}>Set as source</button>
                  <button type="button" className="ff-btn" onClick={() => setNode(null)}>Back to path</button>
                </div>
              </>
            ) : !selected ? (
              <p className="coverage-note">Nothing selected.</p>
            ) : (
              <>
                <div className="ff-panel-label">SELECTED PATH</div>
                <h2 className="ff-panel-title">{pathCode(selected)} · {humanizeFindingType(selected.rule_id)}</h2>
                <p className="ff-panel-sub">{btc(startSats)} → {btc(endSats)} · {selected.hop_count ?? steps.length} hops</p>
                {risk && <span className={`ff-risk-badge tone-${risk.tone}`}>{risk.label}</span>}

                <div className="ff-kv"><span>Risk</span><b>{risk?.label.replace(" RISK", "") ?? "—"}</b></div>
                <div className="ff-kv"><span>Pattern</span><b>{humanizeFindingType(selected.rule_id)}</b></div>
                <div className="ff-kv"><span>Amount</span><b>{btc(startSats)}</b></div>
                <div className="ff-kv"><span>Hops</span><b>{selected.hop_count ?? steps.length}</b></div>
                {selected.pattern && (
                  <div className="ff-kv">
                    <span>Pattern</span>
                    <b>
                      {selected.pattern.chain_count > 1
                        ? `${selected.pattern.chain_count} chains merged · ${selected.pattern.transaction_count} txs`
                        : `single chain · ${selected.pattern.transaction_count} txs`}
                    </b>
                  </div>
                )}
                <div className="ff-kv"><span>Time window</span><b>{timeUnavailable ? "not available" : formatDuration(selected.total_duration_sec)}</b></div>
                <div className="ff-kv ff-kv-bar">
                  <span>Pattern score</span>
                  <span className="ff-conf-track"><span className="ff-conf-fill" style={{ width: `${Math.round(selected.score * 100)}%` }} /></span>
                  <b>{selected.score.toFixed(2)}</b>
                </div>
                {selected.confidence?.value !== undefined && selected.confidence?.value !== null && (
                  <div className="ff-kv">
                    <span>Calibrated confidence</span>
                    <b>{formatConfidence(selected.confidence.value)}{selected.confidence.grade === "weak" ? " (weak calibration)" : ""}</b>
                  </div>
                )}

                <div className="ff-panel-label ff-section">WHY FLAGGED</div>
                <ul className="ff-reasons">
                  {selected.reason_codes.map((code) => <li key={code}>{humanizeReasonCode(code)}</li>)}
                </ul>
                <p className="coverage-note">{selected.explanation}</p>

                <div className="ff-panel-label ff-section">EVIDENCE</div>
                <div className="ff-evidence">
                  <span className="ff-tag ff-tag-observed">OBSERVED</span>
                  <div>
                    <div className="ff-evidence-main">{steps.length} verified on-chain transaction hop(s)</div>
                    <div className="ff-evidence-sub mono-id">{steps.slice(0, 3).map((s) => shortId(s.spending_transaction_id)).join(" · ")}</div>
                  </div>
                </div>
                {clusterHops > 0 && (
                  <div className="ff-evidence">
                    <span className="ff-tag ff-tag-inferred">INFERRED</span>
                    <div>
                      <div className="ff-evidence-main">Co-spend cluster at {clusterHops} of {steps.length} hop(s)</div>
                      <div className="ff-evidence-sub">common-input-ownership heuristic · no identity asserted</div>
                    </div>
                  </div>
                )}
                {signals && signals.entity_risk_matches.length > 0 && (
                  <div className="ff-evidence">
                    <span className="ff-tag ff-tag-suspected">SUSPECTED</span>
                    <div>
                      <div className="ff-evidence-main">{signals.entity_risk_matches.length} path node(s) flagged by other findings</div>
                      <div className="ff-evidence-sub">cross-reference within this case · not an external attribution</div>
                    </div>
                  </div>
                )}

                {selected.rule_id === "peeling_chain_candidate" && (
                  <>
                    <div className="ff-panel-label ff-section">SIGNAL CONTRIBUTION</div>
                    {signals ? (
                      <>
                        <SignalBar label="Velocity" value={signals.velocity} />
                        <SignalBar label="Peel ratio" value={signals.peel_ratio} />
                        <SignalBar label="Entity risk" value={signals.entity_risk} />
                        <SignalBar label="Cluster link" value={signals.cluster_link} />
                      </>
                    ) : (
                      <p className="coverage-note">Loading signal breakdown…</p>
                    )}
                  </>
                )}

                <div className="ff-actions">
                  <button type="button" className="ff-btn ff-btn-primary" onClick={() => navigate(`/findings/${selected.finding_id}`)}>
                    Open evidence package
                  </button>
                  <button type="button" className={`ff-btn ${pinned.has(selected.finding_id) ? "ff-btn-on" : ""}`} onClick={() => togglePin(selected.finding_id)}>
                    {pinned.has(selected.finding_id) ? "Pinned" : "Pin path"}
                  </button>
                </div>
                <p className="ff-footnote">
                  Observed = on-chain fact · Inferred = heuristic attribution · Suspected = cross-referenced within this case.
                </p>
              </>
            )}
          </aside>
        </div>
      )}
    </Shell>
  );
}
