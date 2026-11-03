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
import type { EditorView } from "@codemirror/view";

type CompletionDataRef<T> = {
  current: T[];
};

export function createEditorCompletionSource(
  pagesRef: CompletionDataRef<PageSummary>,
  sourcesRef: CompletionDataRef<LibraryItemSummary>,
  workspaceRef: { current: string | null } = { current: null },
  citationSearch?: { label: string; open: (view: EditorView, from: number, to: number, query?: string) => void },
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

    const inlineCitation = context.matchBefore(/@cite\{[^}\n]*$/);
    const slashCitation = context.matchBefore(/\/cite$/);
    const slashBoundary = slashCitation && (slashCitation.from === 0 || /\s/.test(context.state.sliceDoc(slashCitation.from - 1, slashCitation.from)));
    const citation = inlineCitation ?? (slashBoundary ? slashCitation : null);
    if (citation && (context.explicit || citation.from < citation.to)) {
      const tokenPrefix = citation.text.startsWith("@cite{") ? "@cite{" : "/cite";
      const options: Completion[] = workspaceRef.current ? [
        ...pagesRef.current.map((page) => citationCompletion(`/p/${workspaceRef.current}/${page.id}`, page.title, "Context")),
        ...sourcesRef.current.map((source) => citationCompletion(`/workspaces/${workspaceRef.current}/library/items/${source.id}`, source.title || source.uri, "Library")),
      ] : [];
      const query = citation.text.slice(tokenPrefix.length);
      if (citationSearch) options.unshift({
        label: query ? `${citationSearch.label} ${query}` : citationSearch.label,
        displayLabel: citationSearch.label,
        boost: 99,
        apply(view, _completion, _from, to) {
          const end = view.state.sliceDoc(to, to + 1) === "}" ? to + 1 : to;
          citationSearch.open(view, citation.from, end, query);
        },
      });
      return {
        from: citation.from + tokenPrefix.length,
        options: tokenPrefix === "/cite" ? options.filter((item) => item.label === citationSearch?.label) : options,
        validFor: /^[^}\n]*$/,
      };
    }

    return null;
  };
}

function citationCompletion(href: string, title: string, detail: string): Completion {
  return {
    label: title, detail,
    apply(view, completion, from, to) {
      const end = view.state.sliceDoc(to, to + 1) === "}" ? to + 1 : to;
      view.dispatch({ ...insertCompletionText(view.state, `${href}}`, from, end), annotations: pickedCompletion.of(completion) });
    },
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

// Read-only compatibility for old deleted-source tombstones. New citations
// always use canonical document links above; no new slug/hash keys are emitted.
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isLikelyUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
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
