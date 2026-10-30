import {
  insertCompletionText,
  pickedCompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete";

import { formatContextPageMarkdown } from "../../shared/lib/contextLinks";

import type { PageSummary } from "../../shared/api/pages";
import type { LibraryItemSummary } from "../../shared/api/library";

type CompletionDataRef<T> = {
  current: T[];
};

export function createEditorCompletionSource(
  pagesRef: CompletionDataRef<PageSummary>,
  sourcesRef: CompletionDataRef<LibraryItemSummary>,
  workspaceRef: { current: string | null } = { current: null },
) {
  return (context: CompletionContext): CompletionResult | null => {
    const wikilink = context.matchBefore(/\[\[[^\]\n|]*$/);
    if (wikilink && (context.explicit || wikilink.from < wikilink.to)) {
      return {
        from: wikilink.from + 2,
        options: workspaceRef.current ? pageCompletionOptions(pagesRef.current, workspaceRef.current) : [],
        validFor: /^[^\]\n|]*$/,
      };
    }

    const citation = context.matchBefore(/@cite\{[^}\n]*$/);
    if (citation && (context.explicit || citation.from < citation.to)) {
      return {
        from: citation.from + "@cite{".length,
        options: sourceCompletionOptions(sourcesRef.current),
        validFor: /^[^}\n]*$/,
      };
    }

    return null;
  };
}

function pageCompletionOptions(pages: PageSummary[], workspaceId: string): Completion[] {
  return pages.map<Completion>((page) => ({
    apply(view, completion, from, to) {
      const link = formatContextPageMarkdown(page.title, workspaceId, page.id);
      // closeBrackets inserts these after [[. They belong to the trigger,
      // but must stay outside CodeMirror's query range while filtering.
      const end = view.state.sliceDoc(to, to + 2) === "]]" ? to + 2 : to;
      view.dispatch({
        ...insertCompletionText(view.state, link, from - 2, end),
        annotations: pickedCompletion.of(completion),
      });
    },
    displayLabel: page.title,
    detail: page.slug,
    label: `${page.title} ${page.slug}`,
    sortText: page.title,
    type: "text",
  }));
}

// Proposal C8: `source-id` inside `@cite{...}` must be a stable kebab/short
// identifier — never a UUID (UUIDs are internal FKs only). Current REST source
// summaries still expose UUID primary keys, so derive a deterministic
// URI-backed key until the backend ships a first-class citation key.
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

let warnedAboutUuidSources = false;

export function isLikelyUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
}

function sourceCompletionOptions(sources: LibraryItemSummary[]): Completion[] {
  const options: Completion[] = [];
  let derivedUuid = 0;
  for (const source of sources) {
    const citationId = citationIdForSource(source);
    if (!citationId) {
      continue;
    }
    if (citationId !== source.id && isLikelyUuid(source.id)) {
      derivedUuid += 1;
    }
    options.push({
      apply: `${citationId}}`,
      displayLabel: source.title || citationId,
      detail: citationId,
      info: source.uri,
      label: [source.title, citationId, source.uri].filter(Boolean).join(" "),
      sortText: source.title || citationId,
      type: "constant",
    });
  }
  if (derivedUuid > 0 && !warnedAboutUuidSources) {
    warnedAboutUuidSources = true;
    console.warn(
      `editorCompletions: ${derivedUuid} source(s) used derived @cite{} keys — ` +
        "their id is a UUID but proposal C8 requires kebab/short stable ids.",
    );
  }
  return options;
}

// Exported so render-time consumers (e.g. the deleted-source tombstone in
// MarkdownView) can recompute the exact `@cite{...}` key from a source's
// id/title/uri without depending on the full LibraryItemSummary shape.
export function citationKeyForSource(source: {
  id: string;
  title: string | null;
  uri: string;
}): string | null {
  if (!isLikelyUuid(source.id)) {
    return sanitizeCitationId(source.id);
  }
  const stem = slugifyCitationStem(source.title || source.uri || "source");
  const suffix = stableBase36Hash(source.uri || source.id);
  return sanitizeCitationId(`${stem}-${suffix}`);
}

function citationIdForSource(source: LibraryItemSummary): string | null {
  return citationKeyForSource(source);
}

function sanitizeCitationId(value: string): string | null {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!normalized || isLikelyUuid(normalized)) {
    return null;
  }
  return normalized.slice(0, 64);
}

function slugifyCitationStem(value: string): string {
  return sanitizeCitationId(value)?.slice(0, 40) || "source";
}

function stableBase36Hash(value: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(36).padStart(6, "0").slice(0, 6);
}
