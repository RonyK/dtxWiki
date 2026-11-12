import type { CSSProperties } from "react";

import {
  GRAPH_EDGE_ARROW, GRAPH_EDGE_PATTERN, GRAPH_EDGE_WIDTH_PX, GRAPH_NODE_VISUALS, GRAPH_REMOVED_EDGE_COLOR,
  type GraphEdgeVisualKind, type GraphNodeColorKey, type GraphNodeVisualKind,
} from "./graphTokens";
import {
  FORCED_OUTLINE_INSETS, GRAPH_RING_BAND_PX, GRAPH_RING_GAP_PX, GRAPH_RING_WIDTH_PX,
  insetOutline, outlinePathData, shapeOutline,
} from "./nodeShapes";

const NODE_COLOR_VARIABLE: Record<GraphNodeColorKey, string> = {
  context: "var(--graph-node-context, #7c3aed)",
  library: "var(--graph-node-library, #0f9f8f)",
  agent: "var(--graph-node-agent, #d97706)",
  conversation: "var(--graph-node-conversation, #0284c7)",
  folder: "var(--graph-node-folder, #64748b)",
};
const EDGE_COLOR_VARIABLE: Record<GraphEdgeVisualKind, string> = {
  link: "var(--graph-edge-link, #64748b)",
  citation: "var(--graph-edge-citation, #0f9f8f)",
  coAccess: "var(--graph-edge-co-access, #0284c7)",
  membership: "var(--graph-edge-membership, #8391a7)",
  actionWrite: "var(--graph-edge-action, #475569)",
  actionCite: "var(--graph-edge-action, #475569)",
  actionRead: "var(--graph-edge-action, #475569)",
  actionReference: "var(--graph-edge-action, #475569)",
  action: "var(--graph-edge-action, #475569)",
};
const SURFACE_VARIABLE = "var(--graph-surface, #ffffff)";

/** SVG presentation attributes cannot resolve var(); route those through style. */
function paint(property: "fill" | "stroke", color: string): { style?: CSSProperties; fill?: string; stroke?: string } {
  return color.startsWith("var(") ? { style: { [property]: color } } : { [property]: color };
}

export const GRAPH_CUE_SIZE = 16;
const CUE_SIZE = GRAPH_CUE_SIZE;
const CUE_CENTER = CUE_SIZE / 2;
// Largest extent (the diamond, √2·r) stays inside the 16px box.
const CUE_RADIUS = 5.2;
/** The current-document seed cue: a node-coloured disc in an ink ring. */
export const GRAPH_SEED_CUE = { radius: 5.5, ringWidth: 2.64 } as const;

/** A count disc's number: surface-coloured text centred on its digits' glyph box. */
export type GraphCueCount = { text: string; x: number; y: number; fontSize: number };
/** The sample number a count-disc cue shows. */
const CUE_COUNT_TEXT = "12";
const CUE_COUNT_FONT_SIZE = 6.5;
/** Digits sit on the baseline with no descender: their box centre is 0.36 em above it. */
const DIGIT_CENTRE_EM = 0.36;

/** One painted element of a node cue; `tone` picks the node ink or the surface. */
export type GraphCuePart =
  | { element: "circle"; cx: number; cy: number; r: number; strokeWidth: number; tone: "ink" }
  | { element: "path"; d: string; tone: "ink" | "surface" };

/**
 * The glyph of a node kind centred at (cx, cy): its body, and the
 * forced-colours Agent outline (a surface gap, then an inner fill) when the
 * kind has one. Legends, lists and the static diagram draw from this.
 */
export function graphNodeCueParts(kind: GraphNodeVisualKind, cx: number, cy: number): {
  body: GraphCuePart; forcedOutline: GraphCuePart[]; count?: GraphCueCount;
} {
  const visual = GRAPH_NODE_VISUALS[kind];
  // The ring's outer edge stays at CUE_RADIUS + 0.9; its band is the canvases' constant pixel band.
  const body: GraphCuePart = visual.shape === "ring"
    ? { element: "circle", cx, cy, r: CUE_RADIUS + 0.9 - GRAPH_RING_BAND_PX / 2,
      strokeWidth: GRAPH_RING_BAND_PX, tone: "ink" }
    : visual.shape === "countDisc"
      ? { element: "path", d: outlinePathData(shapeOutline("circle", cx, cy, CUE_RADIUS + 0.9)), tone: "ink" }
      : { element: "path", d: outlinePathData(shapeOutline(visual.shape, cx, cy, CUE_RADIUS)), tone: "ink" };
  const insets = visual.forcedOutline
    ? FORCED_OUTLINE_INSETS[visual.shape === "triangle" ? "triangle" : "circle"] : null;
  const count = visual.shape === "countDisc"
    ? { text: CUE_COUNT_TEXT, x: cx, y: cy + DIGIT_CENTRE_EM * CUE_COUNT_FONT_SIZE, fontSize: CUE_COUNT_FONT_SIZE }
    : undefined;
  return {
    body,
    ...count ? { count } : {},
    forcedOutline: insets ? [
      { element: "path", d: outlinePathData(insetOutline(visual.shape, cx, cy, CUE_RADIUS, insets[0])), tone: "surface" },
      { element: "path", d: outlinePathData(insetOutline(visual.shape, cx, cy, CUE_RADIUS, insets[1])), tone: "ink" },
    ] : [],
  };
}

function CuePart({ part, ink, surface }: { part: GraphCuePart; ink: string; surface: string }) {
  return part.element === "circle"
    ? <circle cx={part.cx} cy={part.cy} fill="none" r={part.r} strokeWidth={part.strokeWidth}
      vectorEffect="non-scaling-stroke" {...paint("stroke", ink)} />
    : <path d={part.d} {...paint("fill", part.tone === "ink" ? ink : surface)} />;
}

/**
 * A node's legend/list glyph, drawn with the same geometry as the canvases.
 * Without explicit colours it follows the --graph-* theme tokens.
 */
export function GraphNodeCue({
  kind, color, surface, forcedColors, ring, className, title,
}: {
  kind: GraphNodeVisualKind;
  color?: string;
  surface?: string;
  /** Known forced-colours state; when omitted, CSS reveals the Agent outline. */
  forcedColors?: boolean;
  /** Draw the emphasis ring around the glyph, e.g. a history event target. */
  ring?: string;
  className?: string;
  title?: string;
}) {
  const visual = GRAPH_NODE_VISUALS[kind];
  const ink = color ?? NODE_COLOR_VARIABLE[visual.color];
  const background = surface ?? SURFACE_VARIABLE;
  const pad = ring ? GRAPH_RING_GAP_PX + GRAPH_RING_WIDTH_PX : 0;
  const box = CUE_SIZE + 2 * pad;
  const center = CUE_CENTER + pad;
  const { body, forcedOutline, count } = graphNodeCueParts(kind, center, center);
  const outline = forcedColors !== false ? forcedOutline : [];
  return (
    <svg aria-hidden="true" className={["graph-node-cue", className].filter(Boolean).join(" ")}
      data-node-visual-kind={kind} height={box} viewBox={`0 0 ${box} ${box}`} width={box}>
      {title ? <title>{title}</title> : null}
      <CuePart ink={ink} part={body} surface={background} />
      {count ? <text fontSize={count.fontSize} fontWeight={600} textAnchor="middle" x={count.x} y={count.y}
        {...paint("fill", background)}>{count.text}</text> : null}
      {outline.length ? <g className={forcedColors ? undefined : "graph-node-cue-forced-outline"}>
        {outline.map((part, index) => <CuePart ink={ink} key={index} part={part} surface={background} />)}
      </g> : null}
      {ring ? <path d={outlinePathData(shapeOutline(visual.shape, center, center, CUE_RADIUS, GRAPH_RING_GAP_PX + GRAPH_RING_WIDTH_PX / 2))}
        fill="none" strokeWidth={GRAPH_RING_WIDTH_PX} {...paint("stroke", ring)} /> : null}
    </svg>
  );
}

/** The current-document seed: its Context glyph inside a solid ink ring. */
export function GraphSeedCue({ color, ink = "currentColor" }: { color?: string; ink?: string }) {
  const fill = color ?? NODE_COLOR_VARIABLE.context;
  return (
    <svg aria-hidden="true" className="graph-node-cue" data-node-visual-kind="seed" height={CUE_SIZE}
      viewBox={`0 0 ${CUE_SIZE} ${CUE_SIZE}`} width={CUE_SIZE}>
      <circle cx={CUE_CENTER} cy={CUE_CENTER} r={GRAPH_SEED_CUE.radius} stroke={ink}
        strokeWidth={GRAPH_SEED_CUE.ringWidth} {...paint("fill", fill)} />
    </svg>
  );
}

export type GraphEdgeCueKind = GraphEdgeVisualKind | "removed" | "other";

/** A relationship line sample: the canvas dash pattern, colour and weight. */
export function GraphEdgeCue({ kind, color, width = 34, className }: {
  kind: GraphEdgeCueKind; color?: string; width?: number; className?: string;
}) {
  const pattern = kind === "removed" ? GRAPH_EDGE_PATTERN.action
    : kind === "other" ? [2, 2] : GRAPH_EDGE_PATTERN[kind];
  const strokeWidth = GRAPH_EDGE_WIDTH_PX;
  const stroke = color ?? (kind === "removed" ? GRAPH_REMOVED_EDGE_COLOR
    : kind === "other" ? "var(--ink-muted, #475569)" : EDGE_COLOR_VARIABLE[kind]);
  const arrow = kind !== "removed" && kind !== "other" && GRAPH_EDGE_ARROW[kind];
  // The arrowhead ends the sample where the canvas line meets its target.
  const lineEnd = arrow ? width - 6 : width - 1;
  return (
    <svg aria-hidden="true" className={["graph-edge-cue", className].filter(Boolean).join(" ")}
      data-edge-arrow={arrow ? "target" : undefined}
      data-edge-visual-kind={kind} height="10" viewBox={`0 0 ${width} 10`} width={width}>
      <line strokeDasharray={pattern?.join(" ")} strokeWidth={strokeWidth} x1="1" x2={lineEnd} y1="5" y2="5"
        {...paint("stroke", stroke)} />
      {arrow ? <path d={`M${width - 7} 1.5 L${width - 0.5} 5 L${width - 7} 8.5 Z`} {...paint("fill", stroke)} /> : null}
    </svg>
  );
}
