import type { GraphNodeVisualKind } from "../shared/graphTokens";

/** One encoding source for actual API kinds, the renderer and its legend. */
export function localNodeVisualKind(entityKind: string, family: string): GraphNodeVisualKind {
  if (entityKind === "context_page" || entityKind === "context_page_version") return "context";
  if (["library_item", "library_item_version", "library_artifact"].includes(entityKind)) return "library";
  if (family === "structural") return "folder";
  return "conversation";
}
