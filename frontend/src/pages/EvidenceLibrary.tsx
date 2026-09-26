import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { Check, FileSearch, FolderOpen, UploadCloud } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '../components/Badge'
import type { BadgeTone } from '../components/Badge'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { CaseSwitcher } from '../components/CaseSwitcher'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { ApiError, evidenceApi } from '../lib/api/client'
import type {
  EvidenceClassification,
  EvidenceLibraryItem,
  EvidenceProcessingStatus,
  SourceType,
} from '../lib/api/case-types'
import { useAssignedCases } from '../lib/cases'
import { useAuthStore } from '../lib/auth/store'
import { roleHasCaseAction } from '../lib/auth/permissions'
import { formatRelativeTime } from '../lib/format'

const CLASSIFICATION_LABEL: Record<EvidenceClassification, string> = {
  unclassified: 'Standard',
  restricted: 'Standard',
  confidential: 'Sensitive',
  secret: 'Highly Sensitive',
}

const CLASSIFICATION_RANK: Record<EvidenceClassification, number> = {
  unclassified: -1,
  restricted: 0,
  confidential: 1,
  secret: 2,
}

const EVIDENCE_CLASSIFICATIONS: EvidenceClassification[] = [
  'restricted',
  'confidential',
  'secret',
]

const STATUS_TONE: Record<EvidenceProcessingStatus, BadgeTone> = {
  uploaded: 'steel-neutral',
  queued: 'steel-neutral',
  processing: 'berry',
  processed: 'palm',
  failed: 'crimson',
}

const SOURCE_LABEL: Record<SourceType, string> = {
  document: 'Document', cdr: 'CDR', financial: 'Financial', video: 'Video', image: 'Image',
  audio: 'Audio', chat: 'Chat', structured_tabular: 'Spreadsheet', structured_json: 'Structured JSON',
  audio_transcript: 'Audio transcript', audio_diarization: 'Audio diarization', whatsapp_chat: 'WhatsApp',
  telegram_chat: 'Telegram', instagram_chat: 'Instagram', other: 'Other',
}

function ProgressRing({ item }: { item: EvidenceLibraryItem }) {
  const job = item.job
  const percent = job?.progress_percent ?? null
  const isReady = job?.current_stage === 'Ready'
  const isFailed = job?.current_stage === 'Processing failed'
  const dashOffset = percent === null ? 42 : 100 - percent
  return (
    <div className="flex min-w-[108px] flex-col items-center gap-1 text-center">
      <div className={`relative h-11 w-11 ${percent === null && !isReady && !isFailed ? 'animate-spin' : ''}`}>
        <svg viewBox="0 0 36 36" className="h-11 w-11 -rotate-90" aria-label={job?.current_stage ?? 'Waiting to start'}>
          <path className="fill-none stroke-card-border" strokeWidth="3" d="M18 2.5a15.5 15.5 0 1 1 0 31a15.5 15.5 0 1 1 0-31" />
          <path
            className={`fill-none ${isFailed ? 'stroke-crimson' : isReady ? 'stroke-palm' : 'stroke-crimson'}`}
            strokeWidth="3"
            strokeDasharray={percent === null ? '18 82' : '100 100'}
            strokeDashoffset={dashOffset}
            strokeLinecap="round"
            d="M18 2.5a15.5 15.5 0 1 1 0 31a15.5 15.5 0 1 1 0-31"
          />
        </svg>
        {isReady ? <Check size={18} className="absolute left-3 top-3 text-palm" aria-hidden="true" /> : null}
        {percent !== null && !isReady ? <span className="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-text">{percent}%</span> : null}
      </div>
      <span className="max-w-28 text-xs text-text-dim">{job?.current_stage ?? 'Waiting to start'}</span>
    </div>
  )
}

/** One case-scoped library backed by real evidence/jobs/observation output. */
export function EvidenceLibrary() {
  const activeCaseId = useAuthStore((state) => state.activeCaseId)
  const { cases, loading: casesLoading } = useAssignedCases()
  const navigate = useNavigate()
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null)
  const [items, setItems] = useState<EvidenceLibraryItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [searchInput, setSearchInput] = useState('')
  const [query, setQuery] = useState('')
  const [sourceType, setSourceType] = useState<SourceType | ''>('')
  const [processingStatus, setProcessingStatus] = useState<EvidenceProcessingStatus | ''>('')
  const [classification, setClassification] = useState<EvidenceClassification | ''>('')
  const [showUpload, setShowUpload] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [uploadClassification, setUploadClassification] = useState<EvidenceClassification>('restricted')
  const [uploading, setUploading] = useState(false)
  const [uploadMessage, setUploadMessage] = useState<string | null>(null)
  const [retryingEvidenceId, setRetryingEvidenceId] = useState<string | null>(null)

  const caseId = selectedCaseId ?? (cases.some((item) => item.case_id === activeCaseId) ? activeCaseId : cases[0]?.case_id) ?? null
  const selectedCase = cases.find((item) => item.case_id === caseId)
  const canUpload = selectedCase ? roleHasCaseAction(selectedCase.role, 'evidence_write') : false
  const isCaseOwner = selectedCase?.role === 'case_owner'
  const permittedUploadClassifications = selectedCase
    ? EVIDENCE_CLASSIFICATIONS.filter(
      (item) => CLASSIFICATION_RANK[item] >= CLASSIFICATION_RANK[selectedCase.classification as EvidenceClassification],
    )
    : []

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    const handle = window.setTimeout(() => setQuery(searchInput.trim()), 300)
    return () => window.clearTimeout(handle)
  }, [searchInput])

  const filters = useMemo(() => ({
    query: query || undefined,
    source_type: sourceType || undefined,
    processing_status: processingStatus || undefined,
    classification: classification || undefined,
    limit: 100,
  }), [query, sourceType, processingStatus, classification])

  const load = useCallback(async (silent = false) => {
    if (!caseId) return
    if (!silent) setLoading(true)
    try {
      const response = await evidenceApi.library(caseId, filters)
      setItems(response.items)
      setError(null)
      setForbidden(false)
    } catch (err) {
      if (err instanceof ApiError && err.isForbidden) setForbidden(true)
      else setError(err instanceof Error ? err.message : 'Unable to load this evidence library.')
    } finally {
      if (!silent) setLoading(false)
    }
  }, [caseId, filters])

  // oxlint-disable-next-line react/set-state-in-effect
  useEffect(() => { void load() }, [load])

  useEffect(() => {
    const inFlight = items.some((item) => item.job && !['succeeded', 'failed', 'deferred', 'cancelled'].includes(item.job.status))
    if (!caseId || !inFlight) return
    const timer = window.setInterval(() => { void load(true) }, 4000)
    return () => window.clearInterval(timer)
  }, [caseId, items, load])

  async function upload(event: FormEvent) {
    event.preventDefault()
    if (!file || !caseId) return
    setUploading(true)
    setError(null)
    try {
      const response = await evidenceApi.upload(caseId, file, {
        classification: isCaseOwner ? uploadClassification : undefined,
      })
      setUploadMessage(`Uploaded and detected as ${SOURCE_LABEL[response.evidence.source_type]}; ${response.job.current_stage}.`)
      setFile(null)
      setShowUpload(false)
      await load(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to upload evidence.')
    } finally {
      setUploading(false)
    }
  }

  async function retryEvidence(evidenceId: string) {
    if (!caseId) return
    setRetryingEvidenceId(evidenceId)
    setError(null)
    try {
      const response = await evidenceApi.reprocess(caseId, evidenceId, crypto.randomUUID())
      setUploadMessage(`Processing retried; ${response.job.current_stage}.`)
      await load(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to retry this evidence.')
    } finally {
      setRetryingEvidenceId(null)
    }
  }

  if (casesLoading) return <LoadingState label="Loading your cases..." />
  if (cases.length === 0) return <EmptyState title="No assigned cases" description="You need an assigned case before you can use the Evidence Library." />

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-text">Evidence Library</h1>
          <p className="text-sm text-text-dim">Secure, case-scoped sources and real processing status.</p>
        </div>
        {canUpload ? (
          <Button onClick={() => {
            if (!showUpload && selectedCase) {
              setUploadClassification(selectedCase.classification as EvidenceClassification)
            }
            setShowUpload((value) => !value)
          }}><UploadCloud size={16} />Upload Evidence</Button>
        ) : null}
      </div>

      <CaseSwitcher cases={cases} activeCaseId={caseId} onChange={setSelectedCaseId} disabled={uploading} />

      {showUpload ? (
        <Card className="max-w-2xl p-5">
          <h2 className="text-sm font-semibold text-text">Upload evidence</h2>
          <p className="mt-1 text-xs text-text-dim">TraceX inspects the file and selects a permitted processing pipeline.</p>
          <form className="mt-4 flex flex-wrap items-end gap-3" onSubmit={upload}>
            <label className="flex min-w-64 flex-1 flex-col gap-1.5 text-sm"><span className="font-medium text-text-dim">File</span><input type="file" required onChange={(event) => setFile(event.target.files?.[0] ?? null)} className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text file:mr-3 file:rounded-control file:border-0 file:bg-crimson file:px-3 file:py-1.5 file:text-xs file:text-shell-text" /></label>
            {isCaseOwner ? (
              <label className="flex min-w-48 flex-col gap-1.5 text-sm">
                <span className="font-medium text-text-dim">Evidence classification</span>
                <select value={uploadClassification} onChange={(event) => setUploadClassification(event.target.value as EvidenceClassification)} className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text">
                  {permittedUploadClassifications.map((item) => <option key={item} value={item}>{CLASSIFICATION_LABEL[item]}</option>)}
                </select>
              </label>
            ) : null}
            <Button type="submit" disabled={!file || uploading}>{uploading ? 'Uploading securely…' : 'Upload Evidence'}</Button>
          </form>
        </Card>
      ) : null}

      {uploadMessage ? <div className="rounded-control border border-palm/30 bg-palm/10 px-3 py-2 text-sm text-palm">{uploadMessage}</div> : null}
      {forbidden ? <ForbiddenState message="You don't have permission to view this case's evidence." /> : null}
      {error ? <ErrorState message={error} /> : null}

      <Card className="flex flex-wrap gap-3 p-4">
        <label className="min-w-64 flex-1"><span className="sr-only">Search evidence</span><div className="flex items-center gap-2 rounded-control border border-card-border bg-card px-3 py-2"><FileSearch size={16} className="text-text-faint" /><input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="Search by name, source, ID, or extracted text…" className="w-full bg-transparent text-sm text-text outline-none placeholder:text-text-faint" /></div></label>
        <select aria-label="File type" value={sourceType} onChange={(event) => setSourceType(event.target.value as SourceType | '')} className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text"><option value="">All file types</option>{Object.entries(SOURCE_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
        <select aria-label="Processing status" value={processingStatus} onChange={(event) => setProcessingStatus(event.target.value as EvidenceProcessingStatus | '')} className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text"><option value="">All statuses</option><option value="queued">Waiting</option><option value="processing">Processing</option><option value="processed">Ready</option><option value="failed">Failed</option></select>
        <select aria-label="Classification" value={classification} onChange={(event) => setClassification(event.target.value as EvidenceClassification | '')} className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text"><option value="">All classifications</option><option value="restricted">Standard</option><option value="confidential">Sensitive</option><option value="secret">Highly Sensitive</option></select>
      </Card>

      {loading ? <LoadingState label="Loading evidence..." /> : !forbidden && !error && items.length === 0 ? (
        <EmptyState title={query ? 'No matching evidence' : 'No evidence uploaded yet'} description={query ? 'Try a different search or filter.' : 'Upload a supported file to begin secure processing.'} />
      ) : (
        <div className="flex flex-col gap-3">
          {items.map((item) => (
            <Card key={item.evidence.evidence_id} className="flex flex-col gap-4 p-4 lg:flex-row lg:items-center">
              <div className="min-w-0 flex-1"><button type="button" onClick={() => navigate('/evidence', { state: { evidenceId: item.evidence.evidence_id } })} className="max-w-full truncate text-left text-sm font-semibold text-text hover:text-crimson">{item.evidence.original_filename}</button><p className="mt-1 truncate font-mono text-xs text-text-faint">{item.evidence.evidence_id}</p><p className="mt-2 text-xs text-text-dim">{SOURCE_LABEL[item.evidence.source_type]} · {item.uploader_display_name ?? 'Case member'} · {formatRelativeTime(item.evidence.uploaded_at)}</p>{item.preview ? <p className="mt-2 line-clamp-2 text-sm text-text-dim">{item.preview}</p> : <p className="mt-2 text-xs text-text-faint">{item.searchable ? 'No extracted preview is available.' : 'Extracted text will become searchable when processing is ready.'}</p>}</div>
              <div className="flex items-center gap-4"><div className="flex flex-col gap-2"><Badge tone={STATUS_TONE[item.evidence.processing_status]}>{CLASSIFICATION_LABEL[item.evidence.classification]}</Badge><span className="text-xs capitalize text-text-faint">{SOURCE_LABEL[item.evidence.source_type]}</span></div><ProgressRing item={item} /><div className="flex flex-col gap-2"><Button variant="secondary" onClick={() => navigate('/evidence', { state: { evidenceId: item.evidence.evidence_id } })}><FolderOpen size={15} />Open</Button>{canUpload && item.evidence.processing_status === 'failed' ? <Button variant="secondary" disabled={retryingEvidenceId === item.evidence.evidence_id} onClick={() => void retryEvidence(item.evidence.evidence_id)}>{retryingEvidenceId === item.evidence.evidence_id ? 'Retrying…' : 'Retry'}</Button> : null}</div></div>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
