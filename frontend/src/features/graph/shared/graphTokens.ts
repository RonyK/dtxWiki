export type GraphColorMode = "light" | "dark" | "forced";
export type GraphNodeVisualKind = "context" | "library" | "conversation" | "folder";
export type GraphEdgeVisualKind = "link" | "citation" | "coAccess" | "membership" | "action";

export type GraphVisualTokens = {
  surface: string;
  node: Record<GraphNodeVisualKind, string>;
  edge: Record<GraphEdgeVisualKind, string>;
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

export const GRAPH_NODE_RADIUS_PX = {
  local: { min: 3, max: 13 },
  workspace: { min: 1.5, max: 8.5 },
} as const;

const DEFAULT_COLORS = {
  light: {
    surface: "#ffffff",
    node: {
      context: "#7c3aed",
      library: "#0f9f8f",
      conversation: "#ea580c",
      folder: "#64748b",
    },
    edge: {
      link: "#64748b",
      citation: "#0f9f8f",
      coAccess: "#ea580c",
      membership: "#94a3b8",
      action: "#7c3aed",
    },
  },
  dark: {
    surface: "#0f172a",
    node: {
      context: "#8b5cf6",
      library: "#0d9488",
      conversation: "#ea580c",
      folder: "#94a3b8",
    },
    edge: {
      link: "#64748b",
      citation: "#0d9488",
      coAccess: "#ea580c",
      membership: "#64748b",
      action: "#a78bfa",
    },
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
      node: { context: ink, library: ink, conversation: ink, folder: ink },
      edge: { link: ink, citation: ink, coAccess: ink, membership: ink, action: ink },
    };
  }

  const defaults = DEFAULT_COLORS[mode];
  return {
    surface: css("--graph-surface", defaults.surface),
    node: {
      context: css("--graph-node-context", defaults.node.context),
      library: css("--graph-node-library", defaults.node.library),
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
  };
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
