import { Children, isValidElement, useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  FileText,
  Folder,
  Library,
  LoaderCircle,
  RefreshCw,
  type LucideIcon,
} from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type {
  TreeIndexPreview as TreeIndexPreviewData,
  TreeIndexPreviewEntry,
  TreeNodePayload,
} from "../api/workspace";

export interface TreeIndexPreviewProps {
  workspaceId: string;
  nodeId: string;
  nodes: TreeNodePayload[];
  loadIndexPreview: () => Promise<TreeIndexPreviewData>;
}

type PreviewState = {
  workspaceId: string;
  nodeId: string;
  data: TreeIndexPreviewData | null;
  loading: boolean;
  failed: boolean;
};

type PreviewMode = "formatted" | "source";

const UUID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const INDEX_REF_PATTERN = new RegExp(`^dtxwiki://index/(folder|context|library|agent)/(${UUID_PATTERN})$`);
const CANONICAL_ENTRY_HREF_PATTERN = new RegExp(
  `^/workspaces/(${UUID_PATTERN})/(context|library|agent)/(folders|pages|items)/(${UUID_PATTERN})$`,
);

type EntryKind = Exclude<TreeIndexPreviewEntry["kind"], "other">;

/** The tree and navigation icon for each entry kind; colors live in the CSS. */
const ENTRY_ICONS: Record<EntryKind, LucideIcon> = {
  folder: Folder,
  context: FileText,
  library: Library,
  agent: Bot,
};

// ReactMarkdown uses this parser without syntax plugins below. Validate the
// same block/inline structure that its renderer will receive.
const indexMarkdownParser = unified().use(remarkParse).freeze();

function indexRefKey(kind: string, id: string): string | null {
  if (!(kind === "folder" || kind === "context" || kind === "library" || kind === "agent")) {
    return null;
  }
  if (!new RegExp(`^${UUID_PATTERN}$`).test(id)) return null;
  return `dtxwiki://index/${kind}/${id}`;
}

function canonicalEntryHref(entry: TreeIndexPreviewEntry): string | null {
  if (entry.kind === "other" || !entry.href) return null;
  const match = CANONICAL_ENTRY_HREF_PATTERN.exec(entry.href);
  if (!match) return null;

  const [, workspaceId, section, routeKind, routeId] = match;
  const id = entry.id.toLowerCase();
  if (routeId !== id) return null;
  if (entry.kind === "folder") {
    if (routeKind !== "folders") return null;
  } else if (entry.kind === "context") {
    if (section !== "context" || routeKind !== "pages") return null;
  } else if (entry.kind === "library") {
    if (section !== "library" || routeKind !== "items") return null;
  } else if (entry.kind === "agent") {
    if (section !== "agent" || routeKind !== "pages") return null;
  }
  return `/workspaces/${workspaceId}/${section}/${routeKind}/${routeId}`;
}

/** Whether a parsed subtree has anything that could render as a link. */
function containsLink(node: { type: string; children?: unknown[] }): boolean {
  if (["link", "linkReference", "definition", "html"].includes(node.type)) return true;
  return (node.children ?? []).some((child) =>
    containsLink(child as { type: string; children?: unknown[] }),
  );
}

/** Refs of actual, tight list rows, in the order rendered by ReactMarkdown. */
function entryRefs(markdown: string): (string | null)[] | null {
  const refs: (string | null)[] = [];
  for (const block of indexMarkdownParser.parse(markdown).children) {
    if (block.type === "heading" || block.type === "paragraph") continue;
    // Code fences, thematic breaks, HTML blocks and nested/loose lists are
    // not index rows. Keep their exact source instead of interpreting a
    // link-shaped line that their actual Markdown structure does not render.
    if (block.type !== "list" || block.ordered || block.spread) return null;
    for (const item of block.children) {
      const paragraph = item.children[0];
      if (item.spread || item.children.length !== 1 || !paragraph) return null;
      if (paragraph.type !== "paragraph") {
        // A title-only row may start with block syntax ("# Notes") and render
        // as that block. Without any link it is still a row with no ref.
        if (containsLink(paragraph)) return null;
        refs.push(null);
        continue;
      }
      const first = paragraph.children.find((child) =>
        child.type !== "text" || child.value.trim() !== "",
      );
      refs.push(first?.type === "link" ? first.url : null);
    }
  }
  return refs;
}

/** The entry's leading link; links later in its summary stay plain text. */
function leadingIndexRef(
  children: ReactNode,
): { ref: string; label: ReactNode; rest: ReactNode[] } | null {
  const nodes = Children.toArray(children);
  const start = nodes.findIndex((node) => typeof node !== "string" || node.trim() !== "");
  const first = nodes[start];
  if (!isValidElement(first)) return null;
  const props = first.props as { children?: ReactNode; href?: unknown };
  if (typeof props.href !== "string" || !INDEX_REF_PATTERN.test(props.href)) return null;
  return { ref: props.href, label: props.children, rest: nodes.slice(start + 1) };
}

function hasVisibleText(node: ReactNode): boolean {
  if (typeof node === "string" || typeof node === "number") return String(node).trim() !== "";
  if (Array.isArray(node)) return node.some(hasVisibleText);
  if (!isValidElement(node)) return false;
  const props = node.props as { alt?: unknown; children?: ReactNode };
  // An image renders as its alt text, so an image-only summary is visible.
  if (typeof props.alt === "string" && props.alt.trim() !== "") return true;
  return hasVisibleText(props.children);
}

function splitKnownEntrySuffix(
  suffix: string,
  kind: string,
): { titleSuffix: string; summary: string } {
  const separator = suffix.indexOf(" — ");
  const prefix = separator < 0 ? suffix : suffix.slice(0, separator);
  const summary = separator < 0 ? "" : suffix.slice(separator + 3);
  const knownMarker = (kind === "folder" && /^ \(\d+\)$/.test(prefix))
    || (kind === "context" && prefix === " (draft)");
  if (knownMarker) return { titleSuffix: prefix, summary };
  if (prefix === "") return { titleSuffix: "", summary };
  // Keep future or unexpected suffixes intact until their producer contract is known.
  return { titleSuffix: "", summary: suffix };
}

function SourceMarkdown({ markdown, label, hint }: { markdown: string; label: string; hint: string }) {
  return (
    <div className="index-preview__source-view">
      <p className="index-preview__hint"><strong>{label}</strong> {hint}</p>
      <pre
        aria-label={label}
        className="index-preview__source"
        tabIndex={0 /* eslint-disable-line jsx-a11y/no-noninteractive-tabindex -- Keyboard access to the scrollable, read-only index source. */}
      ><code>{markdown}</code></pre>
    </div>
  );
}

/**
 * Verify that each entry row's leading link is one of this payload's known
 * index refs, and that each entry supplies an internal canonical route for the
 * same ID.
 * If verification fails, the caller keeps the complete payload in source view.
 */
function canonicalLinksForPreview(
  data: TreeIndexPreviewData,
  workspaceId: string,
): Map<string, string> | null {
  if (typeof data.markdown !== "string" || !Array.isArray(data.entries)) return null;
  const refs = entryRefs(data.markdown);
  if (refs === null || refs.length !== data.entries.length) return null;
  const links = new Map<string, string>();
  for (const [index, value] of (data.entries as unknown[]).entries()) {
    if (!value || typeof value !== "object") return null;
    const entry = value as TreeIndexPreviewEntry;
    if (typeof entry.kind !== "string" || typeof entry.id !== "string") return null;
    if (entry.kind === "other") {
      if (refs[index] !== null) return null;
      continue;
    }
    const ref = indexRefKey(entry.kind, entry.id.toLowerCase());
    const href = canonicalEntryHref(entry);
    if (!ref || refs[index] !== ref || !href) return null;
    const match = CANONICAL_ENTRY_HREF_PATTERN.exec(href);
    if (!match) return null;
    const entryWorkspaceId = match[1];
    if (workspaceId !== entryWorkspaceId) return null;

    const current = links.get(ref);
    if (current && current !== href) return null;
    links.set(ref, href);
  }
  return links;
}

function FormattedIndex({
  markdown,
  links,
}: {
  markdown: string;
  links: Map<string, string>;
}) {
  const components = useMemo<Components>(() => ({
    a({ children: linkLabel }) {
      return <span>{linkLabel}</span>;
    },
    img({ alt }) {
      return <span>{alt}</span>;
    },
    li({ children, node: _node }) {
      const entry = leadingIndexRef(children);
      const target = entry ? links.get(entry.ref) : undefined;
      if (!entry || !target) return <li>{children}</li>;
      const kind = INDEX_REF_PATTERN.exec(entry.ref)?.[1] as EntryKind;
      const EntryIcon = ENTRY_ICONS[kind];
      const [head, ...tail] = entry.rest;
      const display = typeof head === "string"
        ? splitKnownEntrySuffix(head, kind)
        : { titleSuffix: "", summary: head };
      const summary = [display.summary, ...tail];
      return (
        <li className="index-preview__entry">
          <div className="index-preview__entry-title">
            <EntryIcon
              aria-hidden="true"
              className={`index-preview__entry-icon index-preview__entry-icon--${kind}`}
              size={15}
            />
            <Link to={target}><strong>{entry.label}</strong></Link>{display.titleSuffix}
          </div>
          {hasVisibleText(summary) ? (
            <div className="index-preview__entry-summary">{summary}</div>
          ) : null}
          <code className="index-preview__entry-ref">{entry.ref}</code>
        </li>
      );
    },
  }), [links]);

  return (
    <div className="index-preview__formatted">
      <ReactMarkdown
        components={components}
        urlTransform={(url) => url}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

/** Render the exact server Markdown with mapped internal links or exact source. */
export function TreeIndexPreview({ workspaceId, nodeId, nodes, loadIndexPreview }: TreeIndexPreviewProps) {
  const { t } = useTranslation("tree");
  const bodyId = useId();
  const formattedTabId = useId();
  const sourceTabId = useId();
  const formattedPanelId = useId();
  const sourcePanelId = useId();
  const identity = `${workspaceId}:${nodeId}`;
  const [openNodeId, setOpenNodeId] = useState<string | null>(null);
  const [reloadCount, setReloadCount] = useState(0);
  const [state, setState] = useState<PreviewState | null>(null);
  const [mode, setMode] = useState<PreviewMode>("formatted");
  const [focusedMode, setFocusedMode] = useState<PreviewMode>("formatted");
  const expanded = openNodeId === identity;
  const requestRef = useRef(0);
  const loadRef = useRef(loadIndexPreview);

  useEffect(() => {
    loadRef.current = loadIndexPreview;
  });

  useEffect(() => {
    if (!expanded) return;
    const requestId = ++requestRef.current;
    setState((current) => current?.workspaceId === workspaceId && current.nodeId === nodeId
      ? { ...current, loading: true, failed: false }
      : { workspaceId, nodeId, data: null, loading: true, failed: false });
    loadRef.current().then(
      (data) => {
        if (requestRef.current === requestId) {
          setState({ workspaceId, nodeId, data, loading: false, failed: false });
        }
      },
      () => {
        if (requestRef.current === requestId) {
          setState((current) => ({
            workspaceId,
            nodeId,
            data: current?.workspaceId === workspaceId && current.nodeId === nodeId ? current.data : null,
            loading: false,
            failed: true,
          }));
        }
      },
    );
    return () => {
      requestRef.current += 1;
    };
  }, [expanded, workspaceId, nodeId, nodes, reloadCount]);

  const current = state?.workspaceId === workspaceId && state.nodeId === nodeId ? state : null;
  const data = current?.data ?? null;
  const markdown = data?.markdown ?? "";
  const isEmpty = Boolean(data && markdown.trim() === "");
  const canonicalLinks = useMemo(
    () => data ? canonicalLinksForPreview(data, workspaceId) : null,
    [data, workspaceId],
  );
  const canFormat = canonicalLinks !== null;
  const formattedTabIndex = focusedMode === "formatted" ? 0 : -1;
  const sourceTabIndex = focusedMode === "source" ? 0 : -1;

  const selectMode = (next: PreviewMode, focus = false) => {
    setMode(next);
    setFocusedMode(next);
    if (focus) {
      document.getElementById(next === "formatted" ? formattedTabId : sourceTabId)?.focus();
    }
  };

  return (
    <section className={`index-preview${expanded ? " index-preview--open" : ""}`} aria-busy={current?.loading ?? false}>
      <div className="index-preview__header">
        <button
          aria-controls={expanded ? bodyId : undefined}
          aria-expanded={expanded}
          className="index-preview__toggle"
          onClick={() => setOpenNodeId(expanded ? null : identity)}
          type="button"
        >
          {expanded ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
          <span>{t("navSummary.previewTitle", { defaultValue: "Index preview" })}</span>
        </button>
        {expanded && data && !isEmpty ? (
          <div
            aria-label={t("navSummary.previewMode", { defaultValue: "Index display mode" })}
            className="index-preview__mode-tabs"
            onKeyDown={(event) => {
              if (event.key === "ArrowLeft" || event.key === "ArrowRight" || event.key === "Home" || event.key === "End") {
                event.preventDefault();
                const next = event.key === "Home"
                  ? "formatted"
                  : event.key === "End"
                    ? "source"
                    : mode === "formatted" ? "source" : "formatted";
                selectMode(next, true);
              }
            }}
            role="tablist"
            tabIndex={-1}
          >
            <button
              aria-controls={formattedPanelId}
              aria-selected={mode === "formatted"}
              className="index-preview__mode-tab"
              id={formattedTabId}
              onClick={() => selectMode("formatted")}
              role="tab"
              tabIndex={formattedTabIndex}
              type="button"
            >
              {t("navSummary.previewFormatted", { defaultValue: "Formatted" })}
            </button>
            <button
              aria-controls={sourcePanelId}
              aria-selected={mode === "source"}
              className="index-preview__mode-tab"
              id={sourceTabId}
              onClick={() => selectMode("source")}
              role="tab"
              tabIndex={sourceTabIndex}
              type="button"
            >
              {t("navSummary.previewSource", { defaultValue: "Source" })}
            </button>
          </div>
        ) : null}
        <button
          aria-label={t("navSummary.previewRefresh", { defaultValue: "Refresh" })}
          aria-disabled={!expanded || Boolean(current?.loading)}
          className="index-preview__refresh"
          disabled={!expanded}
          onClick={() => {
            if (expanded && !current?.loading) setReloadCount((count) => count + 1);
          }}
          title={t("navSummary.previewRefresh", { defaultValue: "Refresh" })}
          type="button"
        >
          {current?.loading
            ? <LoaderCircle aria-hidden="true" className="index-preview__refresh-icon is-loading" />
            : <RefreshCw aria-hidden="true" className="index-preview__refresh-icon" />}
        </button>
      </div>
      {expanded ? (
        <div className="index-preview__body" id={bodyId}>
          <span className="visually-hidden" role="status">
            {current?.loading
              ? t("navSummary.previewLoading")
              : current?.data && !current.failed ? t("navSummary.previewUpdated") : ""}
          </span>
          {current?.failed ? (
            <p className="index-preview__error" role="alert">
              {t("navSummary.previewError", { defaultValue: "Could not load the index preview." })}
            </p>
          ) : null}
          {current?.loading && !current.data ? (
            <p className="index-preview__loading">
              {t("navSummary.previewLoading", { defaultValue: "Loading…" })}
            </p>
          ) : null}
          {isEmpty ? (
            <p className="index-preview__empty">
              {t("navSummary.previewEmpty", { defaultValue: "This index has no entries." })}
            </p>
          ) : null}
          {data && !isEmpty ? (
            <div className="index-preview__view">
              <div
                aria-labelledby={formattedTabId}
                className="index-preview__formatted-panel"
                hidden={mode !== "formatted"}
                id={formattedPanelId}
                role="tabpanel"
              >
                {canFormat ? (
                  <FormattedIndex markdown={markdown} links={canonicalLinks} />
                ) : (
                  <div className="index-preview__fallback-panel">
                    <p className="index-preview__fallback" role="status">
                      {t("navSummary.previewFormatFallback", {
                        defaultValue: "Some refs could not be matched safely. Showing the exact source.",
                      })}
                    </p>
                    <SourceMarkdown
                      hint={t("navSummary.previewSourceHint")}
                      label={t("navSummary.previewSourceLabel")}
                      markdown={markdown}
                    />
                  </div>
                )}
              </div>
              <div
                aria-labelledby={sourceTabId}
                className="index-preview__source-panel"
                hidden={mode !== "source"}
                id={sourcePanelId}
                role="tabpanel"
              >
                <SourceMarkdown
                  hint={t("navSummary.previewSourceHint")}
                  label={t("navSummary.previewSourceLabel")}
                  markdown={markdown}
                />
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
