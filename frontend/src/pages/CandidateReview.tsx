import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, RotateCcw, XCircle } from 'lucide-react'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Badge } from '../components/Badge'
import { Card } from '../components/Card'
import { DecisionBar } from '../components/DecisionBar'
import type { DecisionOption } from '../components/DecisionBar'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { ApiError, entityApi } from '../lib/api/client'
import type { EntityResolutionReviewView, EntityReviewOutcome, EntityV1 } from '../lib/api/graph-types'
import { useActiveCaseWorkspace } from '../lib/cases'

const NEEDS_REVIEW_OPTIONS: DecisionOption<EntityReviewOutcome>[] = [
  { value: 'verified_same', label: 'Verify (same identity)', icon: CheckCircle2, tone: 'palm' },
  { value: 'rejected', label: 'Reject', icon: XCircle, tone: 'crimson' },
]

const REVERSAL_OPTIONS: DecisionOption<EntityReviewOutcome>[] = [
  { value: 'split', label: 'Split (reverse verified-same)', icon: RotateCcw, tone: 'berry' },
]

const STATUS_TONE = {
  needs_review: 'steel-neutral',
  verified_same: 'palm',
  rejected: 'crimson',
  split: 'berry',
} as const

function entityLabel(entities: Map<string, EntityV1>, id: string): string {
  return entities.get(id)?.canonical_label ?? id
}

/**
 * Page 10 -- Candidate Review. Real entity-resolution candidates (Section
 * 5's own role text: "Verify/Reject ... on entity-resolution candidates --
 * human-in-the-loop, never auto-merge"). The real backend has no "needs
 * more evidence" outcome anywhere -- only `verified_same`/`rejected`
 * (first decision) and `split` (reversing an earlier `verified_same`); see
 * `DecisionBar`'s own docstring for why this page's options differ from
 * an earlier, unwired three-button version.
 */
export function CandidateReview() {
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()

  const [candidates, setCandidates] = useState<EntityResolutionReviewView[]>([])
  const [entities, setEntities] = useState<EntityV1[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [decisionError, setDecisionError] = useState<string | null>(null)

  const load = useCallback(() => {
    if (!activeCaseId) return
    setLoading(true)
    setError(null)
    setForbidden(false)
    return Promise.all([entityApi.listCandidates(activeCaseId), entityApi.list(activeCaseId)])
      .then(([candidatesRes, entitiesRes]) => {
        setCandidates(candidatesRes.items)
        setEntities(entitiesRes.items)
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.isForbidden) setForbidden(true)
        else setError(err instanceof Error ? err.message : 'Unable to reach the server.')
      })
      .finally(() => setLoading(false))
  }, [activeCaseId])

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect
    load()
  }, [load])

  const entityById = useMemo(() => new Map(entities.map((e) => [e.entity_id, e])), [entities])

  const visibleCandidates = useMemo(
    () => (showAll ? candidates : candidates.filter((c) => c.effective_status === 'needs_review')),
    [candidates, showAll],
  )

  async function handleDecide(
    candidateId: string,
    leftEntityId: string,
    decision: EntityReviewOutcome,
  ) {
    if (!activeCaseId) return
    setDecisionError(null)
    try {
      await entityApi.reviewResolution(leftEntityId, candidateId, decision)
      // Re-fetch from the server rather than mutating local state optimistically
      // (Section 9: "confirmed via re-fetch, not just optimistically in local state").
      await load()
    } catch (err) {
      setDecisionError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          {activeCase ? (
            <span className="font-mono text-xs uppercase tracking-wider text-text-faint">
              {activeCase.case_reference}
            </span>
          ) : null}
          <h1 className="text-2xl font-semibold text-text">Candidate Review</h1>
          <p className="text-sm text-text-dim">
            Verify or reject entity-resolution candidates. Never auto-merged.
          </p>
        </div>
        {activeCase ? (
          <label className="flex items-center gap-2 text-xs text-text-dim">
            <input
              type="checkbox"
              checked={showAll}
              onChange={(event) => setShowAll(event.target.checked)}
              className="h-4 w-4 rounded border-card-border"
            />
            Show already-decided candidates
          </label>
        ) : null}
      </div>

      {!activeCase ? (
        <ActiveCaseGate cases={cases} loading={casesLoading} activeCaseId={activeCaseId} onSelect={setActiveCase} />
      ) : forbidden ? (
        <ForbiddenState message="You don't have permission to review candidates on this case." />
      ) : error ? (
        <ErrorState message={error} />
      ) : loading ? (
        <LoadingState label="Loading candidates..." />
      ) : visibleCandidates.length === 0 ? (
        <EmptyState
          title={showAll ? 'No entity-resolution candidates exist yet' : 'Nothing pending review'}
          description={
            showAll
              ? 'Candidates appear once entity resolution finds a possible identity match.'
              : 'Every entity-resolution candidate for this case has already been reviewed.'
          }
        />
      ) : (
        <div className="flex flex-col gap-4">
          {decisionError ? <ErrorState message={decisionError} /> : null}
          {visibleCandidates.map((view) => {
            const { candidate, effective_status: status, latest_decision: latestDecision } = view
            const options = status === 'needs_review' ? NEEDS_REVIEW_OPTIONS : status === 'verified_same' ? REVERSAL_OPTIONS : []
            return (
              <Card key={candidate.entity_resolution_candidate_id} className="flex flex-col gap-3 p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex flex-col gap-1">
                    <div className="flex items-center gap-2 text-sm">
                      <span className="font-medium text-text">
                        {entityLabel(entityById, candidate.left_entity_id)}
                      </span>
                      <span className="text-text-faint">&harr;</span>
                      <span className="font-medium text-text">
                        {entityLabel(entityById, candidate.right_entity_id)}
                      </span>
                    </div>
                    <span className="font-mono text-xs text-text-faint">
                      {candidate.entity_resolution_candidate_id}
                    </span>
                  </div>
                  <Badge tone={STATUS_TONE[status]}>{status.replace(/_/g, ' ')}</Badge>
                </div>

                <div className="flex flex-wrap gap-1.5">
                  {candidate.reasons.map((reason) => (
                    <Badge key={reason} tone="steel-neutral">
                      {reason.replace(/_/g, ' ')}
                    </Badge>
                  ))}
                  {candidate.vector_score !== null ? (
                    <Badge tone="steel-neutral">vector score {candidate.vector_score.toFixed(2)}</Badge>
                  ) : null}
                </div>

                {candidate.contradiction_reasons.length > 0 ? (
                  <div className="flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/5 px-3 py-2 text-xs text-crimson">
                    <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                    <span>Contradiction signals: {candidate.contradiction_reasons.join(', ')}</span>
                  </div>
                ) : null}

                <div className="flex flex-col gap-1">
                  <span className="text-xs font-medium uppercase tracking-wide text-text-faint">
                    Supporting observations ({candidate.supporting_observation_ids.length})
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {candidate.supporting_observation_ids.map((id) => (
                      <span key={id} className="font-mono text-xs text-text-faint">
                        {id.slice(0, 8)}
                      </span>
                    ))}
                  </div>
                </div>

                {latestDecision ? (
                  <span className="text-xs text-text-dim">
                    Last decision by <span className="font-mono">{latestDecision.reviewer_user_id.slice(0, 8)}</span> on{' '}
                    {new Date(latestDecision.created_at).toLocaleString()}
                    {latestDecision.rationale ? ` -- "${latestDecision.rationale}"` : ''}
                  </span>
                ) : null}

                {options.length > 0 ? (
                  <DecisionBar
                    options={options}
                    onDecide={(decision) =>
                      handleDecide(
                        candidate.entity_resolution_candidate_id,
                        candidate.left_entity_id,
                        decision,
                      )
                    }
                  />
                ) : null}
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )
}
