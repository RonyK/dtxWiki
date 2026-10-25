import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useParams } from "react-router-dom";

import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { readContextPage } from "../../shared/api/context";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { NotFoundScreen } from "../errors/NotFoundScreen";

/**
 * P10.1: entry point for the canonical
 * `/workspaces/:workspaceId/context/pages/:pageId/edit` permalink.
 *
 * `PageEditor` is still slug-routed end-to-end (readPageBySlug, its own
 * autosave-to-edit address-bar transition, editor draft-recovery keys), so
 * rewriting it to be id-native is out of scope here. This resolver fetches
 * the page by the URL's id, then hands off to the existing `/context/:slug/edit`
 * route via a replace navigation once the slug is known — the canonical URL
 * works as a shareable entry link, but the address bar settles on the
 * slug-based editor route it hands off to (see the P10.1 handoff report).
 */
export function ContextEditPermalinkResolver() {
  const { t } = useTranslation("pages");
  const { pageId = "", workspaceId: routedWorkspaceId = "" } = useParams();
  const workspaceContext = useOptionalWorkspaceContext();
  const selectWorkspaceId = workspaceContext?.selectWorkspaceId;
  const ambientSelectedId = workspaceContext?.selectedId ?? null;
  const ambientWorkspaces = workspaceContext?.workspaces;
  const syncedRouteRef = useRef<string | null>(null);
  const [slug, setSlug] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Silent, member-only resync so the editor session that follows (still
  // ambient-workspace-driven) lands in the workspace the canonical link named.
  // See ContextScreen's identical resync effect for the full rationale.
  useEffect(() => {
    if (
      !routedWorkspaceId ||
      !selectWorkspaceId ||
      syncedRouteRef.current === `${routedWorkspaceId}:${pageId}`
    ) {
      return;
    }
    const isMember = ambientWorkspaces?.some(
      (workspace) => workspace.id === routedWorkspaceId,
    );
    if (!isMember) {
      return;
    }
    syncedRouteRef.current = `${routedWorkspaceId}:${pageId}`;
    if (routedWorkspaceId !== ambientSelectedId) selectWorkspaceId(routedWorkspaceId);
  }, [routedWorkspaceId, pageId, ambientSelectedId, ambientWorkspaces, selectWorkspaceId]);

  useEffect(() => {
    let cancelled = false;
    setSlug(null);
    setNotFound(false);
    setError(null);
    if (!routedWorkspaceId || !pageId) {
      setNotFound(true);
      return undefined;
    }
    void readContextPage(routedWorkspaceId, pageId)
      .then((page) => {
        if (!cancelled) {
          setSlug(page.slug);
        }
      })
      .catch((exc: unknown) => {
        if (cancelled) {
          return;
        }
        if (exc instanceof ApiError && (exc.status === 404 || exc.status === 422)) {
          setNotFound(true);
        } else {
          setError(apiErrorMessage(exc, t("reader.failedLoad")));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [pageId, routedWorkspaceId, t]);

  if (notFound) {
    return <NotFoundScreen />;
  }
  if (slug) {
    return <Navigate replace to={`/context/${encodeURIComponent(slug)}/edit`} />;
  }
  if (error) {
    return <StatusBanner tone="warning">{error}</StatusBanner>;
  }
  return <RouteLoading label={t("reader.loading")} />;
}
