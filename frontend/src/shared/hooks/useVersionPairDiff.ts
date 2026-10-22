import { useEffect, useState } from "react";

import { apiErrorMessage } from "../api/client";
import type { PageVersionDiff } from "../api/pages";
import type { VersionPair } from "../lib/historyVersionPair";

type DiffState = {
  diff: PageVersionDiff | null;
  error: string | null;
  loadingVersionId: string | null;
};

const IDLE: DiffState = { diff: null, error: null, loadingVersionId: null };

// Loads the diff for whichever pair a history surface has open. A new pair
// (or closing with null) aborts the request still in flight, so a slow
// earlier response can never land in the dialog of a later one. `load` must be
// stable across renders (useCallback), or every render refetches.
export function useVersionPairDiff<P extends VersionPair>(
  pair: P | null,
  load: (pair: P, signal: AbortSignal) => Promise<PageVersionDiff>,
  failureMessage: string,
): DiffState {
  const [state, setState] = useState<DiffState>(IDLE);
  useEffect(() => {
    if (!pair) {
      setState(IDLE);
      return undefined;
    }
    const controller = new AbortController();
    setState({ ...IDLE, loadingVersionId: pair.to });
    load(pair, controller.signal)
      .then((diff) => {
        if (!controller.signal.aborted) {
          setState({ ...IDLE, diff });
        }
      })
      .catch((exc: unknown) => {
        if (!controller.signal.aborted) {
          setState({ ...IDLE, error: apiErrorMessage(exc, failureMessage) });
        }
      });
    return () => controller.abort();
  }, [failureMessage, load, pair]);
  // A diff belongs to exactly one pair; never show it under another.
  const matches =
    state.diff !== null &&
    pair !== null &&
    state.diff.from_version_id === pair.from &&
    state.diff.to_version_id === pair.to;
  return matches || state.diff === null ? state : { ...state, diff: null };
}
