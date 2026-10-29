import { useEffect, useRef, type DependencyList } from "react";
import { useLocation, useNavigationType } from "react-router-dom";

type ScreenEntryRefreshOptions = {
  /** Whether this route shows the menu overview rather than a selected item. */
  isHome?: boolean;
  /** Dependencies that already cause the screen's loader to run. */
  queryDeps?: DependencyList;
  /** Reads an arrival notice consumed by a same-address history replacement. */
  readArrivalNotice?: (state: unknown) => unknown;
};

/**
 * The destination owns freshness, regardless of which link opens it. A retained
 * home reloads on a visit, Back/Forward, or same-address navigation. Mounts and
 * changed query dependencies already load; internal address repairs do not open
 * a new screen. Only notice-owning screens need to identify state consumption.
 */
export function useScreenEntryRefresh(
  homePath: string,
  refresh: () => void,
  { isHome = true, queryDeps = [], readArrivalNotice }: ScreenEntryRefreshOptions = {},
) {
  const location = useLocation();
  const navigationType = useNavigationType();
  const atHome = location.pathname.replace(/\/+$/, "") === homePath && !location.hash && isHome;
  const previous = useRef({ location, queryDeps });

  useEffect(() => {
    const prior = previous.current;
    previous.current = { location, queryDeps };
    if (prior.location.key === location.key || !atHome) {
      return;
    }
    if (navigationType === "REPLACE") {
      // Same-URL links use REPLACE too; callers must skip unchanged setSearchParams.
      const sameAddress =
        prior.location.pathname === location.pathname &&
        prior.location.search === location.search &&
        prior.location.hash === location.hash;
      if (
        !sameAddress ||
        (location.state === null && readArrivalNotice?.(prior.location.state))
      ) {
        return;
      }
    }
    if (
      prior.queryDeps.length !== queryDeps.length ||
      prior.queryDeps.some((value, index) => !Object.is(value, queryDeps[index]))
    ) {
      return;
    }
    refresh();
  }, [atHome, location, navigationType, queryDeps, readArrivalNotice, refresh]);
}
