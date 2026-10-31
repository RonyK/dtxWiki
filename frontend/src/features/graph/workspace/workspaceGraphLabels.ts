import type { TFunction } from "i18next";

import { i18n } from "../../../shared/i18n";

export function formatCount(value: number) {
  return value.toLocaleString(i18n.resolvedLanguage || "en");
}

export function workspaceGraphEnumLabel(value: string, t: TFunction<"graph">) {
  switch (value) {
    case "context":
    case "library":
    case "folder_cluster":
    case "link_cluster":
    case "other_cluster":
    case "wikilink":
    case "citation_page":
    case "citation_source":
    case "knowledge":
    case "evidence":
    case "structure":
    case "provenance":
    case "directed":
    case "undirected":
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
