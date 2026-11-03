import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkMath from "remark-math";
import type { Root, RootContent, Text } from "mdast";
import { parsePageDocument } from "./pageFrontmatter";

import type { DocumentReferenceKind, DocumentReferenceTarget, ReferenceView } from "../../shared/api/references";
import { classifyContextLink, formatContextPageUrl, isContextId } from "../../shared/lib/contextLinks";

export function referenceKey(ref: { target_kind: DocumentReferenceKind; target_id?: string | null; metadata_id?: string | null; number?: number }): string {
  return `${ref.target_kind}:${ref.target_id ?? ref.metadata_id ?? `unavailable-${ref.number}`}`;
}

export function renderedReferenceOccurrences(references: ReferenceView[], body: string, renderedBody: string, offsets: "codepoint" | "utf16"): ReferenceView[] {
  if (!references.some((reference) => reference.occurrences?.length)) return references;
  const wanted = new Set(references.flatMap((reference) => (reference.occurrences ?? []).flatMap((item) => [item.start, item.end])));
  const positions = new Map<number, { raw: number; rendered: number }>();
  const normalized = body.replace(/\r\n?/g, "\n");
  const prefix = normalized.endsWith(renderedBody) ? normalized.length - renderedBody.length : 0;
  let raw = 0, codepoints = 0, rendered = 0, previous = "";
  for (const character of body) {
    const index = offsets === "codepoint" ? codepoints : raw;
    if (wanted.has(index)) positions.set(index, { raw, rendered: rendered - prefix });
    if (character !== "\n" || previous !== "\r") rendered += character.length;
    raw += character.length;
    codepoints += 1;
    previous = character;
  }
  positions.set(offsets === "codepoint" ? codepoints : raw, { raw, rendered: rendered - prefix });
  return references.map((reference) => ({ ...reference, occurrences: (reference.occurrences ?? []).flatMap((item) => {
    const start = positions.get(item.start), end = positions.get(item.end);
    if (!start || !end || start.rendered < 0 || end.rendered > renderedBody.length) return [];
    const authored = /^@cite\{([^\s{}]+)\}$/.exec(body.slice(start.raw, end.raw));
    return authored ? [{ href: authored[1], start: start.rendered, end: end.rendered }] : [];
  }) }));
}

export function referenceHref(workspaceId: string, ref: Pick<ReferenceView, "target_kind" | "target_id">): string {
  if (!ref.target_id) return "";
  if (ref.target_kind === "context_page") return formatContextPageUrl(workspaceId, ref.target_id);
  return `/workspaces/${workspaceId}/${ref.target_kind === "library_item" ? "library/items" : "agent/pages"}/${ref.target_id}`;
}

export function referenceTarget(href: string, workspaceId: string): DocumentReferenceTarget | null {
  const exact = /^dtxwiki:\/\/index\/(context|library|agent)\/([0-9a-f-]+)$/.exec(href);
  if (exact && isContextId(exact[2])) return {
    target_kind: exact[1] === "context" ? "context_page" : exact[1] === "library" ? "library_item" : "agent_page",
    target_id: exact[2],
  };
  const context = classifyContextLink(href, { workspaceId, allowWorkspaceNavigation: true });
  if (context.pageId && (context.workspaceId ?? workspaceId) === workspaceId) {
    return { target_kind: "context_page", target_id: context.pageId };
  }
  const match = /^\/workspaces\/([^/]+)\/(library\/items|agent\/pages)\/([^/#?]+)(?:#[^\s{}]*)?$/.exec(context.stableUrl ?? href);
  if (!match || match[1].toLowerCase() !== workspaceId.toLowerCase() || !isContextId(match[3])) return null;
  return { target_kind: match[2] === "library/items" ? "library_item" : "agent_page", target_id: match[3].toLowerCase() };
}

function citationMarkdown(body: string) {
  const parsed = parsePageDocument(body).body;
  let prefix = 0;
  if (parsed !== body) {
    const normalized = body.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const normalizedPrefix = normalized.length - parsed.length;
    let consumed = 0;
    while (consumed < normalizedPrefix && prefix < body.length) {
      prefix += body[prefix] === "\r" && body[prefix + 1] === "\n" ? 2 : 1;
      consumed += 1;
    }
  }
  const parseBody = body.slice(0, prefix).replace(/[^\r\n]/g, " ") + body.slice(prefix);
  const root = unified().use(remarkParse).use(remarkMath).parse(parseBody) as Root;
  return root;
}

function citationExclusions(body: string, root: Root) {
  const opaque = new Uint8Array(body.length);
  const excluded = new Uint8Array(body.length);
  function mark(node: Root | RootContent) {
    if (["code", "inlineCode", "math", "inlineMath", "image", "link", "linkReference", "definition"].includes(node.type)) {
      const start = node.position?.start.offset;
      const end = node.position?.end.offset;
      if (start !== undefined && end !== undefined) opaque.fill(1, start, end);
    } else if ("children" in node) for (const child of node.children) mark(child as RootContent);
  }
  mark(root);
  // The common backend link extractor excludes complete raw HTML regions,
  // including inline tags whose contents are separate Markdown text nodes.
  const stack: Array<{ tag: string; start: number }> = [];
  const voidTags = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"]);
  for (const match of body.matchAll(/<!--[\s\S]*?(?:-->|$)|<\/?([A-Za-z][\w:-]*)(?=\s|\/?>)(?:"[^"]*"|'[^']*'|[^'"<>])*?>/g)) {
    const start = match.index!;
    const end = start + match[0].length;
    if (opaque[start]) continue;
    excluded.fill(1, start, end);
    const tag = match[1]?.toLowerCase();
    if (!tag) continue;
    if (match[0].startsWith("</")) {
      let open = stack.length - 1;
      while (open >= 0 && stack[open].tag !== tag) open -= 1;
      if (open >= 0) { excluded.fill(1, stack[open].start, end); stack.splice(open); }
    } else if (!voidTags.has(tag) && !match[0].endsWith("/>")) stack.push({ tag, start });
  }
  for (const open of stack) excluded.fill(1, open.start);
  for (const match of body.matchAll(/\[\[[^\]\n]*\]\]/g)) {
    if (!opaque[match.index!]) excluded.fill(1, match.index!, match.index! + match[0].length);
  }
  return excluded;
}

// The editor previews syntax locally; authorization remains a server decision.
// Parse Markdown text nodes so code, images and links never create references.
export function inlineCitationOccurrences(body: string): Array<{ href: string; start: number; end: number }> {
  if (!body.includes("@cite{")) return [];
  return citationOccurrencesFromTree(body, citationMarkdown(body));
}

export function citationOccurrencesFromTree(body: string, root: Root): Array<{ href: string; start: number; end: number }> {
  if (!body.includes("@cite{")) return [];
  const excluded = citationExclusions(body, root);
  const result: Array<{ href: string; start: number; end: number }> = [];
  function visit(node: Root | RootContent) {
    if (["code", "inlineCode", "math", "inlineMath", "link", "linkReference", "definition", "image", "html", "yaml"].includes(node.type)) return;
    if (node.type === "text") {
      const text = node as Text;
      const offset = text.position?.start.offset;
      if (offset === undefined) return;
      const source = body.slice(offset, text.position?.end.offset);
      for (const match of source.matchAll(/@cite\{([^\s{}]+)\}/g)) {
        if (excluded[offset + match.index!]) continue;
        if ((/\\+$/.exec(source.slice(0, match.index))?.[0].length ?? 0) % 2 === 1) continue;
        result.push({ href: match[1], start: offset + match.index!, end: offset + match.index! + match[0].length });
      }
    } else if ("children" in node) {
      for (const child of node.children) visit(child as RootContent);
    }
  }
  visit(root);
  return result;
}

export function previewReferences(body: string, known: ReferenceView[], workspaceId: string, storedBody?: string, occurrences = inlineCitationOccurrences(body)): ReferenceView[] {
  const sourceCharacters = storedBody && known.some((item) => !item.target_id) ? Array.from(storedBody) : null;
  // A private target's ID is redacted from the projection. When its authored
  // token is already in this source body, use that text only to keep the
  // source-owned metadata entry aligned during a local preview.
  known = known.map((item) => {
    if (item.target_id || !sourceCharacters) return item;
    const occurrence = item.occurrences?.[0];
    if (!occurrence) return item;
    const token = sourceCharacters.slice(occurrence.start, occurrence.end).join("");
    const href = /^@cite\{([^\s{}]+)\}$/.exec(token)?.[1];
    const authored = href ? referenceTarget(href, workspaceId) : null;
    return authored ? { ...item, target_id: authored.target_id } : item;
  });
  const lookup = new Map(known.map((ref) => [referenceKey(ref), ref]));
  const resolve = citationTargetResolver(known, workspaceId);
  const ordered = new Map<string, ReferenceView>();
  for (const occurrence of occurrences) {
    const target = resolve(occurrence.href);
    if (!target) continue;
    const key = referenceKey(target);
    const current = ordered.get(key);
    if (current) {
      current.occurrences!.push(occurrence);
      current.inline_count += 1;
    } else {
      const stored = lookup.get(key);
      ordered.set(key, {
        ...target, ...stored, number: ordered.size + 1,
        href: stored?.href ?? null, title: stored?.title ?? null,
        state: stored?.state ?? "unavailable", metadata_present: stored?.metadata_present ?? false,
        location: stored?.location ?? null, inline_count: 1, occurrences: [occurrence],
      });
    }
  }
  for (const stored of known) {
    if ((stored.metadata_present || stored.metadata_id) && !ordered.has(referenceKey(stored))) {
      ordered.set(referenceKey(stored), { ...stored, number: ordered.size + 1, inline_count: 0, occurrences: [] });
    }
  }
  return [...ordered.values()];
}

// Saving gives a convertible address (a slug path, an absolute URL) its
// permanent form. Until then the server preview names the document it will
// become, keyed by the address exactly as written.
function citationTargetResolver(known: ReferenceView[], workspaceId: string) {
  const previewed = new Map<string, DocumentReferenceTarget>();
  for (const item of known) {
    if (!item.target_id) continue;
    for (const occurrence of item.occurrences ?? []) {
      if (occurrence.href) previewed.set(occurrence.href, { target_kind: item.target_kind, target_id: item.target_id });
    }
  }
  return (href: string) => referenceTarget(href, workspaceId) ?? previewed.get(href) ?? null;
}

/** Inline citations that name no document yet; saving keeps them as text. */
export function unresolvedCitations(known: ReferenceView[], workspaceId: string, occurrences: Array<{ href: string; start: number; end: number }>) {
  const resolve = citationTargetResolver(known, workspaceId);
  return occurrences.filter((occurrence) => !resolve(occurrence.href));
}

export function selectedDocumentLink(body: string, from: number, to: number): { href: string; label: string; from: number; to: number } | null {
  const tree = citationMarkdown(body);
  const excluded = citationExclusions(body, tree);
  const definitions = new Map(tree.children.filter((node) => node.type === "definition").map((node) => [node.identifier.toLowerCase(), node.url]));
  let result: { href: string; label: string; from: number; to: number } | null = null;
  function visit(node: Root | RootContent) {
    if (["code", "inlineCode", "math", "inlineMath", "image", "html"].includes(node.type)) return;
    if ((node.type === "link" || node.type === "linkReference") && node.position) {
      const start = node.position.start.offset;
      const end = node.position.end.offset;
      const href = node.type === "link" ? node.url : definitions.get(node.identifier.toLowerCase());
      if (start !== undefined && end !== undefined && !excluded[start] && start <= from && end >= to && href) {
        const source = body.slice(start, end);
        const labelEnd = node.children.at(-1)?.position?.end.offset ?? start + 1;
        result = { href, from: start, to: end, label: source.startsWith("[") ? body.slice(start + 1, labelEnd) : href };
      }
    } else if ("children" in node) {
      for (const child of node.children) visit(child as RootContent);
    }
  }
  visit(tree);
  return result;
}

export function kindLabel(kind: DocumentReferenceKind): "context" | "library" | "agent" {
  return kind === "context_page" ? "context" : kind === "library_item" ? "library" : "agent";
}
