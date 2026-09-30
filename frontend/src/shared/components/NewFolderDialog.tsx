import type { FormEvent } from "react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, apiErrorMessage } from "../api/client";
import { normalizeSlugInput, slugify } from "../text/slug";
import { AdvancedSection } from "./AdvancedSection";
import { Dialog, DialogCloseButton } from "./Dialog";

type NewFolderDialogProps = {
  parentLabel: string;
  onClose: () => void;
  onSubmit: (input: { slug?: string; title: string }) => Promise<void>;
};

export function NewFolderDialog({
  parentLabel,
  onClose,
  onSubmit,
}: NewFolderDialogProps) {
  const { t } = useTranslation("tree");
  const [title, setTitle] = useState("");
  const [customSlug, setCustomSlug] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const derivedSlug = useMemo(() => slugify(title), [title]);
  const slug = showAdvanced ? normalizeSlugInput(customSlug) : derivedSlug;
  const invalidTitle = title.trim().length === 0 || title.trim().length > 80;

  function requestClose() {
    if (!submitting) {
      onClose();
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (invalidTitle || submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({
        title: title.trim(),
        slug: showAdvanced ? slug : undefined,
      });
      onClose();
    } catch (exc) {
      setError(
        isDuplicateFolderError(exc)
          ? t("newItem.errors.duplicateSlug")
          : apiErrorMessage(
              exc,
              t("newItem.errors.create", {
                itemType: t("newItem.types.folder"),
              }),
            ),
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      dismissible={!submitting}
      labelledBy="new-folder-title"
      onClose={requestClose}
    >
      <form className="dialog-form" onSubmit={submit}>
        <header className="dialog-header">
          <div>
            <p className="eyebrow">{t("newItem.headings.folder")}</p>
            <h2 id="new-folder-title">{t("newItem.createFolder")}</h2>
            <p className="feature-subtitle">
              {t("newItem.parent", { parent: parentLabel })}
            </p>
          </div>
          <DialogCloseButton
            disabled={submitting}
            label={t("newItem.close")}
            onClick={requestClose}
          />
        </header>
        <label className="form-field">
          <span>{t("newItem.folderName")}</span>
          <input
            autoFocus
            maxLength={80}
            onChange={(event) => setTitle(event.currentTarget.value)}
            value={title}
          />
          <small className="field-hint">
            {invalidTitle
              ? t("newItem.titleLength")
              : t("newItem.slugPreview", { slug })}
          </small>
        </label>
        <AdvancedSection
          label={t("newItem.advancedSlug")}
          open={showAdvanced}
          onOpenChange={setShowAdvanced}
        >
          <label className="form-field">
            <span>{t("newItem.slug")}</span>
            <input
              maxLength={64}
              onChange={(event) => setCustomSlug(event.currentTarget.value)}
              placeholder={derivedSlug}
              value={customSlug}
            />
          </label>
        </AdvancedSection>
        {error ? <p className="form-error">{error}</p> : null}
        <div className="dialog-actions">
          <button
            className="icon-button text-button"
            disabled={submitting}
            type="button"
            onClick={requestClose}
          >
            {t("newItem.cancel")}
          </button>
          <button className="icon-button primary" disabled={invalidTitle || submitting} type="submit">
            {submitting ? t("newItem.creating") : t("newItem.createFolder")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function isDuplicateFolderError(error: unknown) {
  return (
    (error instanceof ApiError &&
      error.body.error_code === "duplicate_slug_under_parent") ||
    (error instanceof Error &&
      error.message === "duplicate_slug_under_parent")
  );
}
