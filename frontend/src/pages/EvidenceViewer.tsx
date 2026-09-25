import { useEffect, useState } from 'react'
import { FileSearch, ShieldAlert, ShieldCheck } from 'lucide-react'
import { useLocation } from 'react-router-dom'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Badge } from '../components/Badge'
import type { BadgeTone } from '../components/Badge'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { ApiError, evidenceApi, graphApi } from '../lib/api/client'
import type { EvidenceIntegrityCheck, EvidenceProcessingStatus, EvidenceView } from '../lib/api/case-types'
import type { EvidenceObservationView } from '../lib/api/graph-types'
import { useActiveCaseWorkspace } from '../lib/cases'
import { formatRelativeTime, formatSourceLocator } from '../lib/format'

const STATUS_TONE: Record<EvidenceProcessingStatus, BadgeTone> = {
  uploaded: 'steel-neutral',
  queued: 'steel-neutral',
  processing: 'berry',
  processed: 'palm',
  failed: 'crimson',
}

interface NavState {
  observationId?: string
}

/**
 * Page 13 -- Evidence Viewer. Real drill-down to the exact source location
 * (page/row/frame/timestamp) for a piece of evidence (Section 5's own role
 * text), backed by `GET .../evidence/{id}/observations` -- each observation's
 * real `source_locator`, never a fabricated placeholder. A citation from
 * Hypotheses/Candidate Review (an `observation_id`) arrives via router
 * `state.observationId` (matches Processing Pipeline's existing `NavState`
 * pattern) and is resolved through `GET .../observations/{id}/provenance`
 * to auto-select its evidence and highlight the exact observation.
 */
export function EvidenceViewer() {
  const location = useLocation()
  const navState = (location.state as NavState | null) ?? null
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()

  const [items, setItems] = useState<EvidenceView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null)
  const [highlightObservationId] = useState<string | null>(navState?.observationId ?? null)

  const [observations, setObservations] = useState<EvidenceObservationView[]>([])
  const [obsLoading, setObsLoading] = useState(false)
  const [obsError, setObsError] = useState<string | null>(null)
  const [truncated, setTruncated] = useState(false)

  const [integrity, setIntegrity] = useState<EvidenceIntegrityCheck | null>(null)
  const [integrityLoading, setIntegrityLoading] = useState(false)
  const [integrityError, setIntegrityError] = useState<string | null>(null)

  useEffect(() => {
    if (!activeCaseId) return
    let cancelled = false
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError(null)
    setForbidden(false)
    evidenceApi
      .list(activeCaseId)
      .then((response) => {
        if (!cancelled) setItems(response.items)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && err.isForbidden) setForbidden(true)
        else setError(err instanceof Error ? err.message : 'Unable to reach the server.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [activeCaseId])

  // Citation drill-down: resolve which evidence a cited observation belongs to.
  useEffect(() => {
    if (!activeCaseId || !highlightObservationId) return
    let cancelled = false
    graphApi
      .getObservationProvenance(activeCaseId, highlightObservationId)
      .then((response) => {
        if (!cancelled && response.evidence) setSelectedEvidenceId(response.evidence.evidence_id)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [activeCaseId, highlightObservationId])

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    setIntegrity(null)
    setIntegrityError(null)
    if (!activeCaseId || !selectedEvidenceId) {
      setObservations([])
      return
    }
    let cancelled = false
    // oxlint-disable-next-line react/set-state-in-effect
    setObsLoading(true)
    setObsError(null)
    graphApi
      .getEvidenceObservations(activeCaseId, selectedEvidenceId)
      .then((response) => {
        if (cancelled) return
        setObservations(response.items)
        setTruncated(response.truncated)
      })
      .catch((err: unknown) => {
        if (!cancelled) setObsError(err instanceof Error ? err.message : 'Unable to reach the server.')
      })
      .finally(() => {
        if (!cancelled) setObsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [activeCaseId, selectedEvidenceId])

  function verifyIntegrity() {
    if (!activeCaseId || !selectedEvidenceId) return
    setIntegrityLoading(true)
    setIntegrityError(null)
    evidenceApi
      .getIntegrity(activeCaseId, selectedEvidenceId)
      .then(setIntegrity)
      .catch((err: unknown) => {
        setIntegrityError(err instanceof Error ? err.message : 'Unable to reach the server.')
      })
      .finally(() => setIntegrityLoading(false))
  }

  const selectedEvidence = items.find((item) => item.evidence_id === selectedEvidenceId) ?? null

  return (
    <div className="flex flex-col gap-6">
      <div>
        {activeCase ? (
          <span className="font-mono text-xs uppercase tracking-wider text-text-faint">
            {activeCase.case_reference}
          </span>
        ) : null}
        <h1 className="text-2xl font-semibold text-text">Evidence Viewer</h1>
        <p className="text-sm text-text-dim">Exact source inspection -- page/row/frame/timestamp drill-down.</p>
      </div>

      {!activeCase ? (
        <ActiveCaseGate
          cases={cases}
          loading={casesLoading}
          activeCaseId={activeCaseId}
          onSelect={setActiveCase}
        />
      ) : forbidden ? (
        <ForbiddenState message="You don't have permission to view this case's evidence." />
      ) : error ? (
        <ErrorState message={error} />
      ) : loading ? (
        <LoadingState label="Loading evidence..." />
      ) : items.length === 0 ? (
        <EmptyState
          title="No evidence uploaded yet"
          description="Upload a file to inspect its exact source locations here."
        />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[320px_1fr]">
          <div className="flex max-h-[640px] flex-col gap-1 overflow-y-auto">
            {items.map((item) => (
              <button
                key={item.evidence_id}
                type="button"
                onClick={() => setSelectedEvidenceId(item.evidence_id)}
                className={`flex flex-col items-start gap-0.5 rounded-control border px-3 py-2 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson ${
                  item.evidence_id === selectedEvidenceId
                    ? 'border-crimson bg-crimson/5'
                    : 'border-card-border bg-card hover:bg-canvas/10'
                }`}
              >
                <span className="w-full truncate text-sm font-medium text-text">
                  {item.original_filename}
                </span>
                <span className="flex w-full items-center justify-between gap-2 text-xs text-text-faint">
                  <span className="truncate">{item.source_type.replace(/_/g, ' ')}</span>
                  <Badge tone={STATUS_TONE[item.processing_status]}>{item.processing_status}</Badge>
                </span>
              </button>
            ))}
          </div>

          {!selectedEvidence ? (
            <Card className="flex flex-col items-center gap-2 p-8 text-center text-sm text-text-dim">
              Select a piece of evidence from the list to inspect its exact source locations.
            </Card>
          ) : (
            <div className="flex flex-col gap-4">
              <Card className="flex flex-col gap-3 p-5">
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <span className="text-sm font-medium text-text">
                      {selectedEvidence.original_filename}
                    </span>
                    <div className="mt-1 font-mono text-xs text-text-faint">
                      {selectedEvidence.evidence_id}
                    </div>
                  </div>
                  <Badge tone={STATUS_TONE[selectedEvidence.processing_status]}>
                    {selectedEvidence.processing_status}
                  </Badge>
                </div>
                <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
                  <div>
                    <dt className="text-text-faint">Type</dt>
                    <dd className="text-text-dim">{selectedEvidence.source_type.replace(/_/g, ' ')}</dd>
                  </div>
                  <div>
                    <dt className="text-text-faint">Classification</dt>
                    <dd className="capitalize text-text-dim">{selectedEvidence.classification}</dd>
                  </div>
                  <div>
                    <dt className="text-text-faint">Uploaded</dt>
                    <dd className="text-text-dim">{formatRelativeTime(selectedEvidence.uploaded_at)}</dd>
                  </div>
                  <div className="col-span-2 sm:col-span-1">
                    <dt className="text-text-faint">SHA-256</dt>
                    <dd className="truncate font-mono text-text-dim" title={selectedEvidence.sha256}>
                      {selectedEvidence.sha256}
                    </dd>
                  </div>
                </dl>
                <div className="flex flex-wrap items-center gap-3 border-t border-card-border pt-3">
                  <Button variant="secondary" onClick={verifyIntegrity} disabled={integrityLoading}>
                    {integrityLoading ? 'Verifying...' : 'Verify hash integrity'}
                  </Button>
                  {integrityError ? <span className="text-xs text-crimson">{integrityError}</span> : null}
                  {integrity ? (
                    integrity.matches ? (
                      <span className="flex items-center gap-1.5 text-xs font-medium text-palm">
                        <ShieldCheck size={14} aria-hidden="true" />
                        Hash verified -- matches the ingestion-time value.
                      </span>
                    ) : (
                      <span className="flex items-center gap-1.5 text-xs font-medium text-crimson">
                        <ShieldAlert size={14} aria-hidden="true" />
                        Hash mismatch -- stored object has changed since ingestion.
                      </span>
                    )
                  ) : null}
                </div>
              </Card>

              <div>
                <h2 className="text-sm font-semibold text-text">Extracted observations &amp; exact source locations</h2>
                {truncated ? (
                  <p className="text-xs text-text-faint">
                    Showing a bounded page -- more observations exist for this evidence.
                  </p>
                ) : null}
              </div>

              {obsError ? (
                <ErrorState message={obsError} />
              ) : obsLoading ? (
                <LoadingState label="Loading observations..." />
              ) : observations.length === 0 ? (
                <EmptyState
                  title="No observations yet"
                  description="This evidence hasn't yielded any extracted observations -- it may still be processing."
                />
              ) : (
                <div className="flex flex-col gap-2">
                  {observations.map((observation) => {
                    const locatorParts = formatSourceLocator(observation.source_locator)
                    const isHighlighted = observation.observation_id === highlightObservationId
                    return (
                      <Card
                        key={observation.observation_id}
                        className={`flex flex-col gap-2 p-4 ${isHighlighted ? 'border-crimson bg-crimson/5' : ''}`}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <span className="flex items-center gap-2 text-sm font-medium text-text">
                            <FileSearch size={14} className="text-text-faint" aria-hidden="true" />
                            {observation.observation_type.replace(/_/g, ' ')}
                          </span>
                          <span className="font-mono text-xs text-text-faint">
                            {observation.observation_id}
                          </span>
                        </div>
                        <div className="flex flex-wrap gap-2">
                          {locatorParts.length === 0 ? (
                            <span className="text-xs text-text-faint">No locator detail recorded.</span>
                          ) : (
                            locatorParts.map((part) => (
                              <Badge key={part.label} tone="steel-neutral">
                                {part.label}: {part.value}
                              </Badge>
                            ))
                          )}
                        </div>
                        <div className="flex flex-wrap items-center gap-3 text-xs text-text-faint">
                          <span>Extraction confidence {(observation.extraction_confidence * 100).toFixed(0)}%</span>
                          <span>&middot;</span>
                          <span>
                            {observation.extractor_name} v{observation.extractor_version}
                          </span>
                          {observation.event_time ? (
                            <>
                              <span>&middot;</span>
                              <span>{formatRelativeTime(observation.event_time)}</span>
                            </>
                          ) : null}
                        </div>
                      </Card>
                    )
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
