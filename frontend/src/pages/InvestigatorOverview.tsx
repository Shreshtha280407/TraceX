import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { Modal } from "../components/Modal";
import { NeoCard, StatTile, Badge, NoticeBanner, ErrorBanner } from "../components/primitives";
import { useAuth } from "../lib/auth";
import { api, ApiError, type CaseWithRole, type Finding, type ScoringMode } from "../lib/api";
import { streamCaseEvents, type CaseEvent } from "../lib/sse";
import { getSessionStats } from "../lib/sessionStats";

type QueueItem = Finding & { caseName: string };

function timeOfDayGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

export function InvestigatorOverview() {
  const { actor, token } = useAuth();
  const navigate = useNavigate();
  const [cases, setCases] = useState<CaseWithRole[] | null>(null);
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  // Real open-finding count across assigned cases. The queue below is only the
  // top few shown; its length is a display cap, never the workload.
  const [openTotal, setOpenTotal] = useState<number | null>(null);
  const [activity, setActivity] = useState<(CaseEvent & { caseName: string })[]>([]);
  const [showNewCase, setShowNewCase] = useState(false);
  const [newCaseName, setNewCaseName] = useState("");
  const [synthetic, setSynthetic] = useState(false);
  const [scoringMode, setScoringMode] = useState<ScoringMode>("auto_eligible");
  const [candidateDomain, setCandidateDomain] = useState("");
  const [newCaseError, setNewCaseError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const stats = getSessionStats();

  useEffect(() => {
    api.listCases().then((result) => {
      setCases(result.cases);
      Promise.all(
        result.cases.map((c) =>
          api
            .listFindings(c.case_id, 200, 0)
            .then((r) => r.findings.filter((f) => f.status === "open").map((f) => ({ ...f, caseName: c.name })))
        )
      ).then((lists) => setQueue(lists.flat().sort((a, b) =>
        Number(b.status === "escalated") - Number(a.status === "escalated") ||
        (a.family_rank ?? Infinity) - (b.family_rank ?? Infinity) ||
        a.rule_id.localeCompare(b.rule_id) || a.caseName.localeCompare(b.caseName) ||
        a.finding_id.localeCompare(b.finding_id)).slice(0, 8)));
      Promise.all(result.cases.map((c) => api.getFindingsSummary(c.case_id).catch(() => null))).then((summaries) =>
        setOpenTotal(summaries.reduce((sum, s) => sum + (s?.open ?? 0), 0))
      );
    });
  }, []);

  useEffect(() => {
    if (!cases) return;
    const stops = cases.map((c) =>
      streamCaseEvents(c.case_id, token, (event) =>
        setActivity((prev) => [{ ...event, caseName: c.name }, ...prev].sort((a, b) => b.id - a.id).slice(0, 10))
      )
    );
    return () => stops.forEach((stop) => stop());
  }, [cases, token]);

  function openNewCase() {
    setNewCaseName("");
    setSynthetic(false);
    setScoringMode("auto_eligible");
    setCandidateDomain("");
    setNewCaseError(null);
    setShowNewCase(true);
  }

  async function onCreateCase(event: FormEvent) {
    event.preventDefault();
    setNewCaseError(null);
    setCreating(true);
    try {
      const created = await api.createCase(newCaseName, synthetic, scoringMode, candidateDomain || undefined);
      // Show it immediately, on the same page the case lead just created it from --
      // no navigation away, no separate click needed to confirm it exists.
      setCases((prev) => [...(prev ?? []), { ...created, role: "case_lead" }]);
      setShowNewCase(false);
    } catch (err) {
      setNewCaseError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend.");
    } finally {
      setCreating(false);
    }
  }

  const pendingReview = openTotal;
  const highPriority = queue?.filter((f) => (f.family_rank ?? Infinity) <= 10).length ?? null;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>{timeOfDayGreeting()}, {actor}</h1>
          <p className="subtitle">
            {cases?.length ?? "…"} case{cases?.length === 1 ? "" : "s"} assigned · {pendingReview ?? "…"} findings awaiting your review
          </p>
        </div>
        <button type="button" className="btn-mustard" onClick={openNewCase}>
          + New Case
        </button>
      </div>

      <Modal open={showNewCase} onClose={() => setShowNewCase(false)} title="Create a new case">
        <p className="modal-subtitle">
          Set up an isolated, case-scoped workspace. Evidence, findings, and access are never shared across cases.
        </p>
        <form onSubmit={onCreateCase}>
          {newCaseError && <ErrorBanner>{newCaseError}</ErrorBanner>}
          <div className="form-field">
            <label htmlFor="new-case-name">Case name</label>
            <input
              id="new-case-name"
              value={newCaseName}
              onChange={(event) => setNewCaseName(event.target.value)}
              required
              autoFocus
              placeholder="PS26146-CASE-004"
            />
            <p className="form-hint">Use a clear, unique identifier — you can rename it later from Settings.</p>
          </div>
          <div className="form-field">
            <label><input type="checkbox" checked={synthetic} onChange={(e) => { setSynthetic(e.target.checked); if (!e.target.checked && scoringMode === "synthetic_demo") setScoringMode("auto_eligible"); }} /> Synthetic/demo data (not real case evidence)</label>
            <label htmlFor="scoring-mode">Scoring procedure</label>
            <select id="scoring-mode" value={scoringMode} onChange={(e) => setScoringMode(e.target.value as ScoringMode)}>
              <option value="auto_eligible">Automatic approved candidate — v2 fallback if ineligible</option>
              <option value="unsupervised">Unsupervised v2 — pinned, retrospective burst</option>
              {synthetic && <option value="synthetic_demo">Synthetic/demo candidate — explicitly opted in</option>}
              <option value="validated_candidate">Approved domain candidate — applicability required</option>
            </select>
            {(scoringMode === "validated_candidate" || scoringMode === "auto_eligible") && <input aria-label="Approved candidate domain" required={scoringMode === "validated_candidate"} value={candidateDomain} onChange={(e) => setCandidateDomain(e.target.value)} placeholder="Approved data domain; blank retains v2" />}
            <p className="form-hint">Unavailable, untrusted or ineligible candidates fall back to v2 with a recorded reason. No upload has measured AP without independent applicable labels.</p>
          </div>
          <div className="modal-actions">
            <button type="button" className="btn-ghost" onClick={() => setShowNewCase(false)} disabled={creating}>
              Cancel
            </button>
            <button type="submit" className="btn-mustard" disabled={creating || !newCaseName.trim()}>
              {creating ? "Creating…" : "Create case"}
            </button>
          </div>
          <p className="modal-footnote">You're added automatically as case lead and can invite teammates afterward.</p>
        </form>
      </Modal>

      <div className="stat-grid">
        <StatTile label="Assigned cases" value={cases?.length ?? "—"} sub={cases ? `${cases.filter((c) => c.role === "case_lead").length} as case lead` : ""} />
        <StatTile label="Pending review" value={pendingReview ?? "—"} sub="across all assigned cases" />
        <StatTile label="Leading family rows" value={highPriority ?? "—"} sub="displayed queue, family rank ≤ 10; not calibrated risk" />
        <StatTile label="Reviewed this session" value={stats.findingsReviewed} sub={`${stats.reversedOnAppeal} reversed on appeal`} />
      </div>

      <div className="two-col">
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard>
            <h2>My Assigned Cases</h2>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Case</th>
                  <th>Open findings</th>
                  <th>Role</th>
                </tr>
              </thead>
              <tbody>
                {(cases ?? []).map((c) => (
                  <tr key={c.case_id} className="clickable" onClick={() => navigate(`/cases/${c.case_id}/dashboard`)}>
                    <td>{c.name}</td>
                    <td>{queue?.filter((f) => f.caseName === c.name).length ?? "—"}</td>
                    <td>
                      <Badge tone="muted">{c.role}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </NeoCard>

          <NeoCard>
            <h2>Pending Review Queue</h2>
            {queue === null ? (
              <p className="coverage-note">Loading…</p>
            ) : queue.length === 0 ? (
              <p className="coverage-note">Nothing awaiting review right now.</p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
                {queue.map((item) => (
                  <li key={item.finding_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <Badge tone="deterministic">{item.finding_type}</Badge>
                      <span className="mono-id">{item.entity_ref}</span>
                      <span className="coverage-note">
                        {item.caseName} · window {new Date(item.window_start).toISOString().slice(11, 16)}–{new Date(item.window_end).toISOString().slice(11, 16)}
                      </span>
                    </div>
                    <div style={{ display: "flex", gap: 14 }}>
                      <a
                        href={`/cases/${item.case_id}/graph?seed=${encodeURIComponent(item.entity_ref)}`}
                        onClick={(e) => { e.preventDefault(); navigate(`/cases/${item.case_id}/graph?seed=${encodeURIComponent(item.entity_ref)}`); }}
                      >
                        Graph
                      </a>
                      <a href={`/findings/${item.finding_id}`} onClick={(e) => { e.preventDefault(); navigate(`/findings/${item.finding_id}`); }}>
                        Review
                      </a>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </NeoCard>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard variant="neo-sm">
            <h2>Recent Activity</h2>
            {activity.length === 0 ? (
              <p className="coverage-note">No cross-case activity observed yet this session.</p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
                {activity.map((event) => (
                  <li key={`${event.caseName}-${event.id}`} style={{ fontSize: 13 }}>
                    <strong>{event.event}</strong> · {event.caseName}
                  </li>
                ))}
              </ul>
            )}
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>This Week</h2>
            <p className="coverage-note">Findings reviewed: {stats.findingsReviewed}</p>
            <p className="coverage-note">Reversed on appeal: {stats.reversedOnAppeal}</p>
            <p className="coverage-note">Exports requested: {stats.exportsRequested}</p>
            <p className="coverage-note" style={{ marginTop: 8 }}>
              Counted from actions taken in this browser session — no analytics endpoint exists yet.
            </p>
          </NeoCard>
          {pendingReview !== null && pendingReview > 0 && (
            <NoticeBanner>
              You have {pendingReview} open finding{pendingReview === 1 ? "" : "s"} awaiting review across{" "}
              {new Set(queue?.map((q) => q.caseName)).size} case(s).
            </NoticeBanner>
          )}
        </div>
      </div>
    </Shell>
  );
}
