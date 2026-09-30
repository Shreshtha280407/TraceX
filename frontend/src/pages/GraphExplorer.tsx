import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, EmptyState, FilterPills } from "../components/primitives";
import { FundFlowGraph, ZOOM_STEPS, type GraphNodeInfo } from "../components/FundFlowGraph";
import { api, ApiError, type Finding, type PathSignals } from "../lib/api";
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

  useEffect(() => {
    if (!caseId) return;
    setFindings(null);
    setSelectedFindingId(null);
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
    setActiveTab("fund-flow");
  }

  /** Re-root the view on a node: mark it as the source (the graph highlights it
   * and scrolls it to centre) and, when another detected path starts at or runs
   * through it, switch to that path. Never fabricates a path that isn't in the
   * data — if nothing else covers the node, the current path simply stays. */
  function setSource(nodeId: string) {
    const list = pathFindings ?? [];
    // graph_path.nodes only records out:/tx: ids, so an address node has to be
    // matched against the step detail instead -- otherwise every address on the
    // chain in front of you would be reported as "not on this path".
    const onCurrent =
      (selected?.graph_path?.nodes.includes(nodeId) ?? false) ||
      (selected?.steps ?? []).some(
        (s) =>
          `address:${s.previous_address}` === nodeId ||
          `address:${s.continuing_address}` === nodeId ||
          s.co_spend_addresses.some((a) => `address:${a}` === nodeId) ||
          s.peel_outputs.some((p) => p.output_id === nodeId || `address:${p.address}` === nodeId)
      );
    const exact = list.find((f) => f.entity_ref === nodeId);
    const containing = list.find((f) => f.finding_id !== selectedFindingId && f.graph_path?.nodes.includes(nodeId));
    const target = exact ?? containing;

    // Nothing to explore from here: say so loudly and leave the current source
    // untouched rather than silently "selecting" a dead end.
    if (!onCurrent && (!target || target.finding_id === selectedFindingId)) {
      setSourceNote(`No detected path starts at or passes through ${shortId(nodeId)} in this case — not set as source.`);
      setSourceNoteTone("danger");
      return;
    }

    setSourceNodeId(nodeId);
    setNode(null);
    setSourceNoteTone("info");
    if (target && target.finding_id !== selectedFindingId) {
      setSourceNote(`Source set to ${shortId(nodeId)} — switched to ${pathCode(target)}, which also covers it.`);
      setSelectedFindingId(target.finding_id);
      return;
    }
    setSourceNote(`Source set to ${shortId(nodeId)} — centred on the path you're viewing.`);
  }

  function togglePin(findingId: string) {
    setPinned((p) => {
      const next = new Set(p);
      if (next.has(findingId)) next.delete(findingId);
      else next.add(findingId);
      return next;
    });
  }

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
            <span>Confidence <b>{selected.score.toFixed(2)}</b></span>
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
            {unresolvedSeed ? (
              <NeoCard>
                <EmptyState
                  title="This finding has no fund-flow path"
                  body={`"${unresolvedSeed}" is a real finding, but its type is a plain address/window observation rather than a UTXO chain, so there is no path to draw. Open its Evidence Package for the full explanation.`}
                />
              </NeoCard>
            ) : pathFindings === null ? (
              <NeoCard><p className="coverage-note">Loading…</p></NeoCard>
            ) : !selected ? (
              <NeoCard><EmptyState title="No suspicious path selected" body="Pick one from the Paths list." /></NeoCard>
            ) : !hasSteps ? (
              <NeoCard>
                <EmptyState
                  title="Per-hop detail unavailable for this finding"
                  body="This finding was materialized before per-hop steps were persisted, or its pattern type has no hop sequence. Re-run ingestion for this case to populate it."
                />
              </NeoCard>
            ) : (
              <>
                <div className="ff-toolbar">
                  <button type="button" onClick={() => setZoomIdx((i) => Math.max(0, i - 1))} disabled={zoomIdx === 0} title="Zoom out">−</button>
                  <span className="ff-zoom-label">{Math.round(ZOOM_STEPS[zoomIdx] * 100)}%</span>
                  <button type="button" onClick={() => setZoomIdx((i) => Math.min(ZOOM_STEPS.length - 1, i + 1))} disabled={zoomIdx === ZOOM_STEPS.length - 1} title="Zoom in">+</button>
                  <button type="button" onClick={() => setZoomIdx(3)} title="Reset zoom">⟲</button>
                  <span className="ff-hint">Tap a node for detail · hover for “Set as source”</span>
                </div>
                {sourceNote && (
                  <div className={`ff-note ${sourceNoteTone === "danger" ? "ff-note-danger" : ""}`} role="status">
                    {sourceNote}
                    {sourceNodeId && sourceNoteTone !== "danger" && (
                      <button type="button" className="ff-note-clear" onClick={() => { setSourceNodeId(null); setSourceNote(null); }}>
                        clear source
                      </button>
                    )}
                    {sourceNoteTone === "danger" && (
                      <button type="button" className="ff-note-clear" onClick={() => setSourceNote(null)}>dismiss</button>
                    )}
                  </div>
                )}
                <FundFlowGraph
                  finding={selected}
                  signals={signals}
                  zoom={ZOOM_STEPS[zoomIdx]}
                  selectedNodeId={node?.id ?? null}
                  sourceNodeId={sourceNodeId}
                  onSelectNode={setNode}
                  onSetSource={setSource}
                />
              </>
            )}
          </div>

          <aside className="ff-panel">
            {node ? (
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
                  <button type="button" className="ff-btn ff-btn-primary" onClick={() => setSource(node.id)}>Set as source</button>
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
                <div className="ff-kv"><span>Time window</span><b>{timeUnavailable ? "not available" : formatDuration(selected.total_duration_sec)}</b></div>
                <div className="ff-kv ff-kv-bar">
                  <span>Confidence</span>
                  <span className="ff-conf-track"><span className="ff-conf-fill" style={{ width: `${Math.round(selected.score * 100)}%` }} /></span>
                  <b>{selected.score.toFixed(2)}</b>
                </div>

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
