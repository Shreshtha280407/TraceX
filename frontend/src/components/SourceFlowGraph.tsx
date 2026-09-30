/** Source-centred fund flow. The chosen source sits in the middle of the canvas;
 * every direct input fans in from the left and every direct output fans out to
 * the right, each on its own curved arrow whose weight follows the amount moved.
 *
 * Counterparties with nothing beyond the source (no further transactions) are
 * drawn as dead ends with a highlighted label, so they are never offered as the
 * next source. Palette matches FundFlowGraph. The canvas takes the width of its
 * panel, so all three columns are visible at 100% zoom. */
import { useEffect, useMemo, useRef, useState } from "react";
import type { FlowCounterparty, FlowResponse } from "../lib/api";

const C = {
  panel: "#12110e",
  panelStroke: "#2a2720",
  ink: "#d8d2c4",
  inkDim: "#8a8474",
  colLabel: "#b8935a",
  mustard: "#e8a935",
  nodeFill: "#201d16",
  nodeStroke: "#8a8474",
  boxFill: "#1c1a14",
  boxStroke: "#5a5546",
  inflow: "#5f8fa3",
  inflowText: "#9cc6d8",
  outflow: "#e8a935",
  dead: "#e3572f",
  deadFill: "#2a1410",
  deadText: "#f08a72",
  pill: "#17150f",
};

const MONO = "'IBM Plex Mono',ui-monospace,Consolas,monospace";

// ---- Geometry (canvas units; the canvas is as wide as its panel) -----------
const MIN_W = 880;
const SIDE_INSET = 104; // counterparty column centre, from each edge
const CARD_W = 172;
const CARD_H = 40;
const ROW_DY = 84; // card + one annotation line + a dead-end pill fit in a row
const PAD_Y = 120;
const MIN_H = 600;
const HEADER_Y = 32;

export type FlowSide = "input" | "output";

function sats(n: number | null | undefined): string {
  if (n === null || n === undefined) return "amount unknown";
  const v = n / 1e8;
  if (v > 0 && v < 0.01) return `${v.toFixed(6).replace(/0+$/, "").replace(/\.$/, "")} BTC`;
  return `${v.toFixed(v >= 100 ? 1 : 2)} BTC`;
}

function short(label: string, keep = 6): string {
  return label.length <= keep * 2 + 2 ? label : `${label.slice(0, keep)}…${label.slice(-4)}`;
}

/** Compact "MM-DD HH:MM" (UTC) for card sub-lines; full stamps live in the panel. */
function when(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString().slice(5, 16).replace("T", " ");
}

/** Point on a cubic Bézier at t, for placing a label on the curve itself. */
function bezierAt(p: number[], t: number): [number, number] {
  const [x0, y0, x1, y1, x2, y2, x3, y3] = p;
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const d = t * t * t;
  return [a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3];
}

function rowsY(count: number, cy: number): number[] {
  return Array.from({ length: count }, (_, i) => cy + (i - (count - 1) / 2) * ROW_DY);
}

function cardTitle(c: FlowCounterparty): string {
  if (c.type === "transaction") return `TX ${short(c.label, 5)}`;
  if (c.type === "output") return `UTXO ${short(c.label, 5)}`;
  return short(c.label, 7);
}

function cardSub(c: FlowCounterparty, side: FlowSide): string {
  const utxos = `${c.utxo_count} UTXO${c.utxo_count === 1 ? "" : "s"}`;
  if (c.type === "transaction") return when(c.timestamp) ? `${utxos} · ${when(c.timestamp)}` : utxos;
  return side === "output" ? `${utxos} · ${c.spent_count} spent on` : utxos;
}

function onwardLabel(c: FlowCounterparty): string {
  if (c.type === "address_or_script") return `↗ ${c.onward_count} more transaction${c.onward_count === 1 ? "" : "s"}`;
  if (c.type === "transaction") return `↗ ${c.onward_count} more leg${c.onward_count === 1 ? "" : "s"}`;
  return "↗ continues";
}

type Placed = { c: FlowCounterparty; side: FlowSide; x: number; y: number; path: number[]; width: number };

export function SourceFlowGraph({
  flow,
  zoom,
  selectedId,
  onSelect,
  onSelectCenter,
  onSetSource,
}: {
  flow: FlowResponse;
  zoom: number;
  selectedId: string | null;
  onSelect: (counterparty: FlowCounterparty, side: FlowSide) => void;
  onSelectCenter: () => void;
  onSetSource: (counterparty: FlowCounterparty) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [panelWidth, setPanelWidth] = useState(0);
  const [hovered, setHovered] = useState<Placed | null>(null);
  const hoverTimer = useRef<number | null>(null);
  const centre = flow.center;
  const isTx = centre.type === "transaction";
  const centreHalfW = isTx ? 46 : 100;
  const centreHalfH = isTx ? 46 : 34;

  // Track the panel width so the canvas fills it: at 100% zoom nothing is
  // clipped, and zooming in scrolls around a source that stays centred.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setPanelWidth(el.clientWidth));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const width = Math.max(MIN_W, Math.floor((panelWidth || MIN_W) / Math.max(zoom, 1)));
  const cx = width / 2;
  const inX = SIDE_INSET;
  const outX = width - SIDE_INSET;

  const layout = useMemo(() => {
    const rows = Math.max(flow.inputs.length, flow.outputs.length, 1);
    const height = Math.max(MIN_H, PAD_Y * 2 + (rows - 1) * ROW_DY);
    const cy = height / 2;
    const maxAmount = Math.max(1, ...[...flow.inputs, ...flow.outputs].map((c) => c.amount_sats ?? 0));
    const weight = (c: FlowCounterparty) => 1.4 + 4.2 * Math.sqrt(Math.max(0, c.amount_sats ?? 0) / maxAmount);
    // Arrows converge on (and leave) a short band of the source's edge rather
    // than one point, so each of many arrows stays individually traceable.
    const port = (y: number) => cy + Math.max(-(centreHalfH - 10), Math.min(centreHalfH - 10, (y - cy) * 0.18));
    const placed: Placed[] = [];
    rowsY(flow.inputs.length, cy).forEach((y, i) => {
      const x0 = inX + CARD_W / 2 + 2;
      const x3 = cx - centreHalfW - 8;
      const py = port(y);
      const dx = x3 - x0;
      placed.push({ c: flow.inputs[i], side: "input", x: inX, y, path: [x0, y, x0 + dx * 0.55, y, x3 - dx * 0.45, py, x3, py], width: weight(flow.inputs[i]) });
    });
    rowsY(flow.outputs.length, cy).forEach((y, i) => {
      const x0 = cx + centreHalfW + 4;
      const x3 = outX - CARD_W / 2 - 8;
      const py = port(y);
      const dx = x3 - x0;
      placed.push({ c: flow.outputs[i], side: "output", x: outX, y, path: [x0, py, x0 + dx * 0.45, py, x3 - dx * 0.55, y, x3, y], width: weight(flow.outputs[i]) });
    });
    return { height, cy, placed };
  }, [flow, centreHalfW, centreHalfH, cx, inX, outX]);

  const { height, cy, placed } = layout;

  // The source is the centre of the canvas; bring it to the centre of the
  // viewport whenever the source, zoom or panel size changes.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({
      left: Math.max(0, cx * zoom - el.clientWidth / 2),
      top: Math.max(0, cy * zoom - el.clientHeight / 2),
      behavior: "smooth",
    });
  }, [centre.id, zoom, cx, cy]);

  // A hover left over from the previous source is dropped: placements are
  // rebuilt per flow, so an old one is simply not among the current ones.
  const hoveredNow = hovered && placed.includes(hovered) ? hovered : null;

  const cancelTimer = () => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
  };
  const hoverable = (p: Placed) => ({
    onMouseEnter: () => {
      cancelTimer();
      hoverTimer.current = window.setTimeout(() => setHovered(p), 200);
    },
    onMouseLeave: () => {
      cancelTimer();
      hoverTimer.current = window.setTimeout(() => setHovered((h) => (h === p ? null : h)), 220);
    },
  });

  const totals = flow.totals;
  const lastY = (side: FlowSide) => {
    const ys = placed.filter((p) => p.side === side).map((p) => p.y);
    return ys.length ? Math.max(...ys) : cy;
  };

  return (
    <div className="ff-scroll ff-scroll-flow" ref={scrollRef}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width * zoom}
        height={height * zoom}
        className="ff-svg"
        role="img"
        aria-label={`Direct fund flow around ${centre.label}`}
      >
        <defs>
          <marker id="sf-in" markerUnits="userSpaceOnUse" markerWidth={12} markerHeight={12} refX={11} refY={6} orient="auto">
            <path d="M0 0L12 6L0 12z" fill={C.inflow} />
          </marker>
          <marker id="sf-out" markerUnits="userSpaceOnUse" markerWidth={12} markerHeight={12} refX={11} refY={6} orient="auto">
            <path d="M0 0L12 6L0 12z" fill={C.outflow} />
          </marker>
          <marker id="sf-dead" markerUnits="userSpaceOnUse" markerWidth={10} markerHeight={10} refX={9} refY={5} orient="auto">
            <path d="M0 0L10 5L0 10z" fill={C.dead} />
          </marker>
        </defs>

        <rect x={0} y={0} width={width} height={height} rx={12} fill={C.panel} stroke={C.panelStroke} />

        {/* Column headers */}
        <text x={inX} y={HEADER_Y} textAnchor="middle" fontSize={11} fill={C.colLabel} fontWeight={600} letterSpacing="0.08em">
          {`INPUTS · ${totals.input_count}`}
        </text>
        <text x={inX} y={HEADER_Y + 16} textAnchor="middle" fontSize={11} fontFamily={MONO} fill={C.inflowText}>{sats(totals.input_sats)}</text>
        <text x={inX} y={HEADER_Y + 31} textAnchor="middle" fontSize={10.5} fill={C.inkDim}>{isTx ? "addresses whose coins it spent" : "transactions that paid it"}</text>
        <text x={cx} y={HEADER_Y} textAnchor="middle" fontSize={11} fill={C.colLabel} fontWeight={600} letterSpacing="0.08em">
          {`SOURCE · ${isTx ? "TRANSACTION" : "ADDRESS"}`}
        </text>
        <text x={outX} y={HEADER_Y} textAnchor="middle" fontSize={11} fill={C.colLabel} fontWeight={600} letterSpacing="0.08em">
          {`OUTPUTS · ${totals.output_count}`}
        </text>
        <text x={outX} y={HEADER_Y + 16} textAnchor="middle" fontSize={11} fontFamily={MONO} fill={C.mustard}>{sats(totals.output_sats)}</text>
        <text x={outX} y={HEADER_Y + 31} textAnchor="middle" fontSize={10.5} fill={C.inkDim}>{isTx ? "addresses it paid" : "transactions that spent from it"}</text>

        <g key={centre.id} className="sf-enter">
          {/* Curved arrows first, so nodes and labels sit on top of them */}
          {placed.map((p) => {
            const [x0, y0, x1, y1, x2, y2, x3, y3] = p.path;
            const d = `M${x0} ${y0} C${x1} ${y1} ${x2} ${y2} ${x3} ${y3}`;
            const dead = !p.c.selectable;
            const stroke = dead ? C.dead : p.side === "input" ? C.inflow : C.outflow;
            const active = selectedId === p.c.id || hoveredNow === p;
            return (
              <g key={`edge-${p.side}-${p.c.id}`} pointerEvents="none">
                {active && <path d={d} fill="none" stroke={stroke} strokeWidth={p.width + 8} opacity={0.14} strokeLinecap="round" />}
                <path
                  d={d}
                  fill="none"
                  stroke={stroke}
                  strokeWidth={p.width}
                  strokeDasharray={dead ? "6 5" : undefined}
                  opacity={dead ? 0.6 : active ? 1 : 0.82}
                  markerEnd={dead ? "url(#sf-dead)" : p.side === "input" ? "url(#sf-in)" : "url(#sf-out)"}
                />
              </g>
            );
          })}

          {/* Amount pills on each curve, near the counterparty where rows are furthest apart */}
          {placed.map((p) => {
            const [lx, ly] = bezierAt(p.path, p.side === "input" ? 0.28 : 0.72);
            const text = sats(p.c.amount_sats);
            const w = text.length * 6.6 + 14;
            const fill = !p.c.selectable ? C.deadText : p.side === "input" ? C.inflowText : C.mustard;
            return (
              <g key={`amt-${p.side}-${p.c.id}`} pointerEvents="none">
                <rect x={lx - w / 2} y={ly - 10} width={w} height={20} rx={10} fill={C.pill} stroke={C.panelStroke} />
                <text x={lx} y={ly + 4} textAnchor="middle" fontSize={11} fontFamily={MONO} fill={fill}>{text}</text>
              </g>
            );
          })}

          {/* Counterparty cards: pill = transaction, box = address/UTXO */}
          {placed.map((p) => {
            const { c, x, y } = p;
            const dead = !c.selectable;
            const sel = selectedId === c.id;
            const rx = c.type === "transaction" ? CARD_H / 2 : 7;
            return (
              <g
                key={`node-${p.side}-${c.id}`}
                data-node-id={c.id}
                style={{ cursor: "pointer" }}
                onClick={() => onSelect(c, p.side)}
                onDoubleClick={() => onSetSource(c)}
                {...hoverable(p)}
              >
                <title>{dead ? `${c.label}\n${c.reason ?? "No further transactions."}` : `${c.label}\nDouble-click, or hover and use “Set as source”, to re-centre here.`}</title>
                {sel && (
                  <rect x={x - CARD_W / 2 - 6} y={y - CARD_H / 2 - 6} width={CARD_W + 12} height={CARD_H + 12} rx={rx + 5} fill="none" stroke={C.mustard} strokeWidth={2} strokeDasharray="4 3" />
                )}
                <rect
                  x={x - CARD_W / 2}
                  y={y - CARD_H / 2}
                  width={CARD_W}
                  height={CARD_H}
                  rx={rx}
                  fill={dead ? C.deadFill : c.type === "transaction" ? C.nodeFill : C.boxFill}
                  stroke={dead ? C.dead : c.type === "transaction" ? C.nodeStroke : C.boxStroke}
                  strokeWidth={1.4}
                  strokeDasharray={dead ? "5 4" : undefined}
                />
                <text x={x} y={y - 3} textAnchor="middle" fontSize={12} fontFamily={MONO} fill={C.ink} fontWeight={500}>{cardTitle(c)}</text>
                <text x={x} y={y + 12} textAnchor="middle" fontSize={10.5} fill={C.inkDim}>{cardSub(c, p.side)}</text>
                {dead ? (
                  // Highlighted, not a quiet grey line: this node cannot be the next source.
                  <g transform={`translate(${x}, ${y + CARD_H / 2 + 14})`}>
                    <rect x={-CARD_W / 2} y={-10} width={CARD_W} height={19} rx={9.5} fill={C.deadFill} stroke={C.dead} />
                    <text x={0} y={3.5} textAnchor="middle" fontSize={10.5} fontWeight={700} fill={C.deadText}>✕ dead end · can't be source</text>
                  </g>
                ) : (
                  <text x={x} y={y + CARD_H / 2 + 15} textAnchor="middle" fontSize={10.5} fill={C.colLabel}>{onwardLabel(c)}</text>
                )}
              </g>
            );
          })}

          {/* Empty sides say why, instead of leaving a blank half-canvas */}
          {flow.inputs.length === 0 && (
            <g pointerEvents="none">
              <text x={inX} y={cy - 4} textAnchor="middle" fontSize={12} fill={C.inkDim}>No inputs in this snapshot</text>
              <text x={inX} y={cy + 13} textAnchor="middle" fontSize={10.5} fill={C.inkDim}>{isTx ? "coinbase, or prevouts outside coverage" : "never paid in the imported data"}</text>
            </g>
          )}
          {flow.outputs.length === 0 && (
            <g pointerEvents="none">
              <text x={outX} y={cy - 4} textAnchor="middle" fontSize={12} fill={C.inkDim}>No outputs in this snapshot</text>
              <text x={outX} y={cy + 13} textAnchor="middle" fontSize={10.5} fill={C.inkDim}>{isTx ? "no outputs were committed" : "funds not spent onward (unspent)"}</text>
            </g>
          )}
          {flow.truncated_inputs > 0 && (
            <text x={inX} y={lastY("input") + ROW_DY * 0.72} textAnchor="middle" fontSize={11} fill={C.colLabel}>+{flow.truncated_inputs} smaller input(s) not drawn</text>
          )}
          {flow.truncated_outputs > 0 && (
            <text x={outX} y={lastY("output") + ROW_DY * 0.72} textAnchor="middle" fontSize={11} fill={C.colLabel}>+{flow.truncated_outputs} smaller output(s) not drawn</text>
          )}

          {/* The source, centred */}
          <g style={{ cursor: "pointer" }} onClick={onSelectCenter}>
            <title>{`Source: ${centre.label}`}</title>
            {isTx ? (
              <>
                <circle cx={cx} cy={cy} r={centreHalfW + 22} fill={C.mustard} fillOpacity={0.07} />
                <circle cx={cx} cy={cy} r={centreHalfW + 10} fill={C.mustard} fillOpacity={0.1} />
                <circle cx={cx} cy={cy} r={centreHalfW + 4} fill="none" stroke={C.mustard} strokeWidth={3} />
                <circle cx={cx} cy={cy} r={centreHalfW - 6} fill={C.nodeFill} stroke={C.nodeStroke} strokeWidth={1.5} />
                <text x={cx} y={cy - 2} textAnchor="middle" fontSize={13} fill={C.ink} fontWeight={700}>TX</text>
                <text x={cx} y={cy + 15} textAnchor="middle" fontSize={11} fontFamily={MONO} fill={C.ink}>{short(centre.label, 4)}</text>
              </>
            ) : (
              <>
                <rect x={cx - centreHalfW - 16} y={cy - centreHalfH - 16} width={(centreHalfW + 16) * 2} height={(centreHalfH + 16) * 2} rx={22} fill={C.mustard} fillOpacity={0.08} />
                <rect x={cx - centreHalfW - 5} y={cy - centreHalfH - 5} width={(centreHalfW + 5) * 2} height={(centreHalfH + 5) * 2} rx={13} fill="none" stroke={C.mustard} strokeWidth={3} />
                <rect x={cx - centreHalfW} y={cy - centreHalfH} width={centreHalfW * 2} height={centreHalfH * 2} rx={9} fill={C.boxFill} stroke={C.boxStroke} strokeWidth={1.3} />
                <text x={cx} y={cy - 3} textAnchor="middle" fontSize={13} fontFamily={MONO} fill={C.ink} fontWeight={600}>{short(centre.label, 8)}</text>
                <text x={cx} y={cy + 15} textAnchor="middle" fontSize={11} fill={C.inkDim}>
                  {centre.transaction_count ?? 0} transaction{centre.transaction_count === 1 ? "" : "s"}
                </text>
              </>
            )}
            <rect x={cx - 38} y={cy - centreHalfH - 46} width={76} height={21} rx={10.5} fill={C.mustard} />
            <text x={cx} y={cy - centreHalfH - 31.5} textAnchor="middle" fontSize={11} fontWeight={700} fill="#161409" letterSpacing="0.06em">SOURCE</text>
            {centre.timestamp && when(centre.timestamp) && (
              <text x={cx} y={cy + centreHalfH + 30} textAnchor="middle" fontSize={11} fill={C.inkDim}>{`${when(centre.timestamp)} UTC`}</text>
            )}
          </g>

          {/* Hover chip: re-centre on this node, or say plainly why it can't be */}
          {hoveredNow && (
            <g
              className="ff-chip"
              transform={`translate(${hoveredNow.x}, ${hoveredNow.y - CARD_H / 2 - 18})`}
              onMouseEnter={cancelTimer}
              onMouseLeave={() => setHovered(null)}
              onClick={(e) => {
                e.stopPropagation();
                setHovered(null);
                onSetSource(hoveredNow.c);
              }}
            >
              <rect x={-58} y={-12} width={116} height={24} rx={12} fill={hoveredNow.c.selectable ? C.mustard : C.dead} />
              <text x={0} y={5} textAnchor="middle" fontSize={12} fontWeight={700} fill={hoveredNow.c.selectable ? "#161409" : "#fff3ee"}>
                {hoveredNow.c.selectable ? "Set as source" : "Can't be source"}
              </text>
            </g>
          )}
        </g>
      </svg>
    </div>
  );
}
