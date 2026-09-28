import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, StatTile, ErrorBanner } from "../components/primitives";
import { api, ApiError, type EvidenceSourceRow, type ImportJob } from "../lib/api";
import { trackJob, jobsForCase } from "../lib/jobRegistry";
import "./EvidenceIntake.css";

const STAGE_LABELS = ["Staging", "Hash + Sample", "Normalize", "Batch Commit", "Index", "Ready"];

function visualStage(job: ImportJob | null): { doneCount: number; activeIndexes: number[]; failed: boolean } {
  if (!job) return { doneCount: 0, activeIndexes: [], failed: false };
  if (job.state === "failed") return { doneCount: job.stage === "queued" ? 0 : 1, activeIndexes: [], failed: true };
  if (job.state === "queued") return { doneCount: 0, activeIndexes: [0], failed: false };
  if (job.stage === "ingested" && job.state === "completed") return { doneCount: 6, activeIndexes: [], failed: false };
  return { doneCount: 1, activeIndexes: [1, 2, 3, 4], failed: false };
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function EvidenceIntake() {
  const { caseId } = useParams<{ caseId: string }>();
  const [sources, setSources] = useState<EvidenceSourceRow[] | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const refreshSources = useCallback(() => {
    if (!caseId) return;
    api.listSources(caseId).then((r) => setSources(r.sources));
  }, [caseId]);

  useEffect(() => {
    refreshSources();
  }, [refreshSources]);

  // Resume polling the most recently tracked job for this case, if any, on page load.
  useEffect(() => {
    if (!caseId) return;
    const tracked = jobsForCase(caseId);
    const lastJobId = tracked[tracked.length - 1];
    if (lastJobId) api.getJob(lastJobId).then(setJob).catch(() => undefined);
  }, [caseId]);

  useEffect(() => {
    if (!job || job.state === "completed" || job.state === "failed") {
      if (job?.state === "completed") refreshSources();
      return;
    }
    const timer = setInterval(() => {
      api.getJob(job.job_id).then(setJob).catch(() => undefined);
    }, 1200);
    return () => clearInterval(timer);
  }, [job, refreshSources]);

  async function upload(file: File) {
    if (!caseId) return;
    setError(null);
    try {
      const idempotencyKey = crypto.randomUUID();
      const result = await api.createImport(caseId, file, idempotencyKey);
      trackJob(caseId, result.job_id);
      const initial = await api.getJob(result.job_id);
      setJob(initial);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Upload failed — could not reach the backend.");
    }
  }

  function onDrop(event: React.DragEvent) {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) void upload(file);
  }

  const stage = visualStage(job);

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Evidence Intake</h1>
          <p className="subtitle">CSV / JSON / XML ingestion in {caseId} — hash-verified, provenance-tracked</p>
        </div>
      </div>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <div className="two-col">
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard>
            <h2>Drop Source</h2>
            <div
              className={`drop-zone ${dragging ? "dragging" : ""}`}
              onClick={() => inputRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
            >
              <div className="drop-icon">↑</div>
              <p>Drop CSV, JSON (NDJSON) or XML, or click to browse</p>
              <input
                ref={inputRef}
                type="file"
                accept=".csv,.json,.ndjson,.xml"
                onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])}
              />
            </div>
          </NeoCard>
          <NeoCard>
            <h2>Staged Sources</h2>
            {sources === null ? (
              <p className="coverage-note">Loading…</p>
            ) : sources.length === 0 ? (
              <p className="coverage-note">No sources uploaded to this case yet.</p>
            ) : (
              sources.map((source) => (
                <div className="source-row" key={source.source_id}>
                  <div>
                    <div className="name">{source.filename}</div>
                    <div className="meta">sha256:{source.sha256.slice(0, 16)}… · {formatBytes(source.byte_size)}</div>
                  </div>
                  <span className="badge tone-muted">{source.source_format.toUpperCase()}</span>
                </div>
              ))
            )}
          </NeoCard>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard>
            <h2>Ingestion Pipeline</h2>
            {!job ? (
              <p className="coverage-note">Upload a source to start a job.</p>
            ) : (
              <>
                <div className="stage-track">
                  {STAGE_LABELS.map((label, index) => (
                    <div
                      key={label}
                      className={`stage-node ${index < stage.doneCount ? "done" : ""} ${stage.activeIndexes.includes(index) ? "active" : ""} ${stage.failed && index === stage.doneCount ? "failed" : ""}`}
                    >
                      <div className="dot-ring">{index < stage.doneCount ? "✓" : index + 1}</div>
                      <div className="label">{label}</div>
                    </div>
                  ))}
                </div>
                <p className="coverage-note" style={{ marginBottom: 16 }}>
                  This job record exposes state <span className="mono-id">{job.state}</span> / stage{" "}
                  <span className="mono-id">{job.stage}</span>. Hash+Sample / Normalize / Batch Commit / Index are not
                  individually observable stages on this backend yet — shown here as one combined in-progress phase.
                </p>
                <div className="stat-grid">
                  <StatTile label="Rows seen" value={job.rows_seen} />
                  <StatTile label="Rows accepted" value={job.rows_accepted} />
                  <StatTile label="Rows quarantined" value={job.rows_quarantined} />
                  <StatTile label="Bytes read" value={formatBytes(job.bytes_read)} />
                </div>
              </>
            )}
          </NeoCard>

          <NeoCard>
            <h2>Quarantine Ledger</h2>
            {!job || job.rows_quarantined === 0 ? (
              <p className="coverage-note">No quarantined rows on the current job.</p>
            ) : (
              <div className="quarantine-row">
                <div className="reason">{job.error_code ?? "rows quarantined"}</div>
                <div>{job.rows_quarantined} row(s) failed validation this job.</div>
                {job.error_detail && <div className="locator">{job.error_detail}</div>}
                <p className="coverage-note" style={{ marginTop: 6 }}>
                  Per-row quarantine reasons and source locators are not yet exposed by{" "}
                  <span className="mono-id">GET /v1/jobs/{"{id}"}</span> — this is a backend gap, not something faked here.
                </p>
              </div>
            )}
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}
