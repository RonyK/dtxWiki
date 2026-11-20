import { useCallback, useEffect, useRef, useState } from "react";

import {
  cleanMergeChoice,
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
};

/** The merge in progress, from the click until it is written, failed or closed. */
type Merge = { cancel: () => void };

/** A conflict shown to the person; confirming writes their answer. */
type MergeAsk = MergeTarget & {
  preview: ContextMergePreview;
  refreshed: boolean;
  write: (resolution: ContextMergeResolution) => Promise<void>;
};

function mergeClosed() {
  return new DOMException("Merge closed", "AbortError");
}

/**
 * The merge flow every caller shares, with git's rules. A clean merge is
 * written at once; a conflict is resolved by the person, then written. A
 * write the server refuses (the draft or the other side moved) or that fails
 * ends the merge with its error: the caller shows it, and doing it again
 * starts from a fresh preview -- as a rejected push is pulled and pushed
 * again. One merge runs at a time, and a closed one writes nothing.
 */
export function useContextMerge() {
  const [ask, setAsk] = useState<MergeAsk | null>(null);
  const [busy, setBusy] = useState(false);
  const currentRef = useRef<Merge | null>(null);

  const end = useCallback((merge: Merge) => {
    if (currentRef.current !== merge) return false;
    currentRef.current = null;
    setAsk(null);
    setBusy(false);
    return true;
  }, []);

  const close = useCallback(() => {
    const merge = currentRef.current;
    if (merge && end(merge)) merge.cancel();
  }, [end]);

  const begin = useCallback((merge: Merge) => {
    close();
    currentRef.current = merge;
  }, [close]);

  // Leaving the screen closes its merge: nothing is written after it is gone.
  useEffect(() => close, [close]);

  /**
   * Publish, approve or bring in. Resolves with the write's result; rejects
   * with its error, or with an AbortError when the person closes the dialog.
   * `preview` is the merge the screen already showed: it is written as shown
   * (a clean result at once, a conflict through the dialog) and never
   * re-read at the click, so a draft or published version that moved since
   * is refused by the server instead of published unseen.
   */
  const runMergeWrite = useCallback(<T,>(target: MergeTarget & (
    | { preview: ContextMergePreview; loadPreview?: undefined }
    | { preview?: undefined; loadPreview: () => Promise<ContextMergePreview> }
  ) & {
    write: (choice: PreparedMergeChoice) => Promise<T>;
  }) => new Promise<T>((resolve, reject) => {
    const merge: Merge = { cancel: () => reject(mergeClosed()) };
    begin(merge);
    const fail = (error: unknown) => {
      if (end(merge)) reject(error);
    };
    const commit = async (choice: PreparedMergeChoice) => {
      if (currentRef.current !== merge) return;
      try {
        const result = await target.write(choice);
        if (end(merge)) resolve(result);
      } catch (error) {
        fail(error);
      }
    };
    (target.preview ? Promise.resolve(target.preview) : target.loadPreview()).then((preview) => {
      if (currentRef.current !== merge) return;
      if (preview.kind !== "conflict") return commit(cleanMergeChoice(preview));
      setAsk({
        ...target,
        preview,
        refreshed: false,
        async write(resolution) {
          let choice: PreparedMergeChoice;
          try {
            choice = await prepareMergeChoice(target.workspaceId, target.pageId, preview, {
              onConflict: resolution.choice,
              bodyMd: resolution.bodyMd,
              pageMetadata: resolution.pageMetadata,
            });
          } catch (error) {
            fail(error);
            return;
          }
          await commit(choice);
        },
      });
    }, fail);
  }), [begin, end]);

  /** The editor's own save overlapped another save: ask, and let the editor apply the answer. */
  const resolveConflict = useCallback((target: MergeTarget & {
    preview: ContextMergePreview;
    refreshed?: boolean;
    apply: (resolution: ContextMergeResolution) => Promise<void>;
  }) => {
    const merge: Merge = { cancel: () => {} };
    begin(merge);
    setAsk({
      ...target,
      refreshed: target.refreshed ?? false,
      async write(resolution) {
        end(merge);
        await target.apply(resolution);
      },
    });
  }, [begin, end]);

  const dialog = ask ? (
    <ContextMergeDialog
      busy={busy}
      kind={ask.kind}
      onClose={close}
      onConfirm={(resolution) => {
        setBusy(true);
        void ask.write(resolution);
      }}
      pageId={ask.pageId}
      preview={ask.preview}
      publishedVersionNumber={ask.publishedVersionNumber}
      refreshed={ask.refreshed}
      workspaceId={ask.workspaceId}
    />
  ) : null;
  return { dialog, runMergeWrite, resolveConflict, close };
}
