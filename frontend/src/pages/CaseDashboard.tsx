import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, StatTile, Badge } from "../components/primitives";
import { useAuth } from "../lib/auth";
import { api, type CaseWithRole } from "../lib/api";
import { useFindings, useTrackedJobs } from "../lib/hooks";
import { setLastCaseId } from "../lib/lastCase";
import { streamCaseEvents, type CaseEvent } from "../lib/sse";

const EVENT_TONE: Record<string, "success" | "danger" | "warning" | "info" | "muted"> = {
  "import.started": "info",
  "import.reconciled": "success",
  "batch.committed": "success",
  "graph.delta_ready": "warning",
  "finding.reviewed": "success",
  "finding.updated": "warning",
  "export.ready": "warning",
};

export function CaseDashboard() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const [cases, setCases] = useState<CaseWithRole[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<CaseEvent[]>([]);

  useEffect(() => {
    api.listCases().then((result) => {
      setCases(result.cases);
      if (result.cases.length === 0) navigate("/onboarding", { replace: true });
      else setSelected((current) => current ?? result.cases[0].case_id);
    });
  }, [navigate]);

  useEffect(() => {
    if (selected) setLastCaseId(selected);
  }, [selected]);

  useEffect(() => {
    if (!selected) return;
    setEvents([]);
    const stop = streamCaseEvents(selected, token, (event) => setEvents((prev) => [event, ...prev].slice(0, 8)));
    return stop;
  }, [selected, token]);

  const { findings } = useFindings(selected);
  const trackedJobs = useTrackedJobs(selected);
  const activeJobs = trackedJobs.filter((job) => ["queued", "running", "checkpointed"].includes(job.state));
  const transactionsIngested = trackedJobs.length > 0 ? trackedJobs.reduce((sum, job) => sum + job.rows_accepted, 0) : null;
  const openFindings = findings?.filter((f) => f.status === "open").length ?? null;
  const reviewBacklog = findings?.filter((f) => f.status === "open" || f.status === "needs_data_review").length ?? null;

  const selectedCase = cases?.find((c) => c.case_id === selected);

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Case Dashboard</h1>
          <p className="subtitle">Overview across all case-isolated workspaces you can access</p>
        </div>
        <div className="pill-row">
          {selectedCase && <span className="badge tone-muted mono-id">{selectedCase.case_id}</span>}
          {selectedCase && <Badge tone="ml">{selectedCase.role.toUpperCase().replace("_", " ")}</Badge>}
        </div>
      </div>

      <div className="stat-grid">
        <StatTile label="Transactions ingested" value={transactionsIngested ?? "—"} sub={trackedJobs.length ? `${trackedJobs.length} tracked job(s)` : "no imports tracked yet"} />
        <StatTile label="Active import jobs" value={activeJobs.length || (trackedJobs.length ? 0 : "—")} sub={activeJobs.length ? "in progress" : "backpressure clear"} />
        <StatTile label="Open findings" value={openFindings ?? "—"} sub={findings ? `of ${findings.length} listed` : "loading…"} />
        <StatTile label="Review backlog" value={reviewBacklog ?? "—"} sub="open + needs data review" />
        <StatTile label="Model status" value="Not started" sub="Phase 5" />
      </div>

      <div className="two-col">
        <NeoCard>
          <h2>Case Workspaces</h2>
          {cases === null ? (
            <p className="coverage-note">Loading…</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Case ID</th>
                  <th>Records</th>
                  <th>Last activity</th>
                  <th>Isolation</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {cases.map((c) => (
                  <tr key={c.case_id} className={`clickable ${selected === c.case_id ? "selected" : ""}`} onClick={() => setSelected(c.case_id)}>
                    <td>{c.name}</td>
                    <td>—</td>
                    <td>—</td>
                    <td>
                      <Badge tone="success">
                        <span className="dot tone-success" /> Isolated
                      </Badge>
                    </td>
                    <td>Active</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </NeoCard>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard variant="neo-sm">
            <h2>System Health</h2>
            <p className="coverage-note">
              Ops placeholder — this backend does not expose a health-metrics endpoint yet. PostgreSQL/worker liveness can
              be checked via <span className="mono-id">GET /v1/readyz</span>.
            </p>
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Recent Events (SSE stream)</h2>
            {events.length === 0 ? (
              <p className="coverage-note">No events yet for this case.</p>
            ) : (
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
                {events.map((event) => (
                  <li key={event.id} style={{ display: "flex", gap: 8, alignItems: "baseline", fontSize: 12 }}>
                    <span className="mono-id">{new Date((event.data.created_at as string) ?? Date.now()).toLocaleTimeString()}</span>
                    <Badge tone={EVENT_TONE[event.event] === "danger" ? "danger" : "muted"}>
                      <span className={`dot tone-${EVENT_TONE[event.event] ?? "muted"}`} /> {event.event}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}
