import { useEffect, useMemo, useState } from 'react'
import { GitMerge, Search } from 'lucide-react'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Card } from '../components/Card'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { EntityDetailPanel } from '../components/EntityDetailPanel'
import { ApiError, entityApi, graphApi } from '../lib/api/client'
import type { EntityV1, GraphObservationView, GraphSnapshotResponse } from '../lib/api/graph-types'
import { useActiveCaseWorkspace } from '../lib/cases'
import { findBridgeEntityIds } from '../lib/graphLayout'
import { useWorkspaceSelectionStore } from '../lib/workspaceSelection'

/**
 * Page 9 -- Entity Intelligence. Real entity profiles, identifiers, related
 * events, and connected evidence (Section 5's own role text for this page)
 * -- shares its detail panel and its selection with Investigation Workspace
 * (Section 8: "synced to the same case ... selection state as the
 * Workspace"), so picking an entity here and then opening the Workspace
 * shows the same one already selected.
 */
export function EntityIntelligence() {
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()
  const selectedEntityId = useWorkspaceSelectionStore((state) => state.selectedEntityId)
  const setSelectedEntityId = useWorkspaceSelectionStore((state) => state.setSelectedEntityId)

  const [entities, setEntities] = useState<EntityV1[]>([])
  const [snapshot, setSnapshot] = useState<GraphSnapshotResponse | null>(null)
  const [observations, setObservations] = useState<GraphObservationView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (!activeCaseId) return
    let cancelled = false
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError(null)
    setForbidden(false)
    Promise.all([
      entityApi.list(activeCaseId),
      graphApi.getSnapshot(activeCaseId),
      graphApi.listObservations(activeCaseId),
    ])
      .then(([entitiesRes, snapshotRes, observationsRes]) => {
        if (cancelled) return
        setEntities(entitiesRes.items)
        setSnapshot(snapshotRes)
        setObservations(observationsRes.items)
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

  const bridgeEntityIds = useMemo(
    () => (snapshot ? findBridgeEntityIds(snapshot.relationships) : new Set<string>()),
    [snapshot],
  )

  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return entities
    return entities.filter(
      (e) =>
        e.canonical_label.toLowerCase().includes(normalized) ||
        e.entity_type.toLowerCase().includes(normalized) ||
        e.aliases.some((a) => a.toLowerCase().includes(normalized)) ||
        e.entity_id.toLowerCase().includes(normalized),
    )
  }, [entities, query])

  const selectedEntity = entities.find((e) => e.entity_id === selectedEntityId) ?? null

  return (
    <div className="flex flex-col gap-6">
      <div>
        {activeCase ? (
          <span className="font-mono text-xs uppercase tracking-wider text-text-faint">
            {activeCase.case_reference}
          </span>
        ) : null}
        <h1 className="text-2xl font-semibold text-text">Entity Intelligence</h1>
        <p className="text-sm text-text-dim">
          Entity profiles, identifiers, related events, connected evidence.
        </p>
      </div>

      {!activeCase ? (
        <ActiveCaseGate
          cases={cases}
          loading={casesLoading}
          activeCaseId={activeCaseId}
          onSelect={setActiveCase}
        />
      ) : forbidden ? (
        <ForbiddenState message="You don't have permission to view this case's entities." />
      ) : error ? (
        <ErrorState message={error} />
      ) : loading ? (
        <LoadingState label="Loading entities..." />
      ) : entities.length === 0 ? (
        <EmptyState
          title="No entities yet"
          description="Entities appear once evidence has been processed and identity signals extracted."
        />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[320px_1fr]">
          <div className="flex flex-col gap-3">
            <span className="flex items-center gap-2 rounded-control border border-card-border bg-card px-3 py-2">
              <Search size={14} className="shrink-0 text-text-faint" aria-hidden="true" />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search by name, alias, type, or ID..."
                className="w-full bg-transparent text-sm text-text placeholder:text-text-faint focus:outline-none"
              />
            </span>
            <div className="flex max-h-[640px] flex-col gap-1 overflow-y-auto">
              {filtered.length === 0 ? (
                <span className="text-xs text-text-dim">No entities match your search.</span>
              ) : (
                filtered.map((entity) => (
                  <button
                    key={entity.entity_id}
                    type="button"
                    onClick={() => setSelectedEntityId(entity.entity_id)}
                    className={`flex flex-col items-start gap-0.5 rounded-control border px-3 py-2 text-left transition-colors ${
                      entity.entity_id === selectedEntityId
                        ? 'border-crimson bg-crimson/5'
                        : 'border-card-border bg-card hover:bg-canvas/10'
                    }`}
                  >
                    <span className="flex w-full items-center gap-1.5 text-sm font-medium text-text">
                      {bridgeEntityIds.has(entity.entity_id) ? (
                        <GitMerge size={12} className="shrink-0 text-crimson" aria-hidden="true" />
                      ) : null}
                      <span className="truncate">{entity.canonical_label}</span>
                    </span>
                    <span className="text-xs text-text-faint">{entity.entity_type}</span>
                  </button>
                ))
              )}
            </div>
          </div>

          {selectedEntity && snapshot ? (
            <EntityDetailPanel
              entity={selectedEntity}
              snapshot={snapshot}
              observations={observations}
              isBridge={bridgeEntityIds.has(selectedEntity.entity_id)}
            />
          ) : (
            <Card className="flex flex-col items-center gap-2 p-8 text-center text-sm text-text-dim">
              Select an entity from the list to see its full profile.
            </Card>
          )}
        </div>
      )}
    </div>
  )
}
