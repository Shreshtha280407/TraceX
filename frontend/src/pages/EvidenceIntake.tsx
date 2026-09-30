import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, StatTile, ErrorBanner } from "../components/primitives";
import { Modal } from "../components/Modal";
import { RecordPreview } from "../components/RecordPreview";
import { api, ApiError, type EvidenceSourceRow, type ImportJob } from "../lib/api";
import { trackJob, jobsForCase } from "../lib/jobRegistry";
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
  const [previewSource, setPreviewSource] = useState<EvidenceSourceRow | null>(null);
  const [previewRecords, setPreviewRecords] = useState<PreviewRecord[] | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [fullscreenSource, setFullscreenSource] = useState<EvidenceSourceRow | null>(null);

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
                              style={{ padding: "4px 10px", fontSize: 11, marginTop: 6 }}
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
