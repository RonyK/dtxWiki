export type GraphColorMode = "light" | "dark" | "forced";
/** A node's colour family: whose thing it is. */
export type GraphNodeColorKey = "context" | "library" | "agent" | "conversation" | "folder";
/** A node's legend identity: colour family plus the shape that says what it is. */
export type GraphNodeVisualKind =
  | "context" | "version" | "agent" | "agentVersion" | "library" | "conversation" | "folder";
export type GraphNodeShape = "circle" | "triangle" | "square" | "diamond" | "ring";
export type GraphEdgeVisualKind = "link" | "citation" | "coAccess" | "membership" | "action";

/**
 * The one node vocabulary for both graph views, their legends and lists.
 * Shape = what (circle: edited document, triangle: one point in a document's
 * history, square: unedited source material, diamond: chat/activity record,
 * ring: folder or grouping). Colour = whose. In forced colours an Agent keeps
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
  citation: [7, 4],
  coAccess: [2, 3.5],
  membership: [3, 3],
  action: [10, 3],
} as const satisfies Record<GraphEdgeVisualKind, readonly number[] | null>;

export const GRAPH_EDGE_WIDTH = {
  link: 2, citation: 2, coAccess: 2, membership: 1.1, action: 1.7,
} as const satisfies Record<GraphEdgeVisualKind, number>;

/** A relationship that was removed in the replayed history. */
export const GRAPH_REMOVED_EDGE_COLOR = "#dc2626";

export const GRAPH_NODE_RADIUS_PX = {
  local: { min: 3, max: 13 },
  workspace: { min: 1.5, max: 8.5 },
} as const;

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
    edge: {
      link: "#64748b",
      citation: "#0f9f8f",
      coAccess: "#0284c7",
      membership: "#8391a7",
      action: "#475569",
    },
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
    edge: {
      link: "#64748b",
      citation: "#0d9488",
      coAccess: "#38bdf8",
      membership: "#64748b",
      action: "#94a3b8",
    },
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
      edge: { link: ink, citation: ink, coAccess: ink, membership: ink, action: ink },
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
    edge: {
      link: css("--graph-edge-link", defaults.edge.link),
      citation: css("--graph-edge-citation", defaults.edge.citation),
      coAccess: css("--graph-edge-co-access", defaults.edge.coAccess),
      membership: css("--graph-edge-membership", defaults.edge.membership),
      action: css("--graph-edge-action", defaults.edge.action),
    },
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
