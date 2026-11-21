import { Check, ChevronDown, ClipboardList, GitCompareArrows, GitMerge, MessageSquare, Send } from "lucide-react";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { listContextDrafts, type ContextPageDetail, type OpenDrafts } from "../../shared/api/context";
import {
  decideReview,
  decisionPins,
  getReview,
  type ContextMergeBinding,
} from "../../shared/api/operations";
import {
  bringInPageVersion,
  contextWriteStateMoved,
  diffPageVersions,
  discardPageEdit,
  previewPageMerge,
  publishPageVersion,
  type ContextMergePreview,
  type PageVersionDiff,
  type PreparedPagePublication,
} from "../../shared/api/pages";
import { useOptionalAuth } from "../../shared/auth/AuthContext";
import { ConfirmDialog } from "../../shared/components/ConfirmDialog";
import { DiffViewerDialog } from "../../shared/components/DiffViewerDialog";
import { StatusPill } from "../../shared/components/StatusPill";
import { TREE_VERSION_PARAM } from "../../shared/components/treeRouteState";
import { useMediaQuery } from "../../shared/hooks/useMediaQuery";
import { isAbortError } from "../../shared/lib/abortError";
import { contextHistoryPresentation } from "../../shared/lib/contextHistory";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { readReviewDraft, reviewDraftKey, writeReviewDraft } from "../../shared/lib/reviewDraftStorage";
import { contextEditHref, workspacePath } from "../../shared/lib/workspaceRoutes";
import { mergeOverlapCount } from "../pages/contextMerge";
import { MarkdownView } from "../pages/MarkdownView";
import { useContextMerge } from "../pages/useContextMerge";

type DecisionResult = Awaited<ReturnType<typeof decideReview>>;

// The version chosen from the switcher (a draft id, or PUBLISHED_VIEW): the
// bar that shows it -- often a new mount once it loads -- takes focus.
const PUBLISHED_VIEW = "published";
let focusSwitcherFor: string | null = null;
type Busy = "publish" | "approve" | "request_changes" | "merge" | "discard" | null;
type PreviewState =
  | { key: string; status: "ready"; value: ContextMergePreview }
  | { key: string; status: "error" };

/**
 * One bar for the version on screen: which version it is (and a switcher to
 * the published version and every open draft), its state, and what can be
 * done to it. Publishing and approving act on this draft only, exactly as
 * shown: the merge preview the bar shows is the one written, so a draft or
 * published version that moved meanwhile is refused and shown again.
 */
export function ContextVersionBar({
  detail,
  workspaceId,
  onChanged,
  onRefresh,
  onDecided,
}: {
  detail: ContextPageDetail;
  workspaceId: string;
  /** Reloads the page after a write ended the version on screen. */
  onChanged: () => Promise<void> | void;
  /** Re-reads the version on screen, keeping this draft selected. */
  onRefresh: () => Promise<void> | void;
  /** A notice for a publish or review decision the bar wrote. */
  onDecided: (notice: string) => Promise<void> | void;
}) {
  const { t } = useTranslation("context");
  const { t: tShell } = useTranslation("shell");
  const [searchParams, setSearchParams] = useSearchParams();
  const merge = useContextMerge();
  const actorId = useOptionalAuth()?.user?.user_id ?? "preview";
  const narrow = useMediaQuery("(max-width: 720px)");
  const discardTitleId = useId();
  const menuId = useId();

  const draft = detail.draft ?? null;
  const draftVersionId = draft ? detail.version_info?.version_id ?? null : null;
  const publishedId = detail.current_published_version_id;
  const publishedNo = detail.published_version_info?.version_no ?? null;
  // A version on screen that is neither an open draft nor the published head
  // (an earlier snapshot, a closed draft) is named as History names it, never
  // as the version readers see.
  const viewed = detail.version_info ?? null;
  const earlier = !draft && viewed && viewed.version_id !== publishedId ? viewed : null;
  // The open review of the draft on screen (the server scopes both fields to
  // the displayed version). Publishing approves exactly this review.
  const openReview = (() => {
    if (!draft) return null;
    const open = (status: string) => ["pending", "changes_requested"].includes(status);
    const pending = detail.pending_review;
    if (pending && open(pending.status)) return pending;
    return draft.review && open(draft.review.status)
      ? { review_item_id: draft.review.id, status: draft.review.status, risk_level: null as string | null }
      : null;
  })();

  const [busy, setBusy] = useState<Busy>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [blockedByFormat, setBlockedByFormat] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [compareOpen, setCompareOpen] = useState(false);

  // --- version switcher -------------------------------------------------
  const [menuOpen, setMenuOpen] = useState(false);
  const switcherRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [summary, setSummary] = useState<OpenDrafts | null>(detail.open_drafts ?? null);
  const [draftsLoading, setDraftsLoading] = useState(false);
  const [draftsError, setDraftsError] = useState(false);
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
    setDraftsLoading(false);
    setDraftsError(false);
    return () => { generationRef.current += 1; };
  }, [detail.open_drafts]);
  // An earlier version is a view of its own: switching to the published
  // version from it must still move focus once that version is on screen.
  const shownView = draft ? draftVersionId : earlier ? earlier.version_id : PUBLISHED_VIEW;
  useEffect(() => {
    if (!focusSwitcherFor || focusSwitcherFor !== shownView || !triggerRef.current) return;
    focusSwitcherFor = null;
    triggerRef.current.focus();
  });
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: MouseEvent) => {
      if (!switcherRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setMenuOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [menuOpen]);

  async function loadDrafts(append: boolean) {
    const cursor = append ? summary?.next_cursor : undefined;
    if ((append && !cursor) || loadingRef.current) return;
    const generation = generationRef.current;
    const isCurrent = () => activeRef.current && generationRef.current === generation;
    loadingRef.current = true;
    setDraftsLoading(true);
    setDraftsError(false);
    try {
      const next = await listContextDrafts(workspaceId, detail.id, cursor ?? undefined);
      if (!isCurrent()) return;
      setSummary((current) => {
        if (!append) return next;
        const items = current?.items ?? [];
        const seen = new Set(items.map((item) => item.version_id));
        return { ...next, items: [...items, ...(next.items ?? []).filter((item) => !seen.has(item.version_id))] };
      });
    } catch {
      if (isCurrent()) setDraftsError(true);
    } finally {
      if (isCurrent()) {
        loadingRef.current = false;
        setDraftsLoading(false);
      }
    }
  }

  function selectVersion(versionId: string | null) {
    const next = new URLSearchParams(searchParams);
    if (versionId) next.set(TREE_VERSION_PARAM, versionId);
    else next.delete(TREE_VERSION_PARAM);
    next.delete("tab");
    next.delete("compareFrom");
    next.delete("compareTo");
    setMenuOpen(false);
    setSearchParams(next);
    // The chosen item leaves the DOM with the list, and the bar may mount
    // again for the new version: the next bar takes focus.
    if (menuOpen) focusSwitcherFor = versionId ?? PUBLISHED_VIEW;
  }

  // --- the merge publishing would write ----------------------------------
  // Read once per draft revision and published version, and again after a
  // refused write. Approval and publication send this exact preview.
  const [previewReload, setPreviewReload] = useState(0);
  const previewKey = draftVersionId
    ? `${workspaceId}:${detail.id}:${draftVersionId}:${detail.editing_revision ?? ""}:${publishedId ?? ""}:${previewReload}`
    : null;
  const [preview, setPreview] = useState<PreviewState | null>(null);
  useEffect(() => {
    if (!previewKey || !draftVersionId) return;
    // A superseded read is left to finish and ignored (not cancelled).
    let active = true;
    void previewPageMerge(workspaceId, detail.id, draftVersionId)
      .then((value) => { if (active) setPreview({ key: previewKey, status: "ready", value }); })
      .catch(() => {
        if (active) setPreview({ key: previewKey, status: "error" });
      });
    return () => {
      active = false;
    };
  }, [detail.id, draftVersionId, previewKey, workspaceId]);
  const currentPreview = preview?.key === previewKey ? preview : null;
  const shownPreview = currentPreview?.status === "ready" ? currentPreview.value : null;
  // The preview must be of the draft revision on screen. A newer save by a
  // co-author means the body shown is stale: reload it rather than publish
  // what nobody looked at.
  const previewMatchesScreen = Boolean(shownPreview
    && shownPreview.version_id === draftVersionId
    && (detail.editing_revision == null || shownPreview.editing_revision === detail.editing_revision)
    && (shownPreview.published_version_id ?? null) === (publishedId ?? null));
  const reloadedForRef = useRef<string | null>(null);
  useEffect(() => {
    if (!shownPreview || previewMatchesScreen || reloadedForRef.current === previewKey) return;
    reloadedForRef.current = previewKey;
    void onRefresh();
  }, [onRefresh, previewKey, previewMatchesScreen, shownPreview]);

  // --- review approval impact (other open reviews of this draft) ----------
  const openReviewId = openReview?.review_item_id ?? null;
  const openReviewStatus = openReview?.status ?? null;
  const impactKey = openReviewId ? `${workspaceId}:${openReviewId}` : null;
  const [impact, setImpact] = useState<{ key: string; count: number } | { key: string; failed: true } | null>(null);
  useEffect(() => {
    if (!impactKey || !openReviewId || openReviewStatus !== "pending") return;
    let active = true;
    void getReview(workspaceId, openReviewId)
      .then((value) => {
        if (active) setImpact({ key: impactKey, count: Math.max(0, value.superseded_review_count ?? 0) });
      })
      .catch(() => { if (active) setImpact({ key: impactKey, failed: true }); });
    return () => { active = false; };
  }, [impactKey, openReviewId, openReviewStatus, workspaceId]);
  const currentImpact = impact?.key === impactKey ? impact : null;
  const otherReviewCount = currentImpact && "count" in currentImpact ? currentImpact.count : null;
  const impactFailed = Boolean(currentImpact && "failed" in currentImpact);
  const impactReady = !openReview || openReview.status !== "pending" || otherReviewCount != null;

  // --- writes -------------------------------------------------------------
  // A refused write means the draft, the published version or the review
  // moved: show the new state and let the person decide again.
  const showChanged = useCallback(async () => {
    setNotice(t("versionBar.changed"));
    setPreviewReload((value) => value + 1);
    await onRefresh();
  }, [onRefresh, t]);
  async function fail(error: unknown, fallback: string) {
    if (isAbortError(error)) return;
    if (contextWriteStateMoved(error)) {
      await showChanged();
      return;
    }
    // Any other refusal may also mean the draft or its review changed (a
    // review closed meanwhile): show the refusal over the state as it is now.
    setPreviewReload((value) => value + 1);
    void onRefresh();
    if (error instanceof ApiError && error.status === 403 && error.body.error_code === "rejected_by_policy"
      && error.body.warnings.length > 0) {
      setBlockedByFormat(true);
      return;
    }
    setNotice(apiErrorMessage(error, fallback));
  }
  function begin(next: Busy) {
    setBusy(next);
    setNotice(null);
    setBlockedByFormat(false);
  }

  const publicationKey = draftVersionId ? `${reviewDraftKey(actorId, workspaceId, `context-page:${detail.id}`)}.reader-publication` : null;
  const preparedRef = useRef<{ key: string; command: PreparedPagePublication } | null>(null);

  async function publish() {
    if (!draftVersionId || !shownPreview || !previewMatchesScreen || busy) return;
    begin("publish");
    const key = `${publicationKey}:${previewKey}`;
    try {
      await merge.runMergeWrite({
        kind: "publish",
        workspaceId,
        pageId: detail.id,
        publishedVersionNumber: publishedNo,
        preview: shownPreview,
        write: (choice) => publishPageVersion(workspaceId, detail.id, draftVersionId, {
          reason: "Published from the Context reader.",
          publish_despite_findings: false,
          expected_content_hash: detail.version_info?.content_hash ?? shownPreview.content_hash,
          expected_references_hash: detail.references_hash ?? shownPreview.references_hash,
          // This bar showed no open review: a review opened meanwhile refuses.
          expected_review_item_id: null,
        }, {
          mergeChoice: choice,
          preparedPublication: preparedRef.current?.key === key ? preparedRef.current.command : undefined,
          onPublicationPrepared: (command) => { preparedRef.current = { key, command }; },
        }),
      });
      preparedRef.current = null;
      selectVersion(null);
      await onDecided(t("versionBar.publishedNotice"));
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) preparedRef.current = null;
      await fail(error, t("drafts.actionError"));
    } finally {
      setBusy(null);
    }
  }

  // Review decisions. An approval whose answer was lost is resent with the
  // decision id and pins it was first sent with.
  const pendingKey = openReview
    ? `${reviewDraftKey(actorId, workspaceId, openReview.review_item_id)}.pending`
    : null;
  function readPendingApproval(): { decisionId: string; mergeBinding: ContextMergeBinding | null } | null {
    if (!pendingKey || !openReview) return null;
    try {
      const value = JSON.parse(readReviewDraft(pendingKey) || "null");
      if (value?.decision === "approve" && value.workspaceId === workspaceId
        && value.reviewId === openReview.review_item_id && typeof value.decisionId === "string") {
        return value as { decisionId: string; mergeBinding: ContextMergeBinding | null };
      }
    } catch { /* The review stays decidable when storage is unavailable. */ }
    return null;
  }

  async function decide(type: "approve" | "request_changes") {
    if (!openReview || !pendingKey || busy) return;
    if (type === "approve" && (!impactReady || !draftVersionId)) return;
    const pending = type === "approve" ? readPendingApproval() : null;
    if (type === "approve" && !pending && (!shownPreview || !previewMatchesScreen)) return;
    begin(type === "approve" ? "approve" : "request_changes");
    const decisionId = pending?.decisionId ?? crypto.randomUUID();
    const settle = () => writeReviewDraft(pendingKey, "");
    const send = async (mergeBinding: ContextMergeBinding | null) => {
      if (type === "approve") {
        writeReviewDraft(pendingKey, JSON.stringify({ decisionId, workspaceId, reviewId: openReview.review_item_id,
          decision: "approve", reason: null, mergeBinding }));
      }
      try {
        const decided = await decideReview(workspaceId, openReview.review_item_id, type,
          type === "request_changes" ? reason : undefined, { decision_id: decisionId, merge_binding: mergeBinding });
        settle();
        return decided;
      } catch (error) {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) settle();
        throw error;
      }
    };
    try {
      let result: DecisionResult;
      if (type === "approve" && !pending) {
        // Approve the draft on screen, never another one: the review is this
        // draft's, and the pins are the merge shown for its revision, so a
        // review of any other version cannot match them.
        result = await merge.runMergeWrite({
          kind: "approve",
          workspaceId,
          pageId: detail.id,
          publishedVersionNumber: publishedNo,
          preview: shownPreview!,
          write: (choice) => send(decisionPins(choice)),
        });
      } else {
        result = await send(pending?.mergeBinding ?? null);
      }
      setReasonOpen(false);
      setReason("");
      if (result.published_version_id) {
        selectVersion(null);
        const settled = result.superseded_review_count ?? 0;
        await onDecided(settled > 0
          ? t("pendingReview.approveSuccessSettled", { count: settled })
          : t("pendingReview.approveSuccess"));
      } else {
        await onDecided(t("versionBar.changesRequestedNotice"));
      }
    } catch (error) {
      await fail(error, type === "approve" ? t("pendingReview.approveError") : t("pendingReview.error"));
    } finally {
      setBusy(null);
    }
  }

  async function mergePublished() {
    if (!draftVersionId || !shownPreview || !previewMatchesScreen || busy) return;
    begin("merge");
    try {
      await merge.runMergeWrite({
        kind: "bring_in",
        workspaceId,
        pageId: detail.id,
        publishedVersionNumber: publishedNo,
        preview: shownPreview,
        write: (choice) => bringInPageVersion(workspaceId, detail.id, draftVersionId, choice),
      });
      await onRefresh();
    } catch (error) {
      await fail(error, t("drafts.actionError"));
    } finally {
      setBusy(null);
    }
  }

  async function discard() {
    if (!draftVersionId || busy) return;
    begin("discard");
    try {
      await discardPageEdit(workspaceId, detail.id, draftVersionId);
      setConfirmDiscard(false);
      selectVersion(null);
      await onChanged();
    } catch (error) {
      setConfirmDiscard(false);
      await fail(error, t("drafts.actionError"));
    } finally {
      setBusy(null);
    }
  }

  const openTotal = summary?.total ?? detail.open_drafts?.total ?? 0;
  if (!draft && openTotal === 0) return null;

  // --- render -------------------------------------------------------------
  const author = draft?.author.display_name || t("drafts.unknownAuthor");
  const draftItems = summary?.items ?? [];
  const viewedIsMine = draftItems.find((item) => item.version_id === draftVersionId)?.is_mine ?? false;
  const behind = draft?.is_behind === true && Boolean(publishedId);
  const conflictCount = shownPreview?.kind === "conflict" ? mergeOverlapCount(shownPreview) : 0;
  const ready = Boolean(shownPreview && previewMatchesScreen);
  const publishLabel = openReview ? t("versionBar.approvePublish") : t("versionBar.publish");

  return (
    <section aria-label={t("versionBar.label")} className="context-version-bar">
      <div className="context-version-bar-head">
        <div className="context-version-switcher" ref={switcherRef}>
          <button
            aria-controls={menuId}
            aria-expanded={menuOpen}
            aria-label={t("versionBar.switch")}
            className="context-version-switch"
            onClick={() => {
              const opening = !menuOpen;
              setMenuOpen(opening);
              if (opening) void loadDrafts(false);
            }}
            ref={triggerRef}
            type="button"
          >
            <span className="context-version-switch-label">
              {draft ? (
                <>
                  <strong className="is-draft">{t("versionBar.draft")}</strong>
                  <span>· {author}</span>
                  {viewedIsMine ? <span className="context-version-mine">{t("drafts.mine")}</span> : null}
                </>
              ) : earlier ? (
                <>
                  <strong className="is-earlier">
                    {earlier.history_status
                      ? t(contextHistoryPresentation(earlier.history_status, earlier.closed_reason).labelKey)
                      : t("versionBar.earlier")}
                  </strong>
                  {/* The detail does not carry this version's number; its time names it. */}
                  {earlier.created_at ? <span className="context-version-sub">{formatVersionTimestamp(earlier.created_at)}</span> : null}
                </>
              ) : (
                <>
                  <strong className="is-published">{t("versionBar.published")}</strong>
                  {publishedNo != null ? <span className="context-version-sub">v{publishedNo}</span> : null}
                </>
              )}
            </span>
            <ChevronDown aria-hidden="true" size={16} />
          </button>
          {menuOpen ? (
            <div className="context-version-menu" id={menuId}>
              {publishedId ? (
                <button
                  aria-current={!draft && !earlier ? "true" : undefined}
                  className="context-version-item"
                  onClick={() => selectVersion(null)}
                  type="button"
                >
                  <span>
                    <strong>{t("versionBar.published")}</strong>
                    {publishedNo != null ? ` v${publishedNo}` : null}
                  </span>
                  <small>{t("versionBar.readersSee")}</small>
                </button>
              ) : null}
              <p className="context-version-menu-label">{t("drafts.count", { count: openTotal })}</p>
              {draftItems.length ? (
                <ul>
                  {draftItems.map((item) => (
                    <li key={item.version_id}>
                      <button
                        aria-current={item.version_id === draftVersionId ? "true" : undefined}
                        className="context-version-item"
                        onClick={() => selectVersion(item.version_id)}
                        type="button"
                      >
                        <span>
                          {item.author.display_name || t("drafts.unknownAuthor")}
                          {item.is_mine ? <span className="context-version-mine">{t("drafts.mine")}</span> : null}
                        </span>
                        <small>
                          {item.state === "reviewing" ? t("versionBar.reviewWaiting") : t("drafts.writing")}
                          {" · "}
                          <time dateTime={item.last_saved_at}>
                            {t("drafts.lastSaved", { time: formatVersionTimestamp(item.last_saved_at) })}
                          </time>
                        </small>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : draftsLoading ? null : <p className="context-version-menu-empty">{t("drafts.empty")}</p>}
              {draftsError ? <p role="alert">{t("drafts.loadError")}</p> : null}
              {summary?.next_cursor ? (
                <button className="text-button" disabled={draftsLoading} onClick={() => void loadDrafts(true)} type="button">
                  {draftsLoading ? t("drafts.loading") : t("drafts.more")}
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="context-version-state">
          {draft ? (
            <>
              {openReview?.status === "changes_requested" ? (
                <StatusPill compact status="changes_requested" label={t("versionBar.changesRequested")} />
              ) : openReview ? (
                <StatusPill compact status="pending" label={t("versionBar.reviewWaiting")} />
              ) : (
                <StatusPill compact status="draft" tone="muted" label={t("drafts.writing")} />
              )}
              {behind ? <StatusPill compact status="pending" label={t("drafts.behind")} /> : null}
              {openReview?.risk_level ? (
                <span>{t("pendingReview.risk", { risk: tShell(`status.${openReview.risk_level}`, { defaultValue: openReview.risk_level }) })}</span>
              ) : null}
              <time dateTime={draft.last_saved_at}>
                {t("drafts.lastSaved", { time: formatVersionTimestamp(draft.last_saved_at) })}
              </time>
            </>
          ) : (
            <>
              <span>{t("drafts.count", { count: openTotal })}</span>
              {detail.open_review_count > 0 ? (
                <StatusPill
                  compact
                  status="pending"
                  label={t("versionBar.reviewWaitingCount", { count: detail.open_review_count })}
                />
              ) : null}
            </>
          )}
        </div>
      </div>
      {draft && publishedId && shownPreview?.kind === "automatic_merge" && shownPreview.result ? (
        <div className="context-version-merge is-merge" role="status">
          <span>{t("versionBar.mergeAutomatic")}</span>
          <details>
            <summary>{t("versionBar.mergeShowResult")}</summary>
            <MarkdownView body={shownPreview.result.body_md} workspaceId={workspaceId} />
          </details>
        </div>
      ) : null}
      {draft && conflictCount > 0 ? (
        <p className="context-version-merge is-conflict" role="status">
          {t("versionBar.mergeConflict", { count: conflictCount })}
        </p>
      ) : null}
      {draft && currentPreview?.status === "error" ? (
        <p className="context-version-merge is-conflict" role="alert">
          {t("versionBar.mergeLoadError")}{" "}
          <button className="text-button" onClick={() => setPreviewReload((value) => value + 1)} type="button">
            {t("versionBar.mergeRetry")}
          </button>
        </p>
      ) : null}
      {draft && draftVersionId ? (
        <div aria-label={t("drafts.actionsLabel")} className="context-version-actions" role="group">
          <div className="context-version-group">
            <button className="icon-button text-button context-version-action" onClick={() => setCompareOpen(true)} type="button">
              <GitCompareArrows aria-hidden="true" size={16} />
              <span>{t("versionBar.compare")}</span>
            </button>
            {openReview ? (
              <Link
                className="icon-button text-button context-version-action"
                to={`${workspacePath(workspaceId, `review/${encodeURIComponent(openReview.review_item_id)}`)}${openReview.status === "changes_requested" ? "?status=changes_requested" : ""}`}
              >
                <ClipboardList aria-hidden="true" size={16} />
                <span>{t("versionBar.review")}</span>
              </Link>
            ) : null}
            {behind ? (
              <button
                className="icon-button text-button context-version-action"
                disabled={busy !== null || !ready}
                onClick={() => void mergePublished()}
                title={t("versionBar.mergePublishedHint")}
                type="button"
              >
                <GitMerge aria-hidden="true" size={16} />
                <span>{busy === "merge" ? t("versionBar.merging") : t("versionBar.mergePublished")}</span>
              </button>
            ) : null}
            <button
              className="icon-button text-button danger context-version-action"
              disabled={busy !== null}
              onClick={() => setConfirmDiscard(true)}
              type="button"
            >
              <span>{t("versionBar.discard")}</span>
            </button>
          </div>
          <div className="context-version-group">
            {openReview ? (
              <button
                aria-expanded={reasonOpen}
                className="icon-button warning context-version-action"
                disabled={busy !== null}
                onClick={() => setReasonOpen((open) => !open)}
                type="button"
              >
                <MessageSquare aria-hidden="true" size={16} />
                <span>{t("pendingReview.requestChanges")}</span>
              </button>
            ) : null}
            <button
              className="icon-button success context-version-action"
              disabled={busy !== null || !ready || !impactReady}
              onClick={() => void (openReview ? decide("approve") : publish())}
              type="button"
            >
              <Check aria-hidden="true" size={16} />
              <span>{busy === "publish" || busy === "approve" ? t("versionBar.publishing") : publishLabel}</span>
            </button>
          </div>
        </div>
      ) : null}
      {openReview && openReview.status === "pending" && otherReviewCount != null && otherReviewCount > 0 ? (
        <p className="context-version-note">{t("versionBar.approveImpact", { count: otherReviewCount })}</p>
      ) : null}
      {openReview && openReview.status === "pending" && impactFailed ? (
        <p className="context-version-error">{t("pendingReview.impactLoadError")}</p>
      ) : null}
      {openReview && reasonOpen ? (
        <div className="context-version-reason">
          <textarea
            aria-label={t("pendingReview.requestChanges")}
            onChange={(event) => setReason(event.target.value)}
            placeholder={t("pendingReview.reasonPlaceholder")}
            value={reason}
          />
          <button
            className="icon-button warning context-version-action"
            disabled={busy !== null || !reason.trim()}
            onClick={() => void decide("request_changes")}
            type="button"
          >
            <Send aria-hidden="true" size={16} />
            <span>{t("pendingReview.submitChanges")}</span>
          </button>
        </div>
      ) : null}
      {blockedByFormat && draftVersionId ? (
        <p className="context-version-error" role="alert">
          {t("versionBar.blockedByFormat")}{" "}
          <Link to={`${contextEditHref(workspaceId, detail.id)}?version_id=${encodeURIComponent(draftVersionId)}`}>
            {t("versionBar.openEditor")}
          </Link>
        </p>
      ) : null}
      {notice ? <p className="context-version-error" role="alert">{notice}</p> : null}
      {confirmDiscard ? (
        <ConfirmDialog
          busy={busy === "discard"}
          busyLabel={t("versionBar.discarding")}
          confirmLabel={t("versionBar.discard")}
          labelledBy={discardTitleId}
          onCancel={() => setConfirmDiscard(false)}
          onConfirm={() => void discard()}
          title={t("versionBar.discardTitle")}
          tone="danger"
        >
          <p>{t("versionBar.discardConfirm")}</p>
        </ConfirmDialog>
      ) : null}
      {compareOpen && draftVersionId ? (
        <PublishResultDiff
          detail={detail}
          draftVersionId={draftVersionId}
          narrow={narrow}
          onClose={() => setCompareOpen(false)}
          onStale={() => void showChanged()}
          workspaceId={workspaceId}
        />
      ) : null}
      {merge.dialog}
    </section>
  );
}

/**
 * What publishing this draft changes for readers: the current published
 * version against the publish result -- the merge, when the draft is behind,
 * so changes published meanwhile do not show as reverted. A conflict has no
 * single result yet, so it shows the published version against the draft.
 */
function PublishResultDiff({
  detail,
  draftVersionId,
  narrow,
  onClose,
  onStale,
  workspaceId,
}: {
  detail: ContextPageDetail;
  draftVersionId: string;
  narrow: boolean;
  onClose: () => void;
  /** The comparison is of a newer draft or published version than the screen shows. */
  onStale: () => void;
  workspaceId: string;
}) {
  const { t } = useTranslation("context");
  const [state, setState] = useState<{ diff: PageVersionDiff | null; error: string | null; conflict: boolean } | null>(null);
  const onStaleRef = useRef(onStale);
  useEffect(() => { onStaleRef.current = onStale; }, [onStale]);
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const failed = t("versionBar.compareFailed");
    void diffPageVersions(workspaceId, detail.id, null, draftVersionId, { signal: controller.signal, publishResult: true })
      .then((diff) => {
        if (!active) return;
        // Only the draft revision and published version on screen are compared.
        const moved = (detail.editing_revision != null && diff.editing_revision != null
          && diff.editing_revision !== detail.editing_revision)
          || (diff.published_version_id ?? null) !== (detail.current_published_version_id ?? null);
        if (moved) {
          setState({ diff: null, error: t("versionBar.changed"), conflict: false });
          onStaleRef.current();
          return;
        }
        setState({ diff, error: null, conflict: false });
      })
      .catch(async (error: unknown) => {
        if (!active || isAbortError(error)) return;
        const publishedId = detail.current_published_version_id;
        if (error instanceof ApiError && error.body.error_code === "context_merge_conflict" && publishedId) {
          try {
            const diff = await diffPageVersions(workspaceId, detail.id, publishedId, draftVersionId, { signal: controller.signal });
            if (!active) return;
            if (detail.editing_revision != null && diff.editing_revision != null
              && diff.editing_revision !== detail.editing_revision) {
              setState({ diff: null, error: t("versionBar.changed"), conflict: true });
              onStaleRef.current();
              return;
            }
            setState({ diff, error: null, conflict: true });
          } catch (fallback) {
            if (active && !isAbortError(fallback)) setState({ diff: null, error: apiErrorMessage(fallback, failed), conflict: true });
          }
          return;
        }
        setState({ diff: null, error: apiErrorMessage(error, failed), conflict: false });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [detail.current_published_version_id, detail.editing_revision, detail.id, draftVersionId, t, workspaceId]);
  const publishedNo = state?.diff?.from_version_no ?? detail.published_version_info?.version_no ?? null;
  return (
    <DiffViewerDialog
      baseEndpointLabel={publishedNo
        ? t("versionBar.comparePublished", { version: publishedNo })
        : t("versionBar.compareEmpty")}
      comparedEndpointLabel={state?.conflict ? t("versionBar.compareDraft") : t("versionBar.compareResult")}
      defaultMode={narrow ? "unified" : "split"}
      diff={state?.diff ?? null}
      error={state?.error ?? null}
      fullScreen={narrow}
      loading={state === null}
      onClose={onClose}
      showChangeMarkers
      title={state?.conflict ? t("versionBar.compareConflictTitle") : t("versionBar.compareTitle")}
      warning={state?.conflict ? t("versionBar.compareConflictWarning") : null}
    />
  );
}
