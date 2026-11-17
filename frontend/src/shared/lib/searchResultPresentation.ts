import type { RankedSearchResult } from "../api/operations";

export function searchResultKey(result: Pick<RankedSearchResult, "type" | "id" | "version_id">): string {
  return `${result.type}:${result.id}:${result.version_id ?? ""}`;
}

export function searchResultVersionState(
  result: Pick<RankedSearchResult, "type" | "version_status" | "draft">,
): "published" | "reviewing" | "draft" | null {
  if (result.type !== "context_page" && result.type !== "context_claim") return null;
  if (result.version_status === "published_snapshot") return "published";
  if (result.version_status === "draft") return result.draft?.is_editing === false ? "reviewing" : "draft";
  return null;
}
