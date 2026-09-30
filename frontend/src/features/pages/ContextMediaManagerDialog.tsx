import { RefreshCw, Trash2, X } from "lucide-react";
import type { TFunction } from "i18next";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../shared/api/client";
import {
  deletePageContextMedia,
  listPageContextMedia,
  type ContextMediaAsset,
  type ContextMediaUsageState,
} from "../../shared/api/contextMedia";
import { Dialog } from "../../shared/components/Dialog";
import { StatusBanner } from "../../shared/components/StatusBanner";

type ContextMediaManagerDialogProps = {
  onClose: () => void;
  pageId: string;
  workspaceId: string;
};

type MediaListState = {
  error: boolean;
  loading: boolean;
  unused: ContextMediaAsset[];
  used: ContextMediaAsset[];
};

type DeleteErrorKind = "changed" | "delete" | "inUse";
type MediaGroupKind = "unused" | "used";

const EMPTY_MEDIA_LIST_STATE: MediaListState = {
  error: false,
  loading: true,
  unused: [],
  used: [],
};

export function ContextMediaManagerDialog({
  onClose,
  pageId,
  workspaceId,
}: ContextMediaManagerDialogProps) {
  const { t } = useTranslation("pages");
  const [media, setMedia] = useState<MediaListState>(EMPTY_MEDIA_LIST_STATE);
  const [pendingDeleteAsset, setPendingDeleteAsset] =
    useState<ContextMediaAsset | null>(null);
  const [deletingAssetId, setDeletingAssetId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<DeleteErrorKind | null>(null);
  const [deletedFilename, setDeletedFilename] = useState<string | null>(null);
  const loadRequestRef = useRef(0);

  const loadMedia = useCallback(async () => {
    const requestId = loadRequestRef.current + 1;
    loadRequestRef.current = requestId;
    setMedia((current) => ({ ...current, error: false, loading: true }));
    try {
      const [used, unused] = await Promise.all([
        listPageContextMedia(workspaceId, pageId, "used"),
        listPageContextMedia(workspaceId, pageId, "unused"),
      ]);
      if (loadRequestRef.current !== requestId) {
        return;
      }
      setMedia({ error: false, loading: false, unused, used });
    } catch {
      if (loadRequestRef.current !== requestId) {
        return;
      }
      setMedia((current) => ({
        ...current,
        error: true,
        loading: false,
      }));
    }
  }, [pageId, workspaceId]);

  useEffect(() => {
    void loadMedia();
    return () => {
      loadRequestRef.current += 1;
    };
  }, [loadMedia]);

  function requestClose() {
    if (!deletingAssetId) {
      onClose();
    }
  }

  function requestDelete(asset: ContextMediaAsset) {
    if (!asset.can_delete || deletingAssetId) {
      return;
    }
    setActionError(null);
    setDeletedFilename(null);
    setPendingDeleteAsset(asset);
  }

  async function confirmDelete() {
    const asset = pendingDeleteAsset;
    if (!asset?.can_delete || deletingAssetId) {
      return;
    }
    setDeletingAssetId(asset.id);
    setPendingDeleteAsset(null);
    setActionError(null);
    setDeletedFilename(null);
    try {
      await deletePageContextMedia(workspaceId, asset.id, asset.usage_token);
      setDeletedFilename(asset.original_filename);
      await loadMedia();
    } catch (error) {
      setActionError(deleteErrorKind(error));
    } finally {
      setDeletingAssetId(null);
    }
  }

  return (
    <Dialog
      className="context-media-dialog"
      dismissible={!deletingAssetId}
      labelledBy="context-media-dialog-title"
      onClose={requestClose}
    >
      <section className="context-media-dialog-body">
        <header className="dialog-header">
          <div>
            <h2 id="context-media-dialog-title">{t("mediaManager.title")}</h2>
            <p className="feature-subtitle">
              {t("mediaManager.subtitle")}
            </p>
          </div>
          <button
            aria-label={t("mediaManager.closeAria")}
            className="icon-button"
            disabled={Boolean(deletingAssetId)}
            onClick={requestClose}
            type="button"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </header>

        {pendingDeleteAsset ? (
          <StatusBanner
            action={
              <div className="dialog-actions">
                <button
                  autoFocus
                  className="icon-button text-button"
                  onClick={() => setPendingDeleteAsset(null)}
                  type="button"
                >
                  {t("mediaManager.confirmDelete.cancel")}
                </button>
                <button
                  className="icon-button danger"
                  onClick={() => void confirmDelete()}
                  type="button"
                >
                  {t("mediaManager.confirmDelete.confirm")}
                </button>
              </div>
            }
            tone="warning"
          >
            <strong>
              {t("mediaManager.confirmDelete.title", {
                filename: pendingDeleteAsset.original_filename,
              })}
            </strong>
            <p>{t("mediaManager.confirmDelete.body")}</p>
          </StatusBanner>
        ) : null}
        {deletedFilename ? (
          <StatusBanner tone="info">
            {t("mediaManager.notices.deleted", { filename: deletedFilename })}
          </StatusBanner>
        ) : null}
        {actionError ? (
          <StatusBanner
            action={
              <button
                className="icon-button text-button"
                onClick={() => {
                  setActionError(null);
                  void loadMedia();
                }}
                type="button"
              >
                <RefreshCw size={14} aria-hidden="true" />
                {t("mediaManager.actions.refresh")}
              </button>
            }
            tone="warning"
          >
            {t(`mediaManager.errors.${actionError}`)}
          </StatusBanner>
        ) : null}
        {media.error ? (
          <StatusBanner
            action={
              <button
                className="icon-button text-button"
                onClick={() => void loadMedia()}
                type="button"
              >
                <RefreshCw size={14} aria-hidden="true" />
                {t("mediaManager.actions.retry")}
              </button>
            }
            tone="warning"
          >
            {t("mediaManager.errors.load")}
          </StatusBanner>
        ) : null}

        {media.loading ? (
          <p className="context-media-loading" role="status">
            {t("mediaManager.loading")}
          </p>
        ) : media.error ? null : (
          <div className="context-media-groups">
            <MediaGroup
              assets={media.used}
              confirmingAssetId={pendingDeleteAsset?.id ?? null}
              deletingAssetId={deletingAssetId}
              group="used"
              headingId="context-media-used-heading"
              onDelete={requestDelete}
            />
            <MediaGroup
              assets={media.unused}
              confirmingAssetId={pendingDeleteAsset?.id ?? null}
              deletingAssetId={deletingAssetId}
              group="unused"
              headingId="context-media-unused-heading"
              onDelete={requestDelete}
            />
          </div>
        )}
      </section>
    </Dialog>
  );
}

function MediaGroup({
  assets,
  confirmingAssetId,
  deletingAssetId,
  group,
  headingId,
  onDelete,
}: {
  assets: ContextMediaAsset[];
  confirmingAssetId: string | null;
  deletingAssetId: string | null;
  group: MediaGroupKind;
  headingId: string;
  onDelete: (asset: ContextMediaAsset) => void;
}) {
  const { i18n, t } = useTranslation("pages");
  const heading = t(`mediaManager.groups.${group}.heading`);
  return (
    <section aria-labelledby={headingId} className="context-media-group">
      <div className="context-media-group-heading">
        <h3 id={headingId}>{heading}</h3>
        <span
          aria-label={t(`mediaManager.groups.${group}.count`, {
            count: assets.length,
          })}
        >
          {assets.length}
        </span>
      </div>
      {assets.length === 0 ? (
        <p className="context-media-empty">
          {t(`mediaManager.groups.${group}.empty`)}
        </p>
      ) : (
        <ul
          aria-label={t(`mediaManager.groups.${group}.listAria`)}
          className="context-media-list"
        >
          {assets.map((asset) => (
            <li className="context-media-item" key={asset.id}>
              <img
                alt=""
                className="context-media-preview"
                loading="lazy"
                src={asset.content_url}
              />
              <div className="context-media-item-body">
                <div className="context-media-item-title">
                  <strong>{asset.original_filename}</strong>
                  <span>
                    {t(`mediaManager.usage.${usageStateKey(asset.usage_state)}`)}
                  </span>
                </div>
                <dl className="context-media-facts">
                  <div>
                    <dt>{t("mediaManager.facts.size")}</dt>
                    <dd>{formatBytes(asset.bytes, i18n.language, t)}</dd>
                  </div>
                  <div>
                    <dt>{t("mediaManager.facts.references")}</dt>
                    <dd>
                      {t("mediaManager.facts.referenceSummary", {
                        draftCount: asset.latest_draft_reference_count,
                        historyCount: asset.historical_reference_count,
                        publishedCount: asset.current_published_reference_count,
                      })}
                    </dd>
                  </div>
                </dl>
                {!asset.can_delete ? (
                  <p className="context-media-retained">
                    {t("mediaManager.facts.retained")}
                  </p>
                ) : null}
              </div>
              {asset.can_delete ? (
                <button
                  aria-label={t("mediaManager.actions.deleteAria", {
                    filename: asset.original_filename,
                  })}
                  className="icon-button text-button context-media-delete"
                  disabled={Boolean(deletingAssetId || confirmingAssetId)}
                  onClick={() => void onDelete(asset)}
                  type="button"
                >
                  <Trash2 size={14} aria-hidden="true" />
                  {deletingAssetId === asset.id
                    ? t("mediaManager.actions.deleting")
                    : t("mediaManager.actions.delete")}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function usageStateKey(state: ContextMediaUsageState) {
  if (state === "history_only") {
    return "historyOnly";
  }
  if (state === "unused") {
    return "unused";
  }
  return "active";
}

function formatBytes(bytes: number, language: string, t: TFunction<"pages">) {
  if (bytes < 1024) {
    return t("mediaManager.size.bytes", {
      value: new Intl.NumberFormat(language).format(bytes),
    });
  }
  if (bytes < 1024 * 1024) {
    return t("mediaManager.size.kilobytes", {
      value: new Intl.NumberFormat(language, {
        maximumFractionDigits: 1,
        minimumFractionDigits: 1,
      }).format(bytes / 1024),
    });
  }
  return t("mediaManager.size.megabytes", {
    value: new Intl.NumberFormat(language, {
      maximumFractionDigits: 1,
      minimumFractionDigits: 1,
    }).format(bytes / (1024 * 1024)),
  });
}

function deleteErrorKind(error: unknown): DeleteErrorKind {
  if (error instanceof ApiError && error.status === 412) {
    return "changed";
  }
  if (error instanceof ApiError && error.status === 409) {
    return "inUse";
  }
  return "delete";
}
