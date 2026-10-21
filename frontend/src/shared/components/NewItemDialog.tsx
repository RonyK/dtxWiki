import { FilePlus, FolderPlus, Upload } from "lucide-react";
import type { ChangeEvent, FormEvent } from "react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, apiErrorMessage } from "../api/client";
import type { TreeNodePayload } from "../api/workspace";
import { inputTextError, textInputLimit, trimShortText } from "../lib/inputPolicy";
import { normalizeSlugInput, slugify } from "../text/slug";
import { AdvancedSection } from "./AdvancedSection";
import { Dialog, DialogCloseButton } from "./Dialog";
import { FormField } from "./FormField";
import { SegmentedControl } from "./SegmentedControl";
import { TreePickerDropdown } from "./TreePickerDropdown";
import { collectHiddenSystemRootNodeIds } from "./treeSectionRoot";

export type NewItemType = "page" | "folder" | "upload";

type NewItemDialogProps = {
  canCreateFolder?: boolean;
  canCreatePage?: boolean;
  canUpload?: boolean;
  uploadAccept?: string;
  initialItemType?: NewItemType;
  parentLabel: string;
  // F-5: when provided, the dialog renders a location picker so the user can
  // choose the destination folder (defaulting to `initialParentId`) instead of
  // being silently pinned to wherever the "+" was invoked.
  parentNodes?: readonly TreeNodePayload[];
  initialParentId?: string;
  /** Label for the section root in the location picker (e.g. "Context"). */
  rootLabel?: string;
  typeOrder?: readonly NewItemType[];
  onClose: () => void;
  onSubmit: (input: {
    itemType: NewItemType;
    parentNodeId: string | null;
    slug?: string;
    title: string;
  }) => Promise<void> | void;
  onPickFiles?: (
    files: File[],
    parentNodeId: string | null,
  ) => Promise<void> | void;
};

const TYPE_META: Record<NewItemType, { icon: typeof FilePlus; fallback: string }> = {
  page: { icon: FilePlus, fallback: "Page" },
  folder: { icon: FolderPlus, fallback: "Folder" },
  upload: { icon: Upload, fallback: "Upload" },
};

const DEFAULT_NEW_ITEM_TYPE_ORDER: readonly NewItemType[] = [
  "page",
  "folder",
  "upload",
];

export function NewItemDialog({
  canCreateFolder = true,
  canCreatePage = true,
  canUpload = false,
  uploadAccept,
  initialItemType = "page",
  parentNodes,
  initialParentId = "",
  rootLabel,
  typeOrder = DEFAULT_NEW_ITEM_TYPE_ORDER,
  onClose,
  onSubmit,
  onPickFiles,
}: NewItemDialogProps) {
  const { t } = useTranslation("tree");
  // When `rootLabel` is set the picker already renders a synthetic root row for
  // the section, so the real section-root node must not appear underneath it —
  // that produced a second, identically labelled "Context > Context" row. Same
  // rule the TreeRail uses to hide it from the explorer.
  const hiddenRootNodeIds = useMemo(
    () =>
      rootLabel === undefined || !parentNodes
        ? new Set<string>()
        : collectHiddenSystemRootNodeIds(parentNodes, rootLabel),
    [parentNodes, rootLabel],
  );
  const pickerNodes = useMemo(
    () => (parentNodes ?? []).filter((node) => !hiddenRootNodeIds.has(node.id)),
    [hiddenRootNodeIds, parentNodes],
  );
  // An initial parent pointing at the now-hidden root has no row to select;
  // normalize it to the synthetic root ("") so the trigger label and the
  // submitted destination agree.
  const [parentId, setParentId] = useState(
    hiddenRootNodeIds.has(initialParentId) ? "" : initialParentId,
  );
  const availableTypes = useMemo<NewItemType[]>(
    () =>
      typeOrder.filter((type) => {
        if (type === "page") {
          return canCreatePage;
        }
        if (type === "folder") {
          return canCreateFolder;
        }
        return canUpload;
      }),
    [canCreateFolder, canCreatePage, canUpload, typeOrder],
  );

  const [itemType, setItemType] = useState<NewItemType>(
    availableTypes.includes(initialItemType)
      ? initialItemType
      : (availableTypes[0] ?? "page"),
  );
  useEffect(() => {
    setItemType((current) =>
      availableTypes.includes(current)
        ? current
        : (availableTypes[0] ?? "page"),
    );
  }, [availableTypes]);
  const [title, setTitle] = useState("");
  const [customSlug, setCustomSlug] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const derivedSlug = useMemo(() => slugify(title), [title]);
  // A blank Advanced slug falls back to the derived slug (the placeholder
  // already promises it) instead of producing an empty string.
  const normalizedCustomSlug = normalizeSlugInput(customSlug);
  const slug = showAdvanced && normalizedCustomSlug ? normalizedCustomSlug : derivedSlug;
  const titleLimit = itemType === "folder" ? 80 : 256;
  const invalidTitle = !trimShortText(title) || Boolean(inputTextError(title, titleLimit));
  const invalidSlug = showAdvanced && Boolean(inputTextError(customSlug, 64));
  const canSubmit = itemType !== "upload" && !invalidTitle && !invalidSlug && !submitting;

  if (availableTypes.length === 0) {
    return null;
  }

  function requestClose() {
    if (!submitting) {
      onClose();
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (itemType === "upload" || !canSubmit) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({
        itemType,
        parentNodeId: parentId || null,
        // Only send an explicit slug when the user actually typed one — a blank
        // Advanced field defers to the server-side derivation (an empty string
        // would fail the backend's slug pattern with a 422).
        slug:
          itemType === "folder" && showAdvanced && normalizedCustomSlug
            ? normalizedCustomSlug
            : undefined,
        title: trimShortText(title),
      });
      onClose();
    } catch (exc) {
      setError(
        newItemErrorMessage(
          exc,
          t("newItem.errors.create", {
            defaultValue: "Failed to create {{itemType}}",
            itemType: typeLabel(itemType, t),
          }),
          t("newItem.errors.duplicateSlug", {
            defaultValue: "Slug already in use under this folder",
          }),
        ),
      );
      setSubmitting(false);
    }
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";
    if (files.length === 0) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onPickFiles?.(files, parentId || null);
      onClose();
    } catch (exc) {
      setError(
        apiErrorMessage(
          exc,
          t("newItem.errors.upload", {
            defaultValue: "Failed to upload file",
          }),
        ),
      );
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      dismissible={!submitting}
      labelledBy="new-item-title"
      onClose={requestClose}
    >
      <form className="dialog-form" onSubmit={submit}>
        <header className="dialog-header">
          <div>
            <p className="eyebrow">
              {t("newItem.eyebrow", { defaultValue: "New item" })}
            </p>
            <h2 id="new-item-title">
              {t(`newItem.headings.${itemType}`, {
                defaultValue:
                  itemType === "page"
                    ? "New page"
                    : itemType === "folder"
                      ? "New folder"
                      : "Upload files",
              })}
            </h2>
            <p className="feature-subtitle">
              {availableTypes.length === 1
                ? t(`newItem.subtitleSingle.${availableTypes[0]}`, {
                    defaultValue:
                      availableTypes[0] === "page"
                        ? "Create a page in this location."
                        : availableTypes[0] === "folder"
                          ? "Create a folder in this location."
                          : "Upload source files to this location.",
                  })
                : canUpload
                ? t("newItem.subtitleUpload", {
                    defaultValue: "Upload a source file or add a folder.",
                  })
                : t("newItem.subtitleCreate", {
                    defaultValue:
                      "Choose whether this branch should hold a page or a folder.",
                  })}
            </p>
          </div>
          <DialogCloseButton
            label={t("newItem.close", {
              defaultValue: "Close new item dialog",
            })}
            disabled={submitting}
            onClick={requestClose}
          />
        </header>
        {availableTypes.length > 1 ? (
          <SegmentedControl
            compact
            label={t("newItem.itemType", { defaultValue: "Item type" })}
            options={availableTypes.map((type) => {
              const Icon = TYPE_META[type].icon;
              return {
                key: type,
                label: (
                  <>
                    <Icon size={14} aria-hidden="true" />
                    {typeLabel(type, t)}
                  </>
                ),
              };
            })}
            value={itemType}
            onChange={setItemType}
          />
        ) : null}
        {/* With a rootLabel the synthetic root row is itself a destination, so
            the picker stays useful even when hiding the section root empties
            the node list (a workspace whose only folder IS the root). */}
        {pickerNodes.length > 0 || (rootLabel !== undefined && parentNodes) ? (
          <FormField
            label={t("newItem.location", { defaultValue: "Location" })}
          >
            <TreePickerDropdown
              ariaLabel={t("newItem.location", { defaultValue: "Location" })}
              disabled={submitting}
              rootLabel={rootLabel}
              nodes={pickerNodes as TreeNodePayload[]}
              selectedId={parentId}
              onChange={setParentId}
            />
          </FormField>
        ) : null}
        {itemType === "upload" ? (
          <label className="form-field">
            <span>{t("newItem.file", { defaultValue: "File" })}</span>
            <input
              accept={uploadAccept}
              aria-label={t("newItem.uploadFile", { defaultValue: "Upload file" })}
              disabled={submitting}
              onChange={handleFileChange}
              type="file"
            />
            <small className="field-hint">
              {submitting
                ? t("newItem.uploading", { defaultValue: "Uploading…" })
                : t("newItem.pickSourceFile", {
                    defaultValue: "Pick a source file to add to the Library.",
                  })}
            </small>
          </label>
        ) : (
          <label className="form-field">
            <span>
              {itemType === "page"
                ? t("newItem.pageTitle", { defaultValue: "Page title" })
                : t("newItem.folderName", { defaultValue: "Folder name" })}
            </span>
            <input
              aria-label={
                itemType === "page"
                  ? t("newItem.pageTitle", { defaultValue: "Page title" })
                  : t("newItem.folderName", { defaultValue: "Folder name" })
              }
              autoFocus
              {...textInputLimit(titleLimit)}
              onChange={(event) => setTitle(event.currentTarget.value)}
              value={title}
            />
            <small className="field-hint">
              {invalidTitle
                ? t("newItem.titleLength", {
                    defaultValue: "Enter 1-{{maximum}} characters.",
                    maximum: titleLimit,
                  })
                : itemType === "folder"
                  ? t("newItem.slugPreview", {
                      defaultValue: "Slug preview: {{slug}}",
                      slug,
                    })
                  : t("newItem.editorHint", {
                      defaultValue:
                        "The editor will open with this title and location.",
                    })}
            </small>
          </label>
        )}
        {itemType === "folder" ? (
          <AdvancedSection
            label={t("newItem.advancedSlug", { defaultValue: "Advanced slug" })}
            open={showAdvanced}
            onOpenChange={setShowAdvanced}
          >
            <label className="form-field">
              <span>{t("newItem.slug", { defaultValue: "Slug" })}</span>
              <input
                aria-label={t("newItem.slug", { defaultValue: "Slug" })}
                {...textInputLimit(64)}
                onChange={(event) => setCustomSlug(event.currentTarget.value)}
                placeholder={derivedSlug}
                value={customSlug}
              />
            </label>
          </AdvancedSection>
        ) : null}
        {error ? <p className="form-error">{error}</p> : null}
        <div className="dialog-actions">
          <button
            className="icon-button text-button"
            disabled={submitting}
            type="button"
            onClick={requestClose}
          >
            {t("newItem.cancel", { defaultValue: "Cancel" })}
          </button>
          {itemType !== "upload" ? (
            <button className="icon-button primary" disabled={!canSubmit} type="submit">
              {submitting
                ? t("newItem.creating", { defaultValue: "Creating" })
                : itemType === "page"
                  ? t("newItem.openEditor", { defaultValue: "Open editor" })
                  : t("newItem.createFolder", { defaultValue: "Create folder" })}
            </button>
          ) : null}
        </div>
      </form>
    </Dialog>
  );
}

function typeLabel(
  type: NewItemType,
  t: (key: string, options?: Record<string, unknown>) => string,
) {
  return t(`newItem.types.${type}`, { defaultValue: TYPE_META[type].fallback });
}

function newItemErrorMessage(
  error: unknown,
  fallback: string,
  duplicateMessage: string,
) {
  if (
    (error instanceof ApiError &&
      error.body.error_code === "duplicate_slug_under_parent") ||
    (error instanceof Error &&
      error.message === "duplicate_slug_under_parent")
  ) {
    return duplicateMessage;
  }
  return apiErrorMessage(error, fallback);
}
