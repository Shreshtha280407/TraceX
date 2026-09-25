import { useEffect, useMemo, useState } from 'react'
import { Link2, Share2 } from 'lucide-react'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Badge } from '../components/Badge'
import { Card } from '../components/Card'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { ApiError, entityApi, graphApi } from '../lib/api/client'
import type {
  CorrelationIntegrationView,
  EntityV1,
  GraphCoParticipationMotif,
  GraphSnapshotResponse,
} from '../lib/api/graph-types'
import { useActiveCaseWorkspace } from '../lib/cases'

const PROPOSITION_TONE = { candidate: 'steel-neutral', needs_review: 'berry', rejected: 'crimson' } as const
const PROJECTION_TONE = {
  queued: 'steel-neutral',
  running: 'steel-neutral',
  succeeded: 'palm',
  failed: 'crimson',
  deferred: 'berry',
} as const

/**
 * Page 11 -- Motifs + Correlations. Real generic co-participation motifs
 * (two events sharing a common entity participant -- `GraphCoParticipationMotif`'s
 * own docstring: "a generic, evidence-backed structural signal, never a
 * scenario-specific narrative pattern") and real Phase 5 correlation
 * propositions, both from live endpoints.
 */
export function MotifsCorrelations() {
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()

  const [motifs, setMotifs] = useState<GraphCoParticipationMotif[]>([])
  const [correlations, setCorrelations] = useState<CorrelationIntegrationView[]>([])
  const [snapshot, setSnapshot] = useState<GraphSnapshotResponse | null>(null)
  const [entities, setEntities] = useState<EntityV1[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    if (!activeCaseId) return
    let cancelled = false
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError(null)
    setForbidden(false)
    Promise.all([
      graphApi.getMotifs(activeCaseId),
      graphApi.listCorrelations(activeCaseId),
      graphApi.getSnapshot(activeCaseId),
      entityApi.list(activeCaseId),
    ])
      .then(([motifsRes, correlationsRes, snapshotRes, entitiesRes]) => {
        if (cancelled) return
        setMotifs(motifsRes.co_participation)
        setCorrelations(correlationsRes.items)
        setSnapshot(snapshotRes)
        setEntities(entitiesRes.items)
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

  const entityById = useMemo(() => new Map(entities.map((e) => [e.entity_id, e])), [entities])
  const eventTypeById = useMemo(
    () => new Map((snapshot?.events ?? []).map((e) => [e.event_id, e.event_type])),
    [snapshot],
  )

  return (
    <div className="flex flex-col gap-6">
      <div>
        {activeCase ? (
          <span className="font-mono text-xs uppercase tracking-wider text-text-faint">
            {activeCase.case_reference}
          </span>
        ) : null}
        <h1 className="text-2xl font-semibold text-text">Motifs + Correlations</h1>
        <p className="text-sm text-text-dim">Recurring patterns and cross-modal incident threads.</p>
      </div>

      {!activeCase ? (
        <ActiveCaseGate cases={cases} loading={casesLoading} activeCaseId={activeCaseId} onSelect={setActiveCase} />
      ) : forbidden ? (
        <ForbiddenState message="You don't have permission to view this case's graph." />
      ) : error ? (
        <ErrorState message={error} />
      ) : loading ? (
        <LoadingState label="Loading motifs and correlations..." />
      ) : (
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <Share2 size={16} className="text-text-faint" aria-hidden="true" />
              <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">
                Co-participation motifs ({motifs.length})
              </h2>
            </div>
            {motifs.length === 0 ? (
              <EmptyState
                title="No recurring patterns yet"
                description="A motif appears once one entity participates in two or more projected events."
              />
            ) : (
              <div className="flex flex-col gap-2">
                {motifs.map((motif, index) => (
                  <Card key={`${motif.shared_entity_id}-${motif.event_a_id}-${motif.event_b_id}-${index}`} className="flex flex-col gap-1.5 p-4">
                    <span className="text-sm text-text">
                      <span className="font-medium">
                        {entityById.get(motif.shared_entity_id)?.canonical_label ?? motif.shared_entity_id.slice(0, 8)}
                      </span>{' '}
                      participated in both
                    </span>
                    <div className="flex items-center gap-2 text-xs text-text-dim">
                      <Badge tone="steel-neutral">
                        {eventTypeById.get(motif.event_a_id) ?? motif.event_a_id.slice(0, 8)}
                      </Badge>
                      <span>and</span>
                      <Badge tone="steel-neutral">
                        {eventTypeById.get(motif.event_b_id) ?? motif.event_b_id.slice(0, 8)}
                      </Badge>
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </div>

          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <Link2 size={16} className="text-text-faint" aria-hidden="true" />
              <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">
                Correlations ({correlations.length})
              </h2>
            </div>
            {correlations.length === 0 ? (
              <EmptyState
                title="No correlations yet"
                description="A correlation appears once cross-modal signals link two or more observations."
              />
            ) : (
              <div className="flex flex-col gap-2">
                {correlations.map(({ correlation, projection }) => (
                  <Card key={correlation.correlation_id} className="flex flex-col gap-2 p-4">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-text">{correlation.correlation_type}</span>
                      <div className="flex items-center gap-1.5">
                        <Badge tone={PROPOSITION_TONE[correlation.status]}>{correlation.status}</Badge>
                        <Badge tone={PROJECTION_TONE[projection.status]}>{projection.status}</Badge>
                      </div>
                    </div>
                    <span className="font-mono text-xs text-text-faint">{correlation.correlation_id}</span>
                    <span className="text-xs text-text-dim">
                      {correlation.supporting_observation_ids.length} supporting &middot;{' '}
                      {correlation.contradictory_observation_ids.length} contradicting observation
                      {correlation.contradictory_observation_ids.length === 1 ? '' : 's'}
                    </span>
                    {correlation.hypothesis_reference ? (
                      <span className="text-xs text-text-dim">
                        Hypothesis reference: <span className="font-mono">{correlation.hypothesis_reference}</span>
                      </span>
                    ) : null}
                  </Card>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
