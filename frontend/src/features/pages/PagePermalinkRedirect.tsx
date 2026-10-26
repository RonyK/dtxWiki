import { Navigate, useLocation, useParams } from "react-router-dom";

import { formatCanonicalContextUrl, isContextId } from "../../shared/lib/contextLinks";
import { NotFoundScreen } from "../errors/NotFoundScreen";
import { hasConflictingWorkspaceQuery } from "../../shared/lib/workspaceRoutes";

export function PagePermalinkRedirect() {
  const { workspaceId = "", pageId = "" } = useParams();
  const { hash, search } = useLocation();
  // A short permalink has no workspace authority at this entry point.
  // Existence and access checks belong to the destination's scoped read API.
  if (!isContextId(workspaceId) || !isContextId(pageId) || hasConflictingWorkspaceQuery(search)) return <NotFoundScreen />;
  return <Navigate replace to={formatCanonicalContextUrl(workspaceId.toLowerCase(), pageId.toLowerCase()) + search + hash} />;
}
