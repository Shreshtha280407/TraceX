import { useState } from "react";
import { useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, ErrorBanner } from "../components/primitives";
import { api, ApiError, type FindingsExportBundle, type FeatureExportResponse } from "../lib/api";
import { recordExport } from "../lib/sessionStats";

/** The anchor must be in the document for the click to count as a user-initiated
 * download, and the object URL must outlive that click -- revoking it on the very
 * next line (as this did) can cancel the download before the browser reads it. */
function saveBlob(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  window.setTimeout(() => {
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }, 2000);
}

const FEATURE_PREVIEW_ROWS = 50_000;

function downloadJson(filename: string, data: unknown) {
  saveBlob(filename, new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
}

function downloadCsv(filename: string, rows: Record<string, unknown>[]) {
  if (rows.length === 0) {
    saveBlob(filename, new Blob([""], { type: "text/csv;charset=utf-8" }));
    return;
  }
  // Union of keys across rows -- feature vectors are sparse, so a first-row-only
  // header would silently drop columns that appear later in the export.
  const headers = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const cell = (value: unknown): string => {
    if (value === null || value === undefined) return "";
    const text = typeof value === "object" ? JSON.stringify(value) : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const csv = [headers.join(","), ...rows.map((r) => headers.map((h) => cell(r[h])).join(","))].join("\r\n");
  // BOM so Excel opens UTF-8 addresses correctly instead of mangling them.
  saveBlob(filename, new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
}

function findingsToRows(bundle: FindingsExportBundle): Record<string, unknown>[] {
  return bundle.findings.map(({ finding, review_history }) => ({
    finding_id: finding.finding_id,
    rule_id: finding.rule_id,
    rule_version: finding.rule_version,
    entity_ref: finding.entity_ref,
    status: finding.status,
    score: finding.score,
    rank: finding.rank,
    hop_count: finding.hop_count,
    total_duration_sec: finding.total_duration_sec,
    window_start: finding.window_start,
    window_end: finding.window_end,
    reason_codes: finding.reason_codes.join("; "),
    explanation: finding.explanation,
    benign_alternatives: finding.benign_alternatives.join(" | "),
    review_count: review_history.length,
    latest_disposition: review_history[review_history.length - 1]?.disposition ?? "",
    latest_review_reason: review_history[review_history.length - 1]?.reason ?? "",
  }));
}

function featuresToRows(bundle: FeatureExportResponse): Record<string, unknown>[] {
  return bundle.rows.map((row) => ({
    feature_row_id: row.feature_row_id,
    entity_ref: row.entity_ref,
    snapshot_id: row.snapshot_id,
    window_start: row.window_start,
    window_end: row.window_end,
    ...row.features,
  }));
}

export function ExportDeployment() {
  const { caseId } = useParams<{ caseId: string }>();
  const [findingsBundle, setFindingsBundle] = useState<FindingsExportBundle | null>(null);
  const [featuresBundle, setFeaturesBundle] = useState<FeatureExportResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [featuresBusy, setFeaturesBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function generate() {
    if (!caseId) return;
    setBusy(true);
    setError(null);
    try {
      setFindingsBundle(await api.exportFindings(caseId));
      recordExport();
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend.");
    } finally {
      setBusy(false);
    }
  }

  /** Feature rows are the per-address-per-window ML input set — routinely two
   * orders of magnitude larger than the findings bundle (190k rows on a 20k-row
   * import). Fetched only on request so the evidential export stays fast. */
  async function loadFeatures() {
    if (!caseId) return;
    setFeaturesBusy(true);
    setError(null);
    try {
      // A browser-sized page for JSON/CSV; the full set downloads as Parquet.
      setFeaturesBundle(await api.exportFeatures(caseId, undefined, FEATURE_PREVIEW_ROWS));
      recordExport();
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend.");
    } finally {
      setFeaturesBusy(false);
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
              {featuresBundle && (
                <div className="kv-row">
                  <span className="k">Feature rows</span>
                  <span>
                    {(featuresBundle.total ?? featuresBundle.rows.length).toLocaleString()}
                    {(featuresBundle.total ?? 0) > featuresBundle.rows.length ? ` (first ${featuresBundle.rows.length.toLocaleString()} in JSON/CSV)` : ""}
                  </span>
                </div>
              )}

              <p className="coverage-note" style={{ margin: "14px 0 6px" }}>Findings</p>
              <div className="pill-row">
                <button type="button" className="btn-ghost" onClick={() => downloadJson(`${caseId}-findings-export.json`, findingsBundle)}>
                  JSON
                </button>
                <button type="button" className="btn-ghost" onClick={() => downloadCsv(`${caseId}-findings-export.csv`, findingsToRows(findingsBundle))}>
                  CSV
                </button>
              </div>

              <p className="coverage-note" style={{ margin: "14px 0 6px" }}>
                Feature rows <span className="coverage-note">(ML input set — large)</span>
              </p>
              {featuresBundle ? (
                <div className="pill-row">
                  <button type="button" className="btn-ghost" onClick={() => downloadJson(`${caseId}-features-export.json`, featuresBundle)}>
                    JSON
                  </button>
                  <button type="button" className="btn-ghost" onClick={() => downloadCsv(`${caseId}-features-export.csv`, featuresToRows(featuresBundle))}>
                    CSV
                  </button>
                  {featuresBundle.rows[0]?.snapshot_id && (
                    <button
                      type="button"
                      className="btn-ghost"
                      onClick={async () => {
                        const snapshotId = featuresBundle.rows[0].snapshot_id;
                        if (!caseId) return;
                        saveBlob(`${caseId}-features-${snapshotId}.parquet`, await api.downloadFeaturesParquet(caseId, snapshotId));
                      }}
                    >
                      Parquet (all rows)
                    </button>
                  )}
                </div>
              ) : (
                <button type="button" className="btn-ghost" disabled={featuresBusy} onClick={loadFeatures}>
                  {featuresBusy ? "Fetching feature rows…" : "Load feature rows"}
                </button>
              )}

              <p className="coverage-note" style={{ marginTop: 14 }}>
                CSV flattens one row per finding / feature row for spreadsheet triage. JSON is the complete bundle
                including review history, audit trail and stated limitations — use it for anything evidential.
              </p>
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
