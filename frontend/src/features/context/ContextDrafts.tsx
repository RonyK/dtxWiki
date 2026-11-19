import { useId, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { apiErrorMessage } from "../../shared/api/client";
import { listContextDrafts, type ContextPageDetail, type OpenDrafts } from "../../shared/api/context";
import { bringInPageVersion, discardPageEdit, previewPageMerge } from "../../shared/api/pages";
import { ConfirmDialog } from "../../shared/components/ConfirmDialog";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { TREE_VERSION_PARAM } from "../../shared/components/treeRouteState";
import { isAbortError } from "../../shared/lib/abortError";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { contextEditHref, workspacePath } from "../../shared/lib/workspaceRoutes";
import { useContextMerge } from "../pages/useContextMerge";

export function ContextDrafts({ detail, workspaceId, onCompareWithPublished, onChanged }: {
  detail: ContextPageDetail;
  workspaceId: string;
  /** Opens the draft-against-published comparison; absent when there is nothing to compare. */
  onCompareWithPublished?: (() => void) | null;
  /** Reloads the page after a draft action changed it. */
  onChanged: () => Promise<void> | void;
}) {
  const { t } = useTranslation("context");
  const withdrawTitleId = useId();
  const merge = useContextMerge();
  const [action, setAction] = useState<"bring_in" | "withdraw" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmWithdraw, setConfirmWithdraw] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const [summary, setSummary] = useState<OpenDrafts | null>(detail.open_drafts ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const activeRef = useRef(true);
  const loadingRef = useRef(false);
  const generationRef = useRef(0);
  useLayoutEffect(() => {
    activeRef.current = true;
    return () => { activeRef.current = false; };
  }, []);
  useLayoutEffect(() => {
    generationRef.current += 1;
    loadingRef.current = false;
    setSummary(detail.open_drafts ?? null);
    setLoading(false);
    setError(false);
    return () => { generationRef.current += 1; };
  }, [detail.open_drafts]);

  function selectVersion(versionId: string | null) {
    const next = new URLSearchParams(searchParams);
    if (versionId) next.set(TREE_VERSION_PARAM, versionId);
    else next.delete(TREE_VERSION_PARAM);
    next.delete("tab");
    next.delete("compareFrom");
    next.delete("compareTo");
    setSearchParams(next);
  }

  const draftVersionId = detail.draft ? detail.version_info?.version_id ?? null : null;
  const publishedVersionId = detail.current_published_version_id;
  const failure = (error: unknown) => apiErrorMessage(error, t("drafts.actionError"));

  // Brings the published version into this draft through the shared merge
  // flow: a clean merge is saved at once, overlapping changes ask first.
  async function bringIn() {
    if (!draftVersionId || !publishedVersionId || action) return;
    setAction("bring_in");
    setActionError(null);
    try {
      await merge.runMergeWrite({
        kind: "bring_in",
        workspaceId,
        pageId: detail.id,
        publishedVersionNumber: detail.published_version_info?.version_no ?? null,
        loadPreview: () => previewPageMerge(workspaceId, detail.id, draftVersionId),
        write: (choice) => bringInPageVersion(workspaceId, detail.id, draftVersionId, choice),
      });
      await onChanged();
    } catch (error) {
      if (isAbortError(error)) return;
      // Refused or unknown: say so and show the draft as it is now. If the
      // write did land, the refreshed draft shows it; trying again is bound
      // to the new state, so it is never applied twice.
      setActionError(failure(error));
      await onChanged();
    } finally {
      setAction(null);
    }
  }

  // Withdrawing ends the draft as a finished version that History keeps.
  async function withdraw() {
    if (!draftVersionId || action) return;
    setAction("withdraw");
    setActionError(null);
    try {
      await discardPageEdit(workspaceId, detail.id, draftVersionId);
      setConfirmWithdraw(false);
      selectVersion(null);
      await onChanged();
    } catch (error) {
      setActionError(failure(error));
    } finally {
      setAction(null);
    }
  }

  async function loadDrafts(append: boolean) {
    const cursor = append ? summary?.next_cursor : undefined;
    if ((append && !cursor) || loadingRef.current) return;
    const generation = generationRef.current;
    const isCurrent = () => activeRef.current && generationRef.current === generation;
    loadingRef.current = true;
    setLoading(true);
    setError(false);
    try {
      const next = await listContextDrafts(workspaceId, detail.id, cursor ?? undefined);
      if (!isCurrent()) return;
      setSummary((current) => {
        if (!append) return next;
        const items = current?.items ?? [];
        const seen = new Set(items.map((item) => item.version_id));
        return {
          ...next,
          items: [...items, ...(next.items ?? []).filter((item) => !seen.has(item.version_id))],
        };
      });
    } catch {
      if (isCurrent()) setError(true);
    } finally {
      if (isCurrent()) {
        loadingRef.current = false;
        setLoading(false);
      }
    }
  }

  const draft = detail.draft;
  const author = draft?.author.display_name || t("drafts.unknownAuthor");
  return (
    <div className="context-drafts">
      {draft ? (
        <StatusBanner tone="info">
          <div className="context-draft-banner">
            <strong>{t("drafts.banner")}</strong>
            <span>{author}</span>
            <time dateTime={draft.last_saved_at}>
              {t("drafts.lastSaved", { time: formatVersionTimestamp(draft.last_saved_at) })}
            </time>
            {draft.is_editing ? <span>{t("drafts.writing")}</span> : null}
            {draft.review ? (
              <Link to={workspacePath(workspaceId, `review/${encodeURIComponent(draft.review.id)}`)}>
                <StatusPill compact status={draft.review.status} />
              </Link>
            ) : null}
            {draft.is_behind === true ? <span>{t("drafts.behind")}</span> : null}
            {detail.current_published_version_id ? (
              <button className="text-button" onClick={() => selectVersion(null)} type="button">
                {t("drafts.backPublished")}
              </button>
            ) : null}
          </div>
          {draftVersionId ? (
            <div aria-label={t("drafts.actionsLabel")} className="context-draft-actions" role="group">
              <Link
                className="icon-button text-button"
                to={`${contextEditHref(workspaceId, detail.id)}?version_id=${encodeURIComponent(draftVersionId)}`}
              >
                {t("drafts.edit")}
              </Link>
              {draft.is_behind === true && publishedVersionId ? (
                <button className="icon-button text-button" disabled={action !== null} onClick={() => void bringIn()} type="button">
                  {action === "bring_in" ? t("drafts.bringingIn") : t("drafts.bringIn")}
                </button>
              ) : null}
              {onCompareWithPublished ? (
                <button className="icon-button text-button" onClick={onCompareWithPublished} type="button">
                  {t("drafts.compare")}
                </button>
              ) : null}
              <button className="icon-button text-button danger" disabled={action !== null} onClick={() => setConfirmWithdraw(true)} type="button">
                {t("drafts.withdraw")}
              </button>
            </div>
          ) : null}
          {actionError ? <p className="context-draft-error" role="alert">{actionError}</p> : null}
        </StatusBanner>
      ) : null}
      {confirmWithdraw ? (
        <ConfirmDialog
          busy={action === "withdraw"}
          busyLabel={t("drafts.withdrawing")}
          confirmLabel={t("drafts.withdraw")}
          labelledBy={withdrawTitleId}
          onCancel={() => setConfirmWithdraw(false)}
          onConfirm={() => void withdraw()}
          title={t("drafts.withdrawTitle")}
          tone="danger"
        >
          <p>{t("drafts.withdrawConfirm")}</p>
        </ConfirmDialog>
      ) : null}
      {merge.dialog}
      {summary ? (
        <details className="context-draft-list" onToggle={(event) => {
          if (event.currentTarget.open) void loadDrafts(false);
        }}>
          <summary>{t("drafts.count", { count: summary.total })}</summary>
          {(summary.items ?? []).length ? (
            <ul>
              {(summary.items ?? []).map((item) => (
                <li key={item.version_id}>
                  <button
                    aria-current={item.version_id === detail.version_info?.version_id ? "true" : undefined}
                    className="context-draft-choice text-button"
                    onClick={() => selectVersion(item.version_id)}
                    type="button"
                  >
                    <span>{item.author.display_name || t("drafts.unknownAuthor")}</span>
                    {item.is_mine ? <span>{t("drafts.mine")}</span> : null}
                    <span>{item.state === "reviewing" ? t("drafts.reviewing") : t("drafts.writing")}</span>
                    <time dateTime={item.last_saved_at}>
                      {t("drafts.lastSaved", { time: formatVersionTimestamp(item.last_saved_at) })}
                    </time>
                  </button>
                </li>
              ))}
            </ul>
          ) : <p>{t("drafts.empty")}</p>}
          {error ? <p role="alert">{t("drafts.loadError")}</p> : null}
          {summary.next_cursor ? (
            <button className="text-button" disabled={loading} onClick={() => void loadDrafts(true)} type="button">
              {loading ? t("drafts.loading") : t("drafts.more")}
            </button>
          ) : null}
        </details>
      ) : null}
    </div>
  );
}
