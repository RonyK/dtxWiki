import type { TFunction } from "i18next";

import { i18n } from "../i18n";

// Initialisms that must keep their canonical casing instead of being
// title-cased (which would mangle "OAuth" -> "Oauth", "MCP" -> "Mcp").
const ACRONYMS: Record<string, string> = {
  oauth: "OAuth",
  mcp: "MCP",
  url: "URL",
  api: "API",
  id: "ID",
  pdf: "PDF",
  json: "JSON",
  yaml: "YAML",
  csv: "CSV",
  html: "HTML",
};

/**
 * Humanize a snake_case / space-separated enum value into "Sentence case",
 * preserving known acronyms. Only the first word is capitalized (sentence
 * case); subsequent non-acronym words stay lowercase ("oauth_scope" ->
 * "OAuth scope", "manual_upload" -> "Manual upload").
 */
export function humanizeEnum(value: string): string {
  const words = value.split(/[_\s]+/).filter(Boolean);
  if (words.length === 0) {
    return value;
  }
  return words
    .map((word, index) => {
      const acronym = ACRONYMS[word.toLowerCase()];
      if (acronym) {
        return acronym;
      }
      if (index === 0) {
        return word.charAt(0).toUpperCase() + word.slice(1);
      }
      return word;
    })
    .join(" ");
}

/**
 * Translation-aware enum/status/type -> label resolver. Looks up
 * `labels.<group>.<value>` in the given i18n namespace and falls back to an
 * acronym-aware humanized form when no locale key exists.
 *
 * Generalizes the per-screen `enumLabel`/`labelFor` helpers so Library,
 * Context, Review, Ask, Search, etc. share one casing rule and one place to
 * translate.
 *
 * @param namespace i18n resource ("agent" | "context" | "library" | "review" | "search" | ...)
 * @param group     the `labels.<group>` sub-map ("category", "pageType", "reviewType", ...)
 * @param value     the raw enum value ("manual_upload", "extracted_text", ...)
 * @param t         optional bound TFunction; defaults to the namespace-fixed translator
 */
export function domainLabel(
  namespace: string,
  group: string,
  value: string | null | undefined,
  t: TFunction = i18n.getFixedT(null, namespace),
): string {
  if (!value) {
    return t("labels.unset", { defaultValue: "unset", ns: namespace });
  }
  return t(`labels.${group}.${value}`, {
    ns: namespace,
    defaultValue: humanizeEnum(value),
  });
}
