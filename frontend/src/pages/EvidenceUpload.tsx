import { useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, CheckCircle2, UploadCloud } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { CaseSwitcher } from '../components/CaseSwitcher'
import { EmptyState, ForbiddenState, LoadingState } from '../components/DataState'
import { ApiError, evidenceApi } from '../lib/api/client'
import type { EvidenceClassification, SourceType } from '../lib/api/case-types'
import { useAssignedCases } from '../lib/cases'
import { useAuthStore } from '../lib/auth/store'

const SOURCE_TYPES: { value: SourceType; label: string }[] = [
  { value: 'document', label: 'Document (FIR, report, PDF)' },
  { value: 'cdr', label: 'CDR (call detail records)' },
  { value: 'financial', label: 'Financial records' },
  { value: 'video', label: 'Video' },
  { value: 'image', label: 'Image' },
  { value: 'audio', label: 'Audio' },
  { value: 'audio_transcript', label: 'Audio transcript' },
  { value: 'audio_diarization', label: 'Audio diarization' },
  { value: 'chat', label: 'Chat export (generic)' },
  { value: 'whatsapp_chat', label: 'WhatsApp chat export' },
  { value: 'telegram_chat', label: 'Telegram chat export' },
  { value: 'instagram_chat', label: 'Instagram chat export' },
  { value: 'structured_tabular', label: 'Structured tabular (CSV/XLSX)' },
  { value: 'structured_json', label: 'Structured JSON' },
  { value: 'other', label: 'Other' },
]

const CLASSIFICATIONS: { value: EvidenceClassification; label: string }[] = [
  { value: 'unclassified', label: 'Unclassified' },
  { value: 'restricted', label: 'Restricted' },
  { value: 'confidential', label: 'Confidential' },
  { value: 'secret', label: 'Secret' },
]

/**
 * Page 5 -- Evidence Upload. Real multipart upload against
 * `POST /cases/{id}/evidence` -- every source type and classification is a
 * real backend enum value (app/contracts/evidence.py), never a client
 * invention. A case-scoped 403 (this investigator lacks `evidence_write`
 * on the case picked in the switcher, even if the sidebar's default active
 * case did grant it) is a real, expected outcome here -- rendered calmly,
 * not as a crash (Section 6).
 */
export function EvidenceUpload() {
  const activeCaseId = useAuthStore((state) => state.activeCaseId)
  const { cases, loading: casesLoading } = useAssignedCases()
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [sourceType, setSourceType] = useState<SourceType>('document')
  const [classification, setClassification] = useState<EvidenceClassification>('unclassified')
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [result, setResult] = useState<{ evidenceId: string; jobId: string } | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const caseId = selectedCaseId ?? (cases.some((c) => c.case_id === activeCaseId) ? activeCaseId : cases[0]?.case_id) ?? null

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    if (!file || !caseId) return
    setError(null)
    setForbidden(false)
    setResult(null)
    setSubmitting(true)
    try {
      const response = await evidenceApi.upload(caseId, file, sourceType, classification)
      setResult({ evidenceId: response.evidence.evidence_id, jobId: response.job.job_id })
      setFile(null)
    } catch (err) {
      if (err instanceof ApiError && err.isForbidden) {
        setForbidden(true)
      } else {
        setError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  if (casesLoading) return <LoadingState label="Loading your cases..." />
  if (cases.length === 0) {
    return (
      <EmptyState
        title="No assigned cases"
        description="You need an assigned case before you can upload evidence."
        action={
          <Link to="/cases/new" className="text-xs font-medium text-crimson hover:underline">
            Create a case &rarr;
          </Link>
        }
      />
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold text-text">Evidence Upload</h1>
        <p className="text-sm text-text-dim">Upload FIR / CDR / finance / video / audio / images / chat.</p>
      </div>

      <Card className="max-w-xl p-6">
        <div className="mb-4">
          <CaseSwitcher
            cases={cases}
            activeCaseId={caseId}
            onChange={setSelectedCaseId}
            disabled={submitting}
          />
        </div>

        {forbidden ? (
          <div className="mb-4">
            <ForbiddenState message="You don't have permission to upload evidence to this case." />
          </div>
        ) : null}
        {error ? (
          <div className="mb-4 flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
            <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </div>
        ) : null}
        {result ? (
          <div className="mb-4 flex flex-col gap-2 rounded-control border border-palm/30 bg-palm/10 px-3 py-2 text-xs text-palm">
            <span className="flex items-start gap-2">
              <CheckCircle2 size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
              Uploaded. Evidence ID: <span className="font-mono">{result.evidenceId}</span>
            </span>
            <Link
              to="/pipeline"
              state={{ caseId, jobId: result.jobId, evidenceId: result.evidenceId }}
              className="font-medium underline"
            >
              View in Processing Pipeline &rarr;
            </Link>
          </div>
        ) : null}

        <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="font-medium text-text-dim">File</span>
            <input
              type="file"
              required
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text file:mr-3 file:rounded-control file:border-0 file:bg-crimson file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-shell-text"
            />
          </label>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="font-medium text-text-dim">Source type</span>
            <select
              value={sourceType}
              onChange={(event) => setSourceType(event.target.value as SourceType)}
              className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
            >
              {SOURCE_TYPES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="font-medium text-text-dim">Classification</span>
            <select
              value={classification}
              onChange={(event) => setClassification(event.target.value as EvidenceClassification)}
              className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
            >
              {CLASSIFICATIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <Button type="submit" disabled={submitting || !file || !caseId} className="mt-2 w-fit">
            <UploadCloud size={16} aria-hidden="true" />
            {submitting ? 'Uploading...' : 'Upload evidence'}
          </Button>
        </form>
      </Card>
    </div>
  )
}
