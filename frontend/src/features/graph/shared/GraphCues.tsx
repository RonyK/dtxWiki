import type { CSSProperties } from "react";

import {
  GRAPH_EDGE_PATTERN, GRAPH_EDGE_WIDTH, GRAPH_NODE_VISUALS, GRAPH_REMOVED_EDGE_COLOR,
  type GraphEdgeVisualKind, type GraphNodeColorKey, type GraphNodeVisualKind,
} from "./graphTokens";
import {
  FORCED_OUTLINE_INSETS, GRAPH_RING_GAP_PX, GRAPH_RING_WIDTH_PX, RING_BAND_FACTOR,
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
  action: "var(--graph-edge-action, #475569)",
};
const SURFACE_VARIABLE = "var(--graph-surface, #ffffff)";

/** SVG presentation attributes cannot resolve var(); route those through style. */
function paint(property: "fill" | "stroke", color: string): { style?: CSSProperties; fill?: string; stroke?: string } {
  return color.startsWith("var(") ? { style: { [property]: color } } : { [property]: color };
}

const CUE_SIZE = 16;
const CUE_CENTER = CUE_SIZE / 2;
// Largest extent (the diamond, √2·r) stays inside the 16px box.
const CUE_RADIUS = 5.2;

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
  const body = visual.shape === "ring"
    ? <circle cx={center} cy={center} fill="none" r={CUE_RADIUS * (1 - RING_BAND_FACTOR / 2) + 0.4}
      strokeWidth={CUE_RADIUS * RING_BAND_FACTOR + 1} {...paint("stroke", ink)} />
    : <path d={outlinePathData(shapeOutline(visual.shape, center, center, CUE_RADIUS))} {...paint("fill", ink)} />;
  const insets = visual.forcedOutline && forcedColors !== false
    ? FORCED_OUTLINE_INSETS[visual.shape === "triangle" ? "triangle" : "circle"] : null;
  return (
    <svg aria-hidden="true" className={["graph-node-cue", className].filter(Boolean).join(" ")}
      data-node-visual-kind={kind} height={box} viewBox={`0 0 ${box} ${box}`} width={box}>
      {title ? <title>{title}</title> : null}
      {body}
      {insets ? <g className={forcedColors ? undefined : "graph-node-cue-forced-outline"}>
        <path d={outlinePathData(insetOutline(visual.shape, center, center, CUE_RADIUS, insets[0]))} {...paint("fill", background)} />
        <path d={outlinePathData(insetOutline(visual.shape, center, center, CUE_RADIUS, insets[1]))} {...paint("fill", ink)} />
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
      <circle cx={CUE_CENTER} cy={CUE_CENTER} r="5.5" stroke={ink} strokeWidth="2.64" {...paint("fill", fill)} />
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
  const strokeWidth = kind === "removed" ? GRAPH_EDGE_WIDTH.action
    : kind === "other" ? 1.5 : Math.max(1.5, GRAPH_EDGE_WIDTH[kind]);
  const stroke = color ?? (kind === "removed" ? GRAPH_REMOVED_EDGE_COLOR
    : kind === "other" ? "var(--ink-muted, #475569)" : EDGE_COLOR_VARIABLE[kind]);
  return (
    <svg aria-hidden="true" className={["graph-edge-cue", className].filter(Boolean).join(" ")}
      data-edge-visual-kind={kind} height="10" viewBox={`0 0 ${width} 10`} width={width}>
      <line strokeDasharray={pattern?.join(" ")} strokeWidth={strokeWidth} x1="1" x2={width - 1} y1="5" y2="5"
        {...paint("stroke", stroke)} />
    </svg>
  );
}
