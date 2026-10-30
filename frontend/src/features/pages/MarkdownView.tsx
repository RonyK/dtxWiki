import {
  Children,
  createContext,
  isValidElement,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useContext,
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
import { Link, useInRouterContext, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";

import "highlight.js/styles/github-dark.css";
import "katex/dist/katex.min.css";

import { classifyContextLink, formatContextPageUrl, isContextId, normalizeContextHeading, type ContextLinkTarget } from "../../shared/lib/contextLinks";
import { canPreservePageHref } from "../../shared/lib/customPageHref";
import { createReferenceReturnState } from "../../shared/lib/navigationRecovery";
import { hasConflictingWorkspaceQuery, workspacePath } from "../../shared/lib/workspaceRoutes";

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
import { UnavailableReference } from "./UnavailableReference";
import { WorkspaceLinkBadge } from "./WorkspaceLinkBadge";
import { hasUnsafeCss, resolveMermaidStylesheet } from "./mermaidStylesheet";
import { parsePageDocument } from "./pageFrontmatter";

type MarkdownViewProps = {
  body: string;
  comments?: PageInlineComment[];
  stripLeadingHeading?: string;
  wikilinkPages?: Array<{
    id?: string;
    aliases?: string[];
    slug: string;
    title: string;
  }>;
  linkTargets?: ContextLinkTarget[];
  wikilinkTargets?: Array<{
    target_slug: string;
    target_page_id: string | null;
    current_slug: string | null;
  }>;
  // Authenticated readers use scoped permanent links. Public readers use only
  // public URLs confirmed in linkTargets; every other internal link is text.
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
  renderPolicy?: "page" | "untrusted-preview" | "confirmed-links";
  // The source document/editor workspace supplies authority for short links.
  // Never infer it from whichever workspace the reader most recently selected.
  workspaceId?: string | null;
  // Trusted authored previews/readers may activate only syntactically valid,
  // explicit workspace routes; public readers and untrusted previews stay closed.
  allowWorkspaceNavigationLinks?: boolean;
};

type DeletedSourceMark = {
  citationKey: string;
  label: string;
};

export const MARKDOWN_GEOMETRY_CHANGE_EVENT =
  "dtxwiki:markdown-geometry-change";

type MarkdownAnchorProps = ComponentPropsWithoutRef<"a"> & {
  "data-internal"?: string;
  "data-dtx-unavailable-reference"?: string;
  node?: unknown;
};

type MarkdownAuthoredReference = { locator: string; wikilink: boolean };

type MarkdownLinkBehavior = {
  sourceWorkspaceId: string | null;
  authenticatedAuthored: boolean;
  infoMode: "ask" | "public" | null;
};

type MarkdownRouteState = {
  sourceHref: string;
};

const MarkdownLinkBehaviorContext = createContext<MarkdownLinkBehavior | null>(null);
const MarkdownRouteStateContext = createContext<MarkdownRouteState | null>(null);

function MarkdownRouteStateProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const value = useMemo(() => ({
    sourceHref: `${location.pathname}${location.search}${location.hash}`,
  }), [location.hash, location.pathname, location.search]);
  return (
    <MarkdownRouteStateContext.Provider value={value}>
      {children}
    </MarkdownRouteStateContext.Provider>
  );
}

const MAX_INLINE_TOKEN_TRANSFORM_DEPTH = 128;
// get_index's system namespace; other dtxwiki:// hrefs stay authored links.
const SYSTEM_INDEX_REF_PREFIX = "dtxwiki://index/";
const MAX_INLINE_COMMENT_HIGHLIGHTS = 50;
const MAX_INLINE_COMMENT_HIGHLIGHT_BODY_CHARS = 100_000;
const MERMAID_LANGUAGE = "mermaid";
const MERMAID_FONT_FAMILY =
  'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
const MERMAID_FONT_SIZE = "13px";
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const SVG_NUMBER_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
// The shared anchor namespace. rehype-sanitize clobber-prefixes every id/name
// with this so user-authored markup can never claim (clobber) the app's own
// anchor/DOM namespace; rehypePrefixFragmentHrefs keeps same-page fragment
// links pointing at the prefixed ids.
const CLOBBER_PREFIX = "user-content-";
const TRUSTED_REFERENCE_NODE = "dtxTrustedReference";
const TRUSTED_INLINE_TAG_NODE = "dtxTrustedInlineTag";
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
  linkTargets,
  wikilinkMode = "internal",
  sourceLineNumbers = false,
  deletedSources = [],
  resolveContextMediaAsset,
  renderPolicy = "page",
  workspaceId,
  allowWorkspaceNavigationLinks = false,
}: MarkdownViewProps) {
  const articleRef = useRef<HTMLElement | null>(null);
  const inRouter = useInRouterContext();
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
      resolveDeletedSource: buildDeletedSourceResolver(deletedSources),
      sourceLineNumbers: pageSourceLineNumbers,
      workspaceId,
    }),
    [
      wikilinkPages,
      wikilinkTargets,
      deletedSources,
      pageSourceLineNumbers,
      workspaceId,
    ],
  );
  const contextLinkPluginOptions = useMemo(() => ({
    targets: linkTargets,
    workspaceId,
    publicReader: wikilinkMode === "plain",
    confirmedOnly: renderPolicy === "confirmed-links",
    allowWorkspaceNavigation:
      renderPolicy === "page" && wikilinkMode === "internal" && allowWorkspaceNavigationLinks,
  }), [linkTargets, workspaceId, wikilinkMode, renderPolicy, allowWorkspaceNavigationLinks]);
  const contextMediaPluginOptions = useMemo(
    () => ({ resolveAssetUrl: resolveContextMediaAsset }),
    [resolveContextMediaAsset],
  );
  useLayoutEffect(() => {
    if (pageSourceLineNumbers) {
      notifyMarkdownGeometryChange(articleRef.current);
    }
  }, [markdownBody, pageSourceLineNumbers]);

  const linkBehavior = useMemo<MarkdownLinkBehavior>(() => ({
    sourceWorkspaceId: workspaceId ?? null,
    authenticatedAuthored: renderPolicy === "page" && wikilinkMode === "internal",
    infoMode: renderPolicy === "confirmed-links"
      ? "ask"
      : renderPolicy === "page" && wikilinkMode === "plain"
        ? "public"
        : null,
  }), [workspaceId, renderPolicy, wikilinkMode]);
  const markdownContent = useMemo(() => (
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
              a: renderPolicy === "confirmed-links" ? MarkdownLink : UntrustedPreviewLink,
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
              remarkMarkMarkdownReferences,
              remarkMath,
              remarkGithubBlockquoteAlert,
              [remarkDtxTokens, tokenPluginOptions],
              ...(pageSourceLineNumbers
                ? [() => (tree: Root) => dropDuplicateHeading(tree, stripLeadingHeading)]
                : []),
            ]
          : renderPolicy === "confirmed-links"
            ? [remarkGfm, remarkMarkMarkdownReferences]
            : [remarkGfm]
      }
      rehypePlugins={
        renderPolicy === "page"
          ? [
              rehypeWrapRegisteredReferences,
              [rehypeRaw, { passThrough: [TRUSTED_REFERENCE_NODE, TRUSTED_INLINE_TAG_NODE] }],
              [rehypeResolveContextLinks, contextLinkPluginOptions],
              [rehypeResolveContextMedia, contextMediaPluginOptions],
              rehypeConstrainPageImageSources,
              rehypeSlug,
              [rehypeInlineCommentAnchors, anchorPluginOptions],
              [rehypeAutolinkHeadings, { behavior: "wrap" }],
              rehypePrefixFragmentHrefs,
              rehypeTableAlignment,
              rehypePreservePageAnchorHrefs,
              [rehypeSanitize, markdownSanitizeSchema],
              rehypeRestorePageAnchorHrefs,
              // Sanitize authored Markdown before KaTeX emits its trusted
              // layout spans. KaTeX needs generated styles to render fractions.
              rehypeKatex,
              [rehypeHighlight, {
                detect: true,
                ignoreMissing: true,
                languages: HIGHLIGHT_LANGUAGES,
              }],
              ...(pageSourceLineNumbers ? [rehypeSourceLines] : []),
            ]
          : renderPolicy === "confirmed-links"
            ? [
                rehypeWrapRegisteredReferences,
                [rehypeResolveContextLinks, contextLinkPluginOptions],
                [rehypeSanitize, markdownSanitizeSchema],
              ]
            : [rehypeTableAlignment, [rehypeSanitize, markdownSanitizeSchema]]
      }
      remarkRehypeOptions={REMARK_REHYPE_OPTIONS}
      skipHtml={renderPolicy !== "page"}
      urlTransform={
        renderPolicy === "untrusted-preview"
          ? untrustedPreviewUrlTransform
          : renderPolicy === "confirmed-links"
            ? confirmedPreviewUrlTransform
            : pageUrlTransform
      }
    >
      {markdownBody}
    </ReactMarkdown>
  ), [
    markdownBody,
    renderPolicy,
    tokenPluginOptions,
    pageSourceLineNumbers,
    stripLeadingHeading,
    contextLinkPluginOptions,
    contextMediaPluginOptions,
    anchorPluginOptions,
  ]);

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
      <MarkdownLinkBehaviorContext.Provider value={linkBehavior}>
        {inRouter ? (
          <MarkdownRouteStateProvider>{markdownContent}</MarkdownRouteStateProvider>
        ) : markdownContent}
      </MarkdownLinkBehaviorContext.Provider>
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

function confirmedPreviewUrlTransform(value: string, key: string, node: Readonly<HastElement>) {
  return node.tagName === "a" && key === "href" && value.startsWith("/") && !value.startsWith("//")
    ? value
    : undefined;
}

function pageUrlTransform(
  value: string,
  key: string,
  node: Readonly<HastElement>,
) {
  if (node.tagName === "a" && key === "href") {
    return canPreservePageHref(value) ? value : undefined;
  }
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

// Preserve authored hrefs without widening sanitizer protocols
// for images, MathML or other URL-bearing elements. Raw HTML attributes cannot
// create node.data; only this transform can carry an href past sanitization.
function transformPageAnchorHrefs(tree: HastRoot, restore: boolean) {
  function visit(parent: HastParent) {
    for (const child of parent.children) {
      if (!isHastElement(child)) continue;
      visit(child);
      if (child.tagName !== "a") continue;
      if (restore) {
        const data = child.data as Record<string, unknown> | undefined;
        const original = data?.dtxPreservedPageHref;
        if (typeof original === "string" && canPreservePageHref(original)) {
          child.properties.href = original;
        }
        if (data) delete data.dtxPreservedPageHref;
      } else {
        const href = child.properties.href;
        if (typeof href === "string" && canPreservePageHref(href)) {
          child.data = Object.assign({}, child.data, { dtxPreservedPageHref: href });
          child.properties.href = "/";
        }
      }
    }
  }
  visit(tree);
}

function rehypePreservePageAnchorHrefs() {
  return (tree: HastRoot) => transformPageAnchorHrefs(tree, false);
}

function rehypeRestorePageAnchorHrefs() {
  return (tree: HastRoot) => transformPageAnchorHrefs(tree, true);
}

type ContextLinkResolverOptions = {
  targets?: ContextLinkTarget[];
  workspaceId?: string | null;
  publicReader: boolean;
  confirmedOnly?: boolean;
  allowWorkspaceNavigation?: boolean;
};

function rehypeWrapRegisteredReferences() {
  return (tree: HastRoot) => {
    const visit = (parent: HastParent) => {
      parent.children = parent.children.flatMap((child): HastNode[] => {
        if (!isHastElement(child)) return [child];
        // Inline tag links are generated from the app's `#tag` syntax. Preserve
        // their provenance through rehypeRaw without treating an authored raw
        // HTML `data-tag` attribute as proof of provenance.
        const className = child.properties.className;
        if (
          child.tagName === "a" &&
          Array.isArray(className) &&
          className.includes("inline-tag") &&
          typeof (child.properties.dataTag ?? child.properties["data-tag"]) === "string"
        ) {
          return [{
            type: TRUSTED_INLINE_TAG_NODE,
            children: [child],
          } as unknown as HastNode];
        }
        const rawLocator = child.properties.dataDtxReferenceLocator ??
          child.properties["data-dtx-reference-locator"];
        if (child.tagName === "a" && typeof rawLocator === "string") {
          const wikilink = child.properties.dataDtxReferenceWikilink ??
            child.properties["data-dtx-reference-wikilink"];
          delete child.properties.dataDtxReferenceLocator;
          delete child.properties["data-dtx-reference-locator"];
          delete child.properties.dataDtxReferenceWikilink;
          delete child.properties["data-dtx-reference-wikilink"];
          return [{
            type: TRUSTED_REFERENCE_NODE,
            data: { reference: { locator: rawLocator, wikilink: wikilink === "true" } },
            children: [child],
          } as unknown as HastNode];
        }
        visit(child);
        return [child];
      }) as HastParent["children"];
    };
    visit(tree);
  };
}

function readMarkdownAuthoredReference(value: unknown): MarkdownAuthoredReference | null {
  if (!value || typeof value !== "object") return null;
  const reference = value as Record<string, unknown>;
  return typeof reference.locator === "string" && typeof reference.wikilink === "boolean"
    ? { locator: reference.locator, wikilink: reference.wikilink }
    : null;
}

function readMarkdownReferenceFromNode(node: unknown): MarkdownAuthoredReference | null {
  if (!node || typeof node !== "object") return null;
  const data = (node as { data?: unknown }).data;
  if (!data || typeof data !== "object") return null;
  return readMarkdownAuthoredReference(
    (data as Record<string, unknown>).dtxUnavailableReference,
  );
}

function rehypeResolveContextLinks(options: ContextLinkResolverOptions) {
  const targets = new Map((options.targets ?? []).map((target) => [target.locator, target]));
  const withFragmentPrefix = (href: string) => {
    const index = href.indexOf("#");
    if (index < 0 || index === href.length - 1 || href.slice(index + 1).startsWith(CLOBBER_PREFIX)) return href;
    return `${href.slice(0, index + 1)}${CLOBBER_PREFIX}${href.slice(index + 1)}`;
  };
  function resolveAnchor(child: HastElement, reference: MarkdownAuthoredReference | null): HastNode[] {
      const props = child.properties;
      // Raw HTML may claim these marker names, but it never passes through the
      // private HAST wrapper that carries Markdown-authored reference data.
      delete props.dataDtxReferenceLocator;
      delete props["data-dtx-reference-locator"];
      delete props.dataDtxReferenceToken;
      delete props["data-dtx-reference-token"];
      delete props.dataDtxReferenceWikilink;
      delete props["data-dtx-reference-wikilink"];
      delete props.dataDtxUnavailableReference;
      delete props["data-dtx-unavailable-reference"];
      delete props.ariaDisabled;
      delete props["aria-disabled"];
      const href = typeof props.href === "string" ? props.href : "";
      const locator = reference?.locator ?? href;
      const target = targets.get(locator);
      const unavailable = (): HastNode[] => {
        if (!reference) return child.children;
        delete props.href;
        delete props.title;
        delete props.ariaDisabled;
        delete props["aria-disabled"];
        delete props.dataInternalPageId;
        delete props["data-internal-page-id"];
        delete props.dataInternal;
        delete props["data-internal"];
        delete props.dataContextLinkLocator;
        delete props["data-context-link-locator"];
        props.className = [];
        props["data-dtx-unavailable-reference"] = "true";
        child.data = Object.assign({}, child.data, {
          dtxUnavailableReference: reference,
        }) as typeof child.data;
        return [child];
      };
      if (options.confirmedOnly) {
        if (!target || target.state !== "ok" || !target.href || !target.href.startsWith("/") || target.href.startsWith("//")) return unavailable();
        props.href = withFragmentPrefix(target.href);
        return [child];
      }
      const classification = classifyContextLink(locator, {
        workspaceId: options.workspaceId,
        allowWorkspaceNavigation: true,
      });
      const wikilink = reference?.wikilink === true;
      // An unrecognized href belongs to its author. Only a positively known
      // app locator or a server-resolved target can acquire internal policy.
      // A system index ref is known but never a browser URL: without a server
      // target it stays an unavailable reference.
      const internal = target || wikilink || locator.startsWith(SYSTEM_INDEX_REF_PREFIX) || [
        "page_id", "stable_full", "stable_short", "canonical_route",
        "nav_target", "concept_path", "slug", "resource_uri", "workspace_navigation",
      ].includes(classification.kind);
      if (!internal) return [child];
      delete props.dataContextLinkLocator;
      delete props["data-context-link-locator"];
      const legacyPageId = props.dataInternalPageId ?? props["data-internal-page-id"];
      const resolvedHref = target?.state === "ok" ? target.href
        : target ? null
        : wikilink && classification.lookup === "" && href.startsWith("#") && href !== "#" ? href
        : options.publicReader ? null
        : classification.kind === "workspace_navigation" && !options.allowWorkspaceNavigation ? null
        : classification.stableUrl ?? (wikilink && legacyPageId && href !== "#" ? href : null);
      if (options.publicReader && !resolvedHref) return unavailable();
      if (resolvedHref) {
        props.href = withFragmentPrefix(resolvedHref);
        if (wikilink) props.className = ["wikilink"];
        const pageId = target?.target_page_id ?? classification.pageId ?? legacyPageId;
        if (!options.publicReader && pageId) props.dataInternalPageId = pageId;
        if (options.publicReader) {
          delete props.dataInternalPageId;
          delete props["data-internal-page-id"];
        }
        return [child];
      }
      return unavailable();
  }
  function resolve(parent: HastParent) {
    parent.children = parent.children.flatMap((child): HastNode[] => {
      if ((child as { type: string }).type === TRUSTED_INLINE_TAG_NODE) {
        const trustedNode = child as unknown as { children?: HastNode[] };
        const anchor = trustedNode.children?.[0];
        if (!anchor || !isHastElement(anchor) || anchor.tagName !== "a") {
          return trustedNode.children ?? [];
        }
        return [anchor];
      }
      if ((child as { type: string }).type === TRUSTED_REFERENCE_NODE) {
        const trustedNode = child as unknown as {
          data?: { reference?: unknown };
          children?: HastNode[];
        };
        const anchor = trustedNode.children?.[0];
        if (!anchor || !isHastElement(anchor) || anchor.tagName !== "a") {
          return trustedNode.children ?? [];
        }
        const reference = readMarkdownAuthoredReference(trustedNode.data?.reference);
        resolve(anchor);
        const recoveredSiblings = (trustedNode.children?.slice(1) ?? []).flatMap((sibling): HastNode[] => {
          if (!isHastElement(sibling)) return [sibling];
          resolve(sibling);
          return sibling.tagName === "a" ? resolveAnchor(sibling, null) : [sibling];
        });
        return [
          ...resolveAnchor(anchor, reference),
          ...recoveredSiblings,
        ];
      }
      if (!isHastElement(child)) return [child];
      resolve(child);
      if (child.tagName !== "a") return [child];
      return resolveAnchor(child, null);
    }) as HastParent["children"];
  }
  return (tree: HastRoot) => resolve(tree);
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

function remarkMarkMarkdownReferences() {
  return (tree: Root) => {
    const definitions = new Map<string, string>();
    const visit = (node: unknown, callback: (node: Record<string, unknown>) => void) => {
      if (!node || typeof node !== "object") return;
      const value = node as Record<string, unknown>;
      callback(value);
      if (Array.isArray(value.children)) {
        value.children.forEach((child) => visit(child, callback));
      }
    };
    visit(tree, (node) => {
      if (node.type === "definition" && typeof node.identifier === "string" && typeof node.url === "string") {
        definitions.set(node.identifier, node.url);
      }
    });
    visit(tree, (node) => {
      if (node.type !== "link" && node.type !== "linkReference") return;
      const locator = node.type === "link"
        ? node.url
        : typeof node.identifier === "string" ? definitions.get(node.identifier) : undefined;
      if (typeof locator !== "string") return;
      const priorData = node.data && typeof node.data === "object"
        ? node.data as Record<string, unknown>
        : {};
      const priorProperties = priorData.hProperties && typeof priorData.hProperties === "object"
        ? priorData.hProperties as Record<string, unknown>
        : {};
      node.data = {
        ...priorData,
        hProperties: {
          ...priorProperties,
          "data-dtx-reference-locator": locator,
          "data-dtx-reference-wikilink": "false",
        },
      };
    });
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
      nodes.push(wikilinkNode(match[1], match[2], options, match[0]));
    } else if (match[3]) {
      nodes.push(citationNode(match[3], options));
    } else if (match[4]) {
      nodes.push(highlightNode(match[4]));
    } else if (match[6]) {
      nodes.push(inlineTagNode(match[6], options.workspaceId));
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

function wikilinkNode(rawTarget: string, alias: string | undefined, options: DtxTokenOptions, locator: string): DtxInline {
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
        "data-context-link-locator": locator,
        "data-dtx-reference-locator": locator,
        "data-dtx-reference-wikilink": "true",
        ...(resolution.pageId ? { "data-internal-page-id": resolution.pageId } : {}),
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
  const pageIdBySlug = new Map(pages.filter((page) => page.id).map((page) => [page.slug, page.id!]));
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
      return { pageId: pageIdBySlug.get(slugCandidate) ?? null, slug: slugCandidate };
    }
    const slug = aliasToSlug.get(wikilinkAliasKey(rawTarget)) ?? slugCandidate;
    return { pageId: pageIdBySlug.get(slug) ?? null, slug };
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
    if (workspaceId && isContextId(workspaceId) && isContextId(pageId)) {
      return formatContextPageUrl(workspaceId, pageId, hash);
    }
    return "#";
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

function inlineTagNode(rawTag: string, workspaceId?: string | null): DtxInline {
  const safeTag = sanitizeInlineTag(rawTag);
  const validWorkspaceId = workspaceId && isContextId(workspaceId) ? workspaceId : null;
  return {
    type: "dtxInlineTag",
    data: {
      hName: validWorkspaceId ? "a" : "mark",
      hProperties: {
        ...(validWorkspaceId ? { href: `${workspacePath(validWorkspaceId, "search")}?q=${encodeURIComponent(`#${safeTag}`)}` } : {}),
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
  return normalizeContextHeading(value);
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
  "data-dtx-unavailable-reference": unavailableToken,
  node,
  ...props
}: MarkdownAnchorProps) {
  const { t } = useTranslation("pages");
  const behavior = useContext(MarkdownLinkBehaviorContext);
  const routeState = useContext(MarkdownRouteStateContext);
  const unavailableReference = unavailableToken === "true"
    ? readMarkdownReferenceFromNode(node)
    : null;
  if (unavailableReference) {
    const unavailableTrigger = (linkChildren: ReactNode) => (
      <UnavailableReference
        infoMode={behavior?.infoMode ?? "public"}
        infoOnly={!behavior?.authenticatedAuthored}
        reference={behavior?.authenticatedAuthored ? unavailableReference.locator : null}
        wikilink={unavailableReference.wikilink}
      >
        {linkChildren}
      </UnavailableReference>
    );
    return containsMarkdownImage(children)
      ? <MarkdownLinkedContent renderLink={unavailableTrigger}>{children}</MarkdownLinkedContent>
      : unavailableTrigger(children);
  }
  const className = props.className;
  const accessibleProps = {
    ...props,
    "aria-label": props["aria-label"] ?? linkedImageFallbackLabel(children, t),
  };
  const renderLink = (linkChildren: ReactNode) => {
    if (href == null) {
      return <a {...accessibleProps} className={className}>{linkChildren}</a>;
    }
    if (href.startsWith("#")) {
      return (
        <a {...accessibleProps} className={className} href={href}>
          {linkChildren}
        </a>
      );
    }
    const navigationHref = behavior?.authenticatedAuthored && behavior.sourceWorkspaceId
      ? referenceNavigationHref(href, behavior.sourceWorkspaceId)
      : null;
    const renderedHref = navigationHref ?? href;
    const knownNavigation = classifyContextLink(renderedHref, {
      workspaceId: behavior?.sourceWorkspaceId,
      allowWorkspaceNavigation: true,
    });
    if (renderedHref.startsWith("/") && !renderedHref.startsWith("//") &&
        (navigationHref || ["stable_full", "stable_short", "canonical_route", "workspace_navigation", "public_reader"].includes(knownNavigation.kind))) {
      const returnState = navigationHref && routeState
        ? createReferenceReturnState(routeState.sourceHref)
        : undefined;
      return (
        <Link to={renderedHref} state={returnState} {...accessibleProps} className={className}>
          {linkChildren}
          {behavior?.authenticatedAuthored ? (
            <WorkspaceLinkBadge href={renderedHref} sourceWorkspaceId={behavior.sourceWorkspaceId} />
          ) : null}
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

function referenceNavigationHref(href: string, workspaceId: string): string | null {
  if (/[\\\u0000-\u0020\u007f]/.test(href) || href !== href.trim() || href.startsWith("//")) return null;
  const options = {
    workspaceId,
    allowWorkspaceNavigation: true,
  };
  let classification = classifyContextLink(href, options);
  const hashAt = href.indexOf("#");
  const queryAt = href.indexOf("?");
  if (classification.kind === "unknown" && queryAt >= 0 && (hashAt < 0 || queryAt < hashAt)) {
    const query = href.slice(queryAt, hashAt < 0 ? undefined : hashAt);
    if (!hasConflictingWorkspaceQuery(query)) {
      const withoutQuery = href.slice(0, queryAt) + (hashAt < 0 ? "" : href.slice(hashAt));
      const route = classifyContextLink(withoutQuery, options);
      if (route.kind === "stable_full") classification = route;
    }
  }
  const recognized = classification.kind === "workspace_navigation" ||
    classification.kind === "canonical_route" ||
    classification.kind === "stable_full";
  if (!recognized) return null;
  // Classify the authored path before stripping the origin: URL.pathname
  // would silently normalize dot segments that must remain rejected.
  return /^https?:\/\//i.test(href)
    ? /^https?:\/\/[^/?#]*(.*)$/i.exec(href)?.[1] || "/"
    : href;
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
            className="zoom-viewer-panel"
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
    // Renderers with their own typeface settings (sequence actors, notes and
    // messages) take this one, so every label is measured in the face it is
    // shown in.
    fontFamily: MERMAID_FONT_FAMILY,
    htmlLabels: false,
    securityLevel: "strict",
    startOnLoad: false,
    theme: "neutral",
    themeCSS: "",
    themeVariables: {
      fontFamily: MERMAID_FONT_FAMILY,
      fontSize: MERMAID_FONT_SIZE,
      // Notes stay the yellow they have always been here, in every diagram
      // type; the neutral theme would draw them dark grey with white text.
      noteBkgColor: "#EDF2AE",
      noteBorderColor: "#666",
      noteTextColor: "#333",
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
  // Render into a container we own. Mermaid declares the xlink namespace only
  // on that path, and C4 writes `xlink:href`: without the declaration the
  // strict XML parse in sanitizeMermaidSvg rejects the whole diagram. The
  // container spans the page because renderers such as gantt size themselves
  // against their parent's width; it stays hidden while Mermaid measures.
  const container = document.createElement("div");
  container.setAttribute("aria-hidden", "true");
  container.style.cssText =
    "position:absolute;top:0;left:0;width:100%;visibility:hidden;pointer-events:none";
  document.body.append(container);
  try {
    const result = await mermaid.render(diagramId, source, container);
    return sanitizeMermaidSvg(result.svg);
  } finally {
    // Defense-in-depth for the rare case where parse() passes but render()
    // still throws after appending its temporary node (id `d${diagramId}`,
    // holding the `${diagramId}` svg). A no-op on success (mermaid already
    // cleaned up) and on the invalid path above (render never ran).
    container.remove();
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
  resolveMermaidStylesheet(root);
  sanitizeMermaidElement(root);
  normalizeMermaidSvgDimensions(root);
  constrainMermaidSequenceNumbers(root);
  return String(
    DOMPurify.sanitize(new window.XMLSerializer().serializeToString(root), {
      USE_PROFILES: { svg: true, svgFilters: true },
    }),
  );
}

function constrainMermaidSequenceNumbers(root: Element) {
  if (root.getAttribute("aria-roledescription")?.trim().toLowerCase() !== "sequence") {
    return;
  }
  // Native long numbers can exceed the 24px badge even at Mermaid's smallest
  // font size. Leave 2px inside each edge while keeping its adaptive sizes.
  for (const number of Array.from(root.querySelectorAll("text.sequenceNumber"))) {
    if ((number.textContent?.length ?? 0) <= 3) {
      continue;
    }
    if (!number.hasAttribute("textLength")) {
      number.setAttribute("textLength", "20");
    }
    if (!number.hasAttribute("lengthAdjust")) {
      number.setAttribute("lengthAdjust", "spacingAndGlyphs");
    }
  }
}

// Mermaid's diagram-specific renderers do not agree on intrinsic sizing.
// Flowcharts respect `flowchart.useMaxWidth: false`, while sequence, state,
// class, ER, and future diagram types can still emit `width="100%"` plus an
// inline max-width. Our scroll viewport deliberately renders SVGs at their
// intrinsic size, so a percentage-only root collapses to 0 x 0 in Chromium.
// Renderers provide their measured geometry in viewBox; use that single,
// type-independent source when it is well formed. A renderer that omits it
// (`info` sizes itself with only `width="100%"`) is measured instead.
function normalizeMermaidSvgDimensions(root: Element) {
  let viewBox = parseMermaidViewBox(root.getAttribute("viewBox"));
  if (!viewBox && !root.hasAttribute("viewBox")) {
    viewBox = measureMermaidSvgContent(root);
    if (viewBox) {
      root.setAttribute("viewBox", viewBox.join(" "));
    }
  }
  if (!viewBox) {
    throw new Error("Mermaid returned SVG without positive intrinsic geometry");
  }

  root.setAttribute("width", String(viewBox[2]));
  root.setAttribute("height", String(viewBox[3]));

  removeMermaidInlineStyleProperties(root, ["width", "max-width", "height"]);
}

function parseMermaidViewBox(value: string | null) {
  const tokens = value?.trim().split(/[\s,]+/);
  if (
    tokens?.length !== 4 ||
    !tokens.every((token) => SVG_NUMBER_PATTERN.test(token))
  ) {
    return null;
  }
  const viewBox = tokens.map(Number);
  return viewBox.every(Number.isFinite) && viewBox[2] > 0 && viewBox[3] > 0
    ? viewBox
    : null;
}

// Mermaid's own margin around a diagram's content.
const MERMAID_MEASURED_PADDING = 8;

// Measure the content of an SVG that carries no viewBox. Only markup that has
// already been through DOMPurify is mounted, hidden, for one getBBox() call.
function measureMermaidSvgContent(root: Element) {
  const fragment = DOMPurify.sanitize(
    new window.XMLSerializer().serializeToString(root),
    { RETURN_DOM_FRAGMENT: true, USE_PROFILES: { svg: true, svgFilters: true } },
  );
  const probe = fragment.firstElementChild;
  if (!(probe instanceof window.SVGSVGElement)) {
    return null;
  }
  const host = window.document.createElement("div");
  host.setAttribute("aria-hidden", "true");
  host.style.cssText =
    "position:absolute;top:0;left:0;visibility:hidden;pointer-events:none";
  host.append(probe);
  window.document.body.append(host);
  try {
    const box = probe.getBBox();
    if (!(box.width > 0 && box.height > 0)) {
      return null;
    }
    // getBBox() leaves out strokes: widen Mermaid's margin by half the widest
    // one painted.
    const padding = MERMAID_MEASURED_PADDING + Math.max(
      0,
      ...Array.from(probe.querySelectorAll("*"), (element) =>
        Number.parseFloat(window.getComputedStyle(element).strokeWidth) / 2 || 0),
    );
    return parseMermaidViewBox(
      [
        box.x - padding,
        box.y - padding,
        box.width + 2 * padding,
        box.height + 2 * padding,
      ].join(" "),
    );
  } finally {
    host.remove();
  }
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

function sanitizeMermaidElement(root: Element) {
  for (const element of [root, ...Array.from(root.querySelectorAll("*"))]) {
    const tagName = element.localName.toLowerCase();
    if (isForbiddenMermaidSvgElement(tagName)) {
      if (tagName === "foreignobject") {
        carryMermaidLabelColour(element);
      }
      element.remove();
      continue;
    }
    // SVG style blocks are document-global once inserted with innerHTML. Even
    // syntactically safe CSS can escape the diagram and hide or restyle the
    // application shell (for example `body { display:none }`). What Mermaid's
    // stylesheet drew was resolved onto the elements already, so no authored
    // or generated style element is allowed across this boundary.
    if (tagName === "style") {
      element.remove();
      continue;
    }
    // A raster image embedded in the SVG itself (C4's person icon) loads
    // nothing and runs nothing. An image with any other source goes.
    if (tagName === "image" && !hasOnlyEmbeddedMermaidRaster(element)) {
      element.remove();
      continue;
    }
    for (const attribute of Array.from(element.attributes)) {
      if (isUnsafeMermaidSvgAttribute(attribute.name, attribute.value, root.ownerDocument, tagName)) {
        element.removeAttribute(attribute.name);
      }
    }
  }
}

// A <switch> shows only its first child the browser can render. With the HTML
// label removed, Mermaid's SVG <text> fallback shows instead, so it takes the
// label's text colour: journey sections give that fallback the box's own
// class, which would paint it in the box's fill. The fallback lines move into
// one group so every line shows, not just the first.
function carryMermaidLabelColour(foreignObject: Element) {
  const parent = foreignObject.parentElement;
  if (parent?.localName.toLowerCase() !== "switch") {
    return;
  }
  const label = Array.from(foreignObject.querySelectorAll("*"))
    .reverse()
    .find((element) => (element as HTMLElement).style?.getPropertyValue("color"));
  const lines = Array.from(parent.children).filter(
    (child) => child.localName.toLowerCase() === "text",
  );
  const document = parent.ownerDocument;
  const colour = (label as HTMLElement | undefined)?.style.getPropertyValue("color") ?? "";
  // The label was never validated: it was about to be removed.
  if (!label || lines.length === 0 || hasUnsafeCss(colour, document)) {
    return;
  }
  // The colour may use custom properties declared inside the label. Each
  // level that declares some becomes a nested group, so every var() resolves
  // at the level it did before.
  const outer = document.createElementNS(SVG_NAMESPACE, "g");
  let inner = outer;
  const levels: Element[] = [];
  for (let element: Element | null = label; element && element !== parent; element = element.parentElement) {
    levels.unshift(element);
  }
  for (const level of levels) {
    const source = (level as HTMLElement).style;
    const properties = Array.from(source ?? []).filter((property) =>
      property.startsWith("--") && !hasUnsafeCss(source.getPropertyValue(property), document));
    if (properties.length === 0) {
      continue;
    }
    const group = document.createElementNS(SVG_NAMESPACE, "g");
    for (const property of properties) {
      group.style.setProperty(property, source.getPropertyValue(property));
    }
    inner.append(group);
    inner = group;
  }
  parent.insertBefore(outer, lines[0]);
  for (const line of lines) {
    (line as SVGElement).style.setProperty("fill", colour);
    inner.append(line);
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
    "object",
    "script",
    "set",
    "use",
    "video",
  ].includes(tagName);
}

function isUnsafeMermaidSvgAttribute(
  name: string,
  value: string,
  owner: Document,
  tagName: string,
) {
  const normalizedName = name.toLowerCase();
  const normalizedValue = value.trim().toLowerCase();
  if (normalizedName.startsWith("on")) {
    return true;
  }
  // Every presentation attribute is parsed as CSS, not only `style`.
  if (
    (normalizedName === "style" || /\\|url\s*\(/i.test(normalizedValue)) &&
    hasUnsafeCss(value.trim(), owner)
  ) {
    return true;
  }
  if (
    ["href", "xlink:href", "src"].includes(normalizedName) &&
    !isSafeMermaidSvgReference(normalizedValue) &&
    !(tagName === "image" && isEmbeddedMermaidRaster(value))
  ) {
    return true;
  }
  return false;
}

function isSafeMermaidSvgReference(value: string) {
  return value.startsWith("#");
}

// PNG, JPEG, GIF or WebP data only: SVG data could carry its own references.
const MERMAID_EMBEDDED_RASTER = /^data:image\/(?:png|jpeg|gif|webp);base64,[a-z0-9+/]+={0,2}$/i;
// Far above C4's 3 KB icon; bounds what one diagram can make the browser decode.
const MERMAID_EMBEDDED_RASTER_MAX_LENGTH = 200_000;

function isEmbeddedMermaidRaster(value: string) {
  const source = value.trim();
  return source.length <= MERMAID_EMBEDDED_RASTER_MAX_LENGTH &&
    MERMAID_EMBEDDED_RASTER.test(source);
}

function hasOnlyEmbeddedMermaidRaster(image: Element) {
  const sources = ["href", "xlink:href", "src"]
    .map((name) => image.getAttribute(name))
    .filter((source): source is string => source !== null);
  return sources.length > 0 && sources.every(isEmbeddedMermaidRaster);
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
      "dataInternalPageId",
      "data-internal-page-id",
      "data-dtx-unavailable-reference",
      "dataDtxUnavailableReference",
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
