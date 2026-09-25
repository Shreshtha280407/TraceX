import { useEffect, useState } from 'react'
import { CheckCircle2, FileText, ListChecks, Lightbulb, XCircle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { StatCard } from '../components/StatCard'
import { ApiError, notesApi, reviewApi } from '../lib/api/client'
import type { CaseNoteRecord } from '../lib/api/case-types'
import type { HandoffSummary } from '../lib/api/graph-types'
import { useActiveCaseWorkspace } from '../lib/cases'
import { useHasCaseAction } from '../lib/auth/permissions'
import { formatRelativeTime } from '../lib/format'

/**
 * Page 14 -- Investigation Memory. Real notes, decisions, and handoff/review
 * history for a case (Section 5's own role text): the append-only case-notes
 * feed (`GET/POST .../notes`, Gap-Closure WP-4 G3) plus the same handoff
 * summary (`GET .../handoff`) counting open/accepted/rejected candidates and
 * hypotheses -- computed at read time from Phase 3's real review decisions,
 * never a separate durable table.
 */
export function InvestigationMemory() {
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()
  const canWriteNotes = useHasCaseAction('case_note_write')

  const [handoff, setHandoff] = useState<HandoffSummary | null>(null)
  const [notes, setNotes] = useState<CaseNoteRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  const [draft, setDraft] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  useEffect(() => {
    if (!activeCaseId) return
    let cancelled = false
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError(null)
    setForbidden(false)
    Promise.all([reviewApi.getHandoff(activeCaseId), notesApi.list(activeCaseId)])
      .then(([handoffRes, notesRes]) => {
        if (cancelled) return
        setHandoff(handoffRes)
        setNotes(notesRes.items)
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

  function submitNote() {
    if (!activeCaseId || !draft.trim()) return
    setSubmitting(true)
    setSubmitError(null)
    notesApi
      .create(activeCaseId, { text: draft.trim() })
      .then((note) => {
        setNotes((prev) => [note, ...prev])
        setDraft('')
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.isForbidden) {
          setSubmitError("You don't have permission to add notes on this case.")
        } else {
          setSubmitError(err instanceof Error ? err.message : 'Unable to reach the server.')
        }
      })
      .finally(() => setSubmitting(false))
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        {activeCase ? (
          <span className="font-mono text-xs uppercase tracking-wider text-text-faint">
            {activeCase.case_reference}
          </span>
        ) : null}
        <h1 className="text-2xl font-semibold text-text">Investigation Memory</h1>
        <p className="text-sm text-text-dim">Notes, decisions, and handoff/review history.</p>
      </div>

      {!activeCase ? (
        <ActiveCaseGate
          cases={cases}
          loading={casesLoading}
          activeCaseId={activeCaseId}
          onSelect={setActiveCase}
        />
      ) : forbidden ? (
        <ForbiddenState message="You don't have permission to view this case's memory." />
      ) : error ? (
        <ErrorState message={error} />
      ) : loading ? (
        <LoadingState label="Loading case memory..." />
      ) : (
        <>
          {handoff ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Link to="/review/candidates" className="block">
                <StatCard
                  label="Open candidates"
                  value={handoff.open_candidate_count}
                  icon={ListChecks}
                  subtext={`${handoff.accepted_candidate_count} accepted, ${handoff.rejected_candidate_count} rejected`}
                  deltaTone="steel-neutral"
                />
              </Link>
              <Link to="/hypotheses" className="block">
                <StatCard
                  label="Open hypotheses"
                  value={handoff.open_hypotheses.length}
                  icon={Lightbulb}
                  subtext={`${handoff.accepted_hypothesis_count} accepted, ${handoff.rejected_hypothesis_count} rejected`}
                  deltaTone="steel-neutral"
                />
              </Link>
              <StatCard
                label="Accepted decisions"
                value={handoff.accepted_candidate_count + handoff.accepted_hypothesis_count}
                icon={CheckCircle2}
                deltaTone="palm"
              />
              <StatCard
                label="Rejected decisions"
                value={handoff.rejected_candidate_count + handoff.rejected_hypothesis_count}
                icon={XCircle}
                deltaTone="crimson"
              />
            </div>
          ) : null}

          <div>
            <h2 className="text-sm font-semibold text-text">Case notes</h2>
            <p className="text-xs text-text-dim">
              An append-only investigator narrative -- an edit creates a new note, it never
              overwrites an earlier one.
            </p>
          </div>

          {canWriteNotes ? (
            <Card className="flex flex-col gap-3 p-4">
              <textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="Add a note for the case record..."
                maxLength={8000}
                rows={3}
                className="w-full resize-none rounded-control border border-card-border bg-canvas/5 px-3 py-2 text-sm text-text placeholder:text-text-faint focus:outline-none focus:ring-2 focus:ring-crimson/40"
              />
              <div className="flex items-center justify-between gap-3">
                {submitError ? <span className="text-xs text-crimson">{submitError}</span> : <span />}
                <Button type="button" onClick={submitNote} disabled={submitting || !draft.trim()}>
                  {submitting ? 'Adding...' : 'Add note'}
                </Button>
              </div>
            </Card>
          ) : null}

          {notes.length === 0 ? (
            <EmptyState
              title="No notes yet"
              description="Notes added to this case will appear here for every investigator with access."
            />
          ) : (
            <div className="flex flex-col gap-2">
              {notes.map((note) => (
                <Card key={note.note_id} className="flex flex-col gap-2 p-4">
                  <div className="flex items-center justify-between gap-3 text-xs text-text-faint">
                    <span className="flex items-center gap-1.5">
                      <FileText size={12} aria-hidden="true" />
                      <span className="font-mono">{note.author_user_id}</span>
                    </span>
                    <span>{formatRelativeTime(note.created_at)}</span>
                  </div>
                  <p className="whitespace-pre-wrap text-sm text-text">{note.text}</p>
                  {note.supersedes_note_id ? (
                    <span className="font-mono text-xs text-text-faint">
                      Supersedes {note.supersedes_note_id}
                    </span>
                  ) : null}
                </Card>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
