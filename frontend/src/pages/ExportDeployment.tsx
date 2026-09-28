import { useState } from "react";
import { useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, ErrorBanner, NoticeBanner } from "../components/primitives";
import { api, ApiError, type FindingsExportBundle, type FeatureExportResponse } from "../lib/api";
import { recordExport } from "../lib/sessionStats";

function download(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export function ExportDeployment() {
  const { caseId } = useParams<{ caseId: string }>();
  const [findingsBundle, setFindingsBundle] = useState<FindingsExportBundle | null>(null);
  const [featuresBundle, setFeaturesBundle] = useState<FeatureExportResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function generate() {
    if (!caseId) return;
    setBusy(true);
    setError(null);
    try {
      const [findings, features] = await Promise.all([api.exportFindings(caseId), api.exportFeatures(caseId)]);
      setFindingsBundle(findings);
      setFeaturesBundle(features);
      recordExport();
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Export, Reporting &amp; Deployment</h1>
          <p className="subtitle">Case-scoped export bundles and offline deployment readiness</p>
        </div>
      </div>

      <NoticeBanner>
        Both export endpoints on this backend are synchronous GETs today — there is no async "generate export" job.
        Clicking below calls them directly and shows the real result rather than a fabricated progress tracker.
      </NoticeBanner>
      {error && <ErrorBanner>{error}</ErrorBanner>}

      <div className="two-col">
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard>
            <h2>Build Export Bundle</h2>
            <ul style={{ margin: "0 0 16px", paddingLeft: 18 }}>
              <li className="coverage-note">Findings, features, reviews, audit trail</li>
              <li className="coverage-note">Stated limitations (never dropped)</li>
              <li className="coverage-note">Snapshot and feature-schema versions</li>
            </ul>
            <button type="button" className="btn-mustard" disabled={busy} onClick={generate}>
              {busy ? "Fetching…" : "Generate Export"}
            </button>
          </NeoCard>

          {findingsBundle && (
            <NeoCard>
              <h2>Signed Export Manifest</h2>
              <div className="kv-row"><span className="k">Case</span><span>{findingsBundle.case_id}</span></div>
              <div className="kv-row"><span className="k">Method</span><span>{findingsBundle.method}</span></div>
              <div className="kv-row"><span className="k">ML enabled</span><span>{String(findingsBundle.ml_enabled)}</span></div>
              <div className="kv-row"><span className="k">Findings in bundle</span><span>{findingsBundle.findings.length}</span></div>
              <div className="pill-row" style={{ marginTop: 10 }}>
                <button type="button" className="btn-ghost" onClick={() => download(`${caseId}-findings-export.json`, findingsBundle)}>
                  Download findings JSON
                </button>
                {featuresBundle && (
                  <button type="button" className="btn-ghost" onClick={() => download(`${caseId}-features-export.json`, featuresBundle)}>
                    Download features JSON
                  </button>
                )}
              </div>
              <p className="coverage-note" style={{ marginTop: 10 }}>Stated limitations:</p>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {findingsBundle.limitations.map((line) => (
                  <li key={line} className="coverage-note">{line}</li>
                ))}
              </ul>
            </NeoCard>
          )}

          <NeoCard variant="neo-sm">
            <h2>Backup Status <span className="coverage-note">(deployment reference)</span></h2>
            <p className="coverage-note">
              No backing endpoint in this backend — raw sources, graph fragments and PostgreSQL are backed up by the
              deployment's own infrastructure, not tracked here.
            </p>
          </NeoCard>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard variant="neo-sm">
            <h2>Deployment Profile <span className="coverage-note">(reference)</span></h2>
            <div className="pill-row">
              <Badge tone="ml">Linux CPU (offline, primary)</Badge>
              <Badge tone="muted">Mac-native (optional)</Badge>
              <Badge tone="muted">Linux CUDA (LAN worker)</Badge>
            </div>
            <p className="coverage-note" style={{ marginTop: 10 }}>
              Intended offline deployment posture per the problem statement — not a live selector wired to any endpoint.
            </p>
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Readiness Checks <span className="coverage-note">(reference)</span></h2>
            <div className="kv-row"><span className="k">PostgreSQL</span><span>check via GET /v1/readyz</span></div>
            <div className="kv-row"><span className="k">Evidence vault</span><span>check via GET /v1/healthz</span></div>
            <div className="kv-row"><span className="k">Worker heartbeat</span><span>check via GET /v1/readyz</span></div>
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}
