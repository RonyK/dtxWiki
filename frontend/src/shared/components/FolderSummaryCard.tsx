import { codePointLength, inputTextError, textInputLimit } from "../lib/inputPolicy";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { FileText, Folder } from "lucide-react";

import { AdvancedSection } from "./AdvancedSection";
import { StatusPill } from "./StatusPill";
import type { TreeIndexPreview, TreeNodePayload } from "../api/workspace";

const NAV_SUMMARY_MAX = 160;

export function childNodesFor(
  nodes: TreeNodePayload[],
  parentId: string,
): TreeNodePayload[] {
  return nodes.filter((node) => node.parent_id === parentId);
}

export function nodeIdForPage(
  nodes: TreeNodePayload[],
  pageId: string,
): string | null {
  const node = nodes.find((candidate) => candidate.target_id === pageId);
  return node ? node.id : null;
}

export interface FolderChildMeta {
  subtitle: string;
  pillStatus: string;
  pillLabel?: string;
}

export interface FolderSummaryCardProps {
  node: TreeNodePayload;
  nodes: TreeNodePayload[];
  onSelectNode: (node: TreeNodePayload) => void;
  /** section-specific subtitle+pill for a child row (page rows). */
  childMeta: (child: TreeNodePayload) => FolderChildMeta | null;
  eyebrow: string;
  /** shown grey with an auto badge when nav_summary is NULL. */
  fallbackSummary?: string | null;
  emptyLabel?: string;
  canEdit?: boolean;
  onSaveNavSummary?: (navSummary: string | null) => Promise<void>;
  /** collapsible index preview: the rows agents read, linked for people. */
  loadIndexPreview?: () => Promise<TreeIndexPreview>;
  footer?: React.ReactNode;
  /**
   * Optional totals for a caller that renders only one paginated child slice.
   * When omitted, counts continue to be derived from `nodes` as before.
   */
  totalChildCounts?: { folders: number; pages: number };
}

export function FolderSummaryCard({
  node,
  nodes,
  onSelectNode,
  childMeta,
  eyebrow,
  fallbackSummary,
  emptyLabel,
  canEdit = false,
  onSaveNavSummary,
  loadIndexPreview,
  footer,
  totalChildCounts,
}: FolderSummaryCardProps) {
  const { t } = useTranslation("tree");
  const children = useMemo(
    () => childNodesFor(nodes, node.id),
    [nodes, node.id],
  );
  const renderedFolderCount = children.filter(
    (child) => child.node_kind === "folder",
  ).length;
  const folderCount = totalChildCounts?.folders ?? renderedFolderCount;
  const pageCount =
    totalChildCounts?.pages ?? children.length - renderedFolderCount;

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(node.nav_summary ?? "");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  useEffect(() => {
    setEditing(false);
    setDraft(node.nav_summary ?? "");
    setSaveError(null);
  }, [node.id, node.nav_summary]);

  // The preview belongs to the node it was opened for, so selecting another
  // node closes it and hides an earlier node's rows without an effect.
  const [openNodeId, setOpenNodeId] = useState<string | null>(null);
  const previewOpen = openNodeId === node.id;
  const [previewState, setPreviewState] = useState<{
    nodeId: string;
    data: TreeIndexPreview | null;
    failed: boolean;
  } | null>(null);
  const [previewReload, setPreviewReload] = useState(0);
  // Parent re-renders pass a fresh loadIndexPreview closure every time; read
  // it through a ref so only opening, a tree change or an explicit refresh
  // fetches. Each request is numbered and only the latest may apply.
  const loadPreviewRef = useRef(loadIndexPreview);
  useEffect(() => {
    loadPreviewRef.current = loadIndexPreview;
  });
  const previewRequestRef = useRef(0);
  useEffect(() => {
    const load = loadPreviewRef.current;
    if (!previewOpen || !load) return;
    const request = ++previewRequestRef.current;
    const nodeId = node.id;
    load().then(
      (data) => {
        if (previewRequestRef.current === request) {
          setPreviewState({ nodeId, data, failed: false });
        }
      },
      () => {
        if (previewRequestRef.current === request) {
          setPreviewState({ nodeId, data: null, failed: true });
        }
      },
    );
  }, [previewOpen, node.id, nodes, previewReload]);
  const preview = previewState?.nodeId === node.id ? previewState : null;

  const isFolder = node.node_kind === "folder";
  const submit = async () => {
    if (!onSaveNavSummary) return;
    const inputError = inputTextError(draft, NAV_SUMMARY_MAX, false);
    if (inputError) { setSaveError(inputError); return; }
    setSaving(true);
    setSaveError(null);
    try {
      const trimmed = draft.trim();
      await onSaveNavSummary(trimmed === "" ? null : trimmed);
      setEditing(false);
    } catch {
      setSaveError(
        t("navSummary.saveError", {
          defaultValue: "Could not save the folder summary.",
        }),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="feature-section detail-section">
      <div className="detail-heading">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2>
            {node.is_system_node
              ? t("folder.systemPrefix", { defaultValue: "System-managed " })
              : ""}
            {node.title}
          </h2>
        </div>
        <StatusPill
          status={t("folder.pageFolderCount", {
            pages: t("folder.pageCount", { count: pageCount }),
            folders: t("folder.folderCount", { count: folderCount }),
            defaultValue: "{{pages}} · {{folders}}",
          })}
        />
      </div>

      {editing ? (
        <div className="folder-nav-summary-editor">
          <textarea
            aria-label={t("navSummary.inputLabel", {
              defaultValue: "Folder routing summary",
            })}
            {...textInputLimit(NAV_SUMMARY_MAX, false)}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t("navSummary.placeholder", {
              defaultValue:
                "Describe which queries belong in this folder — and which " +
                "do not ('see X for ~').",
            })}
            rows={3}
            value={draft}
          />
          <div className="folder-nav-summary-editor-footer">
            <span className="folder-nav-summary-counter">
              {codePointLength(draft)}/{NAV_SUMMARY_MAX}
            </span>
            {saveError ? (
              <span className="form-error">{saveError}</span>
            ) : null}
            <button
              className="icon-button"
              disabled={saving}
              onClick={() => {
                setEditing(false);
                setDraft(node.nav_summary ?? "");
              }}
              type="button"
            >
              {t("navSummary.cancel", { defaultValue: "Cancel" })}
            </button>
            <button
              className="icon-button primary"
              disabled={saving || Boolean(inputTextError(draft, NAV_SUMMARY_MAX, false))}
              onClick={submit}
              type="button"
            >
              {t("navSummary.save", { defaultValue: "Save" })}
            </button>
          </div>
        </div>
      ) : (
        <p className="folder-summary-text">
          {node.nav_summary ? (
            node.nav_summary
          ) : children.length === 0 ? (
            (emptyLabel ??
            t("folder.empty", { defaultValue: "This folder is empty." }))
          ) : fallbackSummary ? (
            <span className="folder-summary-fallback">
              {fallbackSummary}{" "}
              <span className="folder-summary-auto-badge">
                {t("navSummary.autoBadge", { defaultValue: "auto" })}
              </span>
            </span>
          ) : (
            t("folder.description", {
              defaultValue:
                "Select a child page or folder from this explorer section.",
            })
          )}
          {canEdit && isFolder && onSaveNavSummary ? (
            <button
              className="folder-nav-summary-edit"
              onClick={() => setEditing(true)}
              type="button"
            >
              {t("navSummary.edit", { defaultValue: "Edit summary" })}
            </button>
          ) : null}
        </p>
      )}

      {isFolder && loadIndexPreview ? (
        <AdvancedSection
          label={t("navSummary.previewTitle", {
            defaultValue: "Index preview",
          })}
          onOpenChange={(open) => setOpenNodeId(open ? node.id : null)}
          open={previewOpen}
        >
          {preview?.failed ? (
            <p className="form-error">
              {t("navSummary.previewError", {
                defaultValue: "Could not load the index preview.",
              })}
            </p>
          ) : !preview?.data ? (
            <p className="folder-summary-fallback">
              {t("navSummary.previewLoading", { defaultValue: "Loading…" })}
            </p>
          ) : (
            <div className="folder-index-preview">
              <ul>
                {preview.data.entries.map((entry) => (
                  <li key={`${entry.kind}:${entry.id}`}>
                    {entry.href ? (
                      <Link to={entry.href}>
                        {entry.title}
                        {entry.kind === "folder" ? "/" : ""}
                      </Link>
                    ) : (
                      entry.title
                    )}
                    {entry.kind === "folder" ? ` (${entry.count ?? 0})` : null}
                    {entry.draft ? <StatusPill compact status="draft" /> : null}
                    {entry.summary ? ` — ${entry.summary}` : null}
                  </li>
                ))}
              </ul>
              {preview.data.overflow > 0 ? (
                <p className="folder-summary-fallback">
                  {t("navSummary.previewOverflow", {
                    count: preview.data.overflow,
                    defaultValue: "…and {{count}} more",
                  })}
                </p>
              ) : null}
            </div>
          )}
          <button
            className="icon-button"
            onClick={() => setPreviewReload((count) => count + 1)}
            type="button"
          >
            {t("navSummary.previewRefresh", { defaultValue: "Refresh" })}
          </button>
        </AdvancedSection>
      ) : null}

      <div className="folder-child-list">
        {children.map((child) => {
          const meta = childMeta(child);
          const Icon = child.node_kind === "folder" ? Folder : FileText;
          return (
            <button
              className="folder-child-row"
              key={child.id}
              onClick={() => onSelectNode(child)}
              type="button"
            >
              <Icon aria-hidden="true" size={16} />
              <div>
                <strong>{child.title}</strong>
                <p>
                  {meta?.subtitle ??
                    t("folder.childPageCount", {
                      count: child.children_count,
                      defaultValue: "{{count}} child pages",
                    })}
                </p>
              </div>
              <StatusPill
                compact
                label={meta?.pillLabel}
                status={meta?.pillStatus ?? child.node_kind}
              />
            </button>
          );
        })}
      </div>
      {footer ? <footer className="folder-summary-footer">{footer}</footer> : null}
    </section>
  );
}
