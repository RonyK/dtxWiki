import type { DependencyList, Dispatch, SetStateAction } from "react";
import { useEffect, useRef, useState } from "react";

import { apiErrorMessage } from "../api/client";

type UseAsyncLoadOptions<T> = {
  errorMessage: string;
  /** Maps a caught value to safe, localized UI copy. */
  formatError?: (error: unknown, fallback: string) => string;
  fallbackData?: T;
  onSuccess?: (data: T) => void;
  /**
   * Whether the loader receives an AbortSignal. Read once at the first render.
   * Toggling this value across renders is unsupported.
   */
  signalAware?: boolean;
};

type UseAsyncLoadResult<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
  setData: Dispatch<SetStateAction<T | null>>;
  setError: Dispatch<SetStateAction<string | null>>;
};

export function useAsyncLoad<T>(
  load: (signal?: AbortSignal) => Promise<T>,
  deps: DependencyList,
  options: UseAsyncLoadOptions<T>,
): UseAsyncLoadResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const signalAwareRef = useRef(options.signalAware);
  // Monotonically-increasing generation token. Each effect run captures its
  // own value and any settled promise (.then / .catch / .finally) compares
  // against the latest token before touching React state, ensuring that
  // stale responses from superseded loads cannot corrupt the rendered UI.
  const generationRef = useRef(0);
  const previousDepsRef = useRef<DependencyList | null>(null);

  useEffect(() => {
    // Did the QUERY change, or is this a reload of the same one? The effect
    // cannot tell from inside the catch, and the two need opposite recovery:
    // a superseded query must drop its rows and cursor, a reload must not.
    const previousDeps = previousDepsRef.current;
    const depsChanged =
      previousDeps === null ||
      previousDeps.length !== deps.length ||
      previousDeps.some((value, index) => !Object.is(value, deps[index]));
    previousDepsRef.current = deps;
    generationRef.current += 1;
    const generation = generationRef.current;
    const controller = signalAwareRef.current ? new AbortController() : undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);

    const isStale = () =>
      cancelled ||
      controller?.signal.aborted === true ||
      generation !== generationRef.current;

    load(controller?.signal)
      .then((nextData) => {
        if (isStale()) {
          return;
        }
        setData(nextData);
        options.onSuccess?.(nextData);
      })
      .catch((exc) => {
        if (isStale()) {
          return;
        }
        // Loaders can throw AbortError defensively when their request id is
        // superseded - treat it the same as a real abort instead of bubbling
        // it to the UI as an error message.
        if (exc instanceof DOMException && exc.name === "AbortError") {
          return;
        }
        setError(
          options.formatError?.(exc, options.errorMessage) ??
            apiErrorMessage(exc, options.errorMessage),
        );
        if ("fallbackData" in options) {
          // fallbackData is the "nothing loaded yet" shape. A failed RELOAD of
          // the same query keeps what is on screen (the error banner reports
          // it) instead of wiping a loaded list to an empty state that reads as
          // "there is nothing here". A failed query CHANGE must still fall back:
          // its rows — and its keyset cursor — belong to a query the user has
          // navigated away from, and Load more would pair that cursor with the
          // new filters.
          setData((current) =>
            current === null || depsChanged ? options.fallbackData ?? null : current,
          );
        }
      })
      .finally(() => {
        if (isStale()) {
          return;
        }
        setLoading(false);
      });

    return () => {
      cancelled = true;
      controller?.abort();
    };
    // This hook intentionally accepts a caller-owned dependency tuple. `load`
    // and `options` are snapshots for that tuple; adding their inline identity
    // would turn every render into a reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, reloadKey]);

  return {
    data,
    error,
    loading,
    reload: () => setReloadKey((current) => current + 1),
    setData,
    setError,
  };
}
