import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import type { CursorListPage } from "../api/mcpAccess";

type CursorNavigation = "initial" | "next" | "previous" | "reload";

type UseCursorPageOptions<TItem, TPage extends CursorListPage<TItem, unknown>> = {
  enabled: boolean;
  errorMessage: string;
  load: (cursor: string | null, signal: AbortSignal) => Promise<TPage>;
  queryKey: string;
};

export type CursorPageController<TItem, TPage extends CursorListPage<TItem, unknown>> = {
  error: unknown | null;
  focusRevision: number;
  hasNext: boolean;
  hasPrevious: boolean;
  loading: boolean;
  next: () => Promise<void>;
  page: TPage | null;
  pageIndex: number;
  previous: () => Promise<void>;
  recoveredFromCursor: boolean;
  reload: () => Promise<void>;
};

/**
 * Cursor navigation shared by high-cardinality Settings lists.
 *
 * Domain status and row actions deliberately stay outside this hook. It owns
 * only query-bound cursor history, stale-response suppression, empty-page
 * fallback after mutation, and the signal used by the footer to restore focus.
 */
export function useCursorPage<
  TItem,
  TPage extends CursorListPage<TItem, unknown>,
>({ enabled, errorMessage, load, queryKey }: UseCursorPageOptions<TItem, TPage>):
  CursorPageController<TItem, TPage> {
  const [page, setPage] = useState<TPage | null>(null);
  const [pageIndex, setPageIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown | null>(null);
  const [focusRevision, setFocusRevision] = useState(0);
  const [recoveredFromCursor, setRecoveredFromCursor] = useState(false);

  const loadRef = useRef(load);
  const queryKeyRef = useRef(queryKey);
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const loadingRef = useRef(false);
  const stackRef = useRef<Array<string | null>>([null]);
  const pageIndexRef = useRef(0);
  const pageRef = useRef<TPage | null>(null);

  useEffect(() => {
    loadRef.current = load;
    queryKeyRef.current = queryKey;
  }, [load, queryKey]);

  useEffect(() => {
    pageRef.current = page;
  }, [page]);

  const loadCursor = useCallback(
    async (
      cursor: string | null,
      targetStack: Array<string | null>,
      targetIndex: number,
      navigation: CursorNavigation,
    ) => {
      if (
        loadingRef.current &&
        navigation !== "initial" &&
        navigation !== "reload"
      ) {
        return;
      }
      generationRef.current += 1;
      const generation = generationRef.current;
      const requestQueryKey = queryKeyRef.current;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      loadingRef.current = true;
      setLoading(true);
      setError(null);
      setRecoveredFromCursor(false);

      const isStale = () =>
        controller.signal.aborted ||
        generation !== generationRef.current ||
        requestQueryKey !== queryKeyRef.current;

      try {
        let resolvedStack = targetStack;
        let resolvedIndex = targetIndex;
        let nextPage: TPage;
        try {
          nextPage = await loadRef.current(cursor, controller.signal);
        } catch (caught) {
          if (!cursor || !isRecoverableCursorError(caught)) {
            throw caught;
          }
          nextPage = await loadRef.current(null, controller.signal);
          resolvedStack = [null];
          resolvedIndex = 0;
          setRecoveredFromCursor(true);
        }

        // Deleting the final row on a non-first page moves back one page. This
        // is a second read, not local splicing, so server counts and eligibility
        // remain authoritative.
        if (nextPage.data.length === 0 && resolvedIndex > 0) {
          resolvedStack = resolvedStack.slice(0, resolvedIndex);
          resolvedIndex -= 1;
          nextPage = await loadRef.current(
            resolvedStack[resolvedIndex] ?? null,
            controller.signal,
          );
        }
        if (isStale()) {
          return;
        }
        stackRef.current = resolvedStack;
        pageIndexRef.current = resolvedIndex;
        pageRef.current = nextPage;
        setPage(nextPage);
        setPageIndex(resolvedIndex);
        if (navigation === "next" || navigation === "previous") {
          setFocusRevision((current) => current + 1);
        }
      } catch (caught) {
        if (isStale() || isAbortError(caught)) {
          return;
        }
        setError(caught instanceof Error ? caught : new Error(errorMessage));
      } finally {
        if (!isStale()) {
          loadingRef.current = false;
          setLoading(false);
        }
      }
    },
    [errorMessage],
  );

  useEffect(() => {
    generationRef.current += 1;
    abortRef.current?.abort();
    loadingRef.current = false;
    stackRef.current = [null];
    pageIndexRef.current = 0;
    pageRef.current = null;
    setPage(null);
    setPageIndex(0);
    setError(null);
    setRecoveredFromCursor(false);
    if (enabled) {
      void loadCursor(null, [null], 0, "initial");
    } else {
      setLoading(false);
    }
    return () => {
      generationRef.current += 1;
      abortRef.current?.abort();
      loadingRef.current = false;
    };
  }, [enabled, loadCursor, queryKey]);

  const next = useCallback(async () => {
    const cursor = pageRef.current?.next_cursor;
    if (!cursor || loadingRef.current) {
      return;
    }
    const nextIndex = pageIndexRef.current + 1;
    const nextStack = [
      ...stackRef.current.slice(0, nextIndex),
      cursor,
    ];
    await loadCursor(cursor, nextStack, nextIndex, "next");
  }, [loadCursor]);

  const previous = useCallback(async () => {
    if (pageIndexRef.current === 0 || loadingRef.current) {
      return;
    }
    const previousIndex = pageIndexRef.current - 1;
    const previousStack = stackRef.current.slice(0, pageIndexRef.current + 1);
    await loadCursor(
      previousStack[previousIndex] ?? null,
      previousStack,
      previousIndex,
      "previous",
    );
  }, [loadCursor]);

  const reload = useCallback(async () => {
    await loadCursor(
      stackRef.current[pageIndexRef.current] ?? null,
      stackRef.current,
      pageIndexRef.current,
      "reload",
    );
  }, [loadCursor]);

  return {
    error,
    focusRevision,
    hasNext: Boolean(page?.next_cursor),
    hasPrevious: pageIndex > 0,
    loading,
    next,
    page,
    pageIndex,
    previous,
    recoveredFromCursor,
    reload,
  };
}

// Deliberately narrower than shared/lib/abortError: cursor pagination only
// ever aborts through its own AbortController, and widening this to the
// duck-typed check would start swallowing unrelated failures that merely carry
// a name of "AbortError" as if the page had been cancelled.
function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

function isRecoverableCursorError(error: unknown) {
  if (!(error instanceof ApiError)) {
    return false;
  }
  return [
    "invalid_cursor",
    "stale_cursor",
    "cursor_query_mismatch",
    "cursor_invalid",
    "mcp_client_cursor_invalid",
    "invalid_access_key_cursor",
  ].includes(error.body.error_code);
}
