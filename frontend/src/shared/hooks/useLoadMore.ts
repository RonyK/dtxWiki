import { useCallback, useEffect, useRef, useState } from "react";

/**
 * One page of a keyset-paginated list as returned by the server wrappers
 * (next_cursor === null means the list is exhausted).
 */
export type LoadMorePage<T> = {
  items: T[];
  next_cursor: string | null;
  total?: number | null;
};

type UseLoadMoreOptions<T> = {
  /** First page (already fetched by the route loader), keyed by `resetKey`. */
  initial: LoadMorePage<T>;
  /**
   * Identity of the current filter/sort context. When it changes the
   * accumulator resets to a fresh `initial`, dropping any appended pages so a
   * filter switch never shows stale rows.
   */
  resetKey?: string;
  /** Fetch the next page given the current cursor. */
  fetchPage: (cursor: string) => Promise<LoadMorePage<T>>;
};

type UseLoadMoreResult<T> = {
  items: T[];
  nextCursor: string | null;
  total: number | null;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => Promise<void>;
  reset: (next: LoadMorePage<T>) => void;
};

/**
 * Reusable cursor accumulator generalized from ChatsListScreen.loadMore: holds
 * the appended items + the live cursor, exposes loadMore()/loadingMore, and
 * skips a stale append when the base page was replaced mid-flight (a filter
 * change advances `resetKey`, so the in-flight cursor no longer matches).
 */
export function useLoadMore<T>({
  initial,
  resetKey,
  fetchPage,
}: UseLoadMoreOptions<T>): UseLoadMoreResult<T> {
  const [page, setPage] = useState<LoadMorePage<T>>(initial);
  const [loadingMore, setLoadingMore] = useState(false);

  // Re-seed when the loader hands us a new first page (filter/sort changed).
  // Tracking the key (not the object) avoids resetting on every render when the
  // caller passes a fresh-but-equivalent `initial`.
  const seededKey = useRef<string | undefined>(resetKey);
  useEffect(() => {
    if (seededKey.current !== resetKey) {
      seededKey.current = resetKey;
      setPage(initial);
    }
  }, [resetKey, initial]);

  const reset = useCallback((next: LoadMorePage<T>) => {
    setPage(next);
  }, []);

  const loadMore = useCallback(async () => {
    let cursor: string | null = null;
    // Read the live cursor without depending on it so the callback identity is
    // stable; bail out when exhausted or a request is already in flight.
    setPage((current) => {
      cursor = current.next_cursor;
      return current;
    });
    if (!cursor || loadingMore) {
      return;
    }
    const requestCursor = cursor;
    setLoadingMore(true);
    try {
      const nextPage = await fetchPage(requestCursor);
      setPage((current) =>
        // Skip the append when a filter change replaced the base page while this
        // request was in flight (the cursor no longer matches the live page).
        current.next_cursor === requestCursor
          ? {
              items: [...current.items, ...nextPage.items],
              next_cursor: nextPage.next_cursor,
              total: nextPage.total ?? current.total ?? null,
            }
          : current,
      );
    } finally {
      setLoadingMore(false);
    }
  }, [fetchPage, loadingMore]);

  return {
    items: page.items,
    nextCursor: page.next_cursor,
    total: page.total ?? null,
    hasMore: page.next_cursor !== null,
    loadingMore,
    loadMore,
    reset,
  };
}
