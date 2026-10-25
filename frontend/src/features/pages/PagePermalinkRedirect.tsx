import { Navigate, useLocation, useParams } from "react-router-dom";

import { formatCanonicalContextUrl, isContextId } from "../../shared/lib/contextLinks";
import { NotFoundScreen } from "../errors/NotFoundScreen";

export function PagePermalinkRedirect() {
  const { workspaceId = "", pageId = "" } = useParams();
  const { hash } = useLocation();
  // A short permalink has no workspace authority at this entry point.
  // Existence and access checks belong to the destination's scoped read API.
  if (!isContextId(workspaceId) || !isContextId(pageId)) return <NotFoundScreen />;
  return <Navigate replace to={formatCanonicalContextUrl(workspaceId, pageId, hash)} />;
}
