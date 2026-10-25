import { Compass } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { parseWorkspaceRoute, workspacePath } from "../../shared/lib/workspaceRoutes";
import { isContextId } from "../../shared/lib/contextLinks";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";

/**
 * Real 404 surface. Rendered inside Shell (under RequireAuth) so the sidebar
 * persists, instead of funnelling unknown URLs into the Pages landing — a
 * mistyped or stale link now says "not found" rather than silently becoming
 * "you have no pages".
 */
export function NotFoundScreen() {
  const { t } = useTranslation("shell");
  const routeWorkspaceId = parseWorkspaceRoute(useLocation().pathname)?.workspaceId;
  const workspaceId = routeWorkspaceId && isContextId(routeWorkspaceId) ? routeWorkspaceId : null;
  return (
    <div className="workspace-screen single-column">
      <section className="feature-main">
        <Breadcrumbs
          items={[
            { label: t("notFound.breadcrumb", { defaultValue: "Not found" }) },
          ]}
        />
        <div className="empty-state empty-state-rich route-not-found">
          <Compass size={22} aria-hidden="true" />
          <h1>
            {t("notFound.title", { defaultValue: "We couldn't find that page" })}
          </h1>
          <p>
            {t("notFound.body", {
              defaultValue:
                "The link may be mistyped or out of date. Use the navigation, or jump back to a known place.",
            })}
          </p>
          <div className="empty-state-actions">
            <Link className="icon-button text-button primary" to={workspaceId ? workspacePath(workspaceId, "dashboard") : "/workspaces/find"}>
              {workspaceId ? t("notFound.backToDashboard", { defaultValue: "Back to dashboard" }) : t("workspaceResolution.find")}
            </Link>
            <Link className="icon-button text-button" to={workspaceId ? workspacePath(workspaceId, "context") : "/account"}>
              {workspaceId ? t("notFound.openContext", { defaultValue: "Open Context" }) : t("topbar.myPage")}
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}
