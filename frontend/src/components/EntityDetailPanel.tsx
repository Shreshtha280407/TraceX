import { useMemo } from 'react'
import { GitMerge } from 'lucide-react'
import { Link } from 'react-router-dom'
import type {
  EntityV1,
  GraphObservationView,
  GraphSnapshotEventView,
  GraphSnapshotResponse,
} from '../lib/api/graph-types'
import { Badge } from './Badge'
import { Card } from './Card'

const REVIEW_STATUS_TONE = { unreviewed: 'steel-neutral', confirmed: 'palm', disputed: 'crimson' } as const

interface EntityDetailPanelProps {
  entity: EntityV1
  snapshot: GraphSnapshotResponse
  observations: GraphObservationView[]
  isBridge: boolean
}

/**
 * A real entity's profile: identifiers/aliases/attributes from `EntityV1`
 * itself, related events derived from the case graph snapshot's
 * `HAS_PARTICIPANT` relationships, and linked evidence derived from
 * `created_from_observation_ids` (the real, designed-for-this field --
 * never inferred from mention-label text matching, since
 * `GraphEntityMentionView` carries no entity_id back-reference). Shared
 * between Investigation Workspace (Section 5, page 7) and Entity
 * Intelligence (page 9) -- the same real detail, not two divergent copies.
 */
export function EntityDetailPanel({ entity, snapshot, observations, isBridge }: EntityDetailPanelProps) {
  const relatedEvents = useMemo(() => {
    const eventIds = snapshot.relationships
      .filter((rel) => rel.kind === 'HAS_PARTICIPANT' && rel.to_id === entity.entity_id)
      .map((rel) => rel.from_id)
    const byId = new Map<string, GraphSnapshotEventView>(snapshot.events.map((e) => [e.event_id, e]))
    return eventIds.map((id) => byId.get(id)).filter((e): e is GraphSnapshotEventView => e !== undefined)
  }, [snapshot, entity.entity_id])

  const observationById = useMemo(
    () => new Map(observations.map((o) => [o.observation_id, o])),
    [observations],
  )
  const linkedEvidenceIds = useMemo(() => {
    const evidenceIds = new Set<string>()
    for (const obsId of entity.created_from_observation_ids) {
      const obs = observationById.get(obsId)
      if (obs) evidenceIds.add(obs.evidence_id)
    }
    return [...evidenceIds]
  }, [entity.created_from_observation_ids, observationById])

  return (
    <Card className="flex flex-col gap-4 p-5">
      <div className="flex items-start justify-between gap-2">
        <div>
          <span className="text-xs font-medium uppercase tracking-wide text-text-faint">
            {entity.entity_type}
          </span>
          <h3 className="text-lg font-semibold text-text">{entity.canonical_label}</h3>
          <span className="font-mono text-xs text-text-faint">{entity.entity_id}</span>
        </div>
        <Badge tone={REVIEW_STATUS_TONE[entity.review_status]}>{entity.review_status}</Badge>
      </div>

      {isBridge ? (
        <Link
          to="/review/candidates"
          className="flex items-center gap-2 rounded-control border border-crimson/40 bg-crimson/5 px-3 py-2 text-xs font-medium text-crimson hover:bg-crimson/10"
        >
          <GitMerge size={14} aria-hidden="true" />
          This entity has an unresolved candidate bridge -- review it &rarr;
        </Link>
      ) : null}

      {entity.aliases.length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium uppercase tracking-wide text-text-faint">Aliases</span>
          <div className="flex flex-wrap gap-1.5">
            {entity.aliases.map((alias) => (
              <Badge key={alias} tone="steel-neutral">
                {alias}
              </Badge>
            ))}
          </div>
        </div>
      ) : null}

      {Object.keys(entity.stable_identifiers).length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium uppercase tracking-wide text-text-faint">Identifiers</span>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            {Object.entries(entity.stable_identifiers).map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="font-mono text-xs text-text-faint">{key}</dt>
                <dd className="font-mono text-text">{String(value)}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}

      {Object.keys(entity.attributes).length > 0 ? (
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium uppercase tracking-wide text-text-faint">Attributes</span>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            {Object.entries(entity.attributes).map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="text-xs text-text-faint">{key}</dt>
                <dd className="text-text">{String(value)}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}

      <div className="flex flex-col gap-1.5">
        <span className="text-xs font-medium uppercase tracking-wide text-text-faint">
          Related events ({relatedEvents.length})
        </span>
        {relatedEvents.length === 0 ? (
          <span className="text-xs text-text-dim">No projected events for this entity yet.</span>
        ) : (
          <ul className="flex flex-col gap-1">
            {relatedEvents.map((event) => (
              <li
                key={event.event_id}
                className="flex items-center justify-between rounded-control bg-canvas/10 px-2.5 py-1.5 text-xs"
              >
                <span className="text-text">{event.event_type}</span>
                <span className="font-mono text-text-faint">
                  {event.event_time ? new Date(event.event_time).toLocaleString() : 'no timestamp'}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <span className="text-xs font-medium uppercase tracking-wide text-text-faint">
          Linked evidence ({linkedEvidenceIds.length})
        </span>
        {linkedEvidenceIds.length === 0 ? (
          <span className="text-xs text-text-dim">
            No linked evidence resolved in the current observation page.
          </span>
        ) : (
          <ul className="flex flex-col gap-1">
            {linkedEvidenceIds.map((evidenceId) => (
              <li key={evidenceId} className="font-mono text-xs text-text-dim">
                {evidenceId}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  )
}
