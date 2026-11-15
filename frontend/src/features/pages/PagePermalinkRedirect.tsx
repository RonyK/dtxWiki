import { Navigate, useLocation } from "react-router-dom";

import { formatCanonicalContextUrl } from "../../shared/lib/workspaceIdentity";
import { NotFoundScreen } from "../errors/NotFoundScreen";
import { isSafeBrowserQuery, matchBrowserRoute } from "../../shared/lib/browserRoutes";

export function PagePermalinkRedirect() {
  const location = useLocation();
  const { hash, search } = location;
  const match = matchBrowserRoute(location.pathname);
  // A short permalink has no workspace authority at this entry point.
  // Existence and access checks belong to the destination's scoped read API.
  if (match?.route.locatorKind !== "stable_full" || !isSafeBrowserQuery(search, location.pathname)) return <NotFoundScreen />;
  return <Navigate replace state={location.state} to={formatCanonicalContextUrl(match.params.workspaceId, match.params.pageId) + search + hash} />;
}
