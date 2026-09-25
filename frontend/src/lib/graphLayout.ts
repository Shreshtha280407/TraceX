import type {
  GraphRelationshipKind,
  GraphSnapshotEntityView,
  GraphSnapshotEventView,
  GraphSnapshotRelationshipView,
} from './api/graph-types'

export type GraphNodeKind = 'entity' | 'event'

export interface PositionedNode {
  id: string
  kind: GraphNodeKind
  typeLabel: string
  displayLabel: string
  reviewStatus: string
  eventTime: string | null
  clusterId: number
  x: number
  y: number
  /** An endpoint of a POSSIBLY_SAME_AS edge that crosses two distinct clusters -- Section 7's
   * "the thing a human still has to decide on", the only place color is reserved for a node. */
  isBridge: boolean
}

export interface PositionedEdge {
  id: string
  kind: GraphRelationshipKind
  fromId: string
  toId: string
  isBridge: boolean
}

export interface GraphCluster {
  id: number
  label: string
  count: number
  centerX: number
  centerY: number
  radius: number
}

export interface LayoutResult {
  nodes: PositionedNode[]
  edges: PositionedEdge[]
  clusters: GraphCluster[]
  width: number
  height: number
}

const NODE_SPACING = 120
const CLUSTER_PADDING = 70
const CLUSTER_GAP = 90

/** Deterministic union-find -- no external graph library needed for this scale. */
class UnionFind {
  private parent = new Map<string, string>()

  find(id: string): string {
    if (!this.parent.has(id)) this.parent.set(id, id)
    let root = this.parent.get(id) as string
    if (root !== id) {
      root = this.find(root)
      this.parent.set(id, root)
    }
    return root
  }

  union(a: string, b: string): void {
    const rootA = this.find(a)
    const rootB = this.find(b)
    if (rootA !== rootB) this.parent.set(rootA, rootB)
  }
}

/**
 * The entity IDs that are an endpoint of a `POSSIBLY_SAME_AS` edge crossing
 * two distinct `HAS_PARTICIPANT`-derived clusters -- exported standalone so
 * pages needing just "is this entity a bridge" (e.g. the Investigation
 * Workspace's entity list and detail panel) don't need to run the full
 * layout to agree with what `NetworkGraph` highlights.
 */
export function findBridgeEntityIds(relationships: GraphSnapshotRelationshipView[]): Set<string> {
  const uf = new UnionFind()
  for (const rel of relationships) {
    if (rel.kind === 'HAS_PARTICIPANT') uf.union(rel.from_id, rel.to_id)
  }
  const bridges = new Set<string>()
  for (const rel of relationships) {
    if (rel.kind === 'POSSIBLY_SAME_AS' && uf.find(rel.from_id) !== uf.find(rel.to_id)) {
      bridges.add(rel.from_id)
      bridges.add(rel.to_id)
    }
  }
  return bridges
}

/**
 * Groups entities/events into connected components using only `HAS_PARTICIPANT`
 * edges (real, evidence-backed event participation) -- a real, honest
 * interpretation of Section 7's "cluster groupings", computed client-side
 * over real snapshot data since no backend endpoint returns pre-computed
 * communities (confirmed: `GET /cases/{id}/analytics` returns only raw
 * node/relationship counts by label, never a clustering). `POSSIBLY_SAME_AS`/
 * `CONTRADICTED_BY` are deliberately excluded from clustering itself: a
 * `POSSIBLY_SAME_AS` edge that crosses two clusters formed this way is
 * exactly the "candidate bridge" the spec describes -- using it to merge
 * clusters would make a bridge impossible to ever detect.
 */
export function layoutGraphSnapshot(
  entities: GraphSnapshotEntityView[],
  events: GraphSnapshotEventView[],
  relationships: GraphSnapshotRelationshipView[],
): LayoutResult {
  const uf = new UnionFind()
  const allIds = new Set<string>()
  for (const e of entities) {
    uf.find(e.entity_id)
    allIds.add(e.entity_id)
  }
  for (const e of events) {
    uf.find(e.event_id)
    allIds.add(e.event_id)
  }
  for (const rel of relationships) {
    if (rel.kind === 'HAS_PARTICIPANT') uf.union(rel.from_id, rel.to_id)
  }

  // Stable cluster numbering: first-seen root, in entity-then-event order.
  const rootToClusterId = new Map<string, number>()
  for (const id of allIds) {
    const root = uf.find(id)
    if (!rootToClusterId.has(root)) rootToClusterId.set(root, rootToClusterId.size)
  }

  const clusterMembers = new Map<number, string[]>()
  for (const id of allIds) {
    const clusterId = rootToClusterId.get(uf.find(id)) as number
    const members = clusterMembers.get(clusterId) ?? []
    members.push(id)
    clusterMembers.set(clusterId, members)
  }

  const entityById = new Map(entities.map((e) => [e.entity_id, e]))
  const eventById = new Map(events.map((e) => [e.event_id, e]))

  // Bridge detection: a POSSIBLY_SAME_AS edge whose two endpoints landed in
  // different HAS_PARTICIPANT-derived clusters.
  const bridgeNodeIds = new Set<string>()
  const bridgeEdgeIds = new Set<string>()
  for (const rel of relationships) {
    if (rel.kind !== 'POSSIBLY_SAME_AS') continue
    const clusterA = rootToClusterId.get(uf.find(rel.from_id))
    const clusterB = rootToClusterId.get(uf.find(rel.to_id))
    if (clusterA !== undefined && clusterB !== undefined && clusterA !== clusterB) {
      bridgeNodeIds.add(rel.from_id)
      bridgeNodeIds.add(rel.to_id)
      bridgeEdgeIds.add(`${rel.kind}:${rel.from_id}:${rel.to_id}`)
    }
  }

  // Arrange clusters left-to-right, wrapping into rows -- a stable, readable
  // grid rather than a physics simulation this codebase has no dependency for.
  const clusterIds = [...clusterMembers.keys()].sort((a, b) => a - b)
  const clusters: GraphCluster[] = []
  const nodes: PositionedNode[] = []
  const columns = Math.max(1, Math.ceil(Math.sqrt(clusterIds.length)))
  let maxRowHeight = 0
  let cursorX = CLUSTER_GAP
  let cursorY = CLUSTER_GAP
  let column = 0

  for (const clusterId of clusterIds) {
    const members = clusterMembers.get(clusterId) as string[]
    const nodeRadius = members.length <= 1 ? 0 : (NODE_SPACING * members.length) / (2 * Math.PI)
    const clusterRadius = Math.max(70, nodeRadius + CLUSTER_PADDING)

    const centerX = cursorX + clusterRadius
    const centerY = cursorY + clusterRadius

    members.forEach((id, index) => {
      const angle = (2 * Math.PI * index) / Math.max(members.length, 1) - Math.PI / 2
      const placementRadius = members.length <= 1 ? 0 : nodeRadius
      const x = centerX + placementRadius * Math.cos(angle)
      const y = centerY + placementRadius * Math.sin(angle)

      const entity = entityById.get(id)
      const event = eventById.get(id)
      if (entity) {
        nodes.push({
          id,
          kind: 'entity',
          typeLabel: entity.entity_type,
          displayLabel: entity.canonical_label,
          reviewStatus: entity.review_status,
          eventTime: null,
          clusterId,
          x,
          y,
          isBridge: bridgeNodeIds.has(id),
        })
      } else if (event) {
        nodes.push({
          id,
          kind: 'event',
          typeLabel: event.event_type,
          displayLabel: event.event_type,
          reviewStatus: event.review_status,
          eventTime: event.event_time,
          clusterId,
          x,
          y,
          isBridge: bridgeNodeIds.has(id),
        })
      }
    })

    clusters.push({
      id: clusterId,
      label: `CLUSTER-${String(clusterId + 1).padStart(2, '0')}`,
      count: members.length,
      centerX,
      centerY,
      radius: clusterRadius,
    })

    maxRowHeight = Math.max(maxRowHeight, clusterRadius * 2)
    cursorX += clusterRadius * 2 + CLUSTER_GAP
    column += 1
    if (column >= columns) {
      column = 0
      cursorX = CLUSTER_GAP
      cursorY += maxRowHeight + CLUSTER_GAP
      maxRowHeight = 0
    }
  }

  const width = Math.max(600, columns * (2 * 250 + CLUSTER_GAP) + CLUSTER_GAP)
  const height = Math.max(400, cursorY + maxRowHeight + CLUSTER_GAP)

  const edges: PositionedEdge[] = relationships
    .filter((rel) => allIds.has(rel.from_id) && allIds.has(rel.to_id))
    .map((rel, index) => ({
      // Two distinct real candidates can share the same (kind, from_id,
      // to_id) -- different config_version retrieval-cascade runs producing
      // separate rows for the same entity pair. relationship_id (when
      // present) makes this genuinely unique; the array index is only a
      // last-resort tiebreaker for HAS_PARTICIPANT, which has none.
      id: `${rel.kind}:${rel.from_id}:${rel.to_id}:${rel.relationship_id ?? index}`,
      kind: rel.kind,
      fromId: rel.from_id,
      toId: rel.to_id,
      isBridge: bridgeEdgeIds.has(`${rel.kind}:${rel.from_id}:${rel.to_id}`),
    }))

  return { nodes, edges, clusters, width, height }
}
