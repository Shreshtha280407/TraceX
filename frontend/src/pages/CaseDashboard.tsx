import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, StatTile, Badge } from "../components/primitives";
import { useAuth } from "../lib/auth";
import { api, ML_RULE_VERSION, type CaseDetail } from "../lib/api";
import { useFindings, useTrackedJobs } from "../lib/hooks";
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

/** The dashboard for one case, reached by clicking that case anywhere in the app.
 * Everything here is scoped to :caseId — no cross-case selector. */
export function CaseDashboard() {
  const { caseId } = useParams<{ caseId: string }>();
  const { token } = useAuth();
  const navigate = useNavigate();
  const [caseDetail, setCaseDetail] = useState<CaseDetail | null>(null);
  const [events, setEvents] = useState<CaseEvent[]>([]);

  useEffect(() => {
    if (!caseId) return;
    setCaseDetail(null);
    api.getCase(caseId).then(setCaseDetail);
  }, [caseId]);

  useEffect(() => {
    if (!caseId) return;
    setEvents([]);
    const stop = streamCaseEvents(caseId, token, (event) => setEvents((prev) => [event, ...prev].slice(0, 8)));
    return stop;
  }, [caseId, token]);

  const { findings, mlEnabled } = useFindings(caseId);
  const trackedJobs = useTrackedJobs(caseId);
  const activeJobs = trackedJobs.filter((job) => ["queued", "running", "checkpointed"].includes(job.state));
  const transactionsIngested = trackedJobs.length > 0 ? trackedJobs.reduce((sum, job) => sum + job.rows_accepted, 0) : null;
  const openFindings = findings?.filter((f) => f.status === "open").length ?? null;
  const pendingReview = findings?.filter((f) => f.status === "open" || f.status === "needs_data_review") ?? null;

  if (!caseId) return null;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>{caseDetail?.name ?? "Case dashboard"}</h1>
          <p className="subtitle">
            {caseDetail
              ? `Created ${caseDetail.created_at ? new Date(caseDetail.created_at).toLocaleDateString() : "—"} · ${caseDetail.members.length} member${caseDetail.members.length === 1 ? "" : "s"}`
              : "Loading case details…"}
          </p>
        </div>
        <div className="pill-row">
          <span className="badge tone-muted mono-id">{caseId}</span>
          {caseDetail?.synthetic && <Badge tone="muted">SYNTHETIC</Badge>}
        </div>
      </div>

      <div className="stat-grid">
        <StatTile
          label="Transactions ingested"
          value={transactionsIngested ?? "—"}
          sub={trackedJobs.length ? `${trackedJobs.length} tracked job(s)` : "no imports tracked yet"}
        />
        <StatTile
          label="Active import jobs"
          value={activeJobs.length || (trackedJobs.length ? 0 : "—")}
          sub={activeJobs.length ? "in progress" : "backpressure clear"}
        />
        <StatTile label="Open findings" value={openFindings ?? "—"} sub={findings ? `of ${findings.length} listed` : "loading…"} />
        <StatTile label="Pending human review" value={pendingReview?.length ?? "—"} sub="open + needs data review" />
        <StatTile
          label="Model status"
          value={findings === null ? "—" : mlEnabled ? "Active" : "No ML findings yet"}
          sub={mlEnabled ? "anomaly-stack-v1" : "unsupervised layer runs automatically on ingest"}
        />
      </div>

      <div className="two-col">
        <NeoCard>
          <h2>Pending Review</h2>
          {pendingReview === null ? (
            <p className="coverage-note">Loading…</p>
          ) : pendingReview.length === 0 ? (
            <p className="coverage-note">Nothing awaiting human review for this case.</p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
              {pendingReview.slice(0, 10).map((item) => (
                <li key={item.finding_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <Badge tone={item.rule_version === ML_RULE_VERSION ? "ml" : "deterministic"}>{item.finding_type}</Badge>
                    <span className="mono-id">{item.entity_ref}</span>
                    <span className="coverage-note">
                      window {new Date(item.window_start).toISOString().slice(11, 16)}–{new Date(item.window_end).toISOString().slice(11, 16)}
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: 14 }}>
                    <a
                      href={`/cases/${caseId}/graph?seed=${encodeURIComponent(item.entity_ref)}`}
                      onClick={(e) => { e.preventDefault(); navigate(`/cases/${caseId}/graph?seed=${encodeURIComponent(item.entity_ref)}`); }}
                    >
                      View in Graph
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
