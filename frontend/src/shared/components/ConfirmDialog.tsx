import type { ReactNode, Ref } from "react";
import { useTranslation } from "react-i18next";

import { Dialog } from "./Dialog";
import { StatusBanner } from "./StatusBanner";

type ConfirmDialogProps = {
  busy?: boolean;
  /** Replaces the confirm label while `busy`. */
  busyLabel?: string;
  /** The question's details: copy, warnings, and any required input. */
  children: ReactNode;
  className?: string;
  closeLabel?: string;
  /** Holds the confirm button disabled, for example until a reason is typed. */
  confirmDisabled?: boolean;
  confirmLabel: string;
  confirmRef?: Ref<HTMLButtonElement>;
  error?: ReactNode;
  labelledBy: string;
  onCancel: () => void;
  onConfirm: () => void;
  restoreFocusFallback?: () => HTMLElement | null;
  title: ReactNode;
  tone?: "primary" | "success" | "warning" | "danger";
};

/**
 * A question with one answer: the shared title bar, the caller's details, then
 * Cancel and a single confirm button in the dialog action band. Nothing can
 * dismiss the dialog while `busy`, so the result stays on screen.
 */
export function ConfirmDialog({
  busy = false,
  busyLabel,
  children,
  className,
  closeLabel,
  confirmDisabled = false,
  confirmLabel,
  confirmRef,
  error,
  labelledBy,
  onCancel,
  onConfirm,
  restoreFocusFallback,
  title,
  tone = "primary",
}: ConfirmDialogProps) {
  const { t } = useTranslation("shell");

  function requestCancel() {
    if (!busy) {
      onCancel();
    }
  }

  return (
    <Dialog
      className={className}
      closeLabel={closeLabel}
      dismissible={!busy}
      labelledBy={labelledBy}
      onClose={requestCancel}
      restoreFocusFallback={restoreFocusFallback}
      title={title}
    >
      {children}
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      <div className="dialog-actions">
        <button
          className="icon-button text-button"
          disabled={busy}
          onClick={requestCancel}
          type="button"
        >
          {t("actions.cancel", { defaultValue: "Cancel" })}
        </button>
        <button
          className={`icon-button ${tone}`}
          disabled={busy || confirmDisabled}
          onClick={onConfirm}
          ref={confirmRef}
          type="button"
        >
          {busy && busyLabel ? busyLabel : confirmLabel}
        </button>
      </div>
    </Dialog>
  );
}
