import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, StatTile, ErrorBanner, NoticeBanner } from "../components/primitives";
import { Modal } from "../components/Modal";
import { RecordPreview } from "../components/RecordPreview";
import { api, ApiError, type EvidenceSourceRow, type ImportJob } from "../lib/api";
import { trackJob, jobsForCase } from "../lib/jobRegistry";
import { requestId } from "../lib/requestId";
import "./EvidenceIntake.css";

type PreviewRecord = { locator: string; record: unknown };

// The backend only supports looking up ONE record at a time by exact locator (no
// bulk/listing endpoint), and the locator format is format-specific (see
// app/engine/adapters/source.py). A "preview" is built by walking that same
// format's locator sequence from the start and stopping at the first 404.
const PREVIEW_SAMPLE_SIZE = 5;
const XML_TAG_CANDIDATES = ["record", "transaction", "row"];

async function fetchPreviewSample(source: EvidenceSourceRow): Promise<PreviewRecord[]> {
  const results: PreviewRecord[] = [];
  if (source.source_format === "csv" || source.source_format === "ndjson") {
    for (let i = 1; i <= PREVIEW_SAMPLE_SIZE; i++) {
      try {
        const res = await api.getEvidenceRecord(source.source_id, `record:${i}`);
        results.push({ locator: res.locator, record: res.record });
      } catch {
        break;
      }
    }
    return results;
  }
  if (source.source_format === "json") {
    for (let i = 0; i < PREVIEW_SAMPLE_SIZE; i++) {
      try {
        const res = await api.getEvidenceRecord(source.source_id, `/${i}`);
        results.push({ locator: res.locator, record: res.record });
      } catch {
        break;
      }
    }
    return results;
  }
  if (source.source_format === "xml") {
    let tag: string | null = null;
    for (const candidate of XML_TAG_CANDIDATES) {
      try {
        const res = await api.getEvidenceRecord(source.source_id, `/${candidate}[1]`);
        results.push({ locator: res.locator, record: res.record });
        tag = candidate;
        break;
      } catch {
        continue;
      }
    }
    if (!tag) return results;
    for (let i = 2; i <= PREVIEW_SAMPLE_SIZE; i++) {
      try {
        const res = await api.getEvidenceRecord(source.source_id, `/${tag}[${i}]`);
        results.push({ locator: res.locator, record: res.record });
      } catch {
        break;
      }
    }
    return results;
  }
  return results;
}

const STAGE_LABELS = ["Staging", "Parsing", "Graph", "Findings", "Anomaly Stack", "Entities & Network", "Review groups", "Ready"];

// Now that the pipeline commits a distinct stage for each phase, the tracker
// can follow the real one instead of lighting up four boxes at once.
const STAGE_INDEX: Record<string, number> = {
  queued: 0,
  ingesting: 1,
  graph_building: 2,
  findings: 3,
  ml_scoring: 4,
  analytics: 5,
  investigation_grouping: 6,
  ingested: 7,
};

function visualStage(job: ImportJob | null): { doneCount: number; activeIndexes: number[]; failed: boolean } {
  if (!job) return { doneCount: 0, activeIndexes: [], failed: false };
  const index = STAGE_INDEX[job.stage] ?? 1;
  if (job.state === "failed") return { doneCount: index, activeIndexes: [], failed: true };
  if (job.state === "completed") return { doneCount: STAGE_LABELS.length, activeIndexes: [], failed: false };
  return { doneCount: index, activeIndexes: [index], failed: false };
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
  const [previewSource, setPreviewSource] = useState<EvidenceSourceRow | null>(null);
  const [previewRecords, setPreviewRecords] = useState<PreviewRecord[] | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [fullscreenSource, setFullscreenSource] = useState<EvidenceSourceRow | null>(null);
  const [uploadingFile, setUploadingFile] = useState<string | null>(null);
  const [activityData, setActivity] = useState<Awaited<ReturnType<typeof api.getActivity>> | null>(null);
  const [activityOffset, setActivityOffset] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const activityJobId = job?.job_id;
  const activity = activityData?.case_id === caseId && activityData?.job_id === activityJobId ? activityData : null;

  useEffect(() => {
    if (!caseId || !activityJobId) return;
    let active = true;
    api.getActivity(caseId, activityJobId, activityOffset).then((value) => { if (active) setActivity(value); })
      .catch((err) => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [caseId, activityJobId, job?.rows_seen, job?.state, activityOffset]);

  async function retry() {
    if (!caseId || !job) return;
    setRetrying(true);
    try { setJob(await api.retryAnalysis(caseId, job.job_id)); }
    catch (err) { setError(String(err)); }
    finally { setRetrying(false); }
  }

  async function togglePreview(source: EvidenceSourceRow) {
    if (previewSource?.source_id === source.source_id) {
      setPreviewSource(null);
      setPreviewRecords(null);
      return;
    }
    setPreviewSource(source);
    setPreviewRecords(null);
    setPreviewLoading(true);
    const records = await fetchPreviewSample(source);
    setPreviewRecords(records);
    setPreviewLoading(false);
  }

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
    // The upload itself is one large multipart POST with no progress events (see
    // api.createImport) -- for a multi-thousand-row file that request alone can run
    // many seconds. Without this, the pipeline card stayed on "Upload a source to
    // start a job" the whole time, indistinguishable from the drop not registering.
    setUploadingFile(file.name);
    try {
      const idempotencyKey = requestId();
      const result = await api.createImport(caseId, file, idempotencyKey);
      trackJob(caseId, result.job_id);
      const initial = await api.getJob(result.job_id);
      setJob(initial);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Upload failed — could not reach the backend.");
    } finally {
      setUploadingFile(null);
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
            {/* A <label htmlFor> opens the picker through the browser's own native
                label-activation path -- no JS, no synthetic .click(). The previous
                `<div onClick={() => input.click()}>` wrapping the input recursed:
                the synthetic click bubbled back to the div and re-entered the
                handler, and Chrome silently refuses to show a file dialog once
                that happens (Playwright never hit it because it intercepts the
                file-chooser request at the CDP layer, above this guard). The input
                is kept OUTSIDE the label as a sibling so no bubbling path exists. */}
            <label
              htmlFor="evidence-file-input"
              className={`drop-zone ${dragging ? "dragging" : ""} ${uploadingFile ? "busy" : ""}`}
              onDragOver={(e) => { e.preventDefault(); if (!uploadingFile) setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={uploadingFile ? (e) => e.preventDefault() : onDrop}
            >
              <div className="drop-icon">{uploadingFile ? <span className="spinner" /> : "↑"}</div>
              <p>{uploadingFile ? `Uploading ${uploadingFile}…` : "Drop CSV, JSON (NDJSON) or XML, or click to browse"}</p>
            </label>
            <input
              id="evidence-file-input"
              ref={inputRef}
              type="file"
              className="visually-hidden-input"
              accept=".csv,.json,.ndjson,.xml"
              disabled={!!uploadingFile}
              onChange={(e) => {
                const file = e.target.files?.[0];
                // Reset so re-picking the SAME file still fires change.
                e.target.value = "";
                if (file) void upload(file);
              }}
            />
          </NeoCard>
          <NeoCard>
            <h2>Staged Sources</h2>
            {sources === null ? (
              <p className="coverage-note">Loading…</p>
            ) : sources.length === 0 ? (
              <p className="coverage-note">No sources uploaded to this case yet.</p>
            ) : (
              sources.map((source) => {
                const isOpen = previewSource?.source_id === source.source_id;
                return (
                  <div className="source-item" key={source.source_id}>
                    <div className="source-row" style={{ cursor: "pointer" }} onClick={() => togglePreview(source)}>
                      <div>
                        <div className="name">{source.filename}</div>
                        <div className="meta">sha256:{source.sha256.slice(0, 16)}… · {formatBytes(source.byte_size)}</div>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span className="badge tone-muted">{source.source_format.toUpperCase()}</span>
                        <span className="source-expand-caret">{isOpen ? "▾" : "▸"}</span>
                      </div>
                    </div>
                    {isOpen && (
                      <div className="source-preview">
                        {previewLoading ? (
                          <p className="coverage-note">Loading preview…</p>
                        ) : !previewRecords || previewRecords.length === 0 ? (
                          <p className="coverage-note">Could not load a preview for this source.</p>
                        ) : (
                          <>
                            <div className="source-preview-list">
                              {previewRecords.map((r) => (
                                <div className="source-preview-record" key={r.locator}>
                                  <div className="mono-id">{r.locator}</div>
                                  <RecordPreview record={r.record} isCsv={source.source_format === "csv"} />
                                </div>
                              ))}
                            </div>
                            <p className="coverage-note" style={{ marginTop: 6 }}>
                              Showing the first {previewRecords.length} record{previewRecords.length === 1 ? "" : "s"} of this source.
                            </p>
                            <button
                              type="button"
                              className="btn-ghost"
                              style={{ padding: "4px 10px", fontSize: 13, marginTop: 6 }}
                              onClick={(e) => { e.stopPropagation(); setFullscreenSource(source); }}
                            >
                              ⤢ Full screen
                            </button>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </NeoCard>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard>
            <h2>Ingestion Pipeline</h2>
            {uploadingFile && !job ? (
              <div className="upload-in-flight">
                <div className="spinner" />
                <p className="coverage-note">Uploading {uploadingFile}… large files can take a while, this page will switch to live stage tracking once the job is accepted.</p>
              </div>
            ) : !job ? (
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
                <div className="ingest-progress">
                  <div className="ingest-progress-head">
                    <span className="mono-id">{job.state} · {job.stage}</span>
                    <span className="ingest-progress-pct">
                      {job.progress?.percent !== null && job.progress?.percent !== undefined
                        ? `${job.progress.percent}%`
                        : "in progress"}
                    </span>
                  </div>
                  <div className={`ingest-progress-track ${job.progress?.determinate === false ? "indeterminate" : ""}`}>
                    <div
                      className="ingest-progress-fill"
                      style={job.progress?.determinate !== false ? { width: `${job.progress?.percent ?? 0}%` } : undefined}
                    />
                  </div>
                  <p className="coverage-note">{job.progress?.basis ?? "waiting for the worker to claim this job"}</p>
                </div>
                <div className="stat-grid">
                  <StatTile
                    label="Total records"
                    value={job.total_records !== null ? job.total_records.toLocaleString() : job.state === "queued" ? "…" : "—"}
                    sub={job.total_records !== null ? "counted before parsing" : job.state === "queued" ? "counted when the worker starts" : "could not be counted"}
                  />
                  <StatTile
                    label="Rows seen"
                    value={job.rows_seen.toLocaleString()}
                    sub={job.total_records ? `${Math.max(0, job.total_records - job.rows_seen).toLocaleString()} remaining` : undefined}
                  />
                  <StatTile label="Rows accepted" value={job.rows_accepted.toLocaleString()} />
                  <StatTile label="Rows quarantined" value={job.rows_quarantined.toLocaleString()} />
                  <StatTile label="Bytes read" value={formatBytes(job.bytes_read)} />
                </div>
                {job.analysis && job.analysis.state !== "complete" && job.state === "completed" && (
                  <NoticeBanner>Evidence imported. Analysis is {job.analysis.state}.
                    {job.analysis.stages.filter((s) => !["complete", "written", "no_rows_flagged"].includes(s.status)).map((s) =>
                      <p key={s.name}>{s.name}: {s.reason ?? s.status}</p>)}
                  </NoticeBanner>
                )}
                {job.analysis?.retry_supported && <button className="btn-secondary" type="button" disabled={retrying} onClick={() => void retry()}>
                  {retrying ? "Queuing…" : "Retry analysis from committed evidence"}
                </button>}
                {job.analysis && <div className="coverage-note" aria-label="Analysis stage outcomes">
                  {job.analysis.stages.map((s) => <p key={s.name}>{s.name}: {s.status}{s.duration_seconds !== null ? ` · ${s.duration_seconds.toFixed(1)}s` : ""}</p>)}
                </div>}
              </>
            )}
          </NeoCard>

          <NeoCard>
            <h2>Committed address activity</h2>
            <p className="coverage-note">{!activity ? "Awaiting committed evidence" : activity.provisional ? "Provisional and incomplete" : "Finalized receipt view"} · addresses are participation evidence; proposed wallet entities become available after analytics.</p>
            {!activity?.entities.length ? <p className="coverage-note">No addresses in committed evidence yet. Parsing may still be active.</p> :
              activity.entities.map((item) => <p key={item.address}><span className="mono-id">{item.address}</span> · {item.transactions} transactions</p>)}
            {activity && activity.total > 20 && <div>
              <button type="button" disabled={activityOffset === 0} onClick={() => setActivityOffset(Math.max(0, activityOffset - 20))}>Previous</button>
              <button type="button" disabled={activityOffset + 20 >= activity.total} onClick={() => setActivityOffset(activityOffset + 20)}>Next addresses</button>
            </div>}
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

      <Modal
        open={!!fullscreenSource}
        onClose={() => setFullscreenSource(null)}
        title={fullscreenSource?.filename ?? "Source preview"}
        wide
      >
        <div className="modal-body-scroll">
          {previewRecords?.map((r) => (
            <div key={r.locator} style={{ marginBottom: 16 }}>
              <div className="mono-id" style={{ marginBottom: 6 }}>{r.locator}</div>
              <RecordPreview record={r.record} isCsv={fullscreenSource?.source_format === "csv"} preClassName="fullscreen-record-pre" />
            </div>
          ))}
        </div>
      </Modal>
    </Shell>
  );
}
