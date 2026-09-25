import { useEffect, useMemo, useState } from 'react'
import { Clock, MapPin } from 'lucide-react'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Badge } from '../components/Badge'
import { Card } from '../components/Card'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { ApiError, entityApi, graphApi } from '../lib/api/client'
import type { EntityV1, GraphObservationView, GraphSnapshotResponse } from '../lib/api/graph-types'
import { useActiveCaseWorkspace } from '../lib/cases'
import { useWorkspaceSelectionStore } from '../lib/workspaceSelection'

interface ChronologicalItem {
  id: string
  time: number
  timeLabel: string
  kind: 'event' | 'observation'
  label: string
  detail: string
}

/**
 * Page 8 -- Timeline + Map. Real chronological reconstruction from the case
 * graph snapshot's events plus real observation timestamps; real spatial
 * plot from observations that actually carry a latitude/longitude (never
 * fabricated coordinates for the ones that don't). Synced to the same
 * case/entity selection as the Workspace (Section 8) via the shared
 * `useWorkspaceSelectionStore` -- selecting an entity there highlights
 * exactly the observations it was created from, here.
 *
 * The map is a real coordinate scatter plot, not a tile-based slippy map:
 * no map-tile provider (Mapbox/OSM/Google) is configured anywhere in this
 * backend or its environment, and fabricating one against no real service
 * would be exactly the kind of "looks finished, isn't real" the master
 * prompt's Section 9 explicitly forbids. Real latitude/longitude values are
 * plotted proportionally on their own axes instead -- honest about what it
 * is, not a fake basemap.
 */
export function TimelineMap() {
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()
  const selectedEntityId = useWorkspaceSelectionStore((state) => state.selectedEntityId)

  const [snapshot, setSnapshot] = useState<GraphSnapshotResponse | null>(null)
  const [observations, setObservations] = useState<GraphObservationView[]>([])
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
      graphApi.getSnapshot(activeCaseId),
      graphApi.listObservations(activeCaseId),
      entityApi.list(activeCaseId),
    ])
      .then(([snapshotRes, observationsRes, entitiesRes]) => {
        if (cancelled) return
        setSnapshot(snapshotRes)
        setObservations(observationsRes.items)
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

  const selectedEntity = entities.find((e) => e.entity_id === selectedEntityId) ?? null
  const highlightedObservationIds = useMemo(
    () => new Set(selectedEntity?.created_from_observation_ids ?? []),
    [selectedEntity],
  )

  const chronologicalItems = useMemo<ChronologicalItem[]>(() => {
    const items: ChronologicalItem[] = []
    if (snapshot) {
      for (const event of snapshot.events) {
        if (!event.event_time) continue
        items.push({
          id: `event:${event.event_id}`,
          time: new Date(event.event_time).getTime(),
          timeLabel: new Date(event.event_time).toLocaleString(),
          kind: 'event',
          label: event.event_type,
          detail: `confidence ${event.confidence.toFixed(2)}`,
        })
      }
    }
    for (const obs of observations) {
      const time = obs.event_time ?? obs.time_window_start
      if (!time) continue
      items.push({
        id: `observation:${obs.observation_id}`,
        time: new Date(time).getTime(),
        timeLabel: new Date(time).toLocaleString(),
        kind: 'observation',
        label: obs.observation_type,
        detail: obs.mentions.map((m) => m.display_label).join(', ') || obs.location_raw_text || 'no detail',
      })
    }
    return items.sort((a, b) => a.time - b.time)
  }, [snapshot, observations])

  const geoObservations = useMemo(
    () => observations.filter((o) => o.location_latitude !== null && o.location_longitude !== null),
    [observations],
  )

  const mapBounds = useMemo(() => {
    if (geoObservations.length === 0) return null
    const lats = geoObservations.map((o) => o.location_latitude as number)
    const lngs = geoObservations.map((o) => o.location_longitude as number)
    return {
      minLat: Math.min(...lats),
      maxLat: Math.max(...lats),
      minLng: Math.min(...lngs),
      maxLng: Math.max(...lngs),
    }
  }, [geoObservations])

  function projectToPlot(lat: number, lng: number): { x: number; y: number } {
    if (!mapBounds) return { x: 50, y: 50 }
    const { minLat, maxLat, minLng, maxLng } = mapBounds
    const latRange = maxLat - minLat || 1
    const lngRange = maxLng - minLng || 1
    return {
      x: 20 + ((lng - minLng) / lngRange) * 360,
      y: 20 + (1 - (lat - minLat) / latRange) * 260,
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        {activeCase ? (
          <span className="font-mono text-xs uppercase tracking-wider text-text-faint">
            {activeCase.case_reference}
          </span>
        ) : null}
        <h1 className="text-2xl font-semibold text-text">Timeline + Map</h1>
        <p className="text-sm text-text-dim">Spatial and chronological reconstruction for this case.</p>
      </div>

      {!activeCase ? (
        <ActiveCaseGate cases={cases} loading={casesLoading} activeCaseId={activeCaseId} onSelect={setActiveCase} />
      ) : forbidden ? (
        <ForbiddenState message="You don't have permission to view this case's graph." />
      ) : error ? (
        <ErrorState message={error} />
      ) : loading ? (
        <LoadingState label="Loading timeline and map data..." />
      ) : (
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <Clock size={16} className="text-text-faint" aria-hidden="true" />
              <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">
                Chronological reconstruction ({chronologicalItems.length})
              </h2>
            </div>
            {selectedEntity ? (
              <span className="text-xs text-text-dim">
                Highlighting items tied to <span className="font-medium text-text">{selectedEntity.canonical_label}</span>.
              </span>
            ) : null}
            {chronologicalItems.length === 0 ? (
              <EmptyState
                title="No dated events or observations yet"
                description="Timestamps appear once evidence has been processed and events/observations projected."
              />
            ) : (
              <Card className="flex max-h-[600px] flex-col divide-y divide-card-border overflow-y-auto p-0">
                {chronologicalItems.map((item) => {
                  const isHighlighted =
                    item.kind === 'observation' &&
                    highlightedObservationIds.has(item.id.replace('observation:', ''))
                  return (
                    <div
                      key={item.id}
                      className={`flex items-start gap-3 p-3 ${isHighlighted ? 'bg-crimson/5' : ''}`}
                    >
                      <span
                        className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
                          item.kind === 'event' ? 'bg-palm' : 'bg-steel-neutral'
                        }`}
                      />
                      <div className="flex min-w-0 flex-col gap-0.5">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-text">{item.label}</span>
                          <Badge tone={item.kind === 'event' ? 'palm' : 'steel-neutral'}>{item.kind}</Badge>
                        </div>
                        <span className="truncate text-xs text-text-dim">{item.detail}</span>
                        <span className="font-mono text-xs text-text-faint">{item.timeLabel}</span>
                      </div>
                    </div>
                  )
                })}
              </Card>
            )}
          </div>

          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <MapPin size={16} className="text-text-faint" aria-hidden="true" />
              <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">
                Spatial plot ({geoObservations.length} of {observations.length} observations geolocated)
              </h2>
            </div>
            {geoObservations.length === 0 ? (
              <EmptyState
                title="No geolocated observations"
                description="No observation in this case carries a real latitude/longitude yet."
              />
            ) : (
              <Card className="p-0">
                <svg viewBox="0 0 400 300" className="h-[400px] w-full rounded-card bg-shell-topbar">
                  {geoObservations.map((obs) => {
                    const { x, y } = projectToPlot(
                      obs.location_latitude as number,
                      obs.location_longitude as number,
                    )
                    const isHighlighted = highlightedObservationIds.has(obs.observation_id)
                    return (
                      <g key={obs.observation_id}>
                        <circle
                          cx={x}
                          cy={y}
                          r={isHighlighted ? 6 : 4}
                          fill={isHighlighted ? '#550527' : '#7B904B'}
                          stroke={isHighlighted ? '#FCE0E5' : 'none'}
                          strokeWidth={1.5}
                        />
                        <title>
                          {obs.location_raw_text ?? `${obs.location_latitude}, ${obs.location_longitude}`}
                        </title>
                      </g>
                    )
                  })}
                </svg>
                <p className="px-4 py-2 text-[10px] text-text-faint">
                  Real latitude/longitude values plotted proportionally -- not a tile-based map (no map-tile
                  provider is configured in this deployment).
                </p>
              </Card>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
