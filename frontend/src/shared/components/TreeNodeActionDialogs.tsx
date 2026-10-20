import { textInputLimit } from "../lib/inputPolicy";
import type { FormEvent } from "react";
import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiErrorMessage } from "../api/client";
import type { TreeNodePayload } from "../api/workspace";
import { Dialog, DialogCloseButton } from "./Dialog";
import { StatusBanner } from "./StatusBanner";
import { TreePickerDropdown } from "./TreePickerDropdown";

export function RenameTreeNodeDialog({
  node,
  onClose,
  onSubmit,
}: {
  node: TreeNodePayload;
  onClose: () => void;
  onSubmit: (displayName: string) => Promise<void>;
}) {
  const { t } = useTranslation("tree");
  const [displayName, setDisplayName] = useState(node.title);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const invalid = displayName.trim().length === 0;
  const restoreFocusFallback = useTreeDialogFocusFallback(node.id);

  function requestClose() {
    if (!submitting) {
      onClose();
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (invalid || submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(displayName.trim());
    } catch (exc) {
      setError(apiErrorMessage(exc, t("dialogs.rename.failed")));
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      dismissible={!submitting}
      labelledBy="rename-tree-node-title"
      onClose={requestClose}
      restoreFocusFallback={restoreFocusFallback}
    >
      <form className="dialog-form" onSubmit={submit}>
        <header className="dialog-header">
          <div>
            <p className="eyebrow">{t("dialogs.eyebrow")}</p>
            <h2 id="rename-tree-node-title">{t("dialogs.rename.title")}</h2>
            {node.slug ? (
              <p className="feature-subtitle">{t("dialogs.rename.slug", { slug: node.slug })}</p>
            ) : null}
          </div>
          <DialogCloseButton disabled={submitting} label={t("dialogs.closeDialog")} onClick={requestClose} />
        </header>
        <label className="form-field">
          <span>{t("dialogs.rename.displayName")}</span>
          <input
            aria-label={t("dialogs.rename.displayName")}
            autoFocus
            {...textInputLimit(node.node_kind === "folder" ? 80 : 256)}
            value={displayName}
            onChange={(event) => setDisplayName(event.currentTarget.value)}
          />
          <small className="field-hint">
            {node.is_system_node ? t("dialogs.rename.hintSystem") : t("dialogs.rename.hint")}
          </small>
        </label>
        {error ? <p className="form-error">{error}</p> : null}
        <div className="dialog-actions">
          <button
            className="icon-button text-button"
            disabled={submitting}
            type="button"
            onClick={requestClose}
          >
            {t("dialogs.cancel")}
          </button>
          <button className="icon-button primary" disabled={invalid || submitting} type="submit">
            {submitting ? t("dialogs.rename.submitting") : t("dialogs.rename.submit")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function MoveTreeNodeDialog({
  node,
  targets,
  onClose,
  onSubmit,
}: {
  node: TreeNodePayload;
  targets: TreeNodePayload[];
  onClose: () => void;
  onSubmit: (parentNodeId: string, reason: string) => Promise<void>;
}) {
  const { t } = useTranslation("tree");
  const initialTargetId =
    targets.find((target) => target.id === node.parent_id)?.id ?? targets[0]?.id ?? "";
  const [parentNodeId, setParentNodeId] = useState(initialTargetId);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Moving to the current parent is not a no-op on the backend: it reassigns
  // the sort key and records a change set, so block it instead of submitting.
  const parentUnchanged = parentNodeId === node.parent_id;
  const invalid = !parentNodeId || parentUnchanged;
  const restoreFocusFallback = useTreeDialogFocusFallback(node.id);

  function requestClose() {
    if (!submitting) {
      onClose();
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (invalid || submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(parentNodeId, reason.trim());
    } catch (exc) {
      setError(apiErrorMessage(exc, t("dialogs.move.failed")));
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      dismissible={!submitting}
      labelledBy="move-tree-node-title"
      onClose={requestClose}
      restoreFocusFallback={restoreFocusFallback}
    >
      <form className="dialog-form" onSubmit={submit}>
        <header className="dialog-header">
          <div>
            <p className="eyebrow">{t("dialogs.move.eyebrow")}</p>
            <h2 id="move-tree-node-title">{t("dialogs.move.title", { title: node.title })}</h2>
            {node.children_count > 0 ? (
              <p className="feature-subtitle">{t("dialogs.move.childBranchNotice")}</p>
            ) : null}
          </div>
          <DialogCloseButton disabled={submitting} label={t("dialogs.closeDialog")} onClick={requestClose} />
        </header>
        <label className="form-field">
          <span>{t("dialogs.move.targetParent")}</span>
          <TreePickerDropdown
            ariaLabel={t("dialogs.move.targetParent")}
            disabled={targets.length === 0}
            emptyLabel={t("dialogs.move.noValidParent")}
            nodes={targets}
            selectedId={parentNodeId}
            onChange={setParentNodeId}
          />
          {parentUnchanged ? (
            <small className="field-hint">{t("dialogs.move.parentUnchanged")}</small>
          ) : null}
        </label>
        <label className="form-field">
          <span>{t("dialogs.move.reasonOptional")}</span>
          <input
            aria-label={t("dialogs.move.reasonOptional")}
            {...textInputLimit(2000, false)}
            value={reason}
            onChange={(event) => setReason(event.currentTarget.value)}
          />
        </label>
        {error ? <p className="form-error">{error}</p> : null}
        <div className="dialog-actions">
          <button
            className="icon-button text-button"
            disabled={submitting}
            type="button"
            onClick={requestClose}
          >
            {t("dialogs.cancel")}
          </button>
          <button className="icon-button primary" disabled={invalid || submitting} type="submit">
            {submitting ? t("dialogs.move.submitting") : t("dialogs.move.submit")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function DeleteTreeNodeDialog({
  node,
  onClose,
  onSubmit,
}: {
  node: TreeNodePayload;
  onClose: () => void;
  onSubmit: () => Promise<void>;
}) {
  const { t } = useTranslation("tree");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restoreFocusFallback = useTreeDialogFocusFallback(node.id);

  function requestClose() {
    if (!submitting) {
      onClose();
    }
  }

  async function submit() {
    if (submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit();
    } catch (exc) {
      setError(apiErrorMessage(exc, t("dialogs.delete.failed")));
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      dismissible={!submitting}
      labelledBy="delete-tree-node-title"
      onClose={requestClose}
      restoreFocusFallback={restoreFocusFallback}
    >
      <div className="dialog-form">
        <header className="dialog-header">
          <div>
            <p className="eyebrow">{t("dialogs.eyebrow")}</p>
            <h2 id="delete-tree-node-title">{t("dialogs.delete.title", { title: node.title })}</h2>
            <p className="feature-subtitle">{t("dialogs.delete.body")}</p>
          </div>
          <DialogCloseButton disabled={submitting} label={t("dialogs.closeDialog")} onClick={requestClose} />
        </header>
        {node.children_count > 0 ? (
          <StatusBanner tone="warning">{t("dialogs.delete.childWarning")}</StatusBanner>
        ) : null}
        {error ? <p className="form-error">{error}</p> : null}
        <div className="dialog-actions">
          <button
            className="icon-button text-button"
            disabled={submitting}
            type="button"
            onClick={requestClose}
          >
            {t("dialogs.cancel")}
          </button>
          <button className="icon-button danger" disabled={submitting} type="button" onClick={submit}>
            {submitting ? t("dialogs.delete.submitting") : t("dialogs.delete.submit")}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

function useTreeDialogFocusFallback(nodeId: string) {
  // A tree action menu is removed in the same commit that mounts its dialog,
  // so capture the owning rail during render while the focused menu item is
  // still connected. Holding that exact rail element also fences restoration
  // from a later route that happens to render a node with the same id.
  const treeRailRef = useRef<HTMLElement | null>(
    typeof document === "undefined"
      ? null
      : document.activeElement instanceof HTMLElement
        ? document.activeElement.closest<HTMLElement>(".tree-rail")
        : null,
  );

  return useCallback(() => {
    const treeRail = treeRailRef.current;
    if (!treeRail?.isConnected) {
      return null;
    }
    const matchingRow = Array.from(
      treeRail.querySelectorAll<HTMLElement>('[role="treeitem"][data-node-id]'),
    ).find((row) => row.dataset.nodeId === nodeId);
    if (matchingRow) {
      return matchingRow.querySelector<HTMLElement>(".tree-row-menu-button")
        ?? matchingRow;
    }
    return treeRail.querySelector<HTMLElement>('[role="treeitem"][tabindex="0"]')
      ?? treeRail.querySelector<HTMLElement>('[role="treeitem"]');
  }, [nodeId]);
}
