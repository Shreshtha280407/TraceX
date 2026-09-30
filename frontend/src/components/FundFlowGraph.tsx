/** Fund-flow canvas — a direct port of the approved tracex_graph_mockup.svg
 * (1600x900), re-origined so (0,0) is the canvas panel's top-left and driven
 * entirely by a real peeling-chain finding's per-hop steps.
 *
 * Every geometry constant below is the mockup's own measurement, so the result
 * matches the approved design rather than approximating it. Interaction (hover
 * chip, node selection, zoom) is layered strictly on top: it adds elements and
 * handlers but never alters the node/edge visual language underneath. */
import { useEffect, useMemo, useRef, useState } from "react";
import type { Finding, PathSignals, PeelOutput, PeelingStep } from "../lib/api";

// ---- Palette (lifted verbatim from the mockup) ----------------------------
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
  guide: "#1f1d17",
  clusterFill: "#15140f",
  clusterStroke: "#4a4536",
  clusterLabel: "#b8a988",
  observed: "#5f8fa3",
  suspected: "#c98a3a",
  legendFill: "#14130f",
  diamondFill: "#1c1a14",
  diamondStroke: "#6f6a5d",
  peelValue: "#d9a45a",
  riskNeutral: "#8a8474",
  riskReview: "#d6c14a",
  riskSuspicious: "#e0802f",
  riskHigh: "#d9482f",
};

const MONO = "'IBM Plex Mono',ui-monospace,Consolas,monospace";
const SANS = "inherit";

// ---- Geometry (mockup coords minus the panel origin at x=20,y=84) ---------
const PANEL_H = 780;
const COL_X0 = 88;
const COL_DX = 162;
const ROW_Y = 366;
const HEADER_Y = 29;
const TIME_Y = 47;
const GUIDE_TOP = 62;
const GUIDE_BOT = 616;
const BANNER_Y = 106;
const BANNER_H = 24;
const BRACKET_Y = 130;
const BAND_H = 64;
const STACK_DY = 42;
const TIMELINE_Y = 644;
const TICK_LABEL_Y = 666;
const LEGEND_Y = 688;
const LEGEND_H = 82;
const LEGEND_W = 880;
const PEEL_UP_Y = ROW_Y - 150;
const PEEL_DOWN_Y = ROW_Y + 170;

export const ZOOM_STEPS = [0.5, 0.65, 0.8, 1, 1.25, 1.5, 2] as const;

/** What the page shows when a node is tapped — the graph owns the meaning of
 * each node, the panel only renders it. */
export type GraphNodeInfo = {
  id: string;
  kind: "tx" | "address" | "output";
  title: string;
  rows: { k: string; v: string }[];
  note: string;
};

function sats(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const v = n / 1e8;
  // A real peeled output is often far below 0.01 BTC -- rounding it to "0.00"
  // would erase the very value the reviewer is looking at.
  if (v > 0 && v < 0.01) return `${v.toFixed(6).replace(/0+$/, "").replace(/\.$/, "")} BTC`;
  return `${v.toFixed(2)} BTC`;
}

function shortAddr(a: string | null): string {
  if (!a) return "unknown";
  return a.length <= 14 ? a : `${a.slice(0, 6)}…${a.slice(-4)}`;
}

function shortTx(t: string): string {
  return t.length <= 12 ? t : `${t.slice(0, 4)}…${t.slice(-4)}`;
}

function clock(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

function stamp(iso: string | null): string {
  return iso ? new Date(iso).toISOString().replace("T", " ").slice(0, 19) + " UTC" : "not available";
}

function gapLabel(a: string | null, b: string | null): string | null {
  if (!a || !b) return null;
  const secs = (new Date(b).getTime() - new Date(a).getTime()) / 1000;
  if (!Number.isFinite(secs) || secs < 0) return null;
  if (secs < 60) return `+${Math.round(secs)}s`;
  const m = Math.round(secs / 60);
  if (m < 60) return `+${m} min`;
  const h = Math.round(m / 60);
  if (h < 48) return `+${h} hr`;
  return `+${Math.round(h / 24)}d`;
}

/** Risk ring colour is a real, explainable rollup of this hop's own evidence —
 * never decorative. Documented in the on-canvas RISK RING legend. */
function txRisk(step: PeelingStep, flagged: Set<string>): { color: string; glow: boolean; why: string; label: string } {
  if (flagged.has(`tx:${step.spending_transaction_id}`))
    return { color: C.riskHigh, glow: true, label: "High risk", why: "This transaction is independently flagged by another open finding in this case." };
  if (step.co_spend_input_address_count >= 2)
    return { color: C.riskSuspicious, glow: false, label: "Suspicious", why: `Spends ${step.co_spend_input_address_count} distinct input addresses together — common-input-ownership heuristic. No identity is asserted.` };
  if (step.peel_outputs.some((p) => !p.is_spent))
    return { color: C.riskReview, glow: false, label: "Review", why: "This hop leaves an unspent peeled output — the peeled value has not moved again in this snapshot." };
  return { color: C.riskNeutral, glow: false, label: "Neutral", why: "No additional signal on this hop beyond being part of the verified chain." };
}

type AddrCol = {
  kind: "address";
  header: string;
  time: string;
  iso: string | null;
  address: string | null;
  script: string | null;
  value: number | null;
  cluster: string[];
  outputId: string | null;
};
type TxCol = { kind: "tx"; header: string; time: string; txid: string; arity: string; step: PeelingStep; index: number };
type Col = AddrCol | TxCol;

function buildColumns(steps: PeelingStep[]): Col[] {
  const cols: Col[] = [];
  const lastIdx = steps.length - 1;
  const hasDestination = !!steps[lastIdx]?.continuing_output_id;
  const addrTotal = hasDestination ? steps.length + 1 : steps.length;

  for (let i = 0; i < addrTotal; i++) {
    const from = i === 0 ? steps[0] : steps[i - 1];
    const isSource = i === 0;
    cols.push({
      kind: "address",
      header: isSource ? "SOURCE" : i === addrTotal - 1 ? "DESTINATION" : `HOP ${i}`,
      time: isSource ? "prior to T0" : `T${2 * i - 1} · ${clock(from.timestamp)}`,
      iso: isSource ? null : from.timestamp,
      address: isSource ? from.previous_address : from.continuing_address,
      script: isSource ? from.previous_script_type : from.continuing_script_type,
      value: isSource ? from.previous_value_sats : from.continuing_value_sats,
      outputId: isSource ? from.previous_output_id : from.continuing_output_id,
      // The addresses co-spent by the transaction that spends OUT of this
      // column -- i.e. this column's real common-input-ownership cluster.
      cluster: (steps[i]?.co_spend_addresses ?? []).length >= 2 ? steps[i].co_spend_addresses : [],
    });
    const step = steps[i];
    if (!step) continue;
    cols.push({
      kind: "tx",
      header: i === 0 ? "ENTRY TX" : i === lastIdx ? "CASH-OUT TX" : steps.length === 3 ? "PEELING" : `PEEL ${i}`,
      time: `T${2 * i} · ${clock(step.timestamp)}`,
      txid: step.spending_transaction_id,
      arity: `${i === 0 ? "consolidation" : i === lastIdx ? "cash-out" : "peel"} ${step.input_count}→${step.output_count}`,
      step,
      index: i,
    });
  }
  return cols;
}

function Diamond({ cx, cy, stroke = C.diamondStroke }: { cx: number; cy: number; stroke?: string }) {
  return <polygon points={`${cx},${cy - 14} ${cx + 14},${cy} ${cx},${cy + 14} ${cx - 14},${cy}`} fill={C.diamondFill} stroke={stroke} strokeWidth={1.5} />;
}

function Hexagon({ cx, cy, stroke }: { cx: number; cy: number; stroke: string }) {
  return (
    <polygon
      points={`${cx + 26},${cy} ${cx + 13},${cy + 22.5} ${cx - 13},${cy + 22.5} ${cx - 26},${cy} ${cx - 13},${cy - 22.5} ${cx + 13},${cy - 22.5}`}
      fill={C.nodeFill}
      stroke={stroke}
      strokeWidth={1.6}
    />
  );
}

function AddressBox({ cx, cy, ring, address, sub }: { cx: number; cy: number; ring: string | null; address: string; sub: string }) {
  return (
    <>
      {ring && <rect x={cx - 63} y={cy - 21} width={126} height={42} rx={8} fill="none" stroke={ring} strokeWidth={1.8} />}
      <rect x={cx - 60} y={cy - 18} width={120} height={36} rx={6} fill={C.boxFill} stroke={C.boxStroke} strokeWidth={1.2} />
      <text x={cx} y={cy - 3} textAnchor="middle" fontSize={12} fontFamily={MONO} fill={C.ink} fontWeight={500}>{address}</text>
      <text x={cx} y={cy + 11} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.inkDim}>{sub}</text>
    </>
  );
}

/** The chosen source node. Deliberately louder than plain selection: a filled
 * halo, a solid thick ring and a badge, so it reads as "this is where I am"
 * from across the canvas rather than as one more dashed outline. */
function SourceMarker({ cx, cy, r }: { cx: number; cy: number; r: number }) {
  return (
    <g pointerEvents="none">
      <circle cx={cx} cy={cy} r={r + 26} fill={C.mustard} fillOpacity={0.1} />
      <circle cx={cx} cy={cy} r={r + 14} fill={C.mustard} fillOpacity={0.14} />
      <circle cx={cx} cy={cy} r={r + 13} fill="none" stroke={C.mustard} strokeWidth={3} />
      <rect x={cx - 36} y={cy - r - 40} width={72} height={20} rx={10} fill={C.mustard} />
      <text x={cx} y={cy - r - 26} textAnchor="middle" fontSize={11} fontWeight={700} fill="#161409" letterSpacing="0.06em">SOURCE</text>
    </g>
  );
}

function FlaggedEdge({ x1, y1, x2, y2 }: { x1: number; y1: number; x2: number; y2: number }) {
  const d = `M${x1} ${y1} L${x2} ${y2}`;
  return (
    <>
      <path d={d} fill="none" stroke={C.mustard} strokeWidth={10} opacity={0.1} strokeLinecap="round" />
      <path d={d} fill="none" stroke={C.mustard} strokeWidth={2.2} markerEnd="url(#ff-ma)" />
    </>
  );
}

export function FundFlowGraph({
  finding,
  signals,
  zoom,
  selectedNodeId,
  sourceNodeId,
  onSelectNode,
  onSetSource,
}: {
  finding: Finding;
  signals: PathSignals | null;
  zoom: number;
  selectedNodeId: string | null;
  sourceNodeId: string | null;
  onSelectNode: (info: GraphNodeInfo) => void;
  onSetSource: (nodeId: string) => void;
}) {
  const [hovered, setHovered] = useState<{ id: string; x: number; y: number } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const steps = useMemo(() => finding.steps ?? [], [finding]);

  // Column layout plus a nodeId -> x index, so the source node can be scrolled
  // to the centre of the viewport without the caller knowing the geometry.
  const { cols, nodeX } = useMemo(() => {
    const built = buildColumns(steps);
    const xs = new Map<string, number>();
    built.forEach((col, i) => {
      const cx = COL_X0 + i * COL_DX;
      if (col.kind === "tx") {
        xs.set(`tx:${col.txid}`, cx);
        col.step.peel_outputs.slice(0, 2).forEach((p, k) => xs.set(p.output_id, cx + (k === 0 ? 100 : -100)));
      } else {
        const members = col.cluster.length >= 2 ? col.cluster : [col.address ?? "unknown"];
        members.forEach((m) => xs.set(`address:${m}`, cx));
      }
    });
    return { cols: built, nodeX: xs };
  }, [steps]);

  const flagged = new Set(signals?.entity_risk_matches ?? []);
  const width = COL_X0 * 2 + Math.max(0, cols.length - 1) * COL_DX;
  const cxOf = (i: number) => COL_X0 + i * COL_DX;

  // Bring the newly-set source node to the middle of the canvas viewport.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !sourceNodeId) return;
    const x = nodeX.get(sourceNodeId);
    if (x === undefined) return;
    el.scrollTo({ left: Math.max(0, x * zoom - el.clientWidth / 2), behavior: "smooth" });
  }, [sourceNodeId, zoom, nodeX]);

  const txCols = cols.filter((c): c is TxCol => c.kind === "tx");
  const firstTx = cols.findIndex((c) => c.kind === "tx");
  const lastTx = cols.length - 1 - [...cols].reverse().findIndex((c) => c.kind === "tx");

  const ladder = [steps[0]?.previous_value_sats, ...steps.map((s) => s.continuing_value_sats)].filter(
    (v): v is number => typeof v === "number"
  );
  const dropSats = ladder.length >= 2 ? ladder[0] - ladder[ladder.length - 1] : 0;
  const dropPct = ladder.length >= 2 && ladder[0] > 0 ? (dropSats / ladder[0]) * 100 : 0;
  const carry = ladder.length >= 2 ? (ladder[ladder.length - 1] / ladder[0]) ** (1 / (ladder.length - 1)) * 100 : 0;
  const ladderX = LEGEND_W + 26;

  function txInfo(col: TxCol): GraphNodeInfo {
    const r = txRisk(col.step, flagged);
    return {
      id: `tx:${col.txid}`,
      kind: "tx",
      title: `TX-${String(col.index + 1).padStart(2, "0")} · transaction`,
      rows: [
        { k: "Txid", v: col.txid },
        { k: "Position", v: col.header },
        { k: "Shape", v: `${col.step.input_count} input(s) → ${col.step.output_count} output(s)` },
        { k: "Spends in", v: sats(col.step.previous_value_sats) },
        { k: "Carries on", v: sats(col.step.continuing_value_sats) },
        { k: "Peeled off", v: col.step.peel_output_total ? `${col.step.peel_output_total} output(s)` : "none" },
        { k: "Co-spent inputs", v: String(col.step.co_spend_input_address_count) },
        { k: "Observed at", v: stamp(col.step.timestamp) },
        { k: "Risk ring", v: r.label },
      ],
      note: r.why,
    };
  }

  function addrInfo(col: AddrCol, member: string, isChain: boolean): GraphNodeInfo {
    return {
      id: `address:${member}`,
      kind: "address",
      title: isChain ? `${col.header} · address` : "Co-spent input address",
      rows: [
        { k: "Address", v: member },
        ...(isChain
          ? [
              { k: "Value", v: sats(col.value) },
              { k: "Script", v: (col.script ?? "unknown").toUpperCase() },
              { k: "Output", v: col.outputId ?? "—" },
              { k: "Observed at", v: stamp(col.iso) },
            ]
          : [{ k: "Role", v: "spent together with this hop's input in one transaction" }]),
        { k: "Flagged elsewhere", v: flagged.has(`address:${member}`) ? "yes — another open finding names it" : "no" },
      ],
      note: isChain
        ? "An address is not a person or a wallet. This says only that an output was locked to this identifier."
        : "Common-input-ownership: these addresses were spent together in one transaction. That is a structural fact, not an identity claim.",
    };
  }

  function peelInfo(col: TxCol, peel: PeelOutput): GraphNodeInfo {
    return {
      id: peel.output_id,
      kind: "output",
      title: "Peeled output",
      rows: [
        { k: "Output", v: peel.output_id },
        { k: "Address", v: peel.address ?? "unknown" },
        { k: "Value", v: sats(peel.amount_sats) },
        { k: "Onward spend", v: peel.is_spent ? "yes — spent again in this snapshot" : "no — unspent here" },
        { k: "Peeled at", v: `TX-${String(col.index + 1).padStart(2, "0")}` },
      ],
      note: "A peel is an output of the hop transaction that is not the chain's continuation. Calling it 'change' would be an ownership inference this build does not make.",
    };
  }

  // The chip is deliberately delayed and parked clear above the node: rendered
  // last, it sits on top of everything, so an instant chip under a moving
  // pointer could swallow the click meant for the node itself.
  const hoverTimer = useRef<number | null>(null);
  const cancelTimer = () => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
  };
  const hoverable = (id: string, cx: number, cy: number) => ({
    "data-node-id": id,
    onMouseEnter: () => {
      cancelTimer();
      hoverTimer.current = window.setTimeout(() => setHovered({ id, x: cx, y: cy }), 220);
    },
    onMouseLeave: () => {
      cancelTimer();
      hoverTimer.current = window.setTimeout(() => setHovered((h) => (h?.id === id ? null : h)), 220);
    },
    style: { cursor: "pointer" as const },
  });

  return (
    <div className="ff-scroll" ref={scrollRef}>
    <svg
      viewBox={`0 0 ${width} ${PANEL_H}`}
      width={width * zoom}
      height={PANEL_H * zoom}
      className="ff-svg"
      role="img"
      aria-label="Fund flow path"
    >
      <defs>
        <marker id="ff-ma" markerUnits="userSpaceOnUse" markerWidth={12} markerHeight={12} refX={11} refY={6} orient="auto">
          <path d="M0 0L12 6L0 12z" fill={C.mustard} />
        </marker>
        <marker id="ff-mc" markerUnits="userSpaceOnUse" markerWidth={9} markerHeight={9} refX={8} refY={4.5} orient="auto">
          <path d="M0 0L9 4.5L0 9z" fill={C.observed} />
        </marker>
        <marker id="ff-mo" markerUnits="userSpaceOnUse" markerWidth={9} markerHeight={9} refX={8} refY={4.5} orient="auto">
          <path d="M0 0L9 4.5L0 9z" fill={C.suspected} />
        </marker>
        <marker id="ff-mg" markerUnits="userSpaceOnUse" markerWidth={8} markerHeight={8} refX={7} refY={4} orient="auto">
          <path d="M0 0L8 4L0 8z" fill={C.inkDim} />
        </marker>
      </defs>

      <rect x={0} y={0} width={width} height={PANEL_H} rx={12} fill={C.panel} stroke={C.panelStroke} />

      {/* Column guides, headers and per-column observation times */}
      {cols.map((col, i) => (
        <g key={`hdr-${i}`}>
          {i > 0 && <line x1={cxOf(i) - COL_DX / 2} y1={GUIDE_TOP} x2={cxOf(i) - COL_DX / 2} y2={GUIDE_BOT} stroke={C.guide} strokeWidth={1} />}
          <text x={cxOf(i)} y={HEADER_Y} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.colLabel} fontWeight={600} letterSpacing="0.08em">{col.header}</text>
          <text x={cxOf(i)} y={TIME_Y} textAnchor="middle" fontSize={12} fontFamily={SANS} fill={C.inkDim}>{col.time}</text>
        </g>
      ))}

      {/* Flagged-path banner + span bracket (left-anchored so it stays on screen
          on a long chain, where the canvas is far wider than the viewport) */}
      <rect x={20} y={BANNER_Y - 16} width={420} height={BANNER_H} rx={12} fill="#2a2110" stroke={C.mustard} strokeWidth={1} />
      <text x={230} y={BANNER_Y} textAnchor="middle" fontSize={12} fontFamily={SANS} fill={C.mustard} fontWeight={600}>
        {`PEELING CHAIN · ${sats(ladder[0])} → ${sats(ladder[ladder.length - 1])} · ${steps.length} hops`}
      </text>
      <line x1={cxOf(0)} y1={BRACKET_Y} x2={cxOf(cols.length - 1)} y2={BRACKET_Y} stroke={C.mustard} strokeWidth={1} opacity={0.45} />
      {cols.map((_, i) => (
        <line key={`tick-${i}`} x1={cxOf(i)} y1={BRACKET_Y} x2={cxOf(i)} y2={BRACKET_Y + 8} stroke={C.mustard} strokeWidth={1} opacity={0.45} />
      ))}

      {/* Highlighted flagged-path band behind the main flow row */}
      {firstTx >= 0 && (
        <rect x={cxOf(firstTx) - 82} y={ROW_Y - BAND_H / 2} width={cxOf(lastTx) + 82 - (cxOf(firstTx) - 82)} height={BAND_H} rx={BAND_H / 2} fill={C.mustard} fillOpacity={0.05} />
      )}

      {/* Co-spend cluster boxes (dashed) behind their stacked member addresses */}
      {cols.map((col, i) => {
        if (col.kind !== "address" || col.cluster.length < 2) return null;
        const n = col.cluster.length;
        const h = n * STACK_DY + 76;
        const top = ROW_Y - ((n - 1) * STACK_DY) / 2 - 21 - 62;
        return (
          <g key={`cluster-${i}`}>
            <rect x={cxOf(i) - 73} y={top} width={146} height={h} rx={10} fill={C.clusterFill} stroke={C.clusterStroke} strokeWidth={1.2} strokeDasharray="5 4" />
            <text x={cxOf(i)} y={top + 22} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.clusterLabel} fontWeight={600}>{`CLUSTER ${n} ADDR`}</text>
            <text x={cxOf(i)} y={top + 36} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.inkDim}>co-spend · inferred</text>
          </g>
        );
      })}

      {/* Main flow edges: address -> tx ("spent by"), tx -> address ("output") */}
      {cols.map((col, i) => {
        if (i === cols.length - 1) return null;
        const a = cxOf(i);
        const b = cxOf(i + 1);
        const fromEdge = col.kind === "tx" ? a + 34 : a + 62;
        const toEdge = cols[i + 1].kind === "tx" ? b - 36 : b - 64;
        const value = col.kind === "tx" ? col.step.continuing_value_sats : col.value;
        return (
          <g key={`edge-${i}`}>
            <FlaggedEdge x1={fromEdge} y1={ROW_Y} x2={toEdge} y2={ROW_Y} />
            <text x={(fromEdge + toEdge) / 2} y={ROW_Y - 10} textAnchor="middle" fontSize={12} fontFamily={SANS} fill={C.mustard} fontWeight={600}>{sats(value)}</text>
            <text x={(fromEdge + toEdge) / 2} y={ROW_Y + 17} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.inkDim}>{col.kind === "tx" ? "output" : "spent by"}</text>
          </g>
        );
      })}

      {/* Peeled sibling outputs, alternating above/below the main row */}
      {cols.map((col, i) => {
        if (col.kind !== "tx") return null;
        return col.step.peel_outputs.slice(0, 2).map((peel: PeelOutput, k: number) => {
          const up = k === 0;
          const py = up ? PEEL_UP_Y : PEEL_DOWN_Y;
          const px = cxOf(i) + (up ? 100 : -100);
          const stroke = peel.is_spent ? C.suspected : C.observed;
          const marker = peel.is_spent ? "url(#ff-mo)" : "url(#ff-mc)";
          const sy = up ? ROW_Y - 34 : ROW_Y + 34;
          const d = `M${cxOf(i)} ${sy} C${cxOf(i) + (up ? 40 : -40)} ${up ? py + 60 : py - 60} ${px - (up ? 40 : -40)} ${py} ${px - (up ? 16 : -16)} ${py}`;
          const sel = selectedNodeId === peel.output_id;
          return (
            <g key={`peel-${i}-${k}`} onClick={() => onSelectNode(peelInfo(col, peel))} {...hoverable(peel.output_id, px, py)}>
              <path d={d} fill="none" stroke={stroke} strokeWidth={1.4} markerEnd={marker} />
              {sourceNodeId === peel.output_id && <SourceMarker cx={px} cy={py} r={16} />}
              {sel && sourceNodeId !== peel.output_id && <polygon points={`${px},${py - 20} ${px + 20},${py} ${px},${py + 20} ${px - 20},${py}`} fill="none" stroke={C.mustard} strokeWidth={2} />}
              <Diamond cx={px} cy={py} />
              <text x={px} y={py - 26} textAnchor="middle" fontSize={12} fontFamily={SANS} fill={C.peelValue} fontWeight={600}>{sats(peel.amount_sats)}</text>
              <text x={px} y={py + 30} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.inkDim}>{peel.is_spent ? "peeled · spent on" : "peeled · unspent"}</text>
              <text x={px} y={py + 43} textAnchor="middle" fontSize={11} fontFamily={MONO} fill={C.inkDim}>{shortAddr(peel.address)}</text>
            </g>
          );
        });
      })}

      {/* Nodes */}
      {cols.map((col, i) => {
        const cx = cxOf(i);
        if (col.kind === "tx") {
          const risk = txRisk(col.step, flagged);
          const id = `tx:${col.txid}`;
          const sel = selectedNodeId === id;
          return (
            <g key={`node-${i}`} onClick={() => onSelectNode(txInfo(col))} {...hoverable(id, cx, ROW_Y)}>
              <title>{risk.why}</title>
              {sourceNodeId === id && <SourceMarker cx={cx} cy={ROW_Y} r={32} />}
              {sel && sourceNodeId !== id && <circle cx={cx} cy={ROW_Y} r={42} fill="none" stroke={C.mustard} strokeWidth={2} strokeDasharray="4 3" />}
              {risk.glow && <circle cx={cx} cy={ROW_Y} r={37} fill="none" stroke={risk.color} strokeWidth={1} opacity={0.35} />}
              <circle cx={cx} cy={ROW_Y} r={32} fill="none" stroke={risk.color} strokeWidth={2} />
              <circle cx={cx} cy={ROW_Y} r={26} fill={C.nodeFill} stroke={C.nodeStroke} strokeWidth={1.5} />
              <text x={cx} y={ROW_Y + 4} textAnchor="middle" fontSize={12} fontFamily={SANS} fill={C.ink} fontWeight={600}>{`TX-${String(col.index + 1).padStart(2, "0")}`}</text>
              <text x={cx} y={ROW_Y + 54} textAnchor="middle" fontSize={12} fontFamily={MONO} fill={C.ink}>{shortTx(col.txid)}</text>
              <text x={cx} y={ROW_Y + 69} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.inkDim}>{col.arity}</text>
            </g>
          );
        }
        const members = col.cluster.length >= 2 ? col.cluster : [col.address ?? "unknown"];
        const top = ROW_Y - ((members.length - 1) * STACK_DY) / 2;
        const isFlaggedAddr = flagged.has(`address:${col.address}`);
        return (
          <g key={`node-${i}`}>
            {members.map((member, k) => {
              const isChain = member === col.address;
              const id = `address:${member}`;
              const cy = top + k * STACK_DY;
              const sel = selectedNodeId === id;
              return (
                <g key={member + k} onClick={() => onSelectNode(addrInfo(col, member, isChain))} {...hoverable(id, cx, cy)}>
                  {sourceNodeId === id && <SourceMarker cx={cx} cy={cy} r={34} />}
                  {sel && sourceNodeId !== id && <rect x={cx - 69} y={cy - 27} width={138} height={54} rx={10} fill="none" stroke={C.mustard} strokeWidth={2} strokeDasharray="4 3" />}
                  <AddressBox
                    cx={cx}
                    cy={cy}
                    ring={isChain ? (isFlaggedAddr ? C.riskHigh : C.riskSuspicious) : null}
                    address={shortAddr(member)}
                    sub={isChain ? `${sats(col.value)} · ${(col.script ?? "unknown").toUpperCase()}` : "co-spent input"}
                  />
                </g>
              );
            })}
            {isFlaggedAddr && (
              <g>
                <title>This address is independently flagged by another open finding in this case</title>
                <Hexagon cx={cx} cy={top - 62} stroke={C.riskHigh} />
                <text x={cx} y={top - 58} textAnchor="middle" fontSize={12} fontFamily={SANS} fill={C.ink} fontWeight={700}>!</text>
                <text x={cx} y={top - 26} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.inkDim}>flagged elsewhere</text>
              </g>
            )}
          </g>
        );
      })}

      {/* Elapsed timeline */}
      <text x={20} y={TIMELINE_Y + 5} fontSize={11} fontFamily={SANS} fill={C.inkDim} fontWeight={600} letterSpacing="0.08em">ELAPSED</text>
      <line x1={cxOf(firstTx)} y1={TIMELINE_Y} x2={cxOf(lastTx)} y2={TIMELINE_Y} stroke={C.mustard} strokeWidth={1} opacity={0.5} />
      {txCols.map((tc, k) => {
        const i = cols.indexOf(tc);
        const prev = k > 0 ? txCols[k - 1] : null;
        const g = prev ? gapLabel(prev.step.timestamp, tc.step.timestamp) : null;
        return (
          <g key={`tl-${k}`}>
            <circle cx={cxOf(i)} cy={TIMELINE_Y} r={4.5} fill={C.panel} stroke={C.mustard} strokeWidth={1.8} />
            <text x={cxOf(i)} y={TICK_LABEL_Y} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.inkDim}>{clock(tc.step.timestamp)}</text>
            {g && <text x={cxOf(i) - COL_DX} y={TIMELINE_Y - 16} textAnchor="middle" fontSize={11} fontFamily={SANS} fill={C.colLabel}>{g}</text>}
          </g>
        );
      })}
      <text x={cxOf(lastTx) + 24} y={TIMELINE_Y + 5} fontSize={12} fontFamily={SANS} fill={C.mustard} fontWeight={600}>
        {finding.total_duration_sec ? `${Math.round(finding.total_duration_sec / 60)} min total` : "time unavailable"}
      </text>

      {/* Legend — NODES / RELATIONSHIPS / RISK RING */}
      <rect x={10} y={LEGEND_Y} width={LEGEND_W} height={LEGEND_H} rx={8} fill={C.legendFill} stroke={C.panelStroke} />
      <text x={24} y={LEGEND_Y + 18} fontSize={11} fontFamily={SANS} fill={C.inkDim} fontWeight={600} letterSpacing="0.08em">NODES</text>
      <circle cx={32} cy={LEGEND_Y + 39} r={6} fill={C.nodeFill} stroke={C.nodeStroke} strokeWidth={1.4} />
      <text x={48} y={LEGEND_Y + 43} fontSize={12} fontFamily={SANS} fill={C.ink}>Transaction</text>
      <rect x={26} y={LEGEND_Y + 58} width={12} height={12} rx={3} fill={C.boxFill} stroke={C.inkDim} strokeWidth={1.4} />
      <text x={48} y={LEGEND_Y + 68} fontSize={12} fontFamily={SANS} fill={C.ink}>Address</text>
      <polygon points={`166,${LEGEND_Y + 32} 173,${LEGEND_Y + 39} 166,${LEGEND_Y + 46} 159,${LEGEND_Y + 39}`} fill={C.diamondFill} stroke={C.inkDim} strokeWidth={1.5} />
      <text x={182} y={LEGEND_Y + 43} fontSize={12} fontFamily={SANS} fill={C.ink}>Peeled output</text>
      <g transform={`translate(0,${LEGEND_Y + 64}) scale(0.42)`}>
        <Hexagon cx={396} cy={0} stroke={C.inkDim} />
      </g>
      <text x={182} y={LEGEND_Y + 68} fontSize={12} fontFamily={SANS} fill={C.ink}>Flagged elsewhere</text>

      <line x1={300} y1={LEGEND_Y + 10} x2={300} y2={LEGEND_Y + LEGEND_H - 10} stroke={C.panelStroke} />
      <text x={316} y={LEGEND_Y + 18} fontSize={11} fontFamily={SANS} fill={C.inkDim} fontWeight={600} letterSpacing="0.08em">RELATIONSHIPS</text>
      <path d={`M316 ${LEGEND_Y + 39} H340`} stroke={C.mustard} strokeWidth={2.2} markerEnd="url(#ff-ma)" fill="none" />
      <text x={356} y={LEGEND_Y + 43} fontSize={12} fontFamily={SANS} fill={C.ink}>Flagged path</text>
      <path d={`M316 ${LEGEND_Y + 64} H340`} stroke={C.observed} strokeWidth={1.4} markerEnd="url(#ff-mc)" fill="none" />
      <text x={356} y={LEGEND_Y + 68} fontSize={12} fontFamily={SANS} fill={C.ink}>Peel · unspent</text>
      <path d={`M470 ${LEGEND_Y + 39} H494`} stroke={C.suspected} strokeWidth={1.4} markerEnd="url(#ff-mo)" fill="none" />
      <text x={510} y={LEGEND_Y + 43} fontSize={12} fontFamily={SANS} fill={C.ink}>Peel · spent on</text>
      <path d={`M470 ${LEGEND_Y + 64} H494`} stroke={C.inkDim} strokeWidth={1.4} strokeDasharray="4 3" markerEnd="url(#ff-mg)" fill="none" />
      <text x={510} y={LEGEND_Y + 68} fontSize={12} fontFamily={SANS} fill={C.ink}>Inferred · co-spend</text>

      <line x1={636} y1={LEGEND_Y + 10} x2={636} y2={LEGEND_Y + LEGEND_H - 10} stroke={C.panelStroke} />
      <text x={652} y={LEGEND_Y + 18} fontSize={11} fontFamily={SANS} fill={C.inkDim} fontWeight={600} letterSpacing="0.08em">RISK RING</text>
      {[
        { c: C.riskNeutral, t: "Neutral", x: 652, y: LEGEND_Y + 43 },
        { c: C.riskReview, t: "Review", x: 762, y: LEGEND_Y + 43 },
        { c: C.riskSuspicious, t: "Suspicious", x: 652, y: LEGEND_Y + 68 },
        { c: C.riskHigh, t: "High risk", x: 762, y: LEGEND_Y + 68 },
      ].map((r) => (
        <g key={r.t}>
          <circle cx={r.x + 7} cy={r.y - 4} r={7} fill="none" stroke={r.c} strokeWidth={2} />
          <circle cx={r.x + 7} cy={r.y - 4} r={3.5} fill={C.nodeFill} stroke={C.inkDim} />
          <text x={r.x + 22} y={r.y} fontSize={12} fontFamily={SANS} fill={C.ink}>{r.t}</text>
        </g>
      ))}

      {/* Peel ladder — the real carry-forward sequence */}
      <rect x={ladderX} y={LEGEND_Y} width={380} height={LEGEND_H} rx={8} fill={C.legendFill} stroke={C.panelStroke} />
      <text x={ladderX + 16} y={LEGEND_Y + 18} fontSize={11} fontFamily={SANS} fill={C.inkDim} fontWeight={600} letterSpacing="0.08em">PEEL LADDER · BTC</text>
      {ladder.slice(0, 4).map((v, k) => (
        <g key={`ladder-${k}`}>
          <rect x={ladderX + 16 + k * 92} y={LEGEND_Y + 28} width={68} height={28} rx={6} fill={C.boxFill} stroke={k === 0 ? C.inkDim : C.mustard} strokeWidth={1.3} />
          <text x={ladderX + 50 + k * 92} y={LEGEND_Y + 47} textAnchor="middle" fontSize={13} fontFamily={MONO} fill={C.ink}>{(v / 1e8).toFixed(2)}</text>
          {k < Math.min(ladder.length, 4) - 1 && (
            <path d={`M${ladderX + 86 + k * 92} ${LEGEND_Y + 42} H${ladderX + 102 + k * 92}`} stroke={C.inkDim} strokeWidth={1.2} markerEnd="url(#ff-mg)" fill="none" />
          )}
        </g>
      ))}
      <text x={ladderX + 16} y={LEGEND_Y + 74} fontSize={11} fontFamily={SANS} fill={C.inkDim}>
        {ladder.length >= 2
          ? `−${(dropSats / 1e8).toFixed(2)} BTC (−${dropPct.toFixed(1)}%) over ${steps.length} hops · ~${carry.toFixed(0)}% carry-forward`
          : "single-value chain — no ladder to compute"}
      </text>

      {/* Hover affordance — re-root the whole view on this node. Centred well
          clear above the node so it can never intercept the node's own click. */}
      {hovered && (
        <g
          className="ff-chip"
          transform={`translate(${hovered.x}, ${hovered.y - 64})`}
          onMouseEnter={cancelTimer}
          onMouseLeave={() => setHovered(null)}
          onClick={(e) => {
            e.stopPropagation();
            setHovered(null);
            onSetSource(hovered.id);
          }}
        >
          <rect x={-50} y={-12} width={100} height={24} rx={12} fill={C.mustard} />
          <text x={0} y={5} textAnchor="middle" fontSize={12} fontWeight={700} fill="#161409">Set as source</text>
        </g>
      )}
    </svg>
    </div>
  );
}
