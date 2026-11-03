import type { RankedSearchResult } from "../../shared/api/operations";
import { displayConceptPath } from "../../shared/lib/conceptPath";

// Shared by Search and the reference picker so a result reads the same way.

export function normalizedText(value: string) {
  return value.normalize("NFKC").toLocaleLowerCase().trim().replace(/\s+/g, " ");
}

export function resultLocation(result: RankedSearchResult): string[] {
  if (result.location?.length) return result.location;
  if (!result.concept_path) return [];
  const path = displayConceptPath(result.concept_path);
  // Generated Context slugs are storage identifiers. Human slugs remain useful
  // as a fallback when the server has no tree placement for the result.
  if ((result.type === "context_page" || result.type === "context_claim") &&
    /^\/?context\/p-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(path)) return [];
  return [path];
}

export function presentLocation(location: string[]) {
  const shorten = (part: string) => {
    const characters = Array.from(part);
    return characters.length > 22 ? `${characters.slice(0, 21).join("")}…` : part;
  };
  return location.flatMap((part, index) => {
    const stage = {
      text: shorten(part),
      desktop: location.length < 4 || index === 0 || index >= location.length - 2,
      mobile: location.length < 3 || index === 0 || index === location.length - 1,
    };
    return index === 1 && location.length >= 3
      ? [{ text: "…", desktop: location.length >= 4, mobile: true }, stage]
      : [stage];
  });
}

// Search previews are plain text. Keep labels/content while removing Markdown
// presentation syntax; never change the stored source or render result HTML.
export function searchPreview(value: string): string {
  const code: string[] = [];
  return value
    .replace(/```[^\n]*\n([\s\S]*?)```|~~~[^\n]*\n([\s\S]*?)~~~|`([^`\n]+)`/g,
      (_match, fenced: string | undefined, tilde: string | undefined, inline: string | undefined) => {
        code.push(fenced ?? tilde ?? inline ?? "");
        return `\uE000${code.length - 1}\uE001`;
      })
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/<((?:https?:\/\/|mailto:)[^>]+)>/g, "$1")
    .replace(/<\/?(?:a|abbr|b|blockquote|br|code|del|div|em|h[1-6]|hr|i|li|ol|p|pre|s|small|span|strong|sub|sup|table|tbody|td|th|thead|tr|ul)(?:\s[^<>]*?)?\s*\/?>/gi, "")
    .replace(/^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)/gm, "")
    .replace(/\*\*(\S(?:[^*]*\S)?)\*\*/g, "$1")
    .replace(/\*(\S(?:[^*]*\S)?)\*/g, "$1")
    .replace(/(^|[^\p{L}\p{N}_])__([^_\n]+)__(?=$|[^\p{L}\p{N}_])/gu, "$1$2")
    .replace(/(^|[^\p{L}\p{N}_])_([^_\n]+)_(?=$|[^\p{L}\p{N}_])/gu, "$1$2")
    .replace(/`+/g, "")
    .replace(/\uE000(\d+)\uE001/g, (_match, index: string) => code[Number(index)] ?? "")
    .replace(/\s+/g, " ").trim();
}
