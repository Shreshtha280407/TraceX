import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, ErrorBanner } from "../components/primitives";
import { ThemedSelect } from "../components/ThemedSelect";
import { useAuth } from "../lib/auth";
import { useLastCaseId } from "../lib/lastCase";
import { api, ApiError, type CaseDetail, type CaseWithRole, type EvidenceSourceRow } from "../lib/api";

const FORWARD_COMMITMENTS = [
  "No search was performed on the final holdout set",
  "Layers, fusion method and review budget were fixed on train/validation only",
  "The held-out set was evaluated exactly once, after the configuration was frozen",
  "The rule-only baseline stays the live comparator on every task, always shown alongside the model",
];

/** Frozen at Phase 5B selection — generator-v2 100K fixture, final holdout, tie-aware
 * average precision. Not recomputed per request: these are a versioned decision
 * (experiments/model_decision.md), not a live metric. See docs/anomaly_stack.md. */
const HOLDOUT_RESULTS = [
  { task: "surge", candidate: "A_global+D_burst", ap: 0.5927, rule: 0.2388, lift: "2.48x" },
  { task: "motif", candidate: "A_global+D_burst", ap: 0.6282, rule: 0.3799, lift: "1.65x" },
  { task: "discrimination", candidate: "rule baseline wins", ap: 0.9569, rule: 0.9569, lift: "1.00x" },
];

type CheckResult = { label: string; ok: boolean; detail: string };

/** Global, account-level settings — reached by clicking the avatar circle, not
 * case-scoped like everything else in the rail. Consolidates two things that
 * used to be separate case-scoped nav items: "Model & Rules" (the deployed
 * model, its frozen holdout numbers, and the selection commitments are
 * deployment-wide facts, not per-case data) and "Case Settings" (per-case
 * metadata and data sources -- genuinely case-scoped, so it gets its own
 * case picker here rather than living in the URL). */
export function Settings() {
  const { actor, logout } = useAuth();
  const navigate = useNavigate();
  const [checks, setChecks] = useState<CheckResult[] | null>(null);
  const [checking, setChecking] = useState(false);

  const lastCaseId = useLastCaseId();
  const [caseList, setCaseList] = useState<CaseWithRole[] | null>(null);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [caseDetail, setCaseDetail] = useState<CaseDetail | null>(null);
  const [caseSources, setCaseSources] = useState<EvidenceSourceRow[] | null>(null);
  const [caseError, setCaseError] = useState<string | null>(null);

  useEffect(() => {
    api.listCases().then((result) => {
      setCaseList(result.cases);
      setSelectedCaseId((current) => {
        if (current) return current;
        const lastStillExists = lastCaseId && result.cases.some((c) => c.case_id === lastCaseId);
        return (lastStillExists ? lastCaseId : result.cases[0]?.case_id) ?? null;
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fetch once on mount; lastCaseId is only a seed for the initial pick
  }, []);

  useEffect(() => {
    if (!selectedCaseId) return;
    setCaseError(null);
    api.getCase(selectedCaseId).then(setCaseDetail).catch((err) => setCaseError(err instanceof ApiError ? String(err.detail) : "Could not load case."));
    api.listSources(selectedCaseId).then((r) => setCaseSources(r.sources));
  }, [selectedCaseId]);

  async function runSystemCheck() {
    setChecking(true);
    const results: CheckResult[] = [];
    try {
      const health = await api.health();
      results.push({
        label: "Database + evidence vault",
        ok: health.status === "ok",
        detail:
          `database: ${health.database} · evidence_vault: ${health.evidence_vault}` +
          (health.resources
            ? ` · host: ${health.resources.cpu_count} CPU, ${health.resources.available_memory_mb ?? "?"} MB free → ` +
              `${health.resources.memory_budget_mb} MB import budget (${health.resources.insert_chunk_rows.toLocaleString()}-row writes)`
            : ""),
      });
    } catch (err) {
      results.push({ label: "Database + evidence vault", ok: false, detail: err instanceof ApiError ? String(err.detail) : "unreachable" });
    }
    try {
      const ready = await api.ready();
      results.push({
        label: "Worker heartbeat",
        ok: ready.status === "ready",
        detail: `worker_id: ${ready.worker_id} · last seen ${new Date(ready.heartbeat_at).toLocaleTimeString()}`,
      });
    } catch (err) {
      results.push({ label: "Worker heartbeat", ok: false, detail: err instanceof ApiError ? String(err.detail) : "unreachable or stale" });
    }
    setChecks(results);
    setChecking(false);
  }

  function onSignOut() {
    logout();
    navigate("/");
  }

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p className="subtitle">Account, case settings, deployed model, and system resources</p>
        </div>
      </div>

      <div className="two-col">
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard>
            <h2>About TraceX</h2>
            <p className="coverage-note">
              Offline, case-scoped Bitcoin-intelligence backend. Case-scoped upload and jobs, receipt-approved bulk
              ingestion, a correct UTXO graph, deterministic reviewable findings, and a six-layer unsupervised anomaly
              stack layered on top of the rules — all case-isolated and evidence-linked.
            </p>
            <div className="pill-row" style={{ marginTop: 10 }}>
              <Badge tone="ml">Linux CPU (offline, primary)</Badge>
              <Badge tone="muted">Case-scoped</Badge>
              <Badge tone="muted">Evidence-first</Badge>
            </div>
          </NeoCard>

          <NeoCard>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: 12 }}>
              <h2 style={{ margin: 0 }}>Case Settings</h2>
              {caseList && caseList.length > 0 && selectedCaseId && (
                <ThemedSelect
                  value={selectedCaseId}
                  onChange={setSelectedCaseId}
                  ariaLabel="Choose a case"
                  options={caseList.map((c) => ({ value: c.case_id, label: c.name }))}
                />
              )}
            </div>
            {caseError && <ErrorBanner>{caseError}</ErrorBanner>}
            {caseList === null ? (
              <p className="coverage-note">Loading…</p>
            ) : caseList.length === 0 ? (
              <p className="coverage-note">No cases yet — create one from Home.</p>
            ) : !caseDetail ? (
              <p className="coverage-note">Loading case details…</p>
            ) : (
              <>
                <div className="kv-row"><span className="k">Case name</span><span>{caseDetail.name}</span></div>
                <div className="kv-row"><span className="k">Case ID</span><span className="mono-id">{caseDetail.case_id}</span></div>
                <div className="kv-row"><span className="k">PS reference</span><span>SIH PS 26146</span></div>
                <div className="kv-row"><span className="k">Created</span><span>{caseDetail.created_at?.slice(0, 10) ?? "—"}</span></div>
                <div className="kv-row"><span className="k">Retention window</span><span>180 days from snapshot</span></div>
                <p className="coverage-note" style={{ marginTop: 8 }}>
                  PS reference and retention window are static per-deployment configuration, not per-case backend fields yet.
                </p>
              </>
            )}
          </NeoCard>

          <NeoCard>
            <h2>Deployed Model</h2>
            <div className="kv-row"><span className="k">Release ID</span><span className="mono-id">anomaly-stack-v1</span></div>
            <div className="kv-row"><span className="k">Layers</span><span>A_global + D_burst</span></div>
            <div className="kv-row"><span className="k">Fusion</span><span>Stouffer (equal-weight ECDF p-values)</span></div>
            <div className="kv-row"><span className="k">Review budget</span><span>1% (TRACEX_ML_REVIEW_BUDGET)</span></div>
            <p className="coverage-note" style={{ marginTop: 8 }}>
              No static model file — the stack refits on each snapshot's own reference period at scoring time. This ID
              names the frozen procedure, not a loaded artifact. See <span className="mono-id">experiments/model_decision.md</span>.
            </p>
          </NeoCard>

          <NeoCard>
            <h2>Comparison — final holdout, generator-v2 100K fixture</h2>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Best candidate</th>
                  <th>AP</th>
                  <th>Rule AP</th>
                  <th>Lift</th>
                </tr>
              </thead>
              <tbody>
                {HOLDOUT_RESULTS.map((row) => (
                  <tr key={row.task}>
                    <td>{row.task}</td>
                    <td>{row.candidate}</td>
                    <td>{row.ap.toFixed(4)}</td>
                    <td>{row.rule.toFixed(4)}</td>
                    <td>{row.lift}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="coverage-note" style={{ marginTop: 10 }}>
              No unsupervised combination beats the rule on discrimination — that decision stays with the reviewer.
              Frozen at Phase 5B selection, evaluated once on the held-out split. Full methodology in{" "}
              <span className="mono-id">docs/anomaly_stack.md</span>.
            </p>
          </NeoCard>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard variant="neo-sm">
            <h2>Account</h2>
            <div className="kv-row"><span className="k">Signed in as</span><span>{actor}</span></div>
            <button type="button" className="btn-ghost" style={{ marginTop: 12 }} onClick={onSignOut}>
              Sign out
            </button>
          </NeoCard>

          <NeoCard variant="neo-sm">
            <h2>Data Sources {caseDetail ? <span className="coverage-note">({caseDetail.name})</span> : null}</h2>
            {caseSources === null ? (
              <p className="coverage-note">{caseList?.length ? "Loading…" : "No case selected yet."}</p>
            ) : caseSources.length === 0 ? (
              <p className="coverage-note">No sources uploaded yet.</p>
            ) : (
              caseSources.map((source) => (
                <div className="kv-row" key={source.source_id}>
                  <span className="k">{source.filename}</span>
                  <Badge tone="success">verified</Badge>
                </div>
              ))
            )}
            <p className="coverage-note" style={{ marginTop: 8 }}>SHA-256 hashes recorded in EvidenceSource.</p>
          </NeoCard>

          <NeoCard variant="neo-sm">
            <h2>Policy</h2>
            <p className="coverage-note">Coverage limits are always shown, never hidden — missing outpoints render as partial coverage, not silent gaps.</p>
            <p className="coverage-note" style={{ marginTop: 8 }}>No cross-case view. Switching cases re-scopes every list, panel and permission check.</p>
            <p className="coverage-note" style={{ marginTop: 8 }}>Reviewer decisions are never auto-applied — every merge/verify/reject is a logged, explicit action.</p>
          </NeoCard>

          <NeoCard variant="neo-sm">
            <h2>System Check</h2>
            <button type="button" className="btn-mustard" onClick={runSystemCheck} disabled={checking}>
              {checking ? "Checking…" : "Run system check"}
            </button>
            {checks && (
              <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
                {checks.map((c) => (
                  <div key={c.label}>
                    <div className="kv-row">
                      <span className="k">{c.label}</span>
                      <Badge tone={c.ok ? "success" : "danger"}>{c.ok ? "PASS" : "FAIL"}</Badge>
                    </div>
                    <p className="coverage-note">{c.detail}</p>
                  </div>
                ))}
              </div>
            )}
            <p className="coverage-note" style={{ marginTop: 8 }}>
              Calls the real <span className="mono-id">GET /v1/healthz</span> and{" "}
              <span className="mono-id">GET /v1/readyz</span> endpoints live — not a cached or simulated status.
            </p>
          </NeoCard>

          <NeoCard variant="neo-sm">
            <h2>Selection Rules Enforced (forward commitment)</h2>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {FORWARD_COMMITMENTS.map((item) => (
                <li key={item} className="coverage-note" style={{ marginBottom: 6 }}>{item}</li>
              ))}
            </ul>
            <div className="pill-row" style={{ marginTop: 10 }}>
              <Badge tone="success">
                <span className="dot tone-success" /> Enforced
              </Badge>
            </div>
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}
