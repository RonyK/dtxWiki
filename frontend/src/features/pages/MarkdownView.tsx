import {
  Children,
  isValidElement,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
import { Maximize2 } from "lucide-react";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import type {
  Element as HastElement,
  Nodes as HastNode,
  Parent as HastParent,
  Properties,
  Root as HastRoot,
  Text as HastText,
} from "hast";
import type { Content, Parent, Root, Text } from "mdast";
import DOMPurify from "dompurify";
import type { TFunction } from "i18next";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import rehypeAutolinkHeadings from "rehype-autolink-headings";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import rehypeSlug from "rehype-slug";
import remarkGfm from "remark-gfm";
import remarkGithubBlockquoteAlert from "remark-github-blockquote-alert";
import remarkMath from "remark-math";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import "highlight.js/styles/github-dark.css";
import "katex/dist/katex.min.css";

import type { PageInlineComment } from "../../shared/api/pages";
import {
  contextMediaAssetIdFromReference,
  type ContextMediaAssetUrlResolver,
} from "../../shared/api/contextMedia";
import { CodeBlockWrapper } from "../../shared/components/CodeBlockWrapper";
import { Dialog } from "../../shared/components/Dialog";
import { slugify } from "../../shared/text/slug";
import {
  MarkdownImage,
  MarkdownImageLinkContext,
  type MarkdownImageProps,
} from "./MarkdownImage";
import { MermaidZoomViewer } from "./MermaidZoomViewer";
import { parsePageDocument } from "./pageFrontmatter";

type MarkdownViewProps = {
  body: string;
  comments?: PageInlineComment[];
  stripLeadingHeading?: string;
  wikilinkPages?: Array<{
    aliases?: string[];
    slug: string;
    title: string;
  }>;
  wikilinkTargets?: Array<{
    target_slug: string;
    target_page_id: string | null;
    current_slug: string | null;
  }>;
  // "internal" uses /context?target=<page-id> when the server supplied an
  // actor-visible target ID, and /pages/{slug} only as a compatibility fallback.
  // plain text with no anchor — used by the unauthenticated public reader so it
  // never emits in-app links and never enumerates private targets (T16 27-f).
  wikilinkMode?: "internal" | "plain";
  // When true (editor split preview), stamp data-source-line on top-level
  // blocks so the editor can line-anchor its scroll sync, and skip the leading
  // heading strip so AST line numbers match the CodeMirror body 1:1.
  sourceLineNumbers?: boolean;
  // Tombstones for hard-deleted Library sources. Each entry's citationKey is the
  // `@cite{...}` slug it matches (the consumer recomputes it via
  // citationKeyForSource); a matching inline cite renders `label` with a
  // "(deleted)" style instead of leaving a dangling token.
  deletedSources?: DeletedSourceMark[];
  // Projects stable dtx-media:// references to an authorization-scoped URL for
  // this exact reader surface. The Markdown source itself stays untouched.
  resolveContextMediaAsset?: ContextMediaAssetUrlResolver;
  // Uploaded and converted documents are not authored Page content. This mode
  // drops raw HTML and ambient resource URLs, and keeps every link as text so
  // untrusted content cannot create a navigation control.
  renderPolicy?: "page" | "untrusted-preview";
  // P10.1: workspace id for the canonical `/workspaces/{id}/context/pages/{id}`
  // wikilink permalink. Only needed in "internal" mode; a caller that omits it
  // still gets a working query-param link when the target has a resolved page
  // id, matching pre-P10.1 behavior.
  workspaceId?: string | null;
};

type DeletedSourceMark = {
  citationKey: string;
  label: string;
};

export const MARKDOWN_GEOMETRY_CHANGE_EVENT =
  "dtxwiki:markdown-geometry-change";

type MarkdownAnchorProps = ComponentPropsWithoutRef<"a"> & {
  "data-internal"?: string;
  node?: unknown;
};

const MAX_INLINE_TOKEN_TRANSFORM_DEPTH = 128;
const MAX_INLINE_COMMENT_HIGHLIGHTS = 50;
const MAX_INLINE_COMMENT_HIGHLIGHT_BODY_CHARS = 100_000;
const MERMAID_LANGUAGE = "mermaid";
const MERMAID_FONT_FAMILY =
  'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
const MERMAID_FONT_SIZE = "13px";
const SVG_NUMBER_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
// The shared anchor namespace. rehype-sanitize clobber-prefixes every id/name
// with this so user-authored markup can never claim (clobber) the app's own
// anchor/DOM namespace; rehypePrefixFragmentHrefs keeps same-page fragment
// links pointing at the prefixed ids.
const CLOBBER_PREFIX = "user-content-";
// remark-rehype would otherwise prefix GFM footnote ids itself; emit them raw
// so rehype-sanitize's clobberPrefix stays the single place ids gain the
// prefix (a second prefix here would orphan footnote links).
const REMARK_REHYPE_OPTIONS = { clobberPrefix: "" };
const HIGHLIGHT_LANGUAGES = {
  bash,
  css,
  diff,
  dockerfile,
  go,
  ini,
  javascript,
  json,
  markdown,
  python,
  rust,
  sql,
  typescript,
  xml,
  yaml,
};

export function MarkdownView({
  body,
  comments = [],
  stripLeadingHeading,
  wikilinkPages = [],
  wikilinkTargets = [],
  wikilinkMode = "internal",
  sourceLineNumbers = false,
  deletedSources = [],
  resolveContextMediaAsset,
  renderPolicy = "page",
  workspaceId,
}: MarkdownViewProps) {
  const articleRef = useRef<HTMLElement | null>(null);
  const pageSourceLineNumbers = renderPolicy === "page" && sourceLineNumbers;
  const parsedBody = renderPolicy === "page" ? parsePageDocument(body).body : body;
  // The editor split preview renders from the full body (no string-level strip)
  // so data-source-line maps 1:1 to the CodeMirror body; the duplicate leading
  // title is dropped at the node level (remarkDropDuplicateHeading) so it still
  // does not render. Readers keep the simple string strip.
  const markdownBody =
    renderPolicy === "page"
      ? pageSourceLineNumbers
        ? parsedBody
        : stripDuplicateLeadingHeading(parsedBody, stripLeadingHeading)
      : body;
  const sourceLineCount = Math.max(1, markdownBody.split(/\r?\n/).length);
  const anchorPluginOptions = useMemo(
    () => ({ highlights: inlineCommentAnchorHighlights(comments, body) }),
    [body, comments],
  );
  const tokenPluginOptions = useMemo(
    () => ({
      resolveWikilinkTarget: buildWikilinkResolver(
        wikilinkPages,
        wikilinkTargets,
      ),
      plainWikilinks: wikilinkMode === "plain",
      resolveDeletedSource: buildDeletedSourceResolver(deletedSources),
      sourceLineNumbers: pageSourceLineNumbers,
      workspaceId,
    }),
    [
      wikilinkPages,
      wikilinkTargets,
      wikilinkMode,
      deletedSources,
      pageSourceLineNumbers,
      workspaceId,
    ],
  );
  const contextMediaPluginOptions = useMemo(
    () => ({ resolveAssetUrl: resolveContextMediaAsset }),
    [resolveContextMediaAsset],
  );
  useLayoutEffect(() => {
    if (pageSourceLineNumbers) {
      notifyMarkdownGeometryChange(articleRef.current);
    }
  }, [markdownBody, pageSourceLineNumbers]);

  return (
    <article className="markdown-body" ref={articleRef}>
      {pageSourceLineNumbers ? (
        <span
          aria-hidden="true"
          className="markdown-scroll-sentinel"
          data-scroll-anchor="top"
          data-source-line={1}
        />
      ) : null}
      <ReactMarkdown
        components={
          renderPolicy === "page"
            ? {
                a: MarkdownLink,
                img: MarkdownImage,
                input: MarkdownTaskState,
                pre: MarkdownCodeBlock,
                table: MarkdownTable,
                video: MarkdownVideo,
              }
            : {
                a: UntrustedPreviewLink,
                img: UntrustedPreviewImage,
                input: MarkdownTaskState,
                pre: UntrustedPreviewCodeBlock,
                table: MarkdownTable,
              }
        }
        remarkPlugins={
          renderPolicy === "page"
            ? [
                remarkGfm,
                remarkMath,
                remarkGithubBlockquoteAlert,
                [remarkDtxTokens, tokenPluginOptions],
                ...(pageSourceLineNumbers
                  ? [
                      () => (tree: Root) =>
                        dropDuplicateHeading(tree, stripLeadingHeading),
                    ]
                  : []),
              ]
            : [remarkGfm]
        }
        rehypePlugins={
          renderPolicy === "page"
            ? [
                rehypeRaw,
                [rehypeResolveContextMedia, contextMediaPluginOptions],
                rehypeConstrainPageImageSources,
                rehypeSlug,
                [rehypeInlineCommentAnchors, anchorPluginOptions],
                [rehypeAutolinkHeadings, { behavior: "wrap" }],
                rehypePrefixFragmentHrefs,
                rehypeTableAlignment,
                [rehypeSanitize, markdownSanitizeSchema],
                // Sanitize authored Markdown before KaTeX emits its trusted
                // layout spans. KaTeX needs its generated inline positioning
                // styles for fractions and super/subscripts to render correctly.
                rehypeKatex,
                [
                  rehypeHighlight,
                  {
                    detect: true,
                    ignoreMissing: true,
                    languages: HIGHLIGHT_LANGUAGES,
                  },
                ],
                ...(pageSourceLineNumbers ? [rehypeSourceLines] : []),
              ]
            : [
                rehypeTableAlignment,
                [rehypeSanitize, markdownSanitizeSchema],
              ]
        }
        remarkRehypeOptions={REMARK_REHYPE_OPTIONS}
        skipHtml={renderPolicy === "untrusted-preview"}
        urlTransform={
          renderPolicy === "untrusted-preview"
            ? untrustedPreviewUrlTransform
            : pageUrlTransform
        }
      >
        {markdownBody}
      </ReactMarkdown>
      {pageSourceLineNumbers ? (
        <span
          aria-hidden="true"
          className="markdown-scroll-sentinel"
          data-scroll-anchor="bottom"
          data-source-line={sourceLineCount + 1}
        />
      ) : null}
    </article>
  );
}

function untrustedPreviewUrlTransform(
  _value: string,
  _key: string,
  _node: Readonly<HastElement>,
) {
  return undefined;
}

function pageUrlTransform(
  value: string,
  key: string,
  node: Readonly<HastElement>,
) {
  return node.tagName === "img" && key === "src"
    ? trustedPageImageSource(value)
    : defaultUrlTransform(value);
}

function UntrustedPreviewImage({ alt = "" }: MarkdownImageProps) {
  return <>{alt}</>;
}

type ContextMediaResolverOptions = {
  resolveAssetUrl?: ContextMediaAssetUrlResolver;
};

function rehypeResolveContextMedia(options: ContextMediaResolverOptions = {}) {
  return (tree: HastRoot) => {
    resolveContextMediaElements(tree, options.resolveAssetUrl);
  };
}

function rehypeConstrainPageImageSources() {
  return (tree: HastRoot) => {
    constrainPageImageSources(tree);
  };
}

function constrainPageImageSources(parent: HastParent) {
  for (const child of parent.children) {
    if (child.type === "element") {
      if (child.tagName === "img") {
        const source = child.properties.src;
        if (typeof source === "string") {
          const trustedSource = trustedPageImageSource(source);
          if (trustedSource) {
            child.properties.src = trustedSource;
          } else {
            delete child.properties.src;
          }
        }
      }
      constrainPageImageSources(child);
    }
  }
}

const PAGE_IMAGE_POLICY_BASE = new URL("https://dtxwiki.invalid/");

function trustedPageImageSource(value: string) {
  const candidate = value.trim();
  if (!candidate || /^[\\/]{2}/.test(candidate)) {
    return undefined;
  }

  const explicitScheme = /^([A-Za-z][A-Za-z\d+.-]*):/.exec(candidate);
  let parsed: URL;
  try {
    parsed = new URL(candidate, PAGE_IMAGE_POLICY_BASE);
  } catch {
    return undefined;
  }

  if (explicitScheme) {
    if (
      explicitScheme[1].toLowerCase() !== "https" ||
      !/^https:\/\//i.test(candidate) ||
      parsed.protocol !== "https:"
    ) {
      return undefined;
    }
    return `https:${candidate.slice(explicitScheme[0].length)}`;
  }

  return parsed.origin === PAGE_IMAGE_POLICY_BASE.origin
    ? candidate
    : undefined;
}

function resolveContextMediaElements(
  parent: HastParent,
  resolveAssetUrl?: ContextMediaAssetUrlResolver,
) {
  for (const child of parent.children) {
    if (child.type === "element") {
      if (child.tagName === "img") {
        delete child.properties["data-dtx-media-state"];
        delete child.properties["data-dtx-media-reason"];
        delete child.properties.dataDtxMediaState;
        delete child.properties.dataDtxMediaReason;
        const source = child.properties.src;
        const assetId =
          typeof source === "string"
            ? contextMediaAssetIdFromReference(source)
            : null;
        if (assetId) {
          const resolution = resolveAssetUrl?.(assetId) ?? {
            status: "unavailable" as const,
            reason: "resolver_unavailable" as const,
          };
          if (resolution.status === "ready" && resolution.url.trim()) {
            child.properties.src = resolution.url;
            child.properties["data-dtx-media-state"] = "ready";
          } else {
            delete child.properties.src;
            child.properties["data-dtx-media-state"] = "unavailable";
            child.properties["data-dtx-media-reason"] =
              resolution.status === "unavailable"
                ? resolution.reason
                : "asset_url_unavailable";
          }
        }
      }
      resolveContextMediaElements(child, resolveAssetUrl);
    }
  }
}

function stripDuplicateLeadingHeading(markdown: string, title?: string) {
  if (!title?.trim()) {
    return markdown;
  }
  const match = /^#\s+(.+?)(?:\n|$)/.exec(markdown.trimStart());
  if (!match || normalizeHeadingText(match[1]) !== normalizeHeadingText(title)) {
    return markdown;
  }
  return markdown.trimStart().slice(match[0].length).replace(/^\n+/, "");
}

function normalizeHeadingText(value: string) {
  return value
    .replace(/[`*_~[\]()]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

type DtxNode = Content | DtxBlock | DtxInline;
type DtxParent = Omit<Parent, "children"> & { children: DtxNode[] };

type DtxBlock = Omit<Parent, "children"> & {
  type: "dtxDecisionBlock" | "dtxActionBlock";
  data: {
    hName: "decision-block" | "action-block";
    hProperties: Properties;
  };
  children: DtxNode[];
};

type DtxInline = {
  type: "dtxWikilink" | "dtxCitation" | "dtxHighlight" | "dtxInlineTag";
  data: {
    hName: "a" | "cite" | "mark";
    hProperties: Properties;
  };
  children: Text[];
};

type DtxTokenOptions = {
  resolveWikilinkTarget?: (rawTarget: string) => WikilinkResolution;
  // When true, render [[wikilinks]] as plain text (no anchor). Used by the
  // public reader so it never emits in-app links or probes private targets.
  plainWikilinks?: boolean;
  // Maps an `@cite{...}` slug to a tombstone label when its source was deleted.
  resolveDeletedSource?: (sourceId: string) => string | undefined;
  sourceLineNumbers?: boolean;
  workspaceId?: string | null;
};

type WikilinkResolution = {
  pageId: string | null;
  slug: string;
};

function buildDeletedSourceResolver(
  deletedSources: DeletedSourceMark[],
): (sourceId: string) => string | undefined {
  if (!deletedSources.length) {
    return () => undefined;
  }
  const byKey = new Map<string, string>();
  for (const source of deletedSources) {
    const key = source.citationKey?.trim();
    if (key) {
      byKey.set(key, source.label);
    }
  }
  return (sourceId: string) => byKey.get(sourceId);
}

function remarkDtxTokens(options: DtxTokenOptions = {}) {
  return (tree: Root) => {
    transformFencedBlocks(tree, options.sourceLineNumbers ?? false);
    transformInlineTokens(tree as unknown as DtxParent, options);
  };
}

function transformFencedBlocks(root: Root, sourceLineNumbers: boolean) {
  const rootParent = root as unknown as DtxParent;
  const nextChildren: DtxNode[] = [];
  const children = rootParent.children;
  for (let index = 0; index < children.length; index += 1) {
    const child = children[index];
    const opening = parseFenceOpen(child);
    if (!opening) {
      nextChildren.push(child);
      continue;
    }
    const closingIndex = findFenceClose(children, index + 1, opening.kind);
    if (closingIndex === -1) {
      nextChildren.push(child);
      continue;
    }
    const fencedBlock = createFencedBlock(
      opening.kind,
      opening.attributes,
      children.slice(index + 1, closingIndex),
    );
    const start = "position" in child ? child.position?.start : undefined;
    const closingNode = children[closingIndex];
    const end = "position" in closingNode ? closingNode.position?.end : undefined;
    if (start && end) {
      fencedBlock.position = { start, end };
      if (sourceLineNumbers) {
        fencedBlock.data.hProperties["data-source-line"] = start.line;
        fencedBlock.data.hProperties["data-source-end-line"] = end.line;
      }
    }
    nextChildren.push(fencedBlock);
    index = closingIndex;
  }
  rootParent.children = nextChildren;
}

function parseFenceOpen(node: DtxNode) {
  if (node.type !== "html") {
    return null;
  }
  const match = /^<!--\s*(decision|action)\b([^]*?)-->\s*$/.exec(node.value);
  if (!match) {
    return null;
  }
  return {
    kind: match[1] as "decision" | "action",
    attributes: parseFenceAttributes(match[2]),
  };
}

// Closing fence patterns only depend on the fence kind, so hoist them to
// module scope to avoid allocating a new RegExp on every call (F-P1-5).
const FENCE_CLOSE_PATTERNS: Record<"decision" | "action", RegExp> = {
  decision: /^<!--\s*\/decision\s*-->\s*$/,
  action: /^<!--\s*\/action\s*-->\s*$/,
};

function findFenceClose(
  children: DtxNode[],
  startIndex: number,
  kind: "decision" | "action",
) {
  const closePattern = FENCE_CLOSE_PATTERNS[kind];
  for (let index = startIndex; index < children.length; index += 1) {
    const child = children[index];
    if (child.type === "html" && closePattern.test(child.value)) {
      return index;
    }
  }
  return -1;
}

function parseFenceAttributes(source: string) {
  const attributes: Record<string, string> = {};
  const pattern = /([A-Za-z_][\w-]*)=("[^"]*"|'[^']*'|[^\s]+)/g;
  let match = pattern.exec(source);
  while (match) {
    attributes[match[1]] = stripFenceQuotes(match[2]);
    match = pattern.exec(source);
  }
  return attributes;
}

function stripFenceQuotes(value: string) {
  if (
    (value.startsWith("\"") && value.endsWith("\"")) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    return value.slice(1, -1);
  }
  return value;
}

function createFencedBlock(
  kind: "decision" | "action",
  attributes: Record<string, string>,
  children: DtxNode[],
): DtxBlock {
  const tagName = kind === "decision" ? "decision-block" : "action-block";
  const idAttribute = kind === "decision" ? "data-decision-id" : "data-action-id";
  const hProperties: Properties = {
    className: [tagName],
    "data-block-kind": kind,
  };
  const id = sanitizeTokenAttribute(attributes.id);
  if (id) {
    hProperties[idAttribute] = id;
  }
  for (const name of ["status", "owner", "due"]) {
    const value = sanitizeTokenAttribute(attributes[name]);
    if (value) {
      hProperties[`data-${name}`] = value;
    }
  }
  return {
    type: kind === "decision" ? "dtxDecisionBlock" : "dtxActionBlock",
    data: {
      hName: tagName,
      hProperties,
    },
    children,
  };
}

function transformInlineTokens(
  node: DtxNode | DtxParent,
  options: DtxTokenOptions,
  depth = 0,
) {
  if (!isParentNode(node) || shouldSkipInlineTransform(node.type)) {
    return;
  }
  if (depth >= MAX_INLINE_TOKEN_TRANSFORM_DEPTH) {
    return;
  }
  node.children = node.children.flatMap((child) => {
    if (child.type === "text") {
      return tokenizeTextNode(child, options);
    }
    transformInlineTokens(child, options, depth + 1);
    return [child];
  });
}

function isParentNode(node: DtxNode | DtxParent): node is DtxParent {
  return Array.isArray((node as Partial<DtxParent>).children);
}

function shouldSkipInlineTransform(type: string) {
  return ["code", "inlineCode", "link", "linkReference", "definition"].includes(type);
}

function tokenizeTextNode(node: Text, options: DtxTokenOptions): DtxNode[] {
  const pattern = /\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]|@cite\{([A-Za-z0-9][A-Za-z0-9._:-]*(?:#[A-Za-z0-9._:-]+)?)\}|==([^=\n]+?)==|(^|[\s([{])#([A-Za-z][A-Za-z0-9_-]{0,47})\b/g;
  const nodes: DtxNode[] = [];
  let cursor = 0;
  let match = pattern.exec(node.value);
  while (match) {
    const tokenStart = match[6] ? match.index + (match[5]?.length ?? 0) : match.index;
    if (tokenStart > cursor) {
      nodes.push(textNode(node.value.slice(cursor, tokenStart)));
    }
    if (match[1]) {
      // Public reader (plainWikilinks): emit the link text as plain text only.
      // We do NOT resolve targets here — resolving publicness per link would
      // require an authenticated lookup and could enumerate private slugs/titles
      // (T16 27-f). Future: resolve to /w/{ws}/pages/{slug} only for targets the
      // public read endpoint confirms are public, without a private list fetch.
      if (options.plainWikilinks) {
        const plainText = (match[2] || match[1]).trim() || "Untitled link";
        nodes.push(textNode(plainText));
      } else {
        nodes.push(wikilinkNode(match[1], match[2], options));
      }
    } else if (match[3]) {
      nodes.push(citationNode(match[3], options));
    } else if (match[4]) {
      nodes.push(highlightNode(match[4]));
    } else if (match[6]) {
      nodes.push(inlineTagNode(match[6]));
    }
    cursor = match.index + match[0].length;
    match = pattern.exec(node.value);
  }
  if (cursor === 0) {
    return [node];
  }
  if (cursor < node.value.length) {
    nodes.push(textNode(node.value.slice(cursor)));
  }
  return nodes;
}

function wikilinkNode(rawTarget: string, alias: string | undefined, options: DtxTokenOptions): DtxInline {
  const target = parseWikilinkTarget(rawTarget);
  const resolution = target.pageTarget
    ? options.resolveWikilinkTarget?.(target.pageTarget) ?? {
        pageId: null,
        slug: slugify(target.pageTarget),
      }
    : { pageId: null, slug: "" };
  const headingSlug = target.headingTarget ? slugifyHeadingAnchor(target.headingTarget) : "";
  const href = wikilinkHref(resolution, headingSlug, options.workspaceId);
  const title = (alias || rawTarget).trim();
  return {
    type: "dtxWikilink",
    data: {
      hName: "a",
      hProperties: {
        href,
        className: href !== "#" ? ["wikilink"] : ["wikilink", "wikilink-broken"],
        "data-internal": "true",
      },
    },
    children: [textNode(title || "Untitled link")],
  };
}

function buildWikilinkResolver(
  pages: NonNullable<MarkdownViewProps["wikilinkPages"]>,
  targets: NonNullable<MarkdownViewProps["wikilinkTargets"]>,
) {
  const pinnedTargets = new Map(
    targets.map(
      (target) =>
        [
          target.target_slug,
          {
            pageId: target.target_page_id,
            slug: target.current_slug ?? "",
          },
        ] as const,
    ),
  );
  const knownSlugs = new Set<string>();
  const aliasToSlug = new Map<string, string>();
  for (const page of pages) {
    if (!page.slug) {
      continue;
    }
    knownSlugs.add(page.slug);
    for (const alias of [page.title, ...(page.aliases ?? [])]) {
      const key = wikilinkAliasKey(alias);
      if (key && !aliasToSlug.has(key)) {
        aliasToSlug.set(key, page.slug);
      }
    }
  }
  return (rawTarget: string) => {
    const slugCandidate = slugify(rawTarget);
    if (pinnedTargets.has(slugCandidate)) {
      return pinnedTargets.get(slugCandidate) ?? { pageId: null, slug: "" };
    }
    if (knownSlugs.has(slugCandidate)) {
      return { pageId: null, slug: slugCandidate };
    }
    return {
      pageId: null,
      slug: aliasToSlug.get(wikilinkAliasKey(rawTarget)) ?? slugCandidate,
    };
  };
}

function parseWikilinkTarget(rawTarget: string) {
  const [pageTarget = "", headingTarget = ""] = rawTarget.split("#", 2);
  return {
    pageTarget: pageTarget.trim(),
    headingTarget: headingTarget.trim(),
  };
}

function wikilinkHref(
  { pageId, slug }: WikilinkResolution,
  headingSlug: string,
  workspaceId?: string | null,
) {
  if (pageId) {
    const hash = headingSlug ? `#${CLOBBER_PREFIX}${headingSlug}` : "";
    if (workspaceId) {
      return `/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${encodeURIComponent(pageId)}${hash}`;
    }
    const target = new URLSearchParams({ target: pageId }).toString();
    return `/context?${target}${hash}`;
  }
  // P10.1: /pages/{slug} (the old slug-only compatibility fallback) is gone.
  // A non-empty `slug` here means the wikilink named a cross-page target the
  // server did not resolve to an actor-visible page id — it can no longer
  // navigate anywhere in-app, so it renders broken (wikilinkNode's
  // "wikilink-broken" styling triggers on href === "#") rather than pointing
  // at a dead route, and rather than misrouting to a heading in the CURRENT
  // document. A genuine same-document anchor (`[[#Heading]]`, no page target
  // at all) has an empty slug and still jumps locally below.
  if (slug) {
    return "#";
  }
  if (headingSlug) {
    return `#${CLOBBER_PREFIX}${headingSlug}`;
  }
  return "#";
}

function wikilinkAliasKey(value: string) {
  return value.trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function citationNode(rawSourceId: string, options: DtxTokenOptions): DtxInline {
  const [libraryItemId, anchor] = rawSourceId.split("#", 2);
  const safeSourceId = sanitizeTokenAttribute(libraryItemId) || "unknown-source";
  const safeAnchor = sanitizeTokenAttribute(anchor);
  const deletedLabel = options.resolveDeletedSource?.(safeSourceId);
  if (deletedLabel) {
    // The source was hard-deleted; render a tombstone in place of the dangling
    // token so the reader sees what was cited instead of a raw `@cite{...}`.
    return {
      type: "dtxCitation",
      data: {
        hName: "cite",
        hProperties: {
          className: ["cite-ref", "cite-ref-deleted"],
          "data-source-id": safeSourceId,
          "data-source-deleted": "true",
          ...(safeAnchor ? { "data-source-anchor": safeAnchor } : {}),
        },
      },
      children: [textNode(deletedLabel)],
    };
  }
  return {
    type: "dtxCitation",
    data: {
      hName: "cite",
      hProperties: {
        className: ["cite-ref"],
        "data-source-id": safeSourceId,
        ...(safeAnchor ? { "data-source-anchor": safeAnchor } : {}),
      },
    },
    children: [textNode(`@cite{${rawSourceId}}`)],
  };
}

function highlightNode(value: string): DtxInline {
  return {
    type: "dtxHighlight",
    data: {
      hName: "mark",
      hProperties: {
        className: ["markdown-highlight"],
      },
    },
    children: [textNode(value)],
  };
}

function inlineTagNode(rawTag: string): DtxInline {
  const safeTag = sanitizeInlineTag(rawTag);
  return {
    type: "dtxInlineTag",
    data: {
      hName: "a",
      hProperties: {
        href: `/search?q=${encodeURIComponent(`#${safeTag}`)}`,
        className: ["inline-tag"],
        "data-tag": safeTag,
      },
    },
    children: [textNode(`#${rawTag}`)],
  };
}

function textNode(value: string): Text {
  return { type: "text", value };
}

function slugifyHeadingAnchor(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function sanitizeTokenAttribute(value?: string) {
  const trimmed = value?.trim();
  if (!trimmed) {
    return "";
  }
  const safe = trimmed.replace(/[^A-Za-z0-9@._:-]/g, "").slice(0, 120);
  return safe;
}

function sanitizeInlineTag(value: string) {
  return value.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 48);
}

function MarkdownLink({
  href,
  children,
  node: _node,
  ...props
}: MarkdownAnchorProps) {
  const { t } = useTranslation("pages");
  // wikilinkNode() already sets the full "wikilink"/"wikilink-broken" class
  // list on hProperties.className for every data-internal anchor -- it is
  // the only producer of that attribute -- so re-deriving it here just
  // duplicated the classes (measured DOM: class="wikilink wikilink").
  const className = props.className;
  const accessibleProps = {
    ...props,
    "aria-label": props["aria-label"] ?? linkedImageFallbackLabel(children, t),
  };
  const renderLink = (linkChildren: ReactNode) => {
    if (!href) {
      return <a {...accessibleProps} className={className}>{linkChildren}</a>;
    }
    if (href.startsWith("#")) {
      return (
        <a {...accessibleProps} className={className} href={href}>
          {linkChildren}
        </a>
      );
    }
    if (href.startsWith("/") && !href.startsWith("//")) {
      return (
        <Link to={href} {...accessibleProps} className={className}>
          {linkChildren}
        </Link>
      );
    }
    return (
      <a
        {...accessibleProps}
        className={className}
        href={href}
        rel="noopener noreferrer"
        target="_blank"
      >
        {linkChildren}
      </a>
    );
  };
  if (containsMarkdownImage(children)) {
    return (
      <MarkdownLinkedContent renderLink={renderLink}>
        {children}
      </MarkdownLinkedContent>
    );
  }
  return renderLink(children);
}

function MarkdownLinkedContent({
  children,
  renderLink,
}: {
  children: ReactNode;
  renderLink: (children: ReactNode) => ReactNode;
}) {
  const [actionsHost, setActionsHost] = useState<HTMLSpanElement | null>(null);
  return (
    <>
      {renderLink(
        <MarkdownImageLinkContext.Provider
          value={{ actionsHost, mode: "within-link" }}
        >
          {children}
        </MarkdownImageLinkContext.Provider>,
      )}
      <span className="markdown-linked-image-actions" ref={setActionsHost} />
    </>
  );
}

function UntrustedPreviewLink({ children }: MarkdownAnchorProps) {
  return <>{children}</>;
}

function linkedImageFallbackLabel(children: ReactNode, t: TFunction<"pages">) {
  const linkedImage = soleMarkdownImage(children);
  if (!linkedImage || String(linkedImage.props.alt ?? "").trim().length > 0) {
    return undefined;
  }
  return t("image.linkedUnnamed", { defaultValue: "Open linked image" });
}

function soleMarkdownImage(children: ReactNode) {
  const content = Children.toArray(children).filter(
    (child) => typeof child !== "string" || child.trim().length > 0,
  );
  if (
    content.length !== 1 ||
    !isValidElement<MarkdownImageProps>(content[0]) ||
    content[0].type !== MarkdownImage
  ) {
    return null;
  }
  return content[0];
}

function containsMarkdownImage(children: ReactNode): boolean {
  return Children.toArray(children).some((child) => {
    if (!isValidElement<{ children?: ReactNode }>(child)) {
      return false;
    }
    return (
      child.type === MarkdownImage || containsMarkdownImage(child.props.children)
    );
  });
}

type MarkdownTableProps = ComponentPropsWithoutRef<"table"> & {
  node?: unknown;
};

/* Wide tables own their horizontal scroll instead of widening the document.
   The named region makes that overflow reachable to keyboard users. */
/* eslint-disable jsx-a11y/no-noninteractive-tabindex */
function MarkdownTable({ node: _node, ...props }: MarkdownTableProps) {
  const { t } = useTranslation("pages");
  return (
    <div
      aria-label={t("blocks.entries.table")}
      className="markdown-table-scroll"
      role="region"
      tabIndex={0}
    >
      <table {...props} />
    </div>
  );
}
/* eslint-enable jsx-a11y/no-noninteractive-tabindex */

type MarkdownVideoProps = ComponentPropsWithoutRef<"video"> & {
  node?: unknown;
};

type MarkdownVideoStyle = CSSProperties & {
  "--markdown-video-width-cap"?: string;
};

/* Compatibility rendering must keep already-saved, sanitized raw video
   readable. A typed media dialect will own mandatory caption-track authoring. */
/* eslint-disable jsx-a11y/media-has-caption */
function MarkdownVideo({
  className,
  height: _height,
  node: _node,
  style,
  width,
  ...props
}: MarkdownVideoProps) {
  const widthCap = normalizedRawVideoWidth(width);
  const videoStyle: MarkdownVideoStyle = { ...style };
  if (widthCap !== null) {
    videoStyle["--markdown-video-width-cap"] = `${widthCap}px`;
  }
  return (
    <video
      {...props}
      className={["markdown-video", className].filter(Boolean).join(" ")}
      style={videoStyle}
    />
  );
}
/* eslint-enable jsx-a11y/media-has-caption */

function normalizedRawVideoWidth(width: MarkdownVideoProps["width"]) {
  if (typeof width !== "number" && typeof width !== "string") {
    return null;
  }
  const numericWidth = typeof width === "number" ? width : Number(width.trim());
  if (!Number.isSafeInteger(numericWidth) || numericWidth <= 0) {
    return null;
  }
  return numericWidth;
}

type SourceAnchorAttributes = {
  "data-source-line"?: string | number;
  "data-source-end-line"?: string | number;
  "data-scroll-sync-anchor"?: string;
};

type MarkdownPreProps = ComponentPropsWithoutRef<"pre"> & SourceAnchorAttributes & {
  node?: unknown;
};

function UntrustedPreviewCodeBlock({
  children,
  node: _node,
}: MarkdownPreProps) {
  return (
    <pre>
      <code>{extractCodeText(children)}</code>
    </pre>
  );
}

/* Horizontally scrollable code must be keyboard-focusable so keyboard users
   can reach content beyond the visible edge. This narrowly scoped exception
   applies to the named read-only region below, not arbitrary static content. */
/* eslint-disable jsx-a11y/no-noninteractive-tabindex */
function MarkdownCodeBlock({ children, node: _node, ...props }: MarkdownPreProps) {
  const { t } = useTranslation("pages");
  const codeText = extractCodeText(children);
  const rawLanguage = extractCodeLanguage(children);
  const sourceAnchorAttributes = pickSourceAnchorAttributes(props, children);
  if (rawLanguage === MERMAID_LANGUAGE) {
    return (
      <MermaidDiagram
        source={codeText}
        sourceAnchorAttributes={sourceAnchorAttributes}
      />
    );
  }
  const language = displayLanguage(rawLanguage);
  const preProps = { ...props };
  delete preProps["data-source-line"];
  delete preProps["data-source-end-line"];
  delete preProps["data-scroll-sync-anchor"];

  return (
    <CodeBlockWrapper
      code={codeText}
      language={language}
      sourceAnchorAttributes={sourceAnchorAttributes}
    >
      <pre
        {...preProps}
        aria-label={t("reader.scrollableCode", { language })}
        role="region"
        tabIndex={0}
      >
        {children}
      </pre>
    </CodeBlockWrapper>
  );
}
/* eslint-enable jsx-a11y/no-noninteractive-tabindex */

type MarkdownInputProps = ComponentPropsWithoutRef<"input"> & {
  node?: unknown;
};

function MarkdownTaskState({
  checked,
  disabled,
  node: _node,
  type,
  ...props
}: MarkdownInputProps) {
  const { t } = useTranslation("pages");
  if (type === "checkbox" && disabled) {
    return (
      <span
        aria-label={
          checked ? t("reader.taskComplete") : t("reader.taskIncomplete")
        }
        className="markdown-task-state"
        role="img"
      >
        {checked ? "☑" : "☐"}
      </span>
    );
  }
  return <input checked={checked} disabled={disabled} type={type} {...props} />;
}

function extractCodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(extractCodeText).join("");
  }
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return extractCodeText(node.props.children);
  }
  return "";
}

function extractCodeLanguage(node: ReactNode): string | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const language = extractCodeLanguage(child);
      if (language) {
        return language;
      }
    }
    return null;
  }
  if (!isValidElement<{ className?: unknown; children?: ReactNode }>(node)) {
    return null;
  }
  const className = String(node.props.className ?? "");
  const match = /(?:^|\s)language-([A-Za-z0-9_+-]+)/.exec(className);
  if (match) {
    return match[1].toLowerCase();
  }
  return extractCodeLanguage(node.props.children);
}

function displayLanguage(language: string | null) {
  if (!language) {
    return "text";
  }
  const labels: Record<string, string> = {
    js: "JavaScript",
    javascript: "JavaScript",
    jsx: "JSX",
    ts: "TypeScript",
    tsx: "TSX",
    typescript: "TypeScript",
    py: "Python",
    python: "Python",
    sh: "Shell",
    bash: "Shell",
    shell: "Shell",
    html: "HTML",
    xml: "XML",
    md: "Markdown",
    markdown: "Markdown",
    yml: "YAML",
    yaml: "YAML",
    json: "JSON",
    css: "CSS",
    sql: "SQL",
    diff: "Diff",
    dockerfile: "Dockerfile",
    go: "Go",
    rust: "Rust",
    ini: "INI",
    mermaid: "Mermaid",
  };
  return labels[language] ?? language.toUpperCase();
}

type MermaidDiagramState =
  | { status: "rendering" }
  | { status: "rendered"; svg: string }
  | { status: "error"; message: string };

let mermaidDiagramSequence = 0;

function MermaidDiagram({
  source,
  sourceAnchorAttributes,
}: {
  source: string;
  sourceAnchorAttributes: SourceAnchorAttributes;
}) {
  const { t } = useTranslation("pages");
  const figureRef = useRef<HTMLElement | null>(null);
  const [state, setState] = useState<MermaidDiagramState>({ status: "rendering" });
  const [zoomOpen, setZoomOpen] = useState(false);
  const titleId = useId();

  useEffect(() => {
    let canceled = false;
    mermaidDiagramSequence += 1;
    const diagramId = `dtxwiki-mermaid-${mermaidDiagramSequence}`;
    setState({ status: "rendering" });
    void renderMermaidDiagram(source, diagramId)
      .then((svg) => {
        if (!canceled) {
          setState({ status: "rendered", svg });
        }
      })
      .catch((error: unknown) => {
        if (!canceled) {
          setState({
            status: "error",
            message: error instanceof Error ? error.message : "Invalid Mermaid diagram",
          });
        }
      });
    return () => {
      canceled = true;
    };
  }, [source]);

  useLayoutEffect(() => {
    notifyMarkdownGeometryChange(figureRef.current);
  }, [state.status]);

  if (state.status === "rendered") {
    return (
      <>
        <figure
          {...sourceAnchorAttributes}
          className="mermaid-diagram mermaid-diagram-rendered"
          ref={figureRef}
        >
          <div className="mermaid-diagram-toolbar">
            <button
              aria-expanded={zoomOpen}
              aria-haspopup="dialog"
              aria-label={t("mermaid.expand", {
                defaultValue: "Expand Mermaid diagram",
              })}
              className="mermaid-diagram-zoom-trigger icon-button"
              type="button"
              onClick={() => setZoomOpen(true)}
            >
              <Maximize2 size={14} aria-hidden="true" />
            </button>
          </div>
          <div className="mermaid-diagram-viewport">
            <div
              className="mermaid-diagram-svg"
              dangerouslySetInnerHTML={{ __html: state.svg }}
              onDoubleClick={() => setZoomOpen(true)}
            />
          </div>
        </figure>
        {zoomOpen ? (
          <Dialog
            className="mermaid-zoom-panel"
            labelledBy={titleId}
            onClose={() => setZoomOpen(false)}
          >
            <h2 className="visually-hidden" id={titleId}>
              {t("mermaid.viewerTitle", { defaultValue: "Diagram viewer" })}
            </h2>
            <MermaidZoomViewer
              svg={state.svg}
              onClose={() => setZoomOpen(false)}
            />
          </Dialog>
        ) : null}
      </>
    );
  }

  if (state.status === "error") {
    return (
      <figure
        {...sourceAnchorAttributes}
        className="mermaid-diagram mermaid-diagram-error"
        ref={figureRef}
      >
        <figcaption>
          {t("mermaid.renderFailed", {
            defaultValue: "Mermaid diagram could not be rendered.",
          })}
        </figcaption>
        <CodeBlockWrapper code={source} language="Mermaid">
          <pre>
            <code>{source}</code>
          </pre>
        </CodeBlockWrapper>
      </figure>
    );
  }

  return (
    <figure
      {...sourceAnchorAttributes}
      className="mermaid-diagram mermaid-diagram-loading"
      ref={figureRef}
    >
      <figcaption>
        {t("mermaid.rendering", {
          defaultValue: "Rendering Mermaid diagram…",
        })}
      </figcaption>
    </figure>
  );
}

function pickSourceAnchorAttributes(
  props: MarkdownPreProps,
  children: ReactNode,
): SourceAnchorAttributes {
  const attributes = sourceAnchorProperties(
    props as Record<string, unknown>,
    children,
  );
  const sourceLine = attributes["data-source-line"];
  const sourceEndLine = attributes["data-source-end-line"];
  const scrollSyncAnchor = attributes["data-scroll-sync-anchor"];
  return {
    ...(typeof sourceLine === "string" || typeof sourceLine === "number"
      ? { "data-source-line": sourceLine }
      : {}),
    ...(typeof sourceEndLine === "string" || typeof sourceEndLine === "number"
      ? { "data-source-end-line": sourceEndLine }
      : {}),
    ...(typeof scrollSyncAnchor === "string"
      ? { "data-scroll-sync-anchor": scrollSyncAnchor }
      : {}),
  };
}

function sourceAnchorProperties(
  props: Record<string, unknown>,
  node: ReactNode,
): Record<string, unknown> {
  if (
    props["data-source-line"] !== undefined ||
    props["data-source-end-line"] !== undefined
  ) {
    return props;
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = sourceAnchorProperties({}, child);
      if (found["data-source-line"] !== undefined) {
        return found;
      }
    }
  } else if (isValidElement<{ children?: ReactNode }>(node)) {
    return sourceAnchorProperties(
      node.props as Record<string, unknown>,
      node.props.children,
    );
  }
  return props;
}

function notifyMarkdownGeometryChange(element: HTMLElement | null) {
  element?.dispatchEvent(
    new CustomEvent(MARKDOWN_GEOMETRY_CHANGE_EVENT, { bubbles: true }),
  );
}

async function renderMermaidDiagram(source: string, diagramId: string) {
  const mermaid = (await import("mermaid")).default;
  mermaid.initialize({
    flowchart: {
      htmlLabels: false,
      nodeSpacing: 32,
      padding: 10,
      rankSpacing: 36,
      useMaxWidth: false,
    },
    htmlLabels: false,
    securityLevel: "strict",
    startOnLoad: false,
    theme: "neutral",
    themeCSS: "",
    themeVariables: {
      fontFamily: MERMAID_FONT_FAMILY,
      fontSize: MERMAID_FONT_SIZE,
    },
  });
  // Validate syntax WITHOUT touching the DOM before rendering. mermaid.render()
  // is the only thing that appends a temporary measuring node to <body>; on a
  // syntax error it renders a "Syntax error" bomb SVG into that node and can
  // leak it at the bottom of the page. Parsing first means render() is never
  // called for invalid input, so the orphan can never be created.
  // `suppressErrors` makes parse() return false (instead of throwing) on
  // invalid syntax, so the fallback path is a clean boolean check.
  const parsed = await mermaid.parse(source, { suppressErrors: true });
  if (!parsed) {
    throw new Error("Invalid Mermaid diagram");
  }
  try {
    const result = await mermaid.render(diagramId, source);
    return sanitizeMermaidSvg(result.svg);
  } finally {
    // Defense-in-depth for the rare case where parse() passes but render()
    // still throws after appending its temporary node (id `d${diagramId}`,
    // holding the `${diagramId}` svg) to <body>. A no-op on success (mermaid
    // already cleaned up) and on the invalid path above (render never ran).
    document.getElementById(`d${diagramId}`)?.remove();
    document.getElementById(diagramId)?.remove();
  }
}

function sanitizeMermaidSvg(svg: string) {
  if (typeof window === "undefined") {
    throw new Error("Mermaid rendering requires a browser DOM");
  }
  const document = new window.DOMParser().parseFromString(svg, "image/svg+xml");
  if (document.querySelector("parsererror")) {
    throw new Error("Mermaid returned invalid SVG");
  }
  const root = document.documentElement;
  if (root.localName.toLowerCase() !== "svg") {
    throw new Error("Mermaid returned a non-SVG document");
  }
  sanitizeMermaidElement(root);
  normalizeMermaidSvgDimensions(root);
  normalizeMermaidSvgTypography(root);
  applyMermaidFallbackStyles(root);
  return String(
    DOMPurify.sanitize(new window.XMLSerializer().serializeToString(root), {
      USE_PROFILES: { svg: true, svgFilters: true },
    }),
  );
}

// Mermaid's diagram-specific renderers do not agree on intrinsic sizing.
// Flowcharts respect `flowchart.useMaxWidth: false`, while sequence, state,
// class, ER, and future diagram types can still emit `width="100%"` plus an
// inline max-width. Our scroll viewport deliberately renders SVGs at their
// intrinsic size, so a percentage-only root collapses to 0 x 0 in Chromium.
// Every Mermaid renderer does provide its measured geometry in viewBox; use
// that single, type-independent source when it is well formed.
function normalizeMermaidSvgDimensions(root: Element) {
  const viewBoxTokens = root
    .getAttribute("viewBox")
    ?.trim()
    .split(/[\s,]+/);
  const viewBox = viewBoxTokens?.map(Number);
  if (
    viewBoxTokens?.length !== 4 ||
    !viewBoxTokens.every((token) => SVG_NUMBER_PATTERN.test(token)) ||
    viewBox?.length !== 4 ||
    !viewBox.every(Number.isFinite) ||
    viewBox[2] <= 0 ||
    viewBox[3] <= 0
  ) {
    throw new Error("Mermaid returned SVG without positive intrinsic geometry");
  }

  root.setAttribute("width", String(viewBox[2]));
  root.setAttribute("height", String(viewBox[3]));

  removeMermaidInlineStyleProperties(root, ["width", "max-width", "height"]);
}

function normalizeMermaidSvgTypography(root: Element) {
  setMermaidSvgTypography(root);

  for (const element of Array.from(root.querySelectorAll("text, tspan"))) {
    setMermaidSvgTypography(element);
  }
}

function setMermaidSvgTypography(element: Element) {
  const style = (element as SVGElement).style;
  const preservedFontProperties = [
    "font-style",
    "font-variant",
    "font-weight",
    "font-stretch",
    "line-height",
  ].map((property) => ({
    property,
    priority: style.getPropertyPriority(property),
    value: style.getPropertyValue(property),
  }));
  removeMermaidInlineStyleProperties(element, [
    "font",
    "font-family",
    "font-size",
  ]);
  for (const { property, priority, value } of preservedFontProperties) {
    if (value) {
      style.setProperty(property, value, priority);
    }
  }
  element.setAttribute("font-family", MERMAID_FONT_FAMILY);
  element.setAttribute("font-size", MERMAID_FONT_SIZE);
  style.setProperty("font-family", MERMAID_FONT_FAMILY);
  style.setProperty("font-size", MERMAID_FONT_SIZE);
}

function removeMermaidInlineStyleProperties(
  element: Element,
  properties: string[],
) {
  const styledElement = element as SVGElement;
  for (const property of properties) {
    styledElement.style.removeProperty(property);
  }
  if (!element.getAttribute("style")?.trim()) {
    element.removeAttribute("style");
  }
}

// Every shape Mermaid uses for a node body, not just rectangles: decision
// diamonds are polygons, circles/ellipses back circle nodes, and paths back
// cylinders and the v11 expanded shape set. Without a repaint they inherit
// the SVG default black fill once the gradient style block is stripped.
const MERMAID_NODE_SHAPE_TAGS = new Set([
  "rect",
  "polygon",
  "circle",
  "ellipse",
  "path",
]);

// Mermaid stamps the parsed diagram type on the SVG root for every family.
// The root `class` attribute is the obvious alternative and does not work:
// sequence diagrams ship no class at all.
function mermaidDiagramFamily(root: Element) {
  return root.getAttribute("aria-roledescription")?.trim().toLowerCase() ?? "";
}

// Only flowcharts centre their node labels, so only flowcharts may have the
// centring restored after their stylesheet is stripped.
//
// Measured against Mermaid 11.16.1 under this module's `htmlLabels: false`
// config. Flowchart ships `.node .label text { text-anchor: middle }` and
// places the label group at x 0, so the label is centred by the anchor.
// ER, class and state ship no such rule: they pre-shift the group left by
// half the label's width and rely on the SVG default `start`
// (`WORKSPACE_MEMBERSHIPS`, a 147px label, sits at `translate(-73.5, …)`).
// Injecting `middle` there moves every label left by a second half-width,
// which is what pushed ER table names and attribute rows off their left edge.
const MERMAID_CENTRED_LABEL_FAMILIES = new Set(["flowchart-v2", "flowchart"]);

// The marker shapes Mermaid deliberately leaves unfilled, and the tags it
// draws them with. Matched on the marker's SEMANTIC CLASS rather than its
// id: Mermaid stamps `class="marker aggregation class"` identically under
// both `look: classic` and `look: neo`, while the ids diverge (`neo` appends
// `-margin`, and it is the `-margin` ids the edges actually reference). The
// id is still checked as a second signal in case a future renderer drops the
// class, and `includes` rather than `endsWith` so it survives that suffix.
const MERMAID_HOLLOW_MARKER_WORDS = ["aggregation", "extension"];

// Every tag a marker can be drawn with. `neo` draws the inheritance triangle
// as a <polygon> where `classic` uses a <path>, so a path-only rule misses it
// entirely -- and the generic node-shape rule then paints it like a node box.
const MERMAID_MARKER_SHAPE_TAGS = new Set([
  "path",
  "polygon",
  "circle",
  "ellipse",
  "rect",
]);

// Sequence message lines (solid `messageLine0`, dotted `messageLine1`) are the
// one connector Mermaid 11.16.1 ships with `stroke="none"` as an attribute,
// leaving the colour to its `.messageLine0/1` stylesheet rule. Once that rule
// is stripped, the fill-if-missing connector rule keeps the `none`, and only
// the arrowhead markers stay visible. So these alone get their stroke
// overwritten, at the stylesheet's 1.5 width. Other `stroke="none"` attributes
// (gantt axis text, wardley markers) are intentional and stay untouched.
const MERMAID_SEQUENCE_MESSAGE_CLASSES = new Set(["messageLine0", "messageLine1"]);

function mermaidMarkerFill(marker: Element, isEntityRelationship: boolean) {
  // ER hollows out every cardinality end at once: its stylesheet overrides
  // the shared base with `.marker { fill: none !important }`.
  if (isEntityRelationship) {
    return "none";
  }
  const classes = (marker.getAttribute("class") ?? "").split(/\s+/);
  const id = marker.getAttribute("id") ?? "";
  const hollow = MERMAID_HOLLOW_MARKER_WORDS.some(
    (word) => classes.includes(word) || id.includes(word),
  );
  return hollow ? "transparent" : "#64748b";
}

function applyMermaidFallbackStyles(root: Element) {
  const family = mermaidDiagramFamily(root);
  const centresNodeLabels = MERMAID_CENTRED_LABEL_FAMILIES.has(family);
  const isEntityRelationship = family === "er";
  for (const element of Array.from(root.querySelectorAll("*"))) {
    const tagName = element.localName.toLowerCase();
    const className = element.getAttribute("class") ?? "";
    // Marker shapes first, and then skip the node rules below. A marker is not
    // a node: without the `continue` the generic shape rule claims `neo`'s
    // <polygon> inheritance triangle and paints it like a node box, and
    // whichever rule runs first wins because these all write only if the
    // attribute is absent.
    //
    // Markers are painted only by the stylesheet this sanitiser strips, so
    // without a repaint they fall back to the browser default `fill: black`.
    // "Filled" is not a safe blanket answer: in UML a hollow diamond is
    // aggregation and a filled one is composition, and a hollow triangle is
    // inheritance, so flattening them states a different model rather than
    // merely looking wrong. Mermaid's own `.marker { fill: #666 }` is the base
    // rule, so filled is the default and only the shapes it hollows out are
    // listed. An ER marker's <circle> carries its own `fill` already and keeps
    // it, since these never overwrite an attribute Mermaid set.
    const markerAncestor = MERMAID_MARKER_SHAPE_TAGS.has(tagName)
      ? element.closest("marker")
      : null;
    if (markerAncestor) {
      setSvgAttributeIfMissing(
        element,
        "fill",
        mermaidMarkerFill(markerAncestor, isEntityRelationship),
      );
      setSvgAttributeIfMissing(element, "stroke", "#64748b");
      setSvgAttributeIfMissing(element, "stroke-width", "1");
      continue;
    }
    if (tagName === "rect" && className.includes("background")) {
      setSvgAttributeIfMissing(element, "fill", "transparent");
    }
    if (
      MERMAID_NODE_SHAPE_TAGS.has(tagName) &&
      className.includes("label-container")
    ) {
      setSvgAttributeIfMissing(element, "fill", "#f8fafc");
      setSvgAttributeIfMissing(element, "stroke", "#334155");
    }
    if (
      className
        .split(/\s+/)
        .some((name) => MERMAID_SEQUENCE_MESSAGE_CLASSES.has(name))
    ) {
      element.setAttribute("stroke", "#64748b");
      element.setAttribute("stroke-width", "1.5");
    }
    if (
      ["line", "polyline"].includes(tagName) ||
      (tagName === "path" &&
        /flowchart-link|transition|relation|relationship|messageline|actor-line|edgepath/i.test(
          className,
        ))
    ) {
      setSvgAttributeIfMissing(element, "stroke", "#64748b");
      setSvgAttributeIfMissing(element, "fill", "none");
    }
    if (
      ["rect", "polygon", "circle", "ellipse"].includes(tagName) &&
      !className.includes("background")
    ) {
      setSvgAttributeIfMissing(element, "fill", "#f8fafc");
      setSvgAttributeIfMissing(element, "stroke", "#334155");
    }
    if (tagName === "path" && className.includes("arrowMarkerPath")) {
      setSvgAttributeIfMissing(element, "fill", "#64748b");
      setSvgAttributeIfMissing(element, "stroke", "#64748b");
    }
    if (tagName === "text") {
      setSvgAttributeIfMissing(element, "fill", "#0f172a");
      if (centresNodeLabels && element.closest(".node")) {
        setSvgAttributeIfMissing(element, "text-anchor", "middle");
      }
    }
  }
}

function setSvgAttributeIfMissing(element: Element, name: string, value: string) {
  if (!element.hasAttribute(name)) {
    element.setAttribute(name, value);
  }
}

function sanitizeMermaidElement(root: Element) {
  for (const element of [root, ...Array.from(root.querySelectorAll("*"))]) {
    const tagName = element.localName.toLowerCase();
    if (isForbiddenMermaidSvgElement(tagName)) {
      element.remove();
      continue;
    }
    // SVG style blocks are document-global once inserted with innerHTML. Even
    // syntactically safe CSS can escape the diagram and hide or restyle the
    // application shell (for example `body { display:none }`). Mermaid's
    // generated geometry is preserved with presentation attributes below, so
    // no authored or generated style element is allowed across this boundary.
    if (tagName === "style") {
      element.remove();
      continue;
    }
    for (const attribute of Array.from(element.attributes)) {
      if (isUnsafeMermaidSvgAttribute(attribute.name, attribute.value)) {
        element.removeAttribute(attribute.name);
      }
    }
  }
}

function isForbiddenMermaidSvgElement(tagName: string) {
  return [
    "audio",
    "animate",
    "animatemotion",
    "animatetransform",
    "canvas",
    "discard",
    "embed",
    "feimage",
    "filter",
    "foreignobject",
    "iframe",
    "image",
    "object",
    "script",
    "set",
    "use",
    "video",
  ].includes(tagName);
}

function isUnsafeMermaidSvgAttribute(name: string, value: string) {
  const normalizedName = name.toLowerCase();
  const normalizedValue = value.trim().toLowerCase();
  if (normalizedName.startsWith("on")) {
    return true;
  }
  if (normalizedName === "style" && hasUnsafeCss(normalizedValue)) {
    return true;
  }
  if (
    ["href", "xlink:href", "src"].includes(normalizedName) &&
    !isSafeMermaidSvgReference(normalizedValue)
  ) {
    return true;
  }
  return false;
}

function hasUnsafeCss(value: string) {
  return /@import|expression\s*\(|url\s*\(/i.test(value);
}

function isSafeMermaidSvgReference(value: string) {
  return value.startsWith("#");
}

type AnchorHighlight = {
  className: "anchor--exact" | "anchor--fuzzy";
  commentId: string;
  occurrenceIndex: number;
  status: "exact" | "fuzzy";
  text: string;
};

type AnchorHighlightState = AnchorHighlight & {
  rendered: boolean;
  skippedOccurrences: number;
};

function inlineCommentAnchorHighlights(
  comments: PageInlineComment[],
  body: string,
): AnchorHighlight[] {
  if (body.length > MAX_INLINE_COMMENT_HIGHLIGHT_BODY_CHARS) {
    return [];
  }
  return comments
    .slice(0, MAX_INLINE_COMMENT_HIGHLIGHTS)
    .map((comment): AnchorHighlight | null => {
      const status = comment.resolved.status;
      if (status !== "exact" && status !== "fuzzy") {
        return null;
      }
      const text = comment.resolved.resolved_text?.trim();
      if (!text) {
        return null;
      }
      return {
        className: status === "exact" ? "anchor--exact" : "anchor--fuzzy",
        commentId: comment.id,
        occurrenceIndex: resolvedOccurrenceIndex(
          body,
          text,
          comment.resolved.char_offset,
        ),
        status,
        text,
      };
    })
    .filter((highlight): highlight is AnchorHighlight => highlight !== null);
}

function rehypeInlineCommentAnchors({ highlights }: { highlights: AnchorHighlight[] }) {
  return (tree: HastParent) => {
    if (highlights.length === 0) {
      return;
    }
    highlightAnchorText(
      tree,
      highlights.map((highlight) => ({
        ...highlight,
        rendered: false,
        skippedOccurrences: 0,
      })),
    );
  };
}

function highlightAnchorText(parent: HastParent, highlights: AnchorHighlightState[]) {
  for (let index = 0; index < parent.children.length; index += 1) {
    const child = parent.children[index];
    if (isHastElement(child)) {
      if (!shouldSkipAnchorHighlight(child)) {
        highlightAnchorText(child, highlights);
      }
      continue;
    }
    if (!isHastText(child)) {
      continue;
    }
    const replacement = splitTextWithAnchorHighlights(child.value, highlights);
    if (!replacement) {
      continue;
    }
    parent.children.splice(index, 1, ...replacement);
    index += replacement.length - 1;
  }
}

function splitTextWithAnchorHighlights(
  value: string,
  highlights: AnchorHighlightState[],
): Array<HastText | HastElement> | null {
  const nodes: Array<HastText | HastElement> = [];
  let cursor = 0;

  while (cursor < value.length) {
    const match = nextAnchorHighlight(value, cursor, highlights);
    if (!match) {
      break;
    }
    if (match.index > cursor) {
      nodes.push(textNode(value.slice(cursor, match.index)));
    }
    match.highlight.rendered = true;
    nodes.push(anchorMarkNode(match.highlight));
    cursor = match.index + match.highlight.text.length;
  }

  if (nodes.length === 0) {
    return null;
  }
  if (cursor < value.length) {
    nodes.push(textNode(value.slice(cursor)));
  }
  return nodes;
}

function nextAnchorHighlight(
  value: string,
  cursor: number,
  highlights: AnchorHighlightState[],
) {
  let best: { highlight: AnchorHighlightState; index: number } | null = null;
  for (const highlight of highlights) {
    const index = nextIndexForHighlight(value, cursor, highlight);
    if (index === -1) {
      continue;
    }
    if (
      best === null ||
      index < best.index ||
      (index === best.index && highlight.text.length > best.highlight.text.length)
    ) {
      best = { highlight, index };
    }
  }
  return best;
}

function nextIndexForHighlight(
  value: string,
  cursor: number,
  highlight: AnchorHighlightState,
) {
  if (highlight.rendered) {
    return -1;
  }
  let searchFrom = cursor;
  while (searchFrom < value.length) {
    const index = value.indexOf(highlight.text, searchFrom);
    if (index === -1) {
      return -1;
    }
    if (highlight.skippedOccurrences < highlight.occurrenceIndex) {
      highlight.skippedOccurrences += 1;
      searchFrom = index + highlight.text.length;
      continue;
    }
    return index;
  }
  return -1;
}

function resolvedOccurrenceIndex(
  body: string,
  resolvedText: string,
  charOffset: number | null,
) {
  if (charOffset === null || charOffset <= 0) {
    return 0;
  }
  let occurrenceIndex = 0;
  let searchFrom = 0;
  while (searchFrom < charOffset) {
    const index = body.indexOf(resolvedText, searchFrom);
    if (index === -1 || index >= charOffset) {
      break;
    }
    occurrenceIndex += 1;
    searchFrom = index + resolvedText.length;
  }
  return occurrenceIndex;
}

function anchorMarkNode(highlight: AnchorHighlight): HastElement {
  return {
    type: "element",
    tagName: "mark",
    properties: {
      className: ["anchor", highlight.className],
      "data-anchor-status": highlight.status,
      "data-comment-id": highlight.commentId,
    },
    children: [textNode(highlight.text)],
  };
}

function isHastElement(node: HastNode): node is HastElement {
  return node.type === "element" && "children" in node && Array.isArray(node.children);
}

function isHastText(node: HastNode): node is HastText {
  return node.type === "text" && "value" in node && typeof node.value === "string";
}

function shouldSkipAnchorHighlight(element: HastElement) {
  if (["a", "code", "pre", "cite"].includes(element.tagName)) {
    return true;
  }
  const className = element.properties?.className;
  const classes = Array.isArray(className) ? className.map(String) : [];
  return classes.some((value) =>
    ["wikilink", "wikilink-broken", "cite-ref", "cite-ref-deleted", "anchor"].includes(
      value,
    ),
  );
}

// Stamp trusted source ranges only after sanitization. Raw HTML may contain
// similarly named data attributes, but it cannot opt itself into the editor's
// scroll-anchor query because the private marker is added here to top-level
// rendered blocks only.
function rehypeSourceLines() {
  return (tree: HastRoot) => {
    for (const child of tree.children) {
      if (child.type !== "element") {
        continue;
      }
      const line = child.position?.start.line;
      if (!line) {
        continue;
      }
      child.properties = {
        ...child.properties,
        "data-source-line": line,
        "data-source-end-line": child.position?.end.line ?? line,
        "data-scroll-sync-anchor": "true",
      };
    }
  };
}

// Editor split preview only: drop the leading "# Title" node when it duplicates
// the page title (the same nicety the readers get from the string-level strip),
// without shifting the source line numbers of the remaining blocks.
function dropDuplicateHeading(tree: Root, title?: string) {
  if (!title?.trim()) {
    return;
  }
  const first = tree.children[0] as { type?: string; depth?: number } | undefined;
  if (
    first &&
    first.type === "heading" &&
    first.depth === 1 &&
    normalizeHeadingText(mdastNodeText(first)) === normalizeHeadingText(title)
  ) {
    tree.children.shift();
  }
}

function mdastNodeText(node: unknown): string {
  if (node && typeof (node as { value?: unknown }).value === "string") {
    return (node as { value: string }).value;
  }
  const children = (node as { children?: unknown[] } | null)?.children;
  if (Array.isArray(children)) {
    return children.map(mdastNodeText).join("");
  }
  return "";
}

function rehypeTableAlignment() {
  return (tree: HastParent) => {
    normalizeTableAlignment(tree);
  };
}

function normalizeTableAlignment(parent: HastParent) {
  for (const child of parent.children) {
    if (!isHastElement(child)) {
      continue;
    }
    if (child.tagName === "th" || child.tagName === "td") {
      const align = safeTableAlignment(child.properties);
      if (align) {
        child.properties = { ...child.properties, align };
      }
      delete child.properties?.style;
    }
    normalizeTableAlignment(child);
  }
}

function safeTableAlignment(properties?: Properties) {
  const align = String(properties?.align ?? "").toLowerCase();
  if (isTableAlignment(align)) {
    return align;
  }
  const textAlign = String(properties?.textAlign ?? "").toLowerCase();
  if (isTableAlignment(textAlign)) {
    return textAlign;
  }
  const style = String(properties?.style ?? "").toLowerCase();
  const match = /text-align\s*:\s*(left|center|right)/.exec(style);
  return isTableAlignment(match?.[1]) ? match[1] : "";
}

function isTableAlignment(value: string | undefined): value is "left" | "center" | "right" {
  return value === "left" || value === "center" || value === "right";
}

// rehype-sanitize clobber-prefixes every id/name with CLOBBER_PREFIX but
// leaves hrefs alone, so same-page fragment links produced before it (heading
// autolinks, GFM footnotes, hand-written "#section" links) must gain the same
// prefix or they would point at ids that no longer exist after sanitizing.
// Fragments already inside the namespace (wikilink heading anchors) pass
// through untouched.
function rehypePrefixFragmentHrefs() {
  return (tree: HastParent) => {
    prefixFragmentHrefs(tree);
  };
}

function prefixFragmentHrefs(parent: HastParent) {
  for (const child of parent.children) {
    if (!isHastElement(child)) {
      continue;
    }
    const href = child.tagName === "a" ? child.properties?.href : undefined;
    if (
      typeof href === "string" &&
      href.length > 1 &&
      href.startsWith("#") &&
      !href.startsWith(`#${CLOBBER_PREFIX}`)
    ) {
      child.properties = { ...child.properties, href: `#${CLOBBER_PREFIX}${href.slice(1)}` };
    }
    prefixFragmentHrefs(child);
  }
}

// rehype-sanitize accepts attribute entries as either a string (any value) or
// `[attrName, ...allowedValues]`. When two entries for the same attribute
// appear in the list (e.g. defaultSchema's existing `["className", ...]` plus
// our custom `["className", "wikilink", ...]`) only the first wins, silently
// stripping our custom values. Merge them into a single entry so custom
// className tokens (alert-*, wikilink, anchor--exact, etc.) actually survive.
type SanitizeAttributeEntry = string | unknown[];
type SanitizeAllowedValue = string | number | boolean | RegExp | null | undefined;

function mergeAttributeAllowlists(
  base: readonly SanitizeAttributeEntry[] | undefined,
  attrName: string,
  customValues: readonly SanitizeAllowedValue[],
): SanitizeAttributeEntry[] {
  const remaining: SanitizeAttributeEntry[] = [];
  const defaultValues: SanitizeAllowedValue[] = [];
  for (const entry of base ?? []) {
    if (entry === attrName) {
      // Plain-string form means "any value allowed" — preserve that by leaving
      // defaultValues untouched (custom values are still appended below).
      continue;
    }
    if (Array.isArray(entry) && entry[0] === attrName) {
      // defaultSchema may already restrict className to specific tokens; keep
      // them by appending the remainder of the entry to our merged list.
      for (const value of entry.slice(1)) {
        defaultValues.push(value as SanitizeAllowedValue);
      }
      continue;
    }
    remaining.push(entry);
  }
  return [...remaining, [attrName, ...defaultValues, ...customValues]];
}

const CUSTOM_SHARED_CLASS_NAMES = [
  "alert-note",
  "alert-tip",
  "alert-important",
  "alert-warning",
  "alert-caution",
  "markdown-alert",
  "markdown-alert-note",
  "markdown-alert-tip",
  "markdown-alert-important",
  "markdown-alert-warning",
  "markdown-alert-caution",
  "markdown-alert-title",
  "decision-block",
  "action-block",
  "wikilink",
  "wikilink-broken",
  "inline-tag",
  "cite-ref",
  "cite-ref-deleted",
  "markdown-highlight",
  "anchor",
  "anchor--exact",
  "anchor--fuzzy",
];

const sharedAttributes = [
  ...mergeAttributeAllowlists(
    defaultSchema.attributes?.["*"],
    "className",
    CUSTOM_SHARED_CLASS_NAMES,
  ),
  "target",
  "rel",
  "data-source-line",
  "data-source-end-line",
  "dataSourceLine",
  "dataSourceEndLine",
];

const KATEX_CLASS_NAMES = [
  /^katex(?:-.+)?$/,
  /^base$/,
  /^strut$/,
  /^m(?:ord|op|bin|rel|open|close|punct|inner)$/,
  /^accent(?:-.+)?$/,
  /^accent-body$/,
  /^delimsizing(?:-.+)?$/,
  /^delimcenter$/,
  /^nulldelimiter$/,
  /^op-symbol$/,
  /^overline-line$/,
  /^sqrt(?:-.+)?$/,
  /^root$/,
  /^sizing(?:.+)?$/,
  /^fontsize-ensurer$/,
  /^reset-(?:text|script|scriptscript)-style$/,
  /^(?:text|display|script|scriptscript)style$/,
  /^vlist(?:-.+)?$/,
  /^mfrac$/,
  /^frac-line$/,
  /^thinbox$/,
  /^mspace$/,
  /^mtable$/,
  /^col-align-[clr]$/,
  /^arraycolsep$/,
  /^pstrut$/,
];

const MATHML_TAG_NAMES = [
  "annotation",
  "maction",
  "math",
  "menclose",
  "merror",
  "mfrac",
  "mi",
  "mmultiscripts",
  "mn",
  "mo",
  "mover",
  "mpadded",
  "mphantom",
  "mprescripts",
  "mroot",
  "mrow",
  "ms",
  "mspace",
  "msqrt",
  "mstyle",
  "msub",
  "msubsup",
  "msup",
  "mtable",
  "mtd",
  "mtext",
  "mtr",
  "munder",
  "munderover",
  "none",
  "semantics",
];

const KATEX_MATHML_ATTRIBUTES = [
  "className",
  "display",
  "encoding",
  "fence",
  "height",
  "href",
  "lspace",
  "mathvariant",
  "minsize",
  "movablelimits",
  "notation",
  "rspace",
  "scriptlevel",
  "stretchy",
  "symmetric",
  "width",
  "xmlns",
];

const markdownSanitizeSchema = {
  ...defaultSchema,
  clobberPrefix: CLOBBER_PREFIX,
  protocols: {
    ...defaultSchema.protocols,
    src: ["https"],
  },
  attributes: {
    ...defaultSchema.attributes,
    "*": sharedAttributes,
    a: [
      ...mergeAttributeAllowlists(
        defaultSchema.attributes?.a,
        "className",
        ["anchor", "wikilink", "wikilink-broken", "inline-tag"],
      ),
      "data-internal",
      "data-tag",
      "dataInternal",
      "dataTag",
      "target",
      "rel",
    ],
    cite: [
      ...mergeAttributeAllowlists(
        defaultSchema.attributes?.cite,
        "className",
        ["cite-ref", "cite-ref-deleted"],
      ),
      "data-source-id",
      "data-source-anchor",
      "data-source-deleted",
      "data-internal",
      "dataSourceId",
      "dataSourceAnchor",
      "dataSourceDeleted",
      "dataInternal",
    ],
    mark: [
      ...mergeAttributeAllowlists(
        defaultSchema.attributes?.mark,
        "className",
        ["anchor", "anchor--exact", "anchor--fuzzy", "markdown-highlight"],
      ),
      "data-anchor-status",
      "data-comment-id",
      "dataAnchorStatus",
      "dataCommentId",
    ],
    img: [
      ...(defaultSchema.attributes?.img ?? []),
      "data-dtx-media-state",
      "data-dtx-media-reason",
      "dataDtxMediaState",
      "dataDtxMediaReason",
    ],
    "decision-block": [
      "data-source-line",
      "data-source-end-line",
      "dataSourceLine",
      "dataSourceEndLine",
      "data-block-kind",
      "data-decision-id",
      "data-status",
      "data-owner",
      "data-due",
      "dataBlockKind",
      "dataDecisionId",
      "dataStatus",
      "dataOwner",
      "dataDue",
      ["className", "decision-block"],
    ],
    "action-block": [
      "data-source-line",
      "data-source-end-line",
      "dataSourceLine",
      "dataSourceEndLine",
      "data-block-kind",
      "data-action-id",
      "data-status",
      "data-owner",
      "data-due",
      "dataBlockKind",
      "dataActionId",
      "dataStatus",
      "dataOwner",
      "dataDue",
      ["className", "action-block"],
    ],
    code: [
      ...mergeAttributeAllowlists(
        defaultSchema.attributes?.code,
        "className",
        [/^language-/, "math-inline", "math-display"],
      ),
    ],
    span: [
      ...mergeAttributeAllowlists(
        defaultSchema.attributes?.span,
        "className",
        KATEX_CLASS_NAMES,
      ),
      "aria-hidden",
      "ariaHidden",
    ],
    math: KATEX_MATHML_ATTRIBUTES,
    annotation: ["encoding"],
    maction: KATEX_MATHML_ATTRIBUTES,
    menclose: KATEX_MATHML_ATTRIBUTES,
    merror: KATEX_MATHML_ATTRIBUTES,
    mfrac: KATEX_MATHML_ATTRIBUTES,
    mi: KATEX_MATHML_ATTRIBUTES,
    mmultiscripts: KATEX_MATHML_ATTRIBUTES,
    mn: KATEX_MATHML_ATTRIBUTES,
    mo: KATEX_MATHML_ATTRIBUTES,
    mover: KATEX_MATHML_ATTRIBUTES,
    mpadded: KATEX_MATHML_ATTRIBUTES,
    mphantom: KATEX_MATHML_ATTRIBUTES,
    mprescripts: KATEX_MATHML_ATTRIBUTES,
    mroot: KATEX_MATHML_ATTRIBUTES,
    mrow: KATEX_MATHML_ATTRIBUTES,
    ms: KATEX_MATHML_ATTRIBUTES,
    mspace: KATEX_MATHML_ATTRIBUTES,
    msqrt: KATEX_MATHML_ATTRIBUTES,
    mstyle: KATEX_MATHML_ATTRIBUTES,
    msub: KATEX_MATHML_ATTRIBUTES,
    msubsup: KATEX_MATHML_ATTRIBUTES,
    msup: KATEX_MATHML_ATTRIBUTES,
    mtable: KATEX_MATHML_ATTRIBUTES,
    mtd: KATEX_MATHML_ATTRIBUTES,
    mtext: KATEX_MATHML_ATTRIBUTES,
    mtr: KATEX_MATHML_ATTRIBUTES,
    munder: KATEX_MATHML_ATTRIBUTES,
    munderover: KATEX_MATHML_ATTRIBUTES,
    none: KATEX_MATHML_ATTRIBUTES,
    semantics: KATEX_MATHML_ATTRIBUTES,
    th: [...(defaultSchema.attributes?.th ?? []), "align"],
    td: [...(defaultSchema.attributes?.td ?? []), "align"],
    video: [
      ...(defaultSchema.attributes?.video ?? []),
      "controls",
      "poster",
      "preload",
      "src",
      "title",
      "width",
    ],
    input: [
      ...(defaultSchema.attributes?.input ?? []),
      ["type", "checkbox"],
      "checked",
      "disabled",
    ],
    h1: [...(defaultSchema.attributes?.h1 ?? []), "id"],
    h2: [...(defaultSchema.attributes?.h2 ?? []), "id"],
    h3: [...(defaultSchema.attributes?.h3 ?? []), "id"],
    h4: [...(defaultSchema.attributes?.h4 ?? []), "id"],
    h5: [...(defaultSchema.attributes?.h5 ?? []), "id"],
    h6: [...(defaultSchema.attributes?.h6 ?? []), "id"],
  },
  tagNames: [
    ...(defaultSchema.tagNames ?? []),
    "cite",
    "decision-block",
    "action-block",
    "figure",
    "figcaption",
    "video",
    "mark",
    ...MATHML_TAG_NAMES,
  ],
};
