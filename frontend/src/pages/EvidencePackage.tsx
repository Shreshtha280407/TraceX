import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, ErrorBanner, NoticeBanner } from "../components/primitives";
import { api, ApiError, type FindingEvidence } from "../lib/api";
import { recordReview } from "../lib/sessionStats";
import "./EvidencePackage.css";

const DISPOSITIONS: { id: "escalated" | "dismissed" | "needs_data_review"; label: string }[] = [
  { id: "escalated", label: "Escalate for Investigation" },
  { id: "dismissed", label: "Mark Benign" },
  { id: "needs_data_review", label: "Needs More Evidence" },
];

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
  const [openRecord, setOpenRecord] = useState<{ locator: string; record: unknown } | null>(null);

  function load() {
    if (!findingId) return;
    api.getFindingEvidence(findingId).then(setEvidence).catch((err) => setError(err instanceof ApiError ? String(err.detail) : "Could not load evidence."));
  }

  useEffect(load, [findingId]);

  async function viewRecord(evidenceId: string, locator: string) {
    try {
      const result = await api.getEvidenceRecord(evidenceId, locator);
      setOpenRecord({ locator, record: result.record });
    } catch {
      setOpenRecord({ locator, record: "Could not reopen this source record." });
    }
  }

  async function submitReview() {
    if (!findingId || !evidence || !disposition || !reason.trim()) return;
    setSubmitting(true);
    setStaleNotice(false);
    setError(null);
    const isReversal = evidence.review_history.length > 0;
    try {
      await api.submitReview(findingId, {
        expected_finding_version: evidence.finding.finding_version,
        disposition,
        reason: reason.trim(),
        counterevidence_refs: [...counterRefs].map((evidence_id) => ({ evidence_id, locator: evidence_id })),
      });
      recordReview(isReversal);
      setReason("");
      setDisposition(null);
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
  const sourceRefs = evidence.source_refs as { evidence_id: string; locator: string; locator_type?: string }[];

  return (
    <Shell>
      <div className="evidence-header">
        <span className="back-link" onClick={() => navigate(-1)}>← Back</span>
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
              sourceRefs.map((ref, index) => (
                <div className="source-ref-row" key={`${ref.evidence_id}-${index}`}>
                  <div style={{ flex: 1 }}>
                    <div className="mono-id">{ref.locator}</div>
                    <div className="meta-line">{ref.locator_type ?? "source"}</div>
                    <button type="button" className="btn-ghost" style={{ padding: "4px 10px", fontSize: 11, marginTop: 4 }} onClick={() => viewRecord(ref.evidence_id, ref.locator)}>
                      View raw record
                    </button>
                    {openRecord?.locator === ref.locator && <pre>{JSON.stringify(openRecord.record, null, 2)}</pre>}
                  </div>
                </div>
              ))
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
            {evidence.opposing_evidence.length === 0 ? (
              <p className="coverage-note">No contrary evidence recorded in this snapshot.</p>
            ) : (
              <pre style={{ fontSize: 10, background: "var(--surface-3)", padding: 8, borderRadius: 8, overflowX: "auto" }}>
                {JSON.stringify(evidence.opposing_evidence, null, 2)}
              </pre>
            )}
            <p className="coverage-note" style={{ marginTop: 8 }}>
              Coverage: {JSON.stringify(evidence.coverage)}
            </p>
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
              <label>Reason</label>
              <textarea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Add reviewer notes for the audit trail…" />
            </div>
            {sourceRefs.length > 0 && (
              <div className="form-field">
                <label>Counterevidence (optional)</label>
                {sourceRefs.map((ref, index) => (
                  <label key={index} style={{ display: "flex", gap: 6, fontSize: 11, marginBottom: 4 }}>
                    <input
                      type="checkbox"
                      checked={counterRefs.has(ref.evidence_id)}
                      onChange={(e) => {
                        const next = new Set(counterRefs);
                        if (e.target.checked) next.add(ref.evidence_id);
                        else next.delete(ref.evidence_id);
                        setCounterRefs(next);
                      }}
                    />
                    <span className="mono-id">{ref.locator}</span>
                  </label>
                ))}
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
    </Shell>
  );
}
