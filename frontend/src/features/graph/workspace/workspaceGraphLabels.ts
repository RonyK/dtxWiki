import type { TFunction } from "i18next";

import type { WorkspaceGraphNode } from "../../../shared/api/workspaceGraph";
import { i18n } from "../../../shared/i18n";
import { domainLabel } from "../../../shared/lib/domainLabel";

export function formatCount(value: number) {
  return value.toLocaleString(i18n.resolvedLanguage || "en");
}

/** The enum value naming what a node is: its document, cluster or folder kind. */
export function workspaceGraphNodeKind(node: WorkspaceGraphNode): string {
  return node.grain === "document" ? node.entity_kind : node.grain === "cluster" ? node.cluster_kind : node.grain;
}

/** The folder holding a folder: its path without its own name. */
function parentPath(path: string, label: string): string | null {
  const suffix = ` / ${label}`;
  return path.endsWith(suffix) ? path.slice(0, -suffix.length) || null : null;
}

/** Where a node sits: a document's primary folder, a folder's parent path, nothing for a cluster. */
export function workspaceGraphNodeLocation(node: WorkspaceGraphNode): string | null {
  return node.grain === "document" ? node.primary_folder_label : node.grain === "folder" ? parentPath(node.path, node.label) : null;
}

/** Map document kinds in the shared registry: the legend's full node names. */
const NODE_LEGEND_KEY = { context: "context", library: "library", agent: "agent", folder: "folder" } as const;
const SHARED_EDGE_KINDS = new Set(["wikilink", "tree", "citation_page", "citation_source", "citation_agent", "ask_citation"]);
const SHARED_LAYERS = new Set(["knowledge", "evidence", "structure", "provenance"]);

/**
 * One name per value for both graphs: node and relation kinds, layers and
 * directions come from the shared registry; only the Map's own scene enums
 * (cluster kinds, representation, completeness) keep Map keys.
 */
export function workspaceGraphEnumLabel(value: string, t: TFunction<"graph">) {
  if (value in NODE_LEGEND_KEY) return t(`graph.legend.node.${NODE_LEGEND_KEY[value as keyof typeof NODE_LEGEND_KEY]}`);
  if (SHARED_EDGE_KINDS.has(value)) return domainLabel("graph", "graphEdgeKind", value, t);
  if (SHARED_LAYERS.has(value)) return domainLabel("graph", "graphLayer", value, t);
  if (value === "directed" || value === "undirected") return domainLabel("graph", "graphDirection", value, t);
  switch (value) {
    case "folder_cluster":
    case "link_cluster":
    case "other_cluster":
    case "exact":
    case "aggregated":
    case "grouped":
    case "complete":
    case "capped":
    case "partial":
    case "privacy_suppressed":
      return t(`workspaceGraph.enums.${value}`);
    default:
      return t("workspaceGraph.enums.unknown");
  }
}
