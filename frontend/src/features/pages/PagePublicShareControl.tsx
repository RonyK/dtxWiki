import { Check, Copy, ExternalLink, Eye, Globe, Info, Lock, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../shared/api/client";
import { setPagePublic } from "../../shared/api/operations";
import { readPage, type PagePayload } from "../../shared/api/pages";
import { Dialog } from "../../shared/components/Dialog";
import { StatusBanner } from "../../shared/components/StatusBanner";
import {
  copyTextWithFallback,
  type CopyTextResult,
} from "../../shared/lib/copyText";
import { MarkdownView } from "./MarkdownView";

export function PagePublicShareControl({
  canPublish,
  canUnpublish,
  isPublic,
  onPublicChange,
  onSavingChange,
  pageId,
  publishDisabledReason = null,
  publishedVersionId = null,
  publishedVersionNo = null,
  slug,
  workspaceId,
  workspaceSlug,
  workspaceVisibility,
}: {
  canPublish: boolean;
  canUnpublish: boolean;
  isPublic: boolean;
  onPublicChange: (isPublic: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  pageId: string;
  publishDisabledReason?: string | null;
  publishedVersionId?: string | null;
  publishedVersionNo?: number | null;
  slug: string;
  workspaceId: string | null;
  workspaceSlug: string | null;
  workspaceVisibility: string | null;
}) {
  const { t } = useTranslation("pages");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copyFeedback, setCopyFeedback] = useState<CopyTextResult | null>(null);
  const [acknowledgedExposure, setAcknowledgedExposure] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewPage, setPreviewPage] = useState<PagePayload | null>(null);
  const [latestPublishedVersionId, setLatestPublishedVersionId] = useState(
    publishedVersionId,
  );
  const [latestPublishedVersionNo, setLatestPublishedVersionNo] = useState(
    publishedVersionNo,
  );
  const publicUrlRef = useRef<HTMLElement | null>(null);

  const publicPath = workspaceSlug ? `/w/${workspaceSlug}/pages/${slug}` : null;
  const publicUrl =
    publicPath && typeof window !== "undefined"
      ? `${window.location.origin}${publicPath}`
      : publicPath;
  const requiresExposureAcknowledgement = workspaceVisibility !== "public";
  const publishedVersionLabel = latestPublishedVersionNo
    ? t("reader.visibilityPublishedVersion", { version: latestPublishedVersionNo })
    : t("reader.visibilityPublishedSnapshot");

  useEffect(() => {
    setAcknowledgedExposure(false);
    setPreviewOpen(false);
    setPreviewPage(null);
    setPreviewError(null);
    setLatestPublishedVersionId(publishedVersionId);
    setLatestPublishedVersionNo(publishedVersionNo);
  }, [pageId, publishedVersionId, publishedVersionNo, workspaceVisibility]);

  useEffect(() => {
    if (!copyFeedback) {
      return undefined;
    }
    const timer = window.setTimeout(
      () => setCopyFeedback(null),
      copyFeedback === "copied" ? 1800 : 4000,
    );
    return () => window.clearTimeout(timer);
  }, [copyFeedback]);

  async function toggle() {
    const next = !isPublic;
    if (saving || !workspaceId || (next ? !canPublish : !canUnpublish)) {
      return;
    }
    setSaving(true);
    onSavingChange?.(true);
    setError(null);
    try {
      const updated = await setPagePublic(workspaceId, pageId, next, {
        acknowledgePublicExposure:
          next && requiresExposureAcknowledgement && acknowledgedExposure,
        expectedPublishedVersionId: next ? latestPublishedVersionId : null,
      });
      onPublicChange(updated.is_public);
      setAcknowledgedExposure(false);
    } catch (exc) {
      if (exc instanceof ApiError) {
        if (exc.body.error_code === "page_has_no_published_version") {
          setError(t("reader.visibilityNeedsPublishedVersion"));
        } else if (exc.body.error_code === "workspace_public_sharing_disabled") {
          setError(t("reader.visibilityPrerequisites"));
        } else if (
          exc.body.error_code === "public_exposure_acknowledgement_required"
        ) {
          setError(t("reader.visibilityAcknowledgementRequired"));
        } else if (exc.body.error_code === "public_share_version_changed") {
          setAcknowledgedExposure(false);
          try {
            if (!workspaceId) {
              throw new Error("workspace unavailable");
            }
            const refreshed = await readPage(workspaceId, pageId);
            setLatestPublishedVersionId(
              refreshed.current_published_version_id ?? null,
            );
            setLatestPublishedVersionNo(refreshed.published_version_no ?? null);
            setPreviewPage(null);
            setError(t("reader.visibilityVersionChanged"));
          } catch {
            setError(t("reader.visibilityVersionRefreshFailed"));
          }
        } else if (exc.body.error_code === "public_exposure_forbidden") {
          setError(t("reader.visibilityOwnerRequired"));
        } else if (exc.status === 403) {
          setError(t("reader.visibilityForbidden"));
        } else {
          setError(t("reader.visibilityFailed"));
        }
      } else {
        setError(t("reader.visibilityFailed"));
      }
    } finally {
      setSaving(false);
      onSavingChange?.(false);
    }
  }

  async function copyUrl() {
    if (!publicUrl) {
      return;
    }
    setCopyFeedback(await copyTextWithFallback(publicUrl, publicUrlRef.current));
  }

  async function openPublishedPreview() {
    if (!latestPublishedVersionId || !workspaceId || previewLoading) {
      return;
    }
    setPreviewOpen(true);
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      setPreviewPage(
        await readPage(workspaceId, pageId, latestPublishedVersionId),
      );
    } catch {
      setPreviewError(t("reader.visibilityPreviewFailed"));
    } finally {
      setPreviewLoading(false);
    }
  }

  return (
    <div className="publish-control">
      {latestPublishedVersionId ? (
        <div className="publish-version-summary">
          <div>
            <span className="status-pill status-info">{publishedVersionLabel}</span>
            <span>{t("reader.visibilityPublishedVersionHelp")}</span>
          </div>
          <button
            className="icon-button text-button"
            onClick={() => void openPublishedPreview()}
            type="button"
          >
            <Eye aria-hidden="true" size={16} />
            <span>{t("reader.visibilityPreviewPublished")}</span>
          </button>
        </div>
      ) : null}
      {canPublish && !isPublic && requiresExposureAcknowledgement ? (
        <label className="checkbox-field public-exposure-acknowledgement">
          <input
            checked={acknowledgedExposure}
            onChange={(event) => setAcknowledgedExposure(event.target.checked)}
            type="checkbox"
          />
          <span>
            <strong>{t("reader.visibilityAcknowledgementTitle")}</strong>
            {t("reader.visibilityAcknowledgementBody")}
          </span>
        </label>
      ) : null}
      {(!isPublic && canPublish) || (isPublic && canUnpublish) ? (
        <button
          aria-pressed={isPublic}
          className="icon-button text-button"
          aria-describedby={
            !isPublic && publishDisabledReason
              ? "public-share-disabled-reason"
              : undefined
          }
          disabled={
            saving ||
            (!isPublic && Boolean(publishDisabledReason)) ||
            (!isPublic && requiresExposureAcknowledgement && !acknowledgedExposure)
          }
          onClick={() => void toggle()}
          type="button"
        >
          {isPublic ? (
            <Globe size={16} aria-hidden="true" />
          ) : (
            <Lock size={16} aria-hidden="true" />
          )}
          <span>
            {saving
              ? t("reader.visibilitySaving")
              : isPublic
                ? t("reader.visibilityUnpublish")
                : t("reader.visibilityMakePublic")}
          </span>
        </button>
      ) : null}
      {!isPublic && publishDisabledReason ? (
        <div
          className="publish-control-disabled-reason"
          id="public-share-disabled-reason"
          role="note"
        >
          <Info size={16} aria-hidden="true" />
          <span>{publishDisabledReason}</span>
        </div>
      ) : null}
      {isPublic && publicPath ? (
        <div className="publish-control-url">
          <code ref={publicUrlRef} title={publicUrl ?? publicPath}>
            {publicPath}
          </code>
          <button
            aria-label={t("reader.visibilityCopyLink")}
            className="icon-button"
            onClick={() => void copyUrl()}
            title={t("reader.visibilityCopyLink")}
            type="button"
          >
            {copyFeedback === "copied" ? (
              <Check size={14} aria-hidden="true" />
            ) : (
              <Copy size={14} aria-hidden="true" />
            )}
          </button>
          <a
            className="icon-button text-button"
            href={publicPath}
            rel="noopener noreferrer"
            target="_blank"
          >
            <ExternalLink aria-hidden="true" size={14} />
            <span>
              {latestPublishedVersionNo
                ? t("reader.visibilityOpenLiveVersion", {
                    version: latestPublishedVersionNo,
                  })
                : t("reader.visibilityOpenLiveSnapshot")}
            </span>
          </a>
          {copyFeedback ? (
            <span className="reader-action-status" role="status">
              {copyFeedback === "copied"
                ? t("reader.visibilityLinkCopied")
                : copyFeedback === "selected"
                  ? t("reader.visibilityLinkSelected")
                  : t("reader.visibilityCopyUnavailable")}
            </span>
          ) : null}
        </div>
      ) : null}
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      {previewOpen ? (
        <Dialog
          className="published-version-preview-dialog"
          labelledBy="published-version-preview-title"
          onClose={() => setPreviewOpen(false)}
        >
          <section className="dialog-form">
            <header className="dialog-header">
              <div>
                <p className="eyebrow">{t("reader.visibilityPreviewEyebrow")}</p>
                <h2 id="published-version-preview-title">{publishedVersionLabel}</h2>
                <p className="feature-subtitle">
                  {t("reader.visibilityPreviewHelp")}
                </p>
              </div>
              <button
                aria-label={t("reader.visibilityClosePreview")}
                className="icon-button"
                onClick={() => setPreviewOpen(false)}
                type="button"
              >
                <X aria-hidden="true" size={16} />
              </button>
            </header>
            {previewLoading ? (
              <p role="status">{t("reader.visibilityPreviewLoading")}</p>
            ) : null}
            {previewError ? <StatusBanner tone="warning">{previewError}</StatusBanner> : null}
            {!previewLoading && !previewError && previewPage ? (
              <article className="published-version-preview-body">
                <h1>{previewPage.title}</h1>
                <MarkdownView
                  allowWorkspaceNavigationLinks
                  body={previewPage.body_md}
                  stripLeadingHeading={previewPage.title}
                  workspaceId={workspaceId}
                />
              </article>
            ) : null}
          </section>
        </Dialog>
      ) : null}
    </div>
  );
}
