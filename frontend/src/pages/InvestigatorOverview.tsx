import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, StatTile, Badge, NoticeBanner } from "../components/primitives";
import { useAuth } from "../lib/auth";
import { api, type CaseWithRole, type Finding } from "../lib/api";
import { streamCaseEvents, type CaseEvent } from "../lib/sse";
import { getSessionStats } from "../lib/sessionStats";

type QueueItem = Finding & { caseName: string };

export function InvestigatorOverview() {
  const { actor, token } = useAuth();
  const navigate = useNavigate();
  const [cases, setCases] = useState<CaseWithRole[] | null>(null);
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [activity, setActivity] = useState<(CaseEvent & { caseName: string })[]>([]);
  const [creating, setCreating] = useState(false);
  const stats = getSessionStats();

  useEffect(() => {
    api.listCases().then((result) => {
      setCases(result.cases);
      if (result.cases.length === 0) {
        navigate("/onboarding", { replace: true });
        return;
      }
      Promise.all(
        result.cases.map((c) =>
          api
            .listFindings(c.case_id, 200, 0)
            .then((r) => r.findings.filter((f) => f.status === "open").map((f) => ({ ...f, caseName: c.name })))
        )
      ).then((lists) => setQueue(lists.flat().sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity)).slice(0, 8)));
    });
  }, [navigate]);

  useEffect(() => {
    if (!cases) return;
    const stops = cases.map((c) =>
      streamCaseEvents(c.case_id, token, (event) =>
        setActivity((prev) => [{ ...event, caseName: c.name }, ...prev].sort((a, b) => b.id - a.id).slice(0, 10))
      )
    );
    return () => stops.forEach((stop) => stop());
  }, [cases, token]);

  async function createCase() {
    const name = prompt("New case name");
    if (!name) return;
    setCreating(true);
    try {
      const created = await api.createCase(name, true);
      navigate(`/cases/${created.case_id}/ingestion`);
    } finally {
      setCreating(false);
    }
  }

  const pendingReview = queue?.length ?? null;
  const highPriority = queue?.filter((f) => (f.rank ?? Infinity) <= 10).length ?? null;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Good evening, {actor}</h1>
          <p className="subtitle">
            {cases?.length ?? "…"} case{cases?.length === 1 ? "" : "s"} assigned · {pendingReview ?? "…"} findings awaiting your review
          </p>
        </div>
        <button type="button" className="btn-mustard" onClick={createCase} disabled={creating}>
          + New Case
        </button>
      </div>

      <div className="stat-grid">
        <StatTile label="Assigned cases" value={cases?.length ?? "—"} sub={cases ? `${cases.filter((c) => c.role === "case_lead").length} as case lead` : ""} />
        <StatTile label="Pending review" value={pendingReview ?? "—"} sub="across all assigned cases" />
        <StatTile label="High priority" value={highPriority ?? "—"} sub="rank ≤ 10" />
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
                  <tr key={c.case_id} className="clickable" onClick={() => navigate(`/cases/${c.case_id}/findings`)}>
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
                    <a href={`/findings/${item.finding_id}`} onClick={(e) => { e.preventDefault(); navigate(`/findings/${item.finding_id}`); }}>
                      Review
                    </a>
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
                  <li key={`${event.caseName}-${event.id}`} style={{ fontSize: 12 }}>
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
