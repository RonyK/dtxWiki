import { codePointLength, inputTextError, textInputLimit } from "../lib/inputPolicy";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { FileText, Folder } from "lucide-react";

import { AdvancedSection } from "./AdvancedSection";
import { StatusPill } from "./StatusPill";
import type { TreeNodePayload } from "../api/workspace";

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
  /** collapsible index preview: how the folder renders for agents. */
  loadIndexPreview?: () => Promise<string>;
  renderMarkdown?: (markdown: string) => React.ReactNode;
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
  renderMarkdown,
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

  const [previewOpen, setPreviewOpen] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  // Refs, not state/deps: parent re-renders pass a fresh loadIndexPreview
  // closure every time — an effect keyed on it would refire in-flight (or
  // failed) fetches, and a cleanup-based cancel would drop the result of
  // the request it just started.
  const previewRequestedRef = useRef(false);
  const previewNodeRef = useRef(node.id);
  useEffect(() => {
    previewNodeRef.current = node.id;
    previewRequestedRef.current = false;
    setPreviewOpen(false);
    setPreview(null);
    setPreviewError(null);
  }, [node.id]);
  useEffect(() => {
    if (!previewOpen || previewRequestedRef.current || !loadIndexPreview) {
      return;
    }
    previewRequestedRef.current = true;
    const requestedNode = node.id;
    loadIndexPreview()
      .then((markdown) => {
        if (previewNodeRef.current === requestedNode) {
          setPreview(markdown);
          setPreviewError(null);
        }
      })
      .catch(() => {
        if (previewNodeRef.current === requestedNode) {
          setPreviewError(
            t("navSummary.previewError", {
              defaultValue: "Could not load the index preview.",
            }),
          );
        }
      });
  }, [previewOpen, loadIndexPreview, node.id, t]);

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
            defaultValue: "Index preview (how agents see this folder)",
          })}
          onOpenChange={setPreviewOpen}
          open={previewOpen}
        >
          {previewError ? (
            <p className="form-error">{previewError}</p>
          ) : preview === null ? (
            <p className="folder-summary-fallback">
              {t("navSummary.previewLoading", { defaultValue: "Loading…" })}
            </p>
          ) : renderMarkdown ? (
            renderMarkdown(preview)
          ) : (
            <pre className="folder-index-preview">{preview}</pre>
          )}
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
