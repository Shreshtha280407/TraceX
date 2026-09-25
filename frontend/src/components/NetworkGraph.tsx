import { useMemo, useRef, useState } from 'react'
import { Crosshair, ZoomIn, ZoomOut } from 'lucide-react'
import type {
  GraphRelationshipKind,
  GraphSnapshotEntityView,
  GraphSnapshotEventView,
  GraphSnapshotRelationshipView,
} from '../lib/api/graph-types'
import { layoutGraphSnapshot } from '../lib/graphLayout'
import type { PositionedNode } from '../lib/graphLayout'

interface NetworkGraphProps {
  entities: GraphSnapshotEntityView[]
  events: GraphSnapshotEventView[]
  relationships: GraphSnapshotRelationshipView[]
  selectedNodeId?: string | null
  onSelectNode?: (node: PositionedNode) => void
  className?: string
}

const CARD_WIDTH = 108
const CARD_HEIGHT = 42

/** Restyled onto our theme (Section 7's "Restyle onto our theme" table),
 * mapped onto exactly the three relationship kinds a real snapshot can ever
 * contain (schemas.py's own scoping comment). `CONTRADICTED_BY` fills the
 * table's fourth ("Hypothesis") row: a hypothesis is never rendered as a
 * graph edge at all -- `hypothesis_models.py`'s own module docstring is
 * explicit that a hypothesis "never creates a timeless entity-to-entity
 * relationship," only citations to observations/candidates, which the
 * Hypotheses page (Section 5, page 12) surfaces directly. Inventing a fake
 * edge for it here would violate this codebase's own no-fabrication rule;
 * `CONTRADICTED_BY` (a real, structurally similar identity-dispute edge)
 * takes that legend slot instead, honestly labeled as itself. */
const EDGE_STYLE: Record<
  GraphRelationshipKind,
  { label: string; color: string; dash: string | undefined; meaning: string }
> = {
  HAS_PARTICIPANT: { label: 'Fact', color: '#7B904B', dash: undefined, meaning: 'Directly observed participant link.' },
  POSSIBLY_SAME_AS: {
    label: 'Inference',
    color: '#5C6B78',
    dash: '6 4',
    meaning: 'System-inferred identity candidate, pending review.',
  },
  CONTRADICTED_BY: {
    label: 'Contradicted',
    color: '#993955',
    dash: '2 3',
    meaning: 'Identity-resolution dispute between two entities.',
  },
}
const BRIDGE_COLOR = '#550527'

function typeInitial(typeLabel: string): string {
  return typeLabel.replace(/[_-]/g, ' ').toUpperCase()
}

function shortId(id: string): string {
  return id.slice(0, 8)
}

/**
 * The Network Graph component (Section 7). Real entities/events/
 * relationships in, positioned via `layoutGraphSnapshot`'s deterministic
 * connected-components clustering -- no backend clustering endpoint exists,
 * so this is a real, honest client-side graph-theory operation over real
 * snapshot data, not a fabricated layout.
 */
export function NetworkGraph({
  entities,
  events,
  relationships,
  selectedNodeId,
  onSelectNode,
  className = '',
}: NetworkGraphProps) {
  const layout = useMemo(
    () => layoutGraphSnapshot(entities, events, relationships),
    [entities, events, relationships],
  )
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [playhead, setPlayhead] = useState<number | null>(null)
  const dragRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)

  const eventTimes = layout.nodes
    .map((n) => n.eventTime)
    .filter((t): t is string => t !== null)
    .map((t) => new Date(t).getTime())
    .sort((a, b) => a - b)
  const minTime = eventTimes[0] ?? null
  const maxTime = eventTimes[eventTimes.length - 1] ?? null
  const hasTimeline = minTime !== null && maxTime !== null && minTime !== maxTime

  const nodeById = useMemo(() => new Map(layout.nodes.map((n) => [n.id, n])), [layout.nodes])

  const viewBox = `${pan.x} ${pan.y} ${layout.width / zoom} ${layout.height / zoom}`

  function handleZoom(factor: number) {
    setZoom((z) => Math.min(3, Math.max(0.4, z * factor)))
  }

  function handleRecenter() {
    setZoom(1)
    setPan({ x: 0, y: 0 })
  }

  function handlePointerDown(event: React.PointerEvent<SVGSVGElement>) {
    dragRef.current = { startX: event.clientX, startY: event.clientY, panX: pan.x, panY: pan.y }
    ;(event.target as Element).setPointerCapture(event.pointerId)
  }

  function handlePointerMove(event: React.PointerEvent<SVGSVGElement>) {
    if (!dragRef.current) return
    const dx = (event.clientX - dragRef.current.startX) / zoom
    const dy = (event.clientY - dragRef.current.startY) / zoom
    setPan({ x: dragRef.current.panX - dx, y: dragRef.current.panY - dy })
  }

  function handlePointerUp() {
    dragRef.current = null
  }

  const isDimmed = (node: PositionedNode) => {
    if (playhead === null || node.kind !== 'event' || node.eventTime === null) return false
    return new Date(node.eventTime).getTime() > playhead
  }

  return (
    <div className={`flex flex-col overflow-hidden rounded-card bg-shell-topbar ${className}`}>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-white/10 px-4 py-3">
        {(Object.keys(EDGE_STYLE) as GraphRelationshipKind[]).map((kind) => {
          const style = EDGE_STYLE[kind]
          return (
            <div key={kind} className="flex items-center gap-2" title={style.meaning}>
              <svg width="24" height="8" className="shrink-0">
                <line
                  x1="0"
                  y1="4"
                  x2="24"
                  y2="4"
                  stroke={style.color}
                  strokeWidth="2"
                  strokeDasharray={style.dash}
                />
              </svg>
              <span className="text-xs font-medium text-shell-text-dim">{style.label}</span>
            </div>
          )
        })}
        <div className="flex items-center gap-2" title="An unresolved entity-resolution candidate crossing two clusters -- the thing a human still has to decide on.">
          <span
            className="h-2.5 w-2.5 shrink-0 rounded-full border-2"
            style={{ borderColor: BRIDGE_COLOR, backgroundColor: `${BRIDGE_COLOR}55` }}
          />
          <span className="text-xs font-medium text-shell-text-dim">Candidate bridge</span>
        </div>
        <span className="ml-auto text-[10px] text-shell-text-dim/70">
          Hypothesis links appear on the Hypotheses page as evidence citations, not as graph edges.
        </span>
      </div>

      <div className="relative flex-1">
        <div className="absolute right-3 top-3 z-10 flex flex-col gap-1">
          <button
            type="button"
            onClick={() => handleZoom(1.25)}
            aria-label="Zoom in"
            className="flex h-8 w-8 items-center justify-center rounded-control bg-black/30 text-shell-text-dim hover:bg-black/50 hover:text-shell-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson"
          >
            <ZoomIn size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={() => handleZoom(0.8)}
            aria-label="Zoom out"
            className="flex h-8 w-8 items-center justify-center rounded-control bg-black/30 text-shell-text-dim hover:bg-black/50 hover:text-shell-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson"
          >
            <ZoomOut size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={handleRecenter}
            aria-label="Recenter"
            className="flex h-8 w-8 items-center justify-center rounded-control bg-black/30 text-shell-text-dim hover:bg-black/50 hover:text-shell-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson"
          >
            <Crosshair size={15} aria-hidden="true" />
          </button>
        </div>

        {layout.nodes.length === 0 ? (
          <div className="flex h-full min-h-[320px] items-center justify-center text-sm text-shell-text-dim">
            No entities or events projected for this case yet.
          </div>
        ) : (
          <svg
            viewBox={viewBox}
            className="h-full min-h-[420px] w-full cursor-grab touch-none active:cursor-grabbing"
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerLeave={handlePointerUp}
          >
            {layout.clusters.map((cluster) => (
              <g key={cluster.id}>
                <circle
                  cx={cluster.centerX}
                  cy={cluster.centerY}
                  r={cluster.radius}
                  fill="none"
                  stroke="#5C6B7855"
                  strokeDasharray="4 4"
                  strokeWidth="1.5"
                />
                <text
                  x={cluster.centerX - cluster.radius}
                  y={cluster.centerY - cluster.radius - 8}
                  className="font-mono"
                  fontSize="10"
                  letterSpacing="0.05em"
                  fill="#A9B8A6"
                >
                  {cluster.label} &middot; {cluster.count}
                </text>
              </g>
            ))}

            {layout.edges.map((edge) => {
              const from = nodeById.get(edge.fromId)
              const to = nodeById.get(edge.toId)
              if (!from || !to) return null
              const style = EDGE_STYLE[edge.kind]
              return (
                <line
                  key={edge.id}
                  x1={from.x}
                  y1={from.y}
                  x2={to.x}
                  y2={to.y}
                  stroke={edge.isBridge ? BRIDGE_COLOR : style.color}
                  strokeWidth={edge.isBridge ? 2 : 1.25}
                  strokeDasharray={edge.isBridge ? '5 3' : style.dash}
                  opacity={isDimmed(from) || isDimmed(to) ? 0.25 : 0.85}
                />
              )
            })}

            {layout.nodes.map((node) => {
              const selected = node.id === selectedNodeId
              const dimmed = isDimmed(node)
              return (
                <g
                  key={node.id}
                  transform={`translate(${node.x - CARD_WIDTH / 2}, ${node.y - CARD_HEIGHT / 2})`}
                  className="cursor-pointer"
                  opacity={dimmed ? 0.35 : 1}
                  onClick={() => onSelectNode?.(node)}
                >
                  <rect
                    width={CARD_WIDTH}
                    height={CARD_HEIGHT}
                    rx={7}
                    fill={node.isBridge ? `${BRIDGE_COLOR}33` : '#2F2B27'}
                    stroke={selected ? '#FCE0E5' : node.isBridge ? BRIDGE_COLOR : '#4A443E'}
                    strokeWidth={selected || node.isBridge ? 2 : 1}
                  />
                  <text x={8} y={15} fontSize="8" letterSpacing="0.04em" fill="#82868F" className="font-mono">
                    {typeInitial(node.typeLabel)}
                  </text>
                  <text
                    x={8}
                    y={27}
                    fontSize="10.5"
                    fill="#E7E6EC"
                    className="font-sans"
                  >
                    {node.displayLabel.length > 16 ? `${node.displayLabel.slice(0, 15)}…` : node.displayLabel}
                  </text>
                  <text x={8} y={37} fontSize="8" fill="#9CA3AF" className="font-mono">
                    {shortId(node.id)}
                  </text>
                </g>
              )
            })}
          </svg>
        )}
      </div>

      {hasTimeline ? (
        <div className="flex items-center gap-3 border-t border-white/10 px-4 py-3">
          <span className="shrink-0 font-mono text-[10px] text-shell-text-dim">
            {new Date(minTime as number).toLocaleDateString()}
          </span>
          <input
            type="range"
            min={minTime as number}
            max={maxTime as number}
            value={playhead ?? (maxTime as number)}
            onChange={(event) => setPlayhead(Number(event.target.value))}
            className="h-1 flex-1 cursor-pointer accent-crimson"
            aria-label="Timeline playhead"
          />
          <span className="shrink-0 font-mono text-[10px] text-shell-text-dim">
            {new Date(maxTime as number).toLocaleDateString()}
          </span>
          {playhead !== null ? (
            <button
              type="button"
              onClick={() => setPlayhead(null)}
              className="shrink-0 rounded-control text-[10px] font-medium text-shell-text-dim underline hover:text-shell-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson"
            >
              Reset
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
