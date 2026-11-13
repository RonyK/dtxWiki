export type GraphColorMode = "light" | "dark" | "forced";
/** A node's colour family: whose thing it is. */
export type GraphNodeColorKey = "context" | "library" | "agent" | "conversation" | "folder";
/** A node's legend identity: colour family plus the shape that says what it is. */
export type GraphNodeVisualKind =
  | "context" | "version" | "agent" | "agentVersion" | "library" | "conversation" | "folder" | "cluster";
export type GraphNodeShape = "circle" | "triangle" | "square" | "diamond" | "ring" | "countDisc";
export type GraphEdgeVisualKind =
  | "link" | "citation" | "coAccess" | "membership"
  | "actionWrite" | "actionCite" | "actionRead" | "actionReference" | "action";

/**
 * Activity lines: what a chat, a person or an event did to a document. They
 * share one colour; the stroke says the verb (solid write, dashed cite, long
 * dash read, dots structure-only look). Each activity points at its target;
 * `action` is every other recorded event.
 */
export const GRAPH_ACTION_EDGE_KINDS = [
  "actionWrite", "actionCite", "actionRead", "actionReference", "action",
] as const satisfies readonly GraphEdgeVisualKind[];

export function isActionEdgeVisualKind(kind: GraphEdgeVisualKind | null | undefined): boolean {
  return (GRAPH_ACTION_EDGE_KINDS as readonly string[]).includes(kind ?? "");
}

export type GraphEdgeEndpointSource = "none" | "open-diamond";
export type GraphEdgeEndpointTarget = "none" | "open-arrow" | "filled-triangle";

/** Endpoint meaning shared by canvas lines, legends and relation-list cues. */
export const GRAPH_EDGE_ENDPOINTS = {
  link: { source: "none", target: "open-arrow" },
  citation: { source: "none", target: "open-arrow" },
  coAccess: { source: "none", target: "none" },
  membership: { source: "open-diamond", target: "open-arrow" },
  actionWrite: { source: "none", target: "open-arrow" },
  actionCite: { source: "none", target: "open-arrow" },
  actionRead: { source: "none", target: "open-arrow" },
  actionReference: { source: "none", target: "open-arrow" },
  action: { source: "none", target: "filled-triangle" },
} as const satisfies Record<GraphEdgeVisualKind, { source: GraphEdgeEndpointSource; target: GraphEdgeEndpointTarget }>;

/**
 * Endpoint sizes and the gap from the node's visible boundary, in CSS pixels.
 * A line too short for its markers scales them down to keep `markerShaft` of
 * line between them.
 */
export const GRAPH_EDGE_ENDPOINT_SIZE_PX = {
  arrowLength: 6,
  arrowHalfWidth: 4,
  diamondLength: 10,
  diamondHalfWidth: 4,
  nodeGap: 1.5,
  markerShaft: 2,
} as const;

/** Compact filled target head for a generic activity, in CSS pixels. */
export const GRAPH_EDGE_TRIANGLE_SIZE_PX = { length: 4, halfWidth: 2.5 } as const;

/** Compatibility view for callers that only need target-arrow presence. */
export const GRAPH_EDGE_ARROW = Object.fromEntries(Object.entries(GRAPH_EDGE_ENDPOINTS)
  .map(([kind, endpoints]) => [kind, endpoints.target !== "none"])) as Readonly<Record<GraphEdgeVisualKind, boolean>>;

/**
 * The one node vocabulary for both graph views, their legends and lists.
 * Shape = what (circle: edited document, triangle: one point in a document's
 * history, square: unedited source material, diamond: chat/activity record,
 * ring: folder, count disc: an aggregate of documents numbered by how many it
 * holds). Colour = whose. In forced colours an Agent keeps
 * its shape and gains a thin inner outline, the only cue left there.
 */
export const GRAPH_NODE_VISUALS = {
  context: { color: "context", shape: "circle", forcedOutline: false },
  version: { color: "context", shape: "triangle", forcedOutline: false },
  agent: { color: "agent", shape: "circle", forcedOutline: true },
  agentVersion: { color: "agent", shape: "triangle", forcedOutline: true },
  library: { color: "library", shape: "square", forcedOutline: false },
  conversation: { color: "conversation", shape: "diamond", forcedOutline: false },
  folder: { color: "folder", shape: "ring", forcedOutline: false },
  cluster: { color: "folder", shape: "countDisc", forcedOutline: false },
} as const satisfies Record<GraphNodeVisualKind, {
  color: GraphNodeColorKey; shape: GraphNodeShape; forcedOutline: boolean;
}>;

export type GraphVisualTokens = {
  surface: string;
  node: Record<GraphNodeColorKey, string>;
  edge: Record<GraphEdgeVisualKind, string>;
  /** History event emphasis: the event's ring and its relationship stroke. */
  highlight: string;
  /** Selection/focus ring around the active node. */
  selection: string;
  /** Text and labels on the graph surface. */
  ink: string;
};

// Dash lengths are screen pixels, independent of graph zoom. A null dash
// means a solid stroke. Shape and dash remain the non-color cues in forced
// colors mode.
export const GRAPH_EDGE_PATTERN = {
  link: null,
  citation: [8, 3, 1.5, 3],
  coAccess: [5, 3.5],
  membership: [10, 4],
  actionWrite: null,
  actionCite: [6, 3],
  actionRead: [10, 5],
  actionReference: [1.5, 3.5],
  action: [1.5, 3.5],
} as const satisfies Record<GraphEdgeVisualKind, readonly number[] | null>;

/** Every relationship line, in both graphs and their legends, is this wide (CSS px). */
export const GRAPH_EDGE_WIDTH_PX = 1.5;

/**
 * Relationship-line opacity: the Map's rules, shared with the Context graph,
 * in one table so the range is tuned in one place (now 0.7 down to 0.4).
 * - Ink by kind: `citation`, and `line` for every other kind.
 * - Long lines fade by their length in the fitted view: full ink up to the near
 *   length, towards transparent at the far one, never below the scene's floor;
 *   scenes up to `small.maxRelations` relations fade less.
 * - While a selection is shown no line fades, ink is multiplied by `emphasis`,
 *   and lines outside it keep `dimmed` of theirs. Forced colours: ink, no fade.
 * - Map only: its overview of over 1,000 documents inks by community
 *   (`overview`); a scene beyond its budget keeps its own tier (`unadmitted`).
 */
export const GRAPH_LINE_OPACITY = {
  line: 0.7,
  citation: 0.68,
  emphasis: 1.235,
  dimmed: 0.1,
  fade: {
    small: { maxRelations: 1_000, rangePx: [40, 350], floor: 0.88 },
    large: { rangePx: [30, 220], floor: 0.74 },
  },
  overview: {
    line: 0.65, citation: 0.67, crossLine: 0.54, crossCitation: 0.56,
    selected: 0.87, selectedFloor: 0.97, dimmed: 0.08,
  },
  unadmitted: { line: 0.72, citation: 0.65, rangePx: [10, 60], floor: 0.62 },
} as const;

/** The long-line fade for a line of this fitted-view length in a scene of this many relations. */
export function graphLineFade(fitLengthPx: number, relations: number): number {
  const { small, large } = GRAPH_LINE_OPACITY.fade;
  const { rangePx: [near, far], floor } = relations <= small.maxRelations ? small : large;
  return Math.max(floor, Math.min(1, (far - fitLengthPx) / (far - near)));
}

/** A relationship that was removed in the replayed history. */
export const GRAPH_REMOVED_EDGE_COLOR = "#dc2626";
export const GRAPH_REMOVED_EDGE_PATTERN = [10, 3] as const;

/**
 * The line table both graphs draw from, one row per line kind in this order.
 * A row's pattern and endpoints are the tokens above (a removed line has
 * no endpoints); the native renderer is generated from this table.
 */
export const GRAPH_LINE_STYLE_ROWS = [
  "link", "citation", "membership", "coAccess", "actionWrite",
  "actionCite", "actionRead", "actionReference", "action", "removed",
] as const satisfies readonly (GraphEdgeVisualKind | "removed")[];
export type GraphLineStyleRow = typeof GRAPH_LINE_STYLE_ROWS[number];

/** A line's style value: its row, past the table when it is emphasized. */
export function graphLineStyle(row: GraphLineStyleRow, emphasized = false): number {
  return GRAPH_LINE_STYLE_ROWS.indexOf(row) + (emphasized ? GRAPH_LINE_STYLE_ROWS.length : 0);
}

export const GRAPH_NODE_RADIUS_PX = {
  local: { min: 3, max: 13 },
  workspace: { min: 1.5, max: 8.5 },
} as const;

type BaseEdgeColors = Record<Exclude<GraphEdgeVisualKind, (typeof GRAPH_ACTION_EDGE_KINDS)[number]> | "action", string>;

/** Every activity line takes the one action colour. */
export function withActionEdgeColors(edge: BaseEdgeColors): Record<GraphEdgeVisualKind, string> {
  return {
    ...edge,
    actionWrite: edge.action,
    actionCite: edge.action,
    actionRead: edge.action,
    actionReference: edge.action,
  };
}

// Fallbacks only: styles/03-editor.css defines the same --graph-* values for
// the light, dark and forced-colours themes on :root.
const DEFAULT_COLORS = {
  light: {
    surface: "#ffffff",
    node: {
      context: "#7c3aed",
      library: "#0f9f8f",
      agent: "#d97706",
      conversation: "#0284c7",
      folder: "#64748b",
    },
    edge: withActionEdgeColors({
      link: "#64748b",
      citation: "#0f9f8f",
      coAccess: "#0284c7",
      membership: "#8391a7",
      action: "#94a3b8",
    }),
    highlight: "#7c3aed",
    selection: "#0f172a",
    ink: "#0f172a",
  },
  dark: {
    surface: "#0f172a",
    node: {
      context: "#8b5cf6",
      library: "#0d9488",
      agent: "#f59e0b",
      conversation: "#38bdf8",
      folder: "#94a3b8",
    },
    edge: withActionEdgeColors({
      link: "#64748b",
      citation: "#0d9488",
      coAccess: "#38bdf8",
      membership: "#64748b",
      action: "#cbd5e1",
    }),
    highlight: "#a78bfa",
    selection: "#f8fafc",
    ink: "#e5e7eb",
  },
} as const satisfies Record<"light" | "dark", GraphVisualTokens>;

/** Read optional graph CSS tokens without requiring a CSS theme to be loaded. */
export function readGraphTokens(
  element: Element,
  mode: GraphColorMode = "light",
): GraphVisualTokens {
  const style = getComputedStyle(element);
  const css = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback;

  if (mode === "forced") {
    const ink = style.color || "#000000";
    const surface = opaqueAncestorBackground(element)
      ?? (isLightInk(ink) ? "#000000" : "#ffffff");
    return {
      surface,
      node: { context: ink, library: ink, agent: ink, conversation: ink, folder: ink },
      edge: withActionEdgeColors({ link: ink, citation: ink, coAccess: ink, membership: ink, action: ink }),
      highlight: ink,
      // Selection uses the system highlight so it never reads as an Agent outline.
      selection: systemColor(element, "Highlight") ?? ink,
      ink,
    };
  }

  const defaults = DEFAULT_COLORS[mode];
  return {
    surface: css("--graph-surface", defaults.surface),
    node: {
      context: css("--graph-node-context", defaults.node.context),
      library: css("--graph-node-library", defaults.node.library),
      agent: css("--graph-node-agent", defaults.node.agent),
      conversation: css("--graph-node-conversation", defaults.node.conversation),
      folder: css("--graph-node-folder", defaults.node.folder),
    },
    edge: withActionEdgeColors({
      link: css("--graph-edge-link", defaults.edge.link),
      citation: css("--graph-edge-citation", defaults.edge.citation),
      coAccess: css("--graph-edge-co-access", defaults.edge.coAccess),
      membership: css("--graph-edge-membership", defaults.edge.membership),
      action: css("--graph-edge-action", defaults.edge.action),
    }),
    highlight: css("--graph-highlight", defaults.highlight),
    selection: css("--graph-selection", defaults.selection),
    ink: css("--graph-ink", defaults.ink),
  };
}

function systemColor(element: Element, keyword: string): string | null {
  const probe = element.ownerDocument.createElement("span");
  probe.style.color = keyword;
  probe.style.display = "none";
  element.appendChild(probe);
  const color = getComputedStyle(probe).color.trim();
  probe.remove();
  return color || null;
}

function opaqueAncestorBackground(element: Element): string | null {
  for (let current: Element | null = element; current; current = current.parentElement) {
    const color = getComputedStyle(current).backgroundColor.trim();
    if (color && color !== "transparent" && !/^rgba\([^)]*,\s*0(?:\.0+)?\)$/.test(color)) {
      return color;
    }
  }
  return null;
}

function isLightInk(color: string): boolean {
  const rgb = color.match(/^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
  if (rgb) {
    const [, red, green, blue] = rgb.map(Number);
    return red * 0.2126 + green * 0.7152 + blue * 0.0722 > 128;
  }
  const hex = color.match(/^#([\da-f]{6})$/i);
  if (!hex) return false;
  const red = Number.parseInt(hex[1].slice(0, 2), 16);
  const green = Number.parseInt(hex[1].slice(2, 4), 16);
  const blue = Number.parseInt(hex[1].slice(4, 6), 16);
  return red * 0.2126 + green * 0.7152 + blue * 0.0722 > 128;
}
