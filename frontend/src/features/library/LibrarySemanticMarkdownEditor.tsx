import { Save, X } from "lucide-react";
import type { TFunction } from "i18next";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../shared/api/client";
import {
  getLibrarySemanticMarkdown,
  type LibrarySemanticMarkdown,
  updateLibrarySemanticMarkdown,
} from "../../shared/api/library";
import { translate } from "../../shared/i18n";
import { isAbortError } from "../../shared/lib/abortError";
import { MarkdownEditorKernel } from "../pages/MarkdownEditorKernel";

type SaveState = "clean" | "dirty" | "saving" | "saved" | "error" | "stale";

export function LibrarySemanticMarkdownEditor({
  onCancel,
  onSaved,
  versionId,
  workspaceId,
}: {
  onCancel: () => void;
  onSaved: (document: LibrarySemanticMarkdown) => void;
  versionId: string;
  workspaceId: string;
}) {
  const { t } = useTranslation("library");
  const requestRef = useRef<AbortController | null>(null);
  const [retry, setRetry] = useState(0);
  const [document, setDocument] = useState<LibrarySemanticMarkdown | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("clean");

  useEffect(() => {
    const controller = new AbortController();
    requestRef.current?.abort();
    requestRef.current = controller;
    setLoading(true);
    setLoadError(null);
    setSaveError(null);
    void getLibrarySemanticMarkdown(workspaceId, versionId, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setDocument(next);
        setDraft(next.body_markdown);
        setSaveState("clean");
        setLoading(false);
      })
      .catch((error) => {
        if (controller.signal.aborted || isAbortError(error)) return;
        setLoading(false);
        setLoadError(
          error instanceof ApiError && error.status === 404
            ? t("reader.editor.unavailable", {
                defaultValue: "This version no longer has editable semantic Markdown.",
              })
            : semanticEditorError(
                error,
                t("reader.editor.loadFailed", {
                  defaultValue: "Editable Markdown could not be loaded.",
                }),
              ),
        );
      });
    return () => controller.abort();
  }, [retry, t, versionId, workspaceId]);

  useEffect(
    () => () => {
      requestRef.current?.abort();
    },
    [],
  );

  const save = useCallback(async () => {
    if (
      !document ||
      draft === document.body_markdown ||
      draft.length === 0 ||
      saveState === "saving" ||
      saveState === "stale"
    ) {
      return;
    }
    const controller = new AbortController();
    requestRef.current?.abort();
    requestRef.current = controller;
    setSaveState("saving");
    setSaveError(null);
    try {
      const saved = await updateLibrarySemanticMarkdown(
        workspaceId,
        versionId,
        {
          body_markdown: draft,
          expected_revision: document.revision,
          expected_base_artifact_id: document.base_artifact_id,
          expected_base_content_sha256: document.base_content_sha256,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setDocument(saved);
      setDraft(saved.body_markdown);
      setSaveState("saved");
      onSaved(saved);
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) return;
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.body.error_code === "stale_base_version"
      ) {
        setSaveState("stale");
        setSaveError(
          t("reader.editor.stale", {
            defaultValue:
              "This Markdown changed after you opened it. Reload the latest version before saving again.",
          }),
        );
        return;
      }
      setSaveState("error");
      setSaveError(
        semanticEditorError(
          error,
          t("reader.editor.saveFailed", {
            defaultValue: "Markdown could not be saved. Your edits are still here.",
          }),
        ),
      );
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  }, [document, draft, onSaved, saveState, t, versionId, workspaceId]);

  if (loading) {
    return (
      <div className="wiki-empty" role="status">
        {t("reader.editor.loading", { defaultValue: "Loading editable Markdown…" })}
      </div>
    );
  }

  if (loadError || !document) {
    return (
      <div className="library-semantic-editor-state" role="alert">
        <p>{loadError ?? t("reader.editor.loadFailed")}</p>
        <div className="library-semantic-editor-actions">
          <button
            className="icon-button text-button"
            onClick={onCancel}
            type="button"
          >
            {t("reader.editor.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            onClick={() => setRetry((current) => current + 1)}
            type="button"
          >
            {t("reader.editor.retry", { defaultValue: "Try again" })}
          </button>
        </div>
      </div>
    );
  }

  const dirty = draft !== document.body_markdown;
  const saving = saveState === "saving";
  const saveDisabled = !dirty || draft.length === 0 || saving || saveState === "stale";

  return (
    <div className="library-semantic-editor">
      <div className="library-semantic-editor-header">
        <div className="library-semantic-editor-status" role="status">
          <strong>
            {t("reader.editor.title", { defaultValue: "Edit semantic Markdown" })}
          </strong>
          <span>{saveStateLabel(saveState, dirty, t)}</span>
        </div>
        <div className="library-semantic-editor-actions">
          {saveState === "stale" ? (
            <button
              className="icon-button text-button"
              disabled={saving}
              onClick={() => setRetry((current) => current + 1)}
              type="button"
            >
              {t("reader.editor.reloadLatest", { defaultValue: "Reload latest" })}
            </button>
          ) : null}
          <button
            className="icon-button text-button"
            disabled={saving}
            onClick={onCancel}
            type="button"
          >
            <X aria-hidden="true" size={16} />
            <span>{t("reader.editor.cancel", { defaultValue: "Cancel" })}</span>
          </button>
          <button
            aria-busy={saving}
            className="icon-button primary"
            disabled={saveDisabled}
            onClick={() => void save()}
            type="button"
          >
            <Save aria-hidden="true" size={16} />
            <span>
              {saving
                ? t("reader.editor.saving", { defaultValue: "Saving…" })
                : t("reader.editor.save", { defaultValue: "Save" })}
            </span>
          </button>
        </div>
      </div>
      {saveError ? (
        <p className="form-error library-semantic-editor-error" role="alert">
          {saveError}
        </p>
      ) : null}
      <MarkdownEditorKernel
        labels={{
          actionBarMore: t("reader.editor.actionBar.more", { defaultValue: "More editor actions" }),
          actionBarMoreMenu: t("reader.editor.actionBar.moreMenu", { defaultValue: "More editor actions" }),
          actionBarMoreShort: t("reader.editor.actionBar.moreShort", { defaultValue: "More" }),
          actionBarReadingDisabled: t("reader.editor.actionBar.readingDisabled", {
            defaultValue: "Editing commands are unavailable in Reading mode.",
          }),
          actionBarToolbar: t("reader.editor.actionBar.toolbar", { defaultValue: "Editor actions" }),
          bodyAria: t("reader.editor.bodyAria", { defaultValue: "Markdown body" }),
          bodyPlaceholder: t("reader.editor.bodyPlaceholder", { defaultValue: "Start typing…" }),
          controlsAria: t("reader.editor.controlsAria", { defaultValue: "Editor controls" }),
          modeAria: t("reader.editor.modeAria", { defaultValue: "Editor mode" }),
          readingMode: t("reader.editor.readingMode", { defaultValue: "Reading" }),
          readingModeTitle: t("reader.editor.readingModeTitle", { defaultValue: "Reading mode" }),
          sourceMode: t("reader.editor.sourceMode", { defaultValue: "Source" }),
          sourceModeTitle: t("reader.editor.sourceModeTitle", { defaultValue: "Source mode" }),
          splitMode: t("reader.editor.splitMode", { defaultValue: "Split" }),
          splitModeTitle: t("reader.editor.splitModeTitle", { defaultValue: "Split mode" }),
        }}
        onChange={(body) => {
          setDraft(body);
          setSaveError(null);
          setSaveState(body === document.body_markdown ? "clean" : "dirty");
        }}
        onSave={() => void save()}
        resolveCommandLabel={(labelKey, fallbackLabel) =>
          translate(`context:${labelKey}`, fallbackLabel)
        }
        renderPolicy="untrusted-preview"
        value={draft}
      />
    </div>
  );
}

function semanticEditorError(error: unknown, fallback: string) {
  return error instanceof ApiError ? error.displayMessage : fallback;
}

function saveStateLabel(
  state: SaveState,
  dirty: boolean,
  t: TFunction,
) {
  if (state === "saving") return t("reader.editor.saving", { defaultValue: "Saving…" });
  if (state === "saved") return t("reader.editor.saved", { defaultValue: "Saved" });
  if (state === "stale") return t("reader.editor.staleStatus", { defaultValue: "Newer version available" });
  if (state === "error") return t("reader.editor.unsaved", { defaultValue: "Not saved" });
  if (dirty) return t("reader.editor.unsavedChanges", { defaultValue: "Unsaved changes" });
  return t("reader.editor.noChanges", { defaultValue: "No unsaved changes" });
}
