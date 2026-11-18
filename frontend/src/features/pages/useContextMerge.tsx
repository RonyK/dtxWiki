import { useCallback, useRef, useState } from "react";

import { ApiError } from "../../shared/api/client";
import {
  cleanMergeChoice,
  ContextMergePreviewChanged,
  prepareMergeChoice,
  type ContextMergePreview,
  type PreparedMergeChoice,
} from "../../shared/api/pages";
import { ContextMergeDialog, type ContextMergeDialogKind } from "./ContextMergeDialog";
import type { ContextMergeResolution } from "./contextMerge";

type MergeTarget = {
  kind: ContextMergeDialogKind;
  workspaceId: string;
  pageId: string;
  publishedVersionNumber?: number | null;
  errorMessage: (error: unknown) => string;
};

/** One merge, from its first preview until it is written, failed or closed. */
type MergeSession = MergeTarget & {
  /** Writes a merge that needed no choice. */
  writeClean?: (preview: ContextMergePreview) => Promise<void>;
  /** Writes the person's answer to the preview they saw. */
  writeChosen: (resolution: ContextMergeResolution, preview: ContextMergePreview) => Promise<void>;
  /** A newer preview, after the server refused a write as stale. */
  reload?: () => Promise<ContextMergePreview>;
  /** Ends the caller's wait with an error (an AbortError when closed). */
  fail: (error: unknown) => void;
};

type MergeView = {
  session: MergeSession;
  preview: ContextMergePreview;
  refreshed: boolean;
  busy: boolean;
  error: string | null;
};

const STALE_CODES = new Set(["stale_base_version", "context_merge_conflict", "editing_revision_conflict"]);

function isStale(error: unknown) {
  return error instanceof ApiError && error.status === 409 && STALE_CODES.has(error.body.error_code);
}

/** The server answered and refused: nothing was written. */
function isRefused(error: unknown) {
  return error instanceof ApiError && error.status >= 400 && error.status < 500;
}

function mergeClosed() {
  return new DOMException("Merge closed", "AbortError");
}

/**
 * The merge flow every caller shares: a clean merge writes at once, a conflict
 * asks the person with the two choices, and a stale write shows the newer
 * preview instead of overwriting what changed. One merge runs at a time; a
 * merge that was closed or replaced never shows or writes anything later.
 * Callers supply only how they write (publish, approve, bring in) or apply
 * (the editor's own save).
 */
export function useContextMerge() {
  const [view, setView] = useState<MergeView | null>(null);
  const sessionRef = useRef<MergeSession | null>(null);

  const finish = useCallback((session: MergeSession, error?: unknown) => {
    if (sessionRef.current !== session) return;
    sessionRef.current = null;
    setView(null);
    if (error !== undefined) session.fail(error);
  }, []);

  const start = useCallback((session: MergeSession) => {
    const previous = sessionRef.current;
    sessionRef.current = session;
    setView(null);
    previous?.fail(mergeClosed());
  }, []);

  // The one transition. A conflicting preview is shown until the person
  // answers; a clean preview, or an answer, is written. A stale answer shows
  // the newer preview. A stale clean write is retried only when the newer
  // preview needs a choice -- otherwise the refusal is the outcome, so a
  // write whose own pins are out of date never repeats. An answer the server
  // refused is asked again in the dialog; any other failure -- including a
  // write whose outcome is unknown, which only its caller can recover -- ends
  // the caller's wait.
  const settle = useCallback(async function settle(
    session: MergeSession,
    preview: ContextMergePreview,
    refreshed: boolean,
    resolution?: ContextMergeResolution,
  ): Promise<void> {
    if (sessionRef.current !== session) return;
    const asking = preview.kind === "conflict";
    if (asking) setView({ session, preview, refreshed, busy: Boolean(resolution), error: null });
    if (asking && !resolution) return;
    try {
      await (resolution ? session.writeChosen(resolution, preview) : session.writeClean?.(preview));
      finish(session);
    } catch (error) {
      if (sessionRef.current !== session) return;
      try {
        const newer = error instanceof ContextMergePreviewChanged ? error.preview
          : isStale(error) && session.reload ? await session.reload() : null;
        if (!newer || (!asking && newer.kind !== "conflict")) throw error;
        await settle(session, newer, true);
      } catch (failure) {
        if (sessionRef.current !== session) return;
        if (asking && isRefused(failure)) {
          setView({ session, preview, refreshed, busy: false, error: session.errorMessage(failure) });
        } else {
          finish(session, failure);
        }
      }
    }
  }, [finish]);

  const close = useCallback(() => {
    const session = sessionRef.current;
    if (session) finish(session, mergeClosed());
  }, [finish]);

  /**
   * Publish, approve or bring in. Resolves with the write's result; rejects
   * with an AbortError when the person closes the dialog.
   */
  const runMergeWrite = useCallback(<T,>(target: MergeTarget & {
    loadPreview: () => Promise<ContextMergePreview>;
    write: (choice: PreparedMergeChoice) => Promise<T>;
  }) => new Promise<T>((resolve, reject) => {
    // Nothing is written for a merge that was closed or replaced meanwhile.
    const commit = async (choice: PreparedMergeChoice) => {
      if (sessionRef.current === session) resolve(await target.write(choice));
    };
    const session: MergeSession = {
      ...target,
      reload: target.loadPreview,
      fail: reject,
      writeClean: (preview) => commit(cleanMergeChoice(preview)),
      writeChosen: async (resolution, preview) => commit(await prepareMergeChoice(
        target.workspaceId, target.pageId, preview,
        { onConflict: resolution.choice, bodyMd: resolution.bodyMd, pageMetadata: resolution.pageMetadata },
      )),
    };
    start(session);
    target.loadPreview().then((preview) => settle(session, preview, false), (error) => finish(session, error));
  }), [finish, settle, start]);

  /**
   * The editor's own save overlapped another save: ask, and let the editor
   * apply the answer. `apply` throws ContextMergePreviewChanged when its save
   * overlaps again, so the same dialog shows the newer choices.
   */
  const resolveConflict = useCallback((target: MergeTarget & {
    preview: ContextMergePreview;
    apply: (resolution: ContextMergeResolution) => Promise<void>;
  }) => {
    const session: MergeSession = { ...target, fail: () => {}, writeChosen: target.apply };
    start(session);
    void settle(session, target.preview, false);
  }, [settle, start]);

  const dialog = view ? (
    <ContextMergeDialog
      busy={view.busy}
      error={view.error}
      kind={view.session.kind}
      onClose={close}
      onConfirm={(resolution) => void settle(view.session, view.preview, view.refreshed, resolution)}
      pageId={view.session.pageId}
      preview={view.preview}
      publishedVersionNumber={view.session.publishedVersionNumber}
      refreshed={view.refreshed}
      workspaceId={view.session.workspaceId}
    />
  ) : null;
  return { dialog, runMergeWrite, resolveConflict, close };
}
