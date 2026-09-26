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
import type {
  EvidenceClassification,
  EvidenceIntegrityCheck,
  EvidenceProcessingStatus,
  EvidenceView,
  SourceType,
} from '../lib/api/case-types'
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

const SOURCE_LABEL: Record<SourceType, string> = {
  document: 'Document', cdr: 'CDR', financial: 'Financial', video: 'Video', image: 'Image',
  audio: 'Audio', chat: 'Chat', structured_tabular: 'Spreadsheet', structured_json: 'Structured JSON',
  audio_transcript: 'Audio transcript', audio_diarization: 'Audio diarization', whatsapp_chat: 'WhatsApp',
  telegram_chat: 'Telegram', instagram_chat: 'Instagram', other: 'Other',
}

function compatibleTypeCorrections(contentType: string, detected: SourceType): SourceType[] {
  const mediaType = contentType.split(';', 1)[0].toLowerCase()
  const candidates: SourceType[] = mediaType === 'application/json'
    ? ['structured_json', 'cdr', 'financial', 'chat', 'audio_transcript', 'audio_diarization', 'telegram_chat', 'instagram_chat']
    : mediaType === 'text/csv' || mediaType.includes('spreadsheetml.sheet')
      ? ['structured_tabular', 'cdr', 'financial']
      : mediaType === 'text/plain'
        ? ['document', 'whatsapp_chat']
        : [detected]
  return Array.from(new Set([detected, ...candidates]))
}

interface NavState {
  observationId?: string
  evidenceId?: string
}

const MAX_CLIENT_STRUCTURED_PREVIEW_BYTES = 25 * 1024 * 1024
const MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024

function csvPreview(text: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let value = ''
  let quoted = false

  for (let index = 0; index < text.length && rows.length < 200; index += 1) {
    const character = text[index]
    if (character === '"') {
      if (quoted && text[index + 1] === '"') {
        value += '"'
        index += 1
      } else {
        quoted = !quoted
      }
    } else if (character === ',' && !quoted) {
      row.push(value)
      value = ''
    } else if ((character === '\n' || character === '\r') && !quoted) {
      if (character === '\r' && text[index + 1] === '\n') index += 1
      row.push(value)
      rows.push(row.slice(0, 30))
      row = []
      value = ''
    } else {
      value += character
    }
  }
  if (rows.length < 200 && (value || row.length)) rows.push([...row, value].slice(0, 30))
  return rows.length ? rows : [['No CSV rows found.']]
}

async function spreadsheetPreview(blob: Blob): Promise<string[][]> {
  if (blob.size > MAX_CLIENT_STRUCTURED_PREVIEW_BYTES) {
    return [['Preview is too large for the browser', 'Use the extracted observations below for a secure summary.']]
  }
  const { default: JSZip } = await import('jszip')
  const zip = await JSZip.loadAsync(await blob.arrayBuffer())
  const sharedStringsXml = await zip.file('xl/sharedStrings.xml')?.async('text')
  const sharedStrings = sharedStringsXml
    ? Array.from(new DOMParser().parseFromString(sharedStringsXml, 'application/xml').querySelectorAll('si')).map((node) => node.textContent ?? '')
    : []
  const sheetName = Object.keys(zip.files).find((name) => /^xl\/worksheets\/sheet\d+\.xml$/.test(name))
  if (!sheetName) return [['No worksheet data found.']]
  const xml = await zip.file(sheetName)?.async('text')
  if (!xml) return [['No worksheet data found.']]
  const rows: string[][] = []
  for (const row of Array.from(new DOMParser().parseFromString(xml, 'application/xml').querySelectorAll('sheetData > row')).slice(0, 200)) {
    const values: string[] = []
    for (const cell of Array.from(row.querySelectorAll('c')).slice(0, 30)) {
      const type = cell.getAttribute('t')
      const raw = cell.querySelector('v')?.textContent ?? cell.querySelector('is')?.textContent ?? ''
      values.push(type === 's' ? (sharedStrings[Number(raw)] ?? '') : raw)
    }
    rows.push(values)
  }
  return rows.length ? rows : [['No worksheet rows found.']]
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
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(navState?.evidenceId ?? null)
  const [highlightObservationId] = useState<string | null>(navState?.observationId ?? null)

  const [observations, setObservations] = useState<EvidenceObservationView[]>([])
  const [obsLoading, setObsLoading] = useState(false)
  const [obsError, setObsError] = useState<string | null>(null)
  const [truncated, setTruncated] = useState(false)

  const [integrity, setIntegrity] = useState<EvidenceIntegrityCheck | null>(null)
  const [integrityLoading, setIntegrityLoading] = useState(false)
  const [integrityError, setIntegrityError] = useState<string | null>(null)
  const [sourceUrl, setSourceUrl] = useState<string | null>(null)
  const [sourceLoading, setSourceLoading] = useState(false)
  const [sourceText, setSourceText] = useState<string | null>(null)
  const [spreadsheetRows, setSpreadsheetRows] = useState<string[][] | null>(null)
  const [classificationSaving, setClassificationSaving] = useState(false)
  const [classificationError, setClassificationError] = useState<string | null>(null)
  const [typeCorrection, setTypeCorrection] = useState<SourceType | null>(null)
  const [typeCorrectionSaving, setTypeCorrectionSaving] = useState(false)
  const [typeCorrectionError, setTypeCorrectionError] = useState<string | null>(null)

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

  useEffect(() => {
    if (!activeCaseId || !selectedEvidenceId) {
      // oxlint-disable-next-line react/set-state-in-effect
      setSourceUrl(null)
      setSourceText(null)
      setSpreadsheetRows(null)
      return
    }
    let cancelled = false
    let objectUrl: string | null = null
    setSourceUrl(null)
    setSourceText(null)
    setSpreadsheetRows(null)
    setTypeCorrection(null)
    setTypeCorrectionError(null)
    setSourceLoading(true)
    evidenceApi.content(activeCaseId, selectedEvidenceId).then(async ({ blob, contentType }) => {
      if (cancelled) return
      const mediaType = contentType.split(';', 1)[0].toLowerCase()
      if (mediaType === 'text/csv') {
        const text = await blob.slice(0, MAX_TEXT_PREVIEW_BYTES).text()
        if (!cancelled) setSpreadsheetRows(csvPreview(text))
      } else if (mediaType.startsWith('text/') || mediaType === 'application/json') {
        const text = await blob.slice(0, MAX_TEXT_PREVIEW_BYTES).text()
        if (!cancelled) setSourceText(text.slice(0, 400_000))
      } else if (contentType.includes('wordprocessingml.document')) {
        const mammoth = await import('mammoth')
        const text = blob.size > MAX_CLIENT_STRUCTURED_PREVIEW_BYTES
          ? 'This document is too large for an in-browser Office preview. Its authorized extracted observations remain available below.'
          : (await mammoth.extractRawText({ arrayBuffer: await blob.arrayBuffer() })).value
        if (!cancelled) setSourceText(text.slice(0, 400_000))
      } else if (contentType.includes('spreadsheetml.sheet')) {
        const rows = await spreadsheetPreview(blob)
        if (!cancelled) setSpreadsheetRows(rows)
      } else {
        objectUrl = URL.createObjectURL(blob)
        setSourceUrl(objectUrl)
      }
    }).catch(() => {
      if (!cancelled) setSourceUrl(null)
    }).finally(() => {
      if (!cancelled) setSourceLoading(false)
    })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
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

  async function downloadSelected() {
    if (!activeCaseId || !selectedEvidence) return
    try {
      const { blob } = await evidenceApi.content(activeCaseId, selectedEvidence.evidence_id, true)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = selectedEvidence.original_filename
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setIntegrityError(err instanceof Error ? err.message : 'Unable to download this evidence.')
    }
  }

  async function updateClassification(classification: EvidenceClassification) {
    if (!activeCaseId || !selectedEvidence) return
    setClassificationSaving(true)
    setClassificationError(null)
    try {
      const updated = await evidenceApi.updateClassification(
        activeCaseId,
        selectedEvidence.evidence_id,
        classification,
      )
      setItems((current) => current.map((item) => (
        item.evidence_id === updated.evidence_id ? updated : item
      )))
    } catch (err) {
      setClassificationError(err instanceof Error ? err.message : 'Unable to update classification.')
    } finally {
      setClassificationSaving(false)
    }
  }

  async function correctDetectedType() {
    if (!activeCaseId || !selectedEvidence || !typeCorrection || typeCorrection === selectedEvidence.source_type) return
    setTypeCorrectionSaving(true)
    setTypeCorrectionError(null)
    try {
      const updated = await evidenceApi.correctDetectedType(
        activeCaseId,
        selectedEvidence.evidence_id,
        typeCorrection,
      )
      setItems((current) => current.map((item) => (
        item.evidence_id === updated.evidence.evidence_id ? updated.evidence : item
      )))
      setTypeCorrection(updated.evidence.source_type)
    } catch (err) {
      setTypeCorrectionError(err instanceof Error ? err.message : 'Unable to correct detected type.')
    } finally {
      setTypeCorrectionSaving(false)
    }
  }

  const selectedEvidence = items.find((item) => item.evidence_id === selectedEvidenceId) ?? null
  const isCaseOwner = activeCase?.role === 'case_owner'
  const permittedClassifications = activeCase
    ? EVIDENCE_CLASSIFICATIONS.filter(
      (classification) => CLASSIFICATION_RANK[classification] >= CLASSIFICATION_RANK[activeCase.classification as EvidenceClassification],
    )
    : []
  const typeCorrectionOptions = selectedEvidence
    ? compatibleTypeCorrections(selectedEvidence.content_type, selectedEvidence.source_type)
    : []

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
                    <dd className="text-text-dim">{CLASSIFICATION_LABEL[selectedEvidence.classification]}</dd>
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
                {isCaseOwner ? (
                  <label className="flex max-w-sm flex-col gap-1.5 border-t border-card-border pt-3 text-xs">
                    <span className="font-medium text-text">Evidence classification</span>
                    <span className="text-text-faint">Raise protection for this source when fewer members should be able to view it. It can never fall below the case classification.</span>
                    <select
                      aria-label="Evidence classification"
                      value={selectedEvidence.classification === 'unclassified' ? activeCase?.classification : selectedEvidence.classification}
                      disabled={classificationSaving}
                      onChange={(event) => void updateClassification(event.target.value as EvidenceClassification)}
                      className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text disabled:opacity-60"
                    >
                      {permittedClassifications.map((classification) => (
                        <option key={classification} value={classification}>{CLASSIFICATION_LABEL[classification]}</option>
                      ))}
                    </select>
                    {classificationError ? <span className="text-crimson">{classificationError}</span> : null}
                  </label>
                ) : null}
                {isCaseOwner && typeCorrectionOptions.length > 1 ? (
                  <div className="flex max-w-sm flex-col gap-1.5 border-t border-card-border pt-3 text-xs">
                    <span className="font-medium text-text">Correct detected type</span>
                    <div className="flex gap-2">
                      <select
                        aria-label="Correct detected type"
                        value={typeCorrection ?? selectedEvidence.source_type}
                        disabled={typeCorrectionSaving}
                        onChange={(event) => setTypeCorrection(event.target.value as SourceType)}
                        className="min-w-0 flex-1 rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text disabled:opacity-60"
                      >
                        {typeCorrectionOptions.map((sourceType) => (
                          <option key={sourceType} value={sourceType}>{SOURCE_LABEL[sourceType]}</option>
                        ))}
                      </select>
                      <Button variant="secondary" disabled={typeCorrectionSaving || (typeCorrection ?? selectedEvidence.source_type) === selectedEvidence.source_type} onClick={() => void correctDetectedType()}>{typeCorrectionSaving ? 'Saving…' : 'Apply'}</Button>
                    </div>
                    {typeCorrectionError ? <span className="text-crimson">{typeCorrectionError}</span> : null}
                  </div>
                ) : null}
                <div className="flex flex-wrap items-center gap-3 border-t border-card-border pt-3">
                  <Button variant="secondary" onClick={verifyIntegrity} disabled={integrityLoading}>
                    {integrityLoading ? 'Verifying...' : 'Verify hash integrity'}
                  </Button>
                  <Button variant="secondary" onClick={downloadSelected}>Download authorized source</Button>
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

              <Card className="p-4">
                <h2 className="text-sm font-semibold text-text">Secure source preview</h2>
                {sourceLoading ? <p className="mt-2 text-xs text-text-faint">Loading authorized source…</p> : null}
                {!sourceLoading && !sourceUrl && !sourceText && !spreadsheetRows ? <p className="mt-2 text-xs text-text-faint">The authorized source could not be previewed. Its extracted observations remain available below.</p> : null}
                {sourceUrl && selectedEvidence.content_type.startsWith('image/') ? <img src={sourceUrl} alt={selectedEvidence.original_filename} className="mt-3 max-h-96 max-w-full rounded-control" /> : null}
                {sourceUrl && selectedEvidence.content_type.startsWith('audio/') ? <audio controls src={sourceUrl} className="mt-3 w-full" /> : null}
                {sourceUrl && selectedEvidence.content_type.startsWith('video/') ? <video controls src={sourceUrl} className="mt-3 max-h-96 w-full rounded-control" /> : null}
                {sourceUrl && selectedEvidence.content_type.split(';', 1)[0].toLowerCase() === 'application/pdf' ? <object aria-label="Authorized PDF evidence source" data={sourceUrl} type="application/pdf" className="mt-3 h-[640px] w-full rounded-control border border-card-border" /> : null}
                {sourceText ? <pre className="mt-3 max-h-[640px] overflow-auto whitespace-pre-wrap rounded-control border border-card-border bg-canvas p-3 font-mono text-xs text-text">{sourceText}</pre> : null}
                {spreadsheetRows ? <div className="mt-3 max-h-[640px] overflow-auto rounded-control border border-card-border"><table className="min-w-full border-collapse text-left text-xs"><tbody>{spreadsheetRows.map((row, rowIndex) => <tr key={rowIndex} className="border-b border-card-border last:border-0">{row.map((cell, cellIndex) => <td key={cellIndex} className="whitespace-nowrap px-3 py-2 text-text-dim">{cell}</td>)}</tr>)}</tbody></table></div> : null}
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
