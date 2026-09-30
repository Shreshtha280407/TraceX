import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, ErrorBanner, NoticeBanner } from "../components/primitives";
import { Modal } from "../components/Modal";
import { RecordPreview } from "../components/RecordPreview";
import { ChatPanel } from "../components/ChatPanel";
import { api, ApiError, type FindingEvidence } from "../lib/api";
import { recordReview } from "../lib/sessionStats";
import "./EvidencePackage.css";

const DISPOSITIONS: { id: "escalated" | "dismissed" | "needs_data_review"; label: string }[] = [
  { id: "escalated", label: "Escalate for Investigation" },
  { id: "dismissed", label: "Dismiss" },
  { id: "needs_data_review", label: "Needs More Evidence" },
];

/** Within one finding, every source_ref commonly shares the same evidence_id (one
 * uploaded source file) and is distinguished only by locator (e.g. "record:8") --
 * so evidence_id alone is not a unique key for a specific record. */
function refKey(ref: { evidence_id: string; locator: string }): string {
  return `${ref.evidence_id}::${ref.locator}`;
}

function humanizeKey(key: string): string {
  return key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

function coverageValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(3);
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

type OpposingItem = { kind?: string; statement?: string; source_refs?: unknown[] };

/** The four factors the peeling-chain detector actually averages into its score
 * (app/engine/motifs/deterministic.py). Showing each one's real value, and what
 * would have to change to pull the score down, is genuine sensitivity — unlike a
 * raw JSON dump, which is what used to sit here. */
function sensitivityRows(evidence: FindingEvidence): { label: string; value: string; note: string }[] | null {
  const finding = evidence.finding;
  if (finding.rule_id !== "peeling_chain_candidate") return null;
  const detector = (evidence.feature_vector?.detector_result ?? {}) as Record<string, unknown>;
  const coverage = (evidence.coverage ?? {}) as Record<string, unknown>;
  const hops = finding.hop_count ?? 0;
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  const peel = num(detector.peel_ratio);
  const velocity = num(detector.velocity);
  const cov = num(coverage.evidence_coverage);
  return [
    {
      label: "Chain length",
      value: `${hops} hop(s)`,
      note: `Contributes min(1, ${hops}/8) = ${Math.min(1, hops / 8).toFixed(2)}. Below 3 hops the detector would not emit this finding at all.`,
    },
    {
      label: "Peel ratio",
      value: peel === null ? "n/a" : peel.toFixed(2),
      note: "Fraction of hops where the continuation is strictly smaller than its input. Drops if any hop stops reducing in value.",
    },
    {
      label: "Velocity",
      value: velocity === null ? "n/a" : velocity.toFixed(2),
      note: "Derived from the real median gap between hop timestamps. Falls toward 0 as hops spread further apart in time.",
    },
    {
      label: "Evidence coverage",
      value: cov === null ? "n/a" : cov.toFixed(2),
      note: "Distinct source records backing the hops. Falls if fewer source rows support the chain.",
    },
  ];
}

export function EvidencePackage() {
  const { findingId } = useParams<{ findingId: string }>();
  const navigate = useNavigate();
  const [evidence, setEvidence] = useState<FindingEvidence | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [disposition, setDisposition] = useState<(typeof DISPOSITIONS)[number]["id"] | null>(null);
  const [reason, setReason] = useState("");
  const [counterRefs, setCounterRefs] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [staleNotice, setStaleNotice] = useState(false);
  const [openRecord, setOpenRecord] = useState<{ locator: string; record: unknown; isCsv: boolean } | null>(null);
  const [evidenceSearch, setEvidenceSearch] = useState("");
  const [counterSearch, setCounterSearch] = useState("");
  const [fullscreenRecord, setFullscreenRecord] = useState<{ locator: string; record: unknown; isCsv: boolean } | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [contraryText, setContraryText] = useState<string | null>(null);
  const [contraryBusy, setContraryBusy] = useState(false);
  const [contraryError, setContraryError] = useState<string | null>(null);

  /** Routed through the same grounded endpoint the chat panel uses, so the model
   * only ever sees this finding's own evidence and is instructed to say it does
   * not know rather than invent. The answer is labelled as generated commentary
   * in the UI -- it never becomes part of the evidence record. */
  async function askContraryCase() {
    if (!findingId) return;
    setContraryBusy(true);
    setContraryError(null);
    try {
      const result = await api.chatAboutFinding(
        findingId,
        "Argue against this finding. Using only the evidence provided, what are the strongest benign explanations, " +
          "and what specific evidence is missing that a reviewer would need before escalating? Be concise.",
        []
      );
      setContraryText(result.answer);
    } catch (err) {
      setContraryError(
        err instanceof ApiError && err.status === 503
          ? "The local model is not reachable right now, so no summary can be generated. Everything above is unaffected — it comes from the stored evidence, not the model."
          : err instanceof ApiError
            ? String(err.detail)
            : "Could not reach the TraceX backend."
      );
    } finally {
      setContraryBusy(false);
    }
  }

  function load() {
    if (!findingId) return;
    api.getFindingEvidence(findingId).then(setEvidence).catch((err) => setError(err instanceof ApiError ? String(err.detail) : "Could not load evidence."));
  }

  useEffect(load, [findingId]);

  async function viewRecord(evidenceId: string, locator: string, isCsv: boolean) {
    try {
      const result = await api.getEvidenceRecord(evidenceId, locator);
      setOpenRecord({ locator, record: result.record, isCsv });
    } catch {
      setOpenRecord({ locator, record: "Could not reopen this source record.", isCsv: false });
    }
  }

  async function submitReview() {
    if (!findingId || !evidence || !disposition || !reason.trim()) return;
    setSubmitting(true);
    setStaleNotice(false);
    setError(null);
    const isReversal = evidence.review_history.length > 0;
    const allRefs = evidence.source_refs as { evidence_id: string; locator: string }[];
    try {
      await api.submitReview(findingId, {
        expected_finding_version: evidence.finding.finding_version,
        disposition,
        reason: reason.trim(),
        counterevidence_refs: allRefs
          .filter((ref) => counterRefs.has(refKey(ref)))
          .map((ref) => ({ evidence_id: ref.evidence_id, locator: ref.locator })),
      });
      recordReview(isReversal);
      setReason("");
      setDisposition(null);
      setCounterRefs(new Set());
      load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setStaleNotice(true);
        load();
      } else {
        setError(err instanceof ApiError ? String(err.detail) : "Could not submit the review.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (error && !evidence) return <Shell><ErrorBanner>{error}</ErrorBanner></Shell>;
  if (!evidence) return <Shell><p className="coverage-note">Loading…</p></Shell>;

  const { finding } = evidence;
  const sensitivity = sensitivityRows(evidence);
  const sourceRefs = evidence.source_refs as { evidence_id: string; locator: string; locator_type?: string }[];
  const matches = (ref: { locator: string }, query: string) =>
    !query.trim() || ref.locator.toLowerCase().includes(query.trim().toLowerCase());
  const filteredSourceRefs = sourceRefs.filter((ref) => matches(ref, evidenceSearch));
  const filteredCounterRefs = sourceRefs.filter((ref) => matches(ref, counterSearch));

  return (
    <Shell>
      <div className="evidence-header">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <span className="back-link" onClick={() => navigate(-1)}>← Back</span>
          <button type="button" className="btn-ghost" onClick={() => setChatOpen(true)}>
            💬 Chat
          </button>
        </div>
        <div className="pill-row" style={{ marginBottom: 6 }}>
          <Badge tone="deterministic">{finding.finding_type}</Badge>
          <Badge tone="warning">{finding.status}</Badge>
        </div>
        <h1>{finding.claim || finding.explanation}</h1>
        <div className="meta-line">
          version {finding.finding_version} · snapshot {finding.snapshot_id}
        </div>
      </div>

      {staleNotice && <NoticeBanner>This finding changed since you opened it — reviewed the update below, try your decision again.</NoticeBanner>}
      {error && <ErrorBanner>{error}</ErrorBanner>}

      <div className="evidence-columns">
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <NeoCard variant="neo-sm">
            <h2>Supporting Evidence</h2>
            {sourceRefs.length === 0 ? (
              <p className="coverage-note">No source references attached.</p>
            ) : (
              <>
                <input
                  type="text"
                  className="list-search-input"
                  placeholder="Search records by locator…"
                  value={evidenceSearch}
                  onChange={(e) => setEvidenceSearch(e.target.value)}
                />
                <div className="scrollable-record-list">
                  {filteredSourceRefs.length === 0 ? (
                    <p className="coverage-note" style={{ padding: "8px 0" }}>No records match "{evidenceSearch}".</p>
                  ) : (
                    filteredSourceRefs.map((ref, index) => (
                      <div className="source-ref-row" key={`${ref.evidence_id}-${index}`}>
                        <div style={{ flex: 1 }}>
                          <div className="mono-id">{ref.locator}</div>
                          <div className="meta-line">{ref.locator_type ?? "source"}</div>
                          <button
                            type="button"
                            className="btn-ghost"
                            style={{ padding: "4px 10px", fontSize: 13, marginTop: 4 }}
                            onClick={() => viewRecord(ref.evidence_id, ref.locator, ref.locator_type === "csv_logical_record")}
                          >
                            View raw record
                          </button>
                          {openRecord?.locator === ref.locator && (
                            <>
                              <div style={{ marginTop: 6 }}>
                                <RecordPreview record={openRecord.record} isCsv={openRecord.isCsv} />
                              </div>
                              <button
                                type="button"
                                className="btn-ghost"
                                style={{ padding: "4px 10px", fontSize: 13, marginTop: 4 }}
                                onClick={() => setFullscreenRecord(openRecord)}
                              >
                                ⤢ Full screen
                              </button>
                            </>
                          )}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </>
            )}
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Feature Snapshot</h2>
            <div className="feature-grid">
              {Object.entries(evidence.feature_vector)
                .filter(([, v]) => typeof v !== "object")
                .slice(0, 12)
                .map(([key, value]) => (
                  <div className="feature-chip" key={key}>
                    <span className="k">{key.replace(/_/g, " ")}</span>
                    {String(value)}
                  </div>
                ))}
            </div>
          </NeoCard>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <NeoCard variant="neo-sm">
            <h2>Observed Indicators</h2>
            <div className="kv-row"><span className="k">Rank</span><span>{finding.rank ? `#${finding.rank}` : "unranked"}</span></div>
            <div className="kv-row"><span className="k">Score</span><span>{finding.score}</span></div>
            {finding.reason_codes.length > 0 && (
              <p className="coverage-note" style={{ marginTop: 8 }}>Reason codes: {finding.reason_codes.join(", ")}</p>
            )}
            <p className="coverage-note" style={{ marginTop: 8 }}>{finding.explanation}</p>
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Alternative Explanations Considered</h2>
            {finding.benign_alternatives.length === 0 ? (
              <p className="coverage-note">None recorded.</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {finding.benign_alternatives.map((alt, i) => (
                  <li key={i} className="coverage-note">{alt}</li>
                ))}
              </ul>
            )}
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Contrary Evidence &amp; Sensitivity</h2>
            <div className="contrary-scroll">

            {evidence.opposing_evidence.length === 0 ? (
              <p className="coverage-note">No contrary evidence recorded in this snapshot.</p>
            ) : (
              <div className="opposing-list">
                {(evidence.opposing_evidence as OpposingItem[]).map((item, i) => (
                  <div className="opposing-item" key={i}>
                    <span className="opposing-kind">{humanizeKey(item.kind ?? "note")}</span>
                    <p className="opposing-statement">{item.statement ?? JSON.stringify(item)}</p>
                    {!!item.source_refs?.length && (
                      <p className="coverage-note">{item.source_refs.length} supporting source record(s)</p>
                    )}
                  </div>
                ))}
              </div>
            )}

            {sensitivity && (
              <>
                <h3 className="sub-heading">What this score rests on</h3>
                <table className="sensitivity-table">
                  <tbody>
                    {sensitivity.map((row) => (
                      <tr key={row.label}>
                        <th>{row.label}</th>
                        <td className="mono-id">{row.value}</td>
                        <td className="sensitivity-note">{row.note}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            <h3 className="sub-heading">Coverage</h3>
            <div className="coverage-scroll">
            <table className="coverage-table">
              <tbody>
                {Object.entries(evidence.coverage ?? {}).flatMap(([k, v]) => {
                  // Coverage nests one level (graph stats, time coverage, notes).
                  // Flatten it into readable rows instead of leaking JSON blobs.
                  if (Array.isArray(v)) {
                    return [
                      <tr key={k}>
                        <th>{humanizeKey(k)}</th>
                        <td>
                          <ul className="coverage-notes-list">
                            {v.map((entry, i) => <li key={i}>{coverageValue(entry)}</li>)}
                          </ul>
                        </td>
                      </tr>,
                    ];
                  }
                  if (v && typeof v === "object") {
                    return [
                      <tr key={k} className="coverage-group">
                        <th colSpan={2}>{humanizeKey(k)}</th>
                      </tr>,
                      ...Object.entries(v as Record<string, unknown>).map(([nk, nv]) => (
                        <tr key={`${k}.${nk}`}>
                          <th className="coverage-sub">{humanizeKey(nk)}</th>
                          <td className="mono-id">{coverageValue(nv)}</td>
                        </tr>
                      )),
                    ];
                  }
                  return [
                    <tr key={k}>
                      <th>{humanizeKey(k)}</th>
                      <td className="mono-id">{coverageValue(v)}</td>
                    </tr>,
                  ];
                })}
              </tbody>
            </table>
            </div>

            <h3 className="sub-heading">Plain-language contrary case</h3>
            <p className="coverage-note">
              Asks the local offline model to argue against this finding using only the evidence above. It is a
              generated summary to help a reviewer think, never a new piece of evidence.
            </p>
            {contraryText && <p className="contrary-text">{contraryText}</p>}
            {contraryError && <p className="field-hint-required">{contraryError}</p>}
            <button type="button" className="btn-ghost" disabled={contraryBusy} onClick={askContraryCase}>
              {contraryBusy ? "Asking local model…" : contraryText ? "Ask again" : "Argue against this finding"}
            </button>
            </div>
          </NeoCard>
        </div>

        <div>
          <NeoCard variant="neo-sm">
            <h2>Review Decision</h2>
            <div className="review-buttons">
              {DISPOSITIONS.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  className={disposition === option.id ? "selected" : ""}
                  onClick={() => setDisposition(option.id)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <div className="form-field">
              <label htmlFor="review-reason">
                Reason <span className="required-tag">required</span>
              </label>
              <textarea
                id="review-reason"
                required
                aria-required="true"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Add reviewer notes for the audit trail…"
              />
              {disposition && !reason.trim() && (
                <p className="field-hint-required">A reason is required — it is written to the immutable audit trail with your decision.</p>
              )}
            </div>
            {sourceRefs.length > 0 && (
              <div className="form-field">
                <label>Counterevidence (optional)</label>
                <input
                  type="text"
                  className="list-search-input"
                  placeholder="Search records by locator…"
                  value={counterSearch}
                  onChange={(e) => setCounterSearch(e.target.value)}
                />
                <div className="scrollable-record-list">
                  {filteredCounterRefs.length === 0 ? (
                    <p className="coverage-note" style={{ padding: "8px 0" }}>No records match "{counterSearch}".</p>
                  ) : (
                    filteredCounterRefs.map((ref) => {
                      const key = refKey(ref);
                      return (
                        <label key={key} style={{ display: "flex", gap: 6, fontSize: 13, marginBottom: 4 }}>
                          <input
                            type="checkbox"
                            checked={counterRefs.has(key)}
                            onChange={(e) => {
                              const next = new Set(counterRefs);
                              if (e.target.checked) next.add(key);
                              else next.delete(key);
                              setCounterRefs(next);
                            }}
                          />
                          <span className="mono-id">{ref.locator}</span>
                        </label>
                      );
                    })
                  )}
                </div>
              </div>
            )}
            <button type="button" className="btn-mustard" disabled={!disposition || !reason.trim() || submitting} onClick={submitReview}>
              {submitting ? "Submitting…" : "Submit decision"}
            </button>
            <p className="coverage-note" style={{ marginTop: 10 }}>
              Optimistic concurrency: expects version {finding.finding_version}; a conflicting update returns 409 and reloads here.
            </p>
          </NeoCard>
          <NeoCard variant="neo-sm" className="audit-card">
            <h2>Audit History</h2>
            {evidence.audit_history.length === 0 ? (
              <p className="coverage-note">No decision recorded yet.</p>
            ) : (
              evidence.audit_history.map((entry) => (
                <div className="audit-entry" key={entry.audit_id}>
                  {entry.created_at?.slice(11, 19)} — {entry.action}
                </div>
              ))
            )}
          </NeoCard>
        </div>
      </div>

      <Modal
        open={!!fullscreenRecord}
        onClose={() => setFullscreenRecord(null)}
        title={fullscreenRecord?.locator ?? "Raw record"}
        wide
      >
        <div className="modal-body-scroll">
          <RecordPreview record={fullscreenRecord?.record} isCsv={fullscreenRecord?.isCsv ?? false} preClassName="fullscreen-record-pre" />
        </div>
      </Modal>

      {chatOpen && findingId && <ChatPanel findingId={findingId} onClose={() => setChatOpen(false)} />}
    </Shell>
  );
}
