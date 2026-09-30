import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Dialog, DialogCloseButton } from "./Dialog";
import { DiffViewer } from "./DiffViewer";
import type { DiffViewerDiff, DiffViewerMode } from "./DiffViewer";
import { Spinner } from "./Spinner";
import { StatusBanner } from "./StatusBanner";

type DiffViewerDialogProps = {
  actionError?: string | null;
  baseEndpointLabel?: string;
  comparedEndpointLabel?: string;
  controls?: ReactNode;
  defaultMode?: DiffViewerMode;
  diff: DiffViewerDiff | null;
  error?: string | null;
  fullScreen?: boolean;
  loading?: boolean;
  onClose: () => void;
  onRestore?: () => void;
  restoreDisabled?: boolean;
  restoreLabel?: string;
  publishedEndpoint?: "from" | "to" | null;
  publishedVersionLabel?: string;
  showChangeMarkers?: boolean;
  title: string;
  warning?: string | null;
};

export function DiffViewerDialog({
  actionError,
  baseEndpointLabel,
  comparedEndpointLabel,
  controls,
  defaultMode = "split",
  diff,
  error,
  fullScreen = false,
  loading = false,
  onClose,
  onRestore,
  restoreDisabled = false,
  restoreLabel,
  publishedEndpoint = null,
  publishedVersionLabel,
  showChangeMarkers = false,
  title,
  warning,
}: DiffViewerDialogProps) {
  const { t } = useTranslation("shell");
  const closeLabel = t("diff.close", { defaultValue: "Close diff" });
  function requestClose() {
    if (!restoreDisabled) {
      onClose();
    }
  }
  return (
    <Dialog
      className={`diff-dialog-panel ${fullScreen ? "is-mobile-fullscreen" : ""}`.trim()}
      dismissible={!restoreDisabled}
      labelledBy="diff-dialog-title"
      onClose={requestClose}
    >
      <section className="diff-dialog">
        <header className="dialog-header">
          <div>
            <p className="eyebrow">
              {t("diff.eyebrow", { defaultValue: "Version history" })}
            </p>
            <h2 id="diff-dialog-title">{title}</h2>
          </div>
          <DialogCloseButton
            disabled={restoreDisabled}
            onClick={requestClose}
            label={closeLabel}
          />
        </header>
        {controls}
        {publishedVersionLabel && !diff ? (
          <p className="diff-viewer-published-version">
            <strong>{publishedVersionLabel}</strong>
          </p>
        ) : null}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {warning ? <StatusBanner tone="warning">{warning}</StatusBanner> : null}
        {actionError ? <StatusBanner tone="warning">{actionError}</StatusBanner> : null}
        {loading ? (
          <p className="page-history-empty diff-dialog-loading">
            <Spinner aria-label={t("diff.loading", { defaultValue: "Loading diff" })} />
          </p>
        ) : null}
        {!loading && diff ? (
          <DiffViewer
            baseEndpointLabel={baseEndpointLabel}
            className="diff-dialog-viewer"
            comparedEndpointLabel={comparedEndpointLabel}
            defaultMode={defaultMode}
            diff={diff}
            publishedEndpoint={publishedEndpoint}
            publishedVersionLabel={publishedVersionLabel}
            showChangeMarkers={showChangeMarkers}
          />
        ) : null}
        {onRestore ? (
          <footer className="dialog-actions">
            <button
              aria-busy={restoreDisabled}
              className="icon-button primary"
              disabled={restoreDisabled}
              onClick={onRestore}
              type="button"
            >
              {restoreDisabled
                ? t("diff.restoring", { defaultValue: "Restoring" })
                : restoreLabel ??
                  t("diff.restore", { defaultValue: "Restore this version" })}
            </button>
          </footer>
        ) : null}
      </section>
    </Dialog>
  );
}
