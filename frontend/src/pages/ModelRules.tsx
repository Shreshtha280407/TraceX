import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, StatTile, NoticeBanner, Badge } from "../components/primitives";
import { api, ML_RULE_VERSION, type FeatureExportResponse } from "../lib/api";
import { useFindings } from "../lib/hooks";

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
  { task: "surge", candidate: "A_global+D_burst", ap: 0.5927, rule: 0.2388, lift: "2.48x", note: "deployable — beats the rule" },
  { task: "motif", candidate: "A_global+D_burst", ap: 0.6282, rule: 0.3799, lift: "1.65x", note: "deployable — beats the rule" },
  { task: "discrimination", candidate: "rule baseline wins", ap: 0.9569, rule: 0.9569, lift: "1.00x", note: "no unsupervised combination beats the rule" },
];

export function ModelRules() {
  const { caseId } = useParams<{ caseId: string }>();
  const [features, setFeatures] = useState<FeatureExportResponse | null>(null);
  const { findings, mlEnabled } = useFindings(caseId);

  useEffect(() => {
    if (!caseId) return;
    api.exportFeatures(caseId).then(setFeatures);
  }, [caseId]);

  const mlFindingCount = findings?.filter((f) => f.rule_version === ML_RULE_VERSION).length ?? null;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Model &amp; Rules</h1>
          <p className="subtitle">Deterministic rules plus a six-layer unsupervised anomaly ranking, deployed automatically on ingest</p>
        </div>
      </div>

      <NoticeBanner>
        The deployed ranking earns its place on surge detection and triage ordering, not on deciding which equal-output
        transaction is really a mixer — no unsupervised combination beats the deterministic rule on that discrimination
        task (see below). That decision stays with the reviewer until enough analyst decisions exist to train on.
      </NoticeBanner>

      <div className="stat-grid">
        <StatTile label="Feature contract" value={features?.feature_schema_version ?? "—"} sub="frozen for this comparison" />
        <StatTile label="Rows exported" value={features?.rows.length ?? "—"} sub={features?.snapshot_id ? `snapshot ${features.snapshot_id}` : "all snapshots"} />
        <StatTile
          label="Feature export scope"
          value={features ? (features.ml_enabled ? "Includes ML" : "Deterministic only") : "—"}
          sub="Phase 4.1 feature export is rule-derived by design, never ML"
        />
        <StatTile
          label="This case's ML status"
          value={findings === null ? "—" : mlEnabled ? "Active" : "No ML findings yet"}
          sub={mlEnabled ? `${mlFindingCount} ranked by anomaly-stack-v1` : "runs automatically once a case has enough transactions"}
        />
      </div>

      <div className="two-col">
        <NeoCard>
          <h2>Comparison — final holdout, same review budget, generator-v2 100K fixture</h2>
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
            Frozen at Phase 5B selection, evaluated once on the held-out split — not recomputed per request. Full
            methodology, the near-miss-negative construction, and the causality/leakage audit are in{" "}
            <span className="mono-id">docs/anomaly_stack.md</span>.
          </p>
        </NeoCard>
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard variant="neo-sm">
            <h2>Deployed Release</h2>
            <div className="kv-row"><span className="k">Release ID</span><span className="mono-id">anomaly-stack-v1</span></div>
            <div className="kv-row"><span className="k">Layers</span><span>A_global + D_burst</span></div>
            <div className="kv-row"><span className="k">Fusion</span><span>Stouffer (equal-weight ECDF p-values)</span></div>
            <div className="kv-row"><span className="k">Review budget</span><span>1% (TRACEX_ML_REVIEW_BUDGET)</span></div>
            <p className="coverage-note" style={{ marginTop: 8 }}>
              No static model file — the stack refits on each snapshot's own reference period at scoring time. This ID
              names the frozen procedure, not a loaded artifact. See <span className="mono-id">experiments/model_decision.md</span>.
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
