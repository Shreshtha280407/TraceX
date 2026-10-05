import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { Modal } from "../components/Modal";
import { NeoCard, StatTile, Badge, NoticeBanner, ErrorBanner } from "../components/primitives";
import { useAuth } from "../lib/auth";
import { api, ApiError, type CaseWithRole, type InvestigationGroup, type InvestigationQueue, type ScoringMode } from "../lib/api";
import { aggregateReviewCounts, REVIEW_COUNT_EVENTS } from "../lib/investigationCounts";
import { streamCaseEvents, type CaseEvent } from "../lib/sse";
import { getSessionStats } from "../lib/sessionStats";

type QueueItem = InvestigationGroup & { caseName: string; queue_position: number };

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
  // Exact per-case summaries, independent of the eight-row preview and case names.
  const [caseQueues, setCaseQueues] = useState<Record<string, InvestigationQueue | null>>({});
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
    api.listCases().then(result => setCases(result.cases));
  }, []);

  useEffect(() => {
    if (!cases) return;
    let active = true;
    const requests = new Map<string, number>();
    const timers = new Map<string, ReturnType<typeof setTimeout>>();
    async function refresh(caseId: string) {
      const requestId = (requests.get(caseId) ?? 0) + 1;
      requests.set(caseId, requestId);
      const result = await api.getInvestigationQueue(caseId).catch(() => null);
      if (active && requests.get(caseId) === requestId) setCaseQueues(prev => ({...prev, [caseId]: result}));
    }
    const schedule = (caseId: string) => {
      clearTimeout(timers.get(caseId));
      timers.set(caseId, setTimeout(() => refresh(caseId), 200));
    };
    cases.forEach(c => { void refresh(c.case_id); });
    const stops = cases.map((c) =>
      streamCaseEvents(c.case_id, token, event => {
        setActivity(prev => [{ ...event, caseName: c.name }, ...prev].sort((a, b) => b.id - a.id).slice(0, 10));
        if (REVIEW_COUNT_EVENTS.has(event.event)) schedule(c.case_id);
      })
    );
    const onFocus = () => cases.forEach(c => schedule(c.case_id));
    window.addEventListener("focus", onFocus);
    return () => { active = false; stops.forEach(stop => stop()); timers.forEach(clearTimeout); window.removeEventListener("focus", onFocus); };
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

  const totals = cases ? aggregateReviewCounts(cases.map(c => caseQueues[c.case_id] ?? null)) : null;
  const queue = useMemo<QueueItem[] | null>(() => {
    if (!cases || cases.some(c => !caseQueues[c.case_id])) return null;
    return cases.flatMap(c => (caseQueues[c.case_id]?.items ?? []).map((f, queue_position) => ({...f, caseName: c.name, queue_position})))
      .sort((a,b) => Number(b.status === "escalated") - Number(a.status === "escalated") || a.queue_position-b.queue_position || a.case_id.localeCompare(b.case_id) || a.group_id.localeCompare(b.group_id)).slice(0,8);
  }, [cases, caseQueues]);
  const pendingReview = totals?.queued ?? null;
  const highPriority = queue?.filter((f) => f.status === "escalated").length ?? null;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>{timeOfDayGreeting()}, {actor}</h1>
          <p className="subtitle">
            {cases?.length ?? "…"} case{cases?.length === 1 ? "" : "s"} assigned · {pendingReview ?? "…"} groups in your review queues · {totals?.backlog ?? "…"} additional unresolved
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
        <StatTile label="In your review queues" value={pendingReview ?? "—"} sub={totals ? `${totals.backlog} additional unresolved · capacity 100 per case` : "loading grouped workload…"} />
        <StatTile label="Escalated groups" value={highPriority ?? "—"} sub="displayed queue; not calibrated risk" />
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
                  <th>Queued groups</th>
                  <th>Additional unresolved</th>
                  <th>Role</th>
                </tr>
              </thead>
              <tbody>
                {(cases ?? []).map((c) => (
                  <tr key={c.case_id} className="clickable" onClick={() => navigate(`/cases/${c.case_id}/dashboard`)}>
                    <td>{c.name}</td>
                    <td>{caseQueues[c.case_id]?.queued_groups ?? "—"}</td>
                    <td>{caseQueues[c.case_id]?.backlog_groups ?? "—"}</td>
                    <td>
                      <Badge tone="muted">{c.role}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </NeoCard>

          <NeoCard>
            <h2>Investigation Review Queue — preview</h2>
            {cases?.some(c => caseQueues[c.case_id] === null) && <p role="alert">Grouped workload is unavailable for a case. Check that the running API/worker uses the current release; raw finding counts are not review tasks.</p>}
            {cases?.some(c => caseQueues[c.case_id]?.grouping_coverage?.state === "incomplete") && <p role="alert">Some underlying observations await grouping. Published queue counts are partial; check investigation_grouping before concluding review is complete.</p>}
            {queue === null ? (
              <p className="coverage-note">Loading…</p>
            ) : queue.length === 0 ? (
              <p className="coverage-note">No published groups in this preview. Check grouping coverage and each case's backlog.</p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
                {queue.map((item) => (
                  <li key={item.group_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <Badge tone="deterministic">{item.family}</Badge>
                      <span className="mono-id">{item.member_count} observations</span>
                      <span className="coverage-note">
                        {item.caseName} · window {new Date(item.window_start).toISOString().slice(11, 16)}–{new Date(item.window_end).toISOString().slice(11, 16)}
                      </span>
                    </div>
                    <div style={{ display: "flex", gap: 14 }}>
                      <a
                        href={`/cases/${item.case_id}/graph?seed=${encodeURIComponent(item.focal_ref)}`}
                        onClick={(e) => { e.preventDefault(); navigate(`/cases/${item.case_id}/graph?seed=${encodeURIComponent(item.focal_ref)}`); }}
                      >
                        Graph
                      </a>
                      <a href={`/investigations/${item.group_id}`} onClick={(e) => { e.preventDefault(); navigate(`/investigations/${item.group_id}`); }}>
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
              You have {pendingReview} queued investigation group{pendingReview === 1 ? "" : "s"} across{" "}
              {cases?.filter(c => (caseQueues[c.case_id]?.queued_groups ?? 0) > 0).length} case(s), with {totals?.backlog} additional unresolved groups accessible in the backlog.
            </NoticeBanner>
          )}
        </div>
      </div>
    </Shell>
  );
}
