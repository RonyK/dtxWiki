import type { GraphEdgeVisualKind } from "../shared/graphTokens";

/** Stable Local legend and drawing order; the Workspace Map has a different lens. */
export const LOCAL_EDGE_VISUAL_KIND_ORDER = [
  "link",
  "citation",
  "coAccess",
  "membership",
  "action",
] as const satisfies readonly GraphEdgeVisualKind[];

/**
 * Classify a visible Local edge by its canonical relationship/event kind,
 * never by layer or action metadata on a different stored relationship.
 * Action cases mirror backend ACTION_SEMANTICS: an event occurrence is not a
 * document link, citation, or membership even if it belongs to that layer.
 * Unknown kinds are omitted until their visual meaning is explicitly designed.
 */
export function localEdgeVisualKind(
  edgeKind: string | null | undefined,
): GraphEdgeVisualKind | null {
  switch (edgeKind) {
    case "wikilink":
      return "link";
    case "citation_page":
    case "citation_source":
    case "ask_citation":
      return "citation";
    case "co_access":
      return "coAccess";
    case "tree":
      return "membership";
    case "searched":
    case "returned":
    case "retrieved":
    case "included":
    case "cited":
    case "created":
    case "edited":
    case "reviewed":
    case "approved":
    case "rejected":
    case "request_changes":
    case "published":
    case "restored":
    case "superseded":
    case "merged":
    case "promoted":
    case "tool_produced":
    case "referenced":
      return "action";
    default:
      return null;
  }
}
