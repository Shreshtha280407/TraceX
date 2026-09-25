import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, GitMerge, MapPin } from 'lucide-react'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Card } from '../components/Card'
import { ErrorState, LoadingState } from '../components/DataState'
import { EntityDetailPanel } from '../components/EntityDetailPanel'
import { NetworkGraph } from '../components/NetworkGraph'
import { ApiError, entityApi, graphApi } from '../lib/api/client'
import type { EntityV1 } from '../lib/api/graph-types'
import type { GraphObservationView, GraphSnapshotResponse } from '../lib/api/graph-types'
import { useActiveCaseWorkspace } from '../lib/cases'
import { findBridgeEntityIds } from '../lib/graphLayout'
import { useWorkspaceSelectionStore } from '../lib/workspaceSelection'

/**
 * Page 7 (hero) -- Investigation Workspace. Real graph + evidence + entity
 * panels in one cockpit. Selecting a node in the graph or a row in the
 * entity list updates the same shared selection (Section 9: "selecting a
 * node updates a real detail panel; the candidate-bridge highlight reflects
 * an actual undecided entity-resolution candidate").
 */
export function InvestigationWorkspace() {
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()
  const selectedEntityId = useWorkspaceSelectionStore((state) => state.selectedEntityId)
  const setSelectedEntityId = useWorkspaceSelectionStore((state) => state.setSelectedEntityId)

  const [snapshot, setSnapshot] = useState<GraphSnapshotResponse | null>(null)
  const [entities, setEntities] = useState<EntityV1[]>([])
  const [observations, setObservations] = useState<GraphObservationView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    if (!activeCaseId) return
    let cancelled = false
    // Fetch-on-case-change with loading/error state: the standard, correct
    // shape for this pattern -- not the "should have been derived during
    // render" case this lint rule targets.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError(null)
    setForbidden(false)
    Promise.all([
      graphApi.getSnapshot(activeCaseId),
      entityApi.list(activeCaseId),
      graphApi.listObservations(activeCaseId),
    ])
      .then(([snapshotRes, entitiesRes, observationsRes]) => {
        if (cancelled) return
        setSnapshot(snapshotRes)
        setEntities(entitiesRes.items)
        setObservations(observationsRes.items)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && err.isForbidden) {
          setForbidden(true)
        } else {
          setError(err instanceof Error ? err.message : 'Unable to reach the server.')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [activeCaseId])

  const selectedEntity = entities.find((e) => e.entity_id === selectedEntityId) ?? null
  // Mirrors NetworkGraph's own bridge detection (same shared helper) so the
  // entity list and detail panel always agree with what the graph highlights.
  const bridgeEntityIds = useMemo(
    () => (snapshot ? findBridgeEntityIds(snapshot.relationships) : new Set<string>()),
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
        <h1 className="text-2xl font-semibold text-text">Investigation Workspace</h1>
        <p className="text-sm text-text-dim">Graph, entities, and evidence in one working surface.</p>
      </div>

      {!activeCase ? (
        <ActiveCaseGate
          cases={cases}
          loading={casesLoading}
          activeCaseId={activeCaseId}
          onSelect={setActiveCase}
        />
      ) : (
        <>
          {error ? <ErrorState message={error} /> : null}
          {forbidden ? (
            <Card className="flex items-start gap-2 border-berry/30 bg-berry/5 p-4 text-sm text-berry">
              <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
              You don&apos;t have permission to view this case&apos;s graph.
            </Card>
          ) : loading ? (
            <LoadingState label="Loading the case graph..." />
          ) : (
            <div className="grid grid-cols-1 gap-6 xl:grid-cols-[280px_1fr_340px]">
              <div className="flex flex-col gap-2">
                <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">
                  Entities ({entities.length})
                </h2>
                <div className="flex max-h-[560px] flex-col gap-1 overflow-y-auto">
                  {entities.length === 0 ? (
                    <span className="text-xs text-text-dim">No entities projected yet.</span>
                  ) : (
                    entities.map((entity) => (
                      <button
                        key={entity.entity_id}
                        type="button"
                        onClick={() => setSelectedEntityId(entity.entity_id)}
                        className={`flex flex-col items-start gap-0.5 rounded-control border px-3 py-2 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson ${
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

              <NetworkGraph
                entities={snapshot?.entities ?? []}
                events={snapshot?.events ?? []}
                relationships={snapshot?.relationships ?? []}
                selectedNodeId={selectedEntityId}
                onSelectNode={(node) => setSelectedEntityId(node.id)}
                className="min-h-[560px]"
              />

              <div>
                {selectedEntity && snapshot ? (
                  <EntityDetailPanel
                    entity={selectedEntity}
                    snapshot={snapshot}
                    observations={observations}
                    isBridge={bridgeEntityIds.has(selectedEntity.entity_id)}
                  />
                ) : (
                  <Card className="flex flex-col items-center gap-2 p-8 text-center text-sm text-text-dim">
                    <MapPin size={20} className="text-text-faint" aria-hidden="true" />
                    Select an entity from the list or the graph to see its detail.
                  </Card>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
