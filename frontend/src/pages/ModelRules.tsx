import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, StatTile, NoticeBanner } from "../components/primitives";
import { api, type FeatureExportResponse } from "../lib/api";

const FORWARD_COMMITMENTS = [
  "No search will be performed on the final test set",
  "Hyperparameters will be tuned on train/validation only",
  "Held-out set will be evaluated exactly once",
  "Rule-only rank stays the live comparator against any future candidate",
];

export function ModelRules() {
  const { caseId } = useParams<{ caseId: string }>();
  const [features, setFeatures] = useState<FeatureExportResponse | null>(null);

  useEffect(() => {
    if (!caseId) return;
    api.exportFeatures(caseId).then(setFeatures);
  }, [caseId]);

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Model &amp; Rules</h1>
          <p className="subtitle">Phase 5A frozen feature handoff — no model exists in this backend yet</p>
        </div>
      </div>

      <NoticeBanner>
        Phase 5 has not started. This view previews the planned deterministic-vs-model comparison once a model exists —
        every number below is either read from the real feature export or explicitly marked as not yet available.
      </NoticeBanner>

      <div className="stat-grid">
        <StatTile label="Feature contract" value={features?.feature_schema_version ?? "—"} sub="frozen for this comparison" />
        <StatTile label="Rows exported" value={features?.rows.length ?? "—"} sub={features?.snapshot_id ? `snapshot ${features.snapshot_id}` : "all snapshots"} />
        <StatTile label="ML enabled" value={features ? String(features.ml_enabled) : "—"} sub="always false pre-Phase 5" />
        <StatTile label="Active model" value="Not started" sub="Phase 5" />
      </div>

      <div className="two-col">
        <NeoCard>
          <h2>Comparison — same held-out set, same review budget</h2>
          <p className="coverage-note">
            No candidate model exists yet, so there is no comparison table to show. Once Phase 5 is authorized, this
            table will report Precision@20/@50, benign false-positive rate, review workload, score stability, CPU
            scoring p95 and peak RSS for the rule-only baseline against each eligible candidate — never a fabricated
            number in the meantime.
          </p>
        </NeoCard>
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard variant="neo-sm">
            <h2>Version History</h2>
            <p className="coverage-note">No model versions exist — Phase 5 has not started.</p>
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Selection Rules Enforced (forward commitment)</h2>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {FORWARD_COMMITMENTS.map((item) => (
                <li key={item} className="coverage-note" style={{ marginBottom: 6 }}>{item}</li>
              ))}
            </ul>
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}
