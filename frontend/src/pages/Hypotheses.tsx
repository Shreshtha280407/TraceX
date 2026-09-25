import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, CheckCircle2, Lightbulb, Plus, ShieldAlert, XCircle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Badge } from '../components/Badge'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { DecisionBar } from '../components/DecisionBar'
import type { DecisionOption } from '../components/DecisionBar'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { ApiError, entityApi, graphApi, reviewApi } from '../lib/api/client'
import type {
  CandidateLinkRecord,
  CorrelationRecord,
  EntityResolutionReviewView,
  EntityV1,
  GraphObservationView,
  HypothesisRecord,
  HypothesisReviewOutcome,
} from '../lib/api/graph-types'
import { useActiveCaseWorkspace } from '../lib/cases'

const STATUS_TONE = { needs_review: 'steel-neutral', accepted_by_reviewer: 'palm', rejected_by_reviewer: 'crimson' } as const

const REVIEW_OPTIONS: DecisionOption<HypothesisReviewOutcome>[] = [
  { value: 'accepted_by_reviewer', label: 'Accept', icon: CheckCircle2, tone: 'palm' },
  { value: 'rejected_by_reviewer', label: 'Reject', icon: XCircle, tone: 'crimson' },
]

interface CreateFormState {
  statement: string
  rationale: string
  observationIds: Set<string>
  candidateIds: Set<string>
  entityResolutionCandidateIds: Set<string>
}

function emptyForm(): CreateFormState {
  return {
    statement: '',
    rationale: '',
    observationIds: new Set(),
    candidateIds: new Set(),
    entityResolutionCandidateIds: new Set(),
  }
}

/**
 * Page 12 (hero) -- Hypotheses + Counter-Evidence. Real supporting-evidence
 * citations from `HypothesisRecord` itself; real contradicting evidence
 * derived honestly from the correlations a hypothesis's cited candidate
 * links belong to (`CorrelationRecord.contradictory_observation_ids` --
 * Nipun's Phase 5 pipeline's real place for tracked counter-evidence). A
 * hypothesis record itself has no `contradicting_observation_ids` field at
 * all (confirmed in `hypothesis_models.py`) -- a hypothesis that cites no
 * correlation-backed candidate genuinely has no contradicting evidence to
 * show, and this page says so honestly rather than fabricating one.
 */
export function Hypotheses() {
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()

  const [hypotheses, setHypotheses] = useState<HypothesisRecord[]>([])
  const [observations, setObservations] = useState<GraphObservationView[]>([])
  const [candidates, setCandidates] = useState<CandidateLinkRecord[]>([])
  const [correlations, setCorrelations] = useState<CorrelationRecord[]>([])
  const [entityCandidates, setEntityCandidates] = useState<EntityResolutionReviewView[]>([])
  const [entities, setEntities] = useState<EntityV1[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [form, setForm] = useState<CreateFormState>(emptyForm())
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const load = useCallback(() => {
    if (!activeCaseId) return Promise.resolve()
    setLoading(true)
    setError(null)
    setForbidden(false)
    return Promise.all([
      reviewApi.listHypotheses(activeCaseId, 50, true),
      graphApi.listObservations(activeCaseId),
      graphApi.listRawCandidates(activeCaseId),
      graphApi.listCorrelations(activeCaseId),
      entityApi.listCandidates(activeCaseId),
      entityApi.list(activeCaseId),
    ])
      .then(([hypothesesRes, observationsRes, candidatesRes, correlationsRes, entityCandidatesRes, entitiesRes]) => {
        setHypotheses(hypothesesRes.items)
        setObservations(observationsRes.items)
        setCandidates(candidatesRes.items)
        setCorrelations(correlationsRes.items.map((v) => v.correlation))
        setEntityCandidates(entityCandidatesRes.items)
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

  const observationById = useMemo(() => new Map(observations.map((o) => [o.observation_id, o])), [observations])
  const candidateById = useMemo(() => new Map(candidates.map((c) => [c.candidate_link_id, c])), [candidates])
  const correlationById = useMemo(() => new Map(correlations.map((c) => [c.correlation_id, c])), [correlations])
  const entityResolutionCandidateById = useMemo(
    () => new Map(entityCandidates.map((v) => [v.candidate.entity_resolution_candidate_id, v.candidate])),
    [entityCandidates],
  )
  const entityById = useMemo(() => new Map(entities.map((e) => [e.entity_id, e])), [entities])

  const visibleHypotheses = useMemo(
    () => (showAll ? hypotheses : hypotheses.filter((h) => h.status === 'needs_review')),
    [hypotheses, showAll],
  )
  const selected = hypotheses.find((h) => h.hypothesis_id === selectedId) ?? visibleHypotheses[0] ?? null

  const contradictingObservationIds = useMemo(() => {
    if (!selected) return []
    const ids = new Set<string>()
    for (const candidateId of selected.supporting_candidate_ids) {
      const candidate = candidateById.get(candidateId)
      if (!candidate) continue
      const correlation = correlationById.get(candidate.correlation_id)
      if (!correlation) continue
      for (const obsId of correlation.contradictory_observation_ids) ids.add(obsId)
    }
    return [...ids]
  }, [selected, candidateById, correlationById])

  async function handleDecide(hypothesisId: string, decision: HypothesisReviewOutcome) {
    if (!activeCaseId) return
    setDecisionError(null)
    try {
      await reviewApi.reviewHypothesis(activeCaseId, hypothesisId, decision)
      await load()
    } catch (err) {
      setDecisionError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    }
  }

  function toggleSetMember(set: Set<string>, id: string): Set<string> {
    const next = new Set(set)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  }

  async function handleCreate(event: FormEvent) {
    event.preventDefault()
    if (!activeCaseId) return
    setFormError(null)
    if (!form.statement.trim()) {
      setFormError('A statement is required.')
      return
    }
    if (form.observationIds.size === 0 && form.candidateIds.size === 0 && form.entityResolutionCandidateIds.size === 0) {
      setFormError('A hypothesis must cite at least one observation or candidate.')
      return
    }
    setSubmitting(true)
    try {
      const created = await reviewApi.proposeHypothesis(activeCaseId, {
        statement: form.statement.trim(),
        rationale: form.rationale.trim() || null,
        supporting_observation_ids: [...form.observationIds],
        supporting_candidate_ids: [...form.candidateIds],
        supporting_entity_resolution_candidate_ids: [...form.entityResolutionCandidateIds],
      })
      setForm(emptyForm())
      setShowCreateForm(false)
      await load()
      setSelectedId(created.hypothesis_id)
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    } finally {
      setSubmitting(false)
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
          <h1 className="text-2xl font-semibold text-text">Hypotheses + Counter-Evidence</h1>
          <p className="text-sm text-text-dim">
            Human-authored, evidence-backed. Never a confirmed fact until reviewed.
          </p>
        </div>
        {activeCase ? (
          <Button variant="secondary" onClick={() => setShowCreateForm((v) => !v)}>
            <Plus size={14} aria-hidden="true" />
            {showCreateForm ? 'Cancel' : 'New hypothesis'}
          </Button>
        ) : null}
      </div>

      {!activeCase ? (
        <ActiveCaseGate cases={cases} loading={casesLoading} activeCaseId={activeCaseId} onSelect={setActiveCase} />
      ) : forbidden ? (
        <ForbiddenState message="You don't have permission to view this case's hypotheses." />
      ) : error ? (
        <ErrorState message={error} />
      ) : loading ? (
        <LoadingState label="Loading hypotheses..." />
      ) : (
        <>
          {showCreateForm ? (
            <Card className="flex flex-col gap-4 p-5">
              {formError ? (
                <div className="flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
                  <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                  <span>{formError}</span>
                </div>
              ) : null}
              <form className="flex flex-col gap-4" onSubmit={handleCreate}>
                <label className="flex flex-col gap-1.5 text-sm">
                  <span className="font-medium text-text-dim">Statement</span>
                  <textarea
                    required
                    maxLength={4000}
                    rows={3}
                    value={form.statement}
                    onChange={(event) => setForm((f) => ({ ...f, statement: event.target.value }))}
                    className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
                  />
                </label>
                <label className="flex flex-col gap-1.5 text-sm">
                  <span className="font-medium text-text-dim">Rationale (optional)</span>
                  <textarea
                    maxLength={4000}
                    rows={2}
                    value={form.rationale}
                    onChange={(event) => setForm((f) => ({ ...f, rationale: event.target.value }))}
                    className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
                  />
                </label>

                <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                  <div className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium uppercase tracking-wide text-text-faint">
                      Cite observations ({form.observationIds.size})
                    </span>
                    <div className="flex max-h-40 flex-col gap-1 overflow-y-auto rounded-control border border-card-border bg-card p-2">
                      {observations.length === 0 ? (
                        <span className="text-xs text-text-faint">None available.</span>
                      ) : (
                        observations.map((obs) => (
                          <label key={obs.observation_id} className="flex items-start gap-2 text-xs">
                            <input
                              type="checkbox"
                              checked={form.observationIds.has(obs.observation_id)}
                              onChange={() =>
                                setForm((f) => ({ ...f, observationIds: toggleSetMember(f.observationIds, obs.observation_id) }))
                              }
                              className="mt-0.5"
                            />
                            <span className="font-mono text-text-dim">{obs.observation_id.slice(0, 8)}</span>
                            <span className="text-text-faint">{obs.observation_type}</span>
                          </label>
                        ))
                      )}
                    </div>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium uppercase tracking-wide text-text-faint">
                      Cite candidates ({form.candidateIds.size})
                    </span>
                    <div className="flex max-h-40 flex-col gap-1 overflow-y-auto rounded-control border border-card-border bg-card p-2">
                      {candidates.length === 0 ? (
                        <span className="text-xs text-text-faint">None available.</span>
                      ) : (
                        candidates.map((candidate) => (
                          <label key={candidate.candidate_link_id} className="flex items-start gap-2 text-xs">
                            <input
                              type="checkbox"
                              checked={form.candidateIds.has(candidate.candidate_link_id)}
                              onChange={() =>
                                setForm((f) => ({ ...f, candidateIds: toggleSetMember(f.candidateIds, candidate.candidate_link_id) }))
                              }
                              className="mt-0.5"
                            />
                            <span className="font-mono text-text-dim">{candidate.candidate_link_id.slice(0, 8)}</span>
                            <span className="text-text-faint">{candidate.status}</span>
                          </label>
                        ))
                      )}
                    </div>
                  </div>

                  <div className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium uppercase tracking-wide text-text-faint">
                      Cite identity candidates ({form.entityResolutionCandidateIds.size})
                    </span>
                    <div className="flex max-h-40 flex-col gap-1 overflow-y-auto rounded-control border border-card-border bg-card p-2">
                      {entityCandidates.length === 0 ? (
                        <span className="text-xs text-text-faint">None available.</span>
                      ) : (
                        entityCandidates.map(({ candidate }) => (
                          <label
                            key={candidate.entity_resolution_candidate_id}
                            className="flex items-start gap-2 text-xs"
                          >
                            <input
                              type="checkbox"
                              checked={form.entityResolutionCandidateIds.has(candidate.entity_resolution_candidate_id)}
                              onChange={() =>
                                setForm((f) => ({
                                  ...f,
                                  entityResolutionCandidateIds: toggleSetMember(
                                    f.entityResolutionCandidateIds,
                                    candidate.entity_resolution_candidate_id,
                                  ),
                                }))
                              }
                              className="mt-0.5"
                            />
                            <span className="text-text-faint">
                              {entityById.get(candidate.left_entity_id)?.canonical_label ?? candidate.left_entity_id.slice(0, 8)}
                              {' ↔ '}
                              {entityById.get(candidate.right_entity_id)?.canonical_label ?? candidate.right_entity_id.slice(0, 8)}
                            </span>
                          </label>
                        ))
                      )}
                    </div>
                  </div>
                </div>

                <Button type="submit" disabled={submitting} className="w-fit">
                  {submitting ? 'Proposing...' : 'Propose hypothesis'}
                </Button>
              </form>
            </Card>
          ) : null}

          {hypotheses.length === 0 && !showCreateForm ? (
            <EmptyState
              title="No hypotheses yet"
              description="Propose one by citing real observations or candidates from this case."
            />
          ) : (
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-[300px_1fr]">
              <div className="flex flex-col gap-2">
                <label className="flex items-center gap-2 text-xs text-text-dim">
                  <input
                    type="checkbox"
                    checked={showAll}
                    onChange={(event) => setShowAll(event.target.checked)}
                    className="h-4 w-4 rounded border-card-border"
                  />
                  Show already-decided hypotheses
                </label>
                <div className="flex max-h-[520px] flex-col gap-1 overflow-y-auto">
                  {visibleHypotheses.length === 0 ? (
                    <span className="text-xs text-text-dim">Nothing pending review.</span>
                  ) : (
                    visibleHypotheses.map((h) => (
                      <button
                        key={h.hypothesis_id}
                        type="button"
                        onClick={() => setSelectedId(h.hypothesis_id)}
                        className={`flex flex-col items-start gap-1 rounded-control border px-3 py-2 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson ${
                          h.hypothesis_id === selected?.hypothesis_id
                            ? 'border-crimson bg-crimson/5'
                            : 'border-card-border bg-card hover:bg-canvas/10'
                        }`}
                      >
                        <span className="line-clamp-2 text-sm text-text">{h.statement}</span>
                        <Badge tone={STATUS_TONE[h.status]}>{h.status.replace(/_/g, ' ')}</Badge>
                      </button>
                    ))
                  )}
                </div>
              </div>

              {selected ? (
                <div className="flex flex-col gap-4">
                  <Card className="flex flex-col gap-3 p-5">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-center gap-2">
                        <Lightbulb size={16} className="text-berry" aria-hidden="true" />
                        <h2 className="text-lg font-semibold text-text">Statement</h2>
                      </div>
                      <Badge tone={STATUS_TONE[selected.status]}>{selected.status.replace(/_/g, ' ')}</Badge>
                    </div>
                    <p className="text-sm text-text">{selected.statement}</p>
                    {selected.rationale ? (
                      <p className="text-xs text-text-dim">Rationale: {selected.rationale}</p>
                    ) : null}
                    <span className="font-mono text-xs text-text-faint">{selected.hypothesis_id}</span>
                    <span className="text-xs text-text-faint">
                      Created {new Date(selected.created_at).toLocaleString()}
                      {selected.decided_at ? ` · decided ${new Date(selected.decided_at).toLocaleString()}` : ''}
                    </span>
                  </Card>

                  {decisionError ? <ErrorState message={decisionError} /> : null}

                  <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                    <Card className="flex flex-col gap-2 border-palm/30 bg-palm/5 p-4">
                      <span className="text-xs font-semibold uppercase tracking-wide text-palm">
                        Supporting evidence (
                        {selected.supporting_observation_ids.length +
                          selected.supporting_candidate_ids.length +
                          selected.supporting_entity_resolution_candidate_ids.length}
                        )
                      </span>
                      {selected.supporting_observation_ids.map((id) => (
                        <Link
                          key={id}
                          to="/evidence"
                          state={{ observationId: id }}
                          className="font-mono text-xs text-text-dim underline-offset-2 hover:text-crimson hover:underline"
                        >
                          observation {id.slice(0, 8)} &middot;{' '}
                          {observationById.get(id)?.observation_type ?? 'unresolved'}
                        </Link>
                      ))}
                      {selected.supporting_candidate_ids.map((id) => (
                        <span key={id} className="font-mono text-xs text-text-dim">
                          candidate {id.slice(0, 8)} &middot; {candidateById.get(id)?.status ?? 'unresolved'}
                        </span>
                      ))}
                      {selected.supporting_entity_resolution_candidate_ids.map((id) => {
                        const c = entityResolutionCandidateById.get(id)
                        return (
                          <span key={id} className="font-mono text-xs text-text-dim">
                            identity candidate {id.slice(0, 8)}
                            {c
                              ? ` · ${entityById.get(c.left_entity_id)?.canonical_label ?? c.left_entity_id.slice(0, 8)} ↔ ${entityById.get(c.right_entity_id)?.canonical_label ?? c.right_entity_id.slice(0, 8)}`
                              : ''}
                          </span>
                        )
                      })}
                      {selected.supporting_observation_ids.length +
                        selected.supporting_candidate_ids.length +
                        selected.supporting_entity_resolution_candidate_ids.length ===
                      0 ? (
                        <span className="text-xs text-text-faint">No citations.</span>
                      ) : null}
                    </Card>

                    <Card className="flex flex-col gap-2 border-crimson/30 bg-crimson/5 p-4">
                      <span className="text-xs font-semibold uppercase tracking-wide text-crimson">
                        Contradicting evidence ({contradictingObservationIds.length})
                      </span>
                      {contradictingObservationIds.length === 0 ? (
                        <span className="flex items-start gap-1.5 text-xs text-text-faint">
                          <ShieldAlert size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
                          No contradicting evidence linked -- this hypothesis doesn't cite a correlation
                          with tracked counter-evidence.
                        </span>
                      ) : (
                        contradictingObservationIds.map((id) => (
                          <Link
                            key={id}
                            to="/evidence"
                            state={{ observationId: id }}
                            className="font-mono text-xs text-text-dim underline-offset-2 hover:text-crimson hover:underline"
                          >
                            observation {id.slice(0, 8)} &middot;{' '}
                            {observationById.get(id)?.observation_type ?? 'unresolved'}
                          </Link>
                        ))
                      )}
                    </Card>
                  </div>

                  {selected.status === 'needs_review' ? (
                    <DecisionBar
                      options={REVIEW_OPTIONS}
                      onDecide={(decision) => handleDecide(selected.hypothesis_id, decision)}
                    />
                  ) : null}
                </div>
              ) : (
                <Card className="flex items-center justify-center p-8 text-sm text-text-dim">
                  Select a hypothesis to see its evidence.
                </Card>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
