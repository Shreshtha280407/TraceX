import { useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Badge } from '../components/Badge'
import type { BadgeTone } from '../components/Badge'
import { CaseSwitcher } from '../components/CaseSwitcher'
import { Card } from '../components/Card'
import { DataTable, DataTableRow } from '../components/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../components/DataState'
import { evidenceApi } from '../lib/api/client'
import type { EvidenceProcessingStatus, EvidenceView, JobView } from '../lib/api/case-types'
import { useAssignedCases } from '../lib/cases'
import { useAuthStore } from '../lib/auth/store'
import { formatRelativeTime } from '../lib/format'

const COLUMNS = [
  { key: 'evidence', header: 'Evidence' },
  { key: 'sourceType', header: 'Type' },
  { key: 'status', header: 'Status' },
  { key: 'uploaded', header: 'Uploaded' },
]

const STATUS_TONE: Record<EvidenceProcessingStatus, BadgeTone> = {
  uploaded: 'steel-neutral',
  queued: 'steel-neutral',
  processing: 'berry',
  processed: 'palm',
  failed: 'crimson',
}

const IN_FLIGHT_STATUSES: EvidenceProcessingStatus[] = ['uploaded', 'queued', 'processing']
const POLL_INTERVAL_MS = 4000

interface NavState {
  caseId?: string
  jobId?: string
  evidenceId?: string
}

/**
 * Page 6 -- Processing Pipeline. Real ingestion status per evidence item,
 * from `GET /cases/{id}/evidence`'s own `processing_status` (Section 8) --
 * there is no separate job-listing endpoint (only `GET .../jobs/{job_id}`
 * for one already-known job), so per-evidence status is the real, honest
 * granularity available. Polls only while something is actually in flight,
 * never a fixed or animated fake progress bar.
 */
export function ProcessingPipeline() {
  const location = useLocation()
  const navState = (location.state as NavState | null) ?? null
  const activeCaseId = useAuthStore((state) => state.activeCaseId)
  const { cases, loading: casesLoading } = useAssignedCases()
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(navState?.caseId ?? null)
  const [items, setItems] = useState<EvidenceView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [highlightedJob, setHighlightedJob] = useState<JobView | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const caseId =
    selectedCaseId ??
    (cases.some((c) => c.case_id === activeCaseId) ? activeCaseId : cases[0]?.case_id) ??
    null

  useEffect(() => {
    if (!caseId) return
    let cancelled = false

    async function load() {
      try {
        const response = await evidenceApi.list(caseId as string)
        if (cancelled) return
        setItems(response.items)
        setError(null)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Unable to reach the server.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    // Fetch-on-case-change with loading state: the standard, correct shape
    // for this pattern -- not the "should have been derived during render"
    // case this lint rule targets.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    load()

    return () => {
      cancelled = true
    }
  }, [caseId])

  // Poll only while at least one item is genuinely still in flight.
  useEffect(() => {
    const anyInFlight = items.some((item) => IN_FLIGHT_STATUSES.includes(item.processing_status))
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
    if (!caseId || !anyInFlight) return
    pollRef.current = setInterval(() => {
      evidenceApi.list(caseId).then((response) => setItems(response.items)).catch(() => undefined)
    }, POLL_INTERVAL_MS)
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [items, caseId])

  useEffect(() => {
    if (!caseId || !navState?.jobId) return
    evidenceApi.getJob(caseId, navState.jobId).then(setHighlightedJob).catch(() => undefined)
  }, [caseId, navState?.jobId])

  if (casesLoading) return <LoadingState label="Loading your cases..." />
  if (cases.length === 0) {
    return (
      <EmptyState
        title="No assigned cases"
        description="You need an assigned case before you can view its processing pipeline."
      />
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold text-text">Processing Pipeline</h1>
        <p className="text-sm text-text-dim">
          Ingestion, extraction, and processing status for this case's evidence.
        </p>
      </div>

      <CaseSwitcher cases={cases} activeCaseId={caseId} onChange={setSelectedCaseId} />

      {highlightedJob ? (
        <Card className="flex flex-col gap-1 border-crimson/30 bg-crimson/5 p-4 text-sm">
          <span className="font-medium text-text">Just uploaded</span>
          <span className="text-xs text-text-dim">
            Job <span className="font-mono">{highlightedJob.job_id}</span> &middot;{' '}
            {highlightedJob.processor_name} &middot; attempt {highlightedJob.attempt} &middot;{' '}
            <span className="capitalize">{highlightedJob.status}</span>
          </span>
        </Card>
      ) : null}

      {error ? <ErrorState message={error} /> : null}

      {loading ? (
        <LoadingState label="Loading evidence..." />
      ) : items.length === 0 ? (
        <EmptyState
          title="No evidence uploaded yet"
          description="Upload a file to see its real processing status here."
          action={
            <Link to="/evidence/upload" className="text-xs font-medium text-crimson hover:underline">
              Upload evidence &rarr;
            </Link>
          }
        />
      ) : (
        <DataTable columns={COLUMNS}>
          {items.map((item) => (
            <DataTableRow
              key={item.evidence_id}
              columns={COLUMNS}
              primary={item.original_filename}
              secondary={item.evidence_id}
              cells={{
                sourceType: (
                  <span className="text-sm text-text-dim">{item.source_type.replace(/_/g, ' ')}</span>
                ),
                status: (
                  <Badge tone={STATUS_TONE[item.processing_status]}>{item.processing_status}</Badge>
                ),
                uploaded: (
                  <span className="text-sm text-text-dim">{formatRelativeTime(item.uploaded_at)}</span>
                ),
              }}
            />
          ))}
        </DataTable>
      )}
    </div>
  )
}
