import { Compass } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { getSelectedWorkspaceId } from "../../shared/api/workspace";
import { isWorkspaceProductRoute, readSafeReferenceReturnTo } from "../../shared/lib/navigationRecovery";
import { workspaceIdFromPathname, workspacePath } from "../../shared/lib/workspaceRoutes";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";

/**
 * Real 404 surface. Rendered inside Shell (under RequireAuth) so the sidebar
 * persists, instead of funnelling unknown URLs into the Pages landing.
 */
export function NotFoundScreen() {
  const { t } = useTranslation("shell");
  const location = useLocation();
  const navigate = useNavigate();
  const currentHref = `${location.pathname}${location.search}${location.hash}`;
  const hasValidWorkspaceRoute = isWorkspaceProductRoute(currentHref);
  const workspaceContext = useOptionalWorkspaceContext();
  const workspaces = workspaceContext?.workspaces ?? [];
  const knownWorkspaceIds = workspaces.map((workspace) => workspace.id);
  const pathWorkspaceId = workspaceIdFromPathname(location.pathname);
  const routeWorkspace = pathWorkspaceId
    ? workspaces.find((workspace) => workspace.id.toLowerCase() === pathWorkspaceId)
    : undefined;
  const selectedMembership = workspaces.find(
    (workspace) => workspace.id.toLowerCase() === (workspaceContext?.selectedId ?? "").toLowerCase(),
  ) ?? workspaces.find(
    (workspace) => workspace.id.toLowerCase() === (getSelectedWorkspaceId() ?? "").toLowerCase(),
  );
  const preferredWorkspace = routeWorkspace ?? selectedMembership ?? workspaces[0] ?? null;
  const previousHref = hasValidWorkspaceRoute
    ? readSafeReferenceReturnTo(location.state, currentHref, knownWorkspaceIds)
    : null;
  const [workspaceChooserOpen, setWorkspaceChooserOpen] = useState(false);
  const [chosenWorkspaceId, setChosenWorkspaceId] = useState<string | null>(null);
  const chosenWorkspace = workspaces.find((workspace) => workspace.id === chosenWorkspaceId) ?? preferredWorkspace;

  function openWorkspaceDashboard(workspaceId: string) {
    setWorkspaceChooserOpen(false);
    navigate(workspacePath(workspaceId, "dashboard"), { replace: true, state: null });
  }

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main">
        <Breadcrumbs
          items={[
            {
              label: hasValidWorkspaceRoute
                ? t("unavailable.breadcrumb", { defaultValue: "Unavailable" })
                : t("notFound.breadcrumb", { defaultValue: "Not found" }),
            },
          ]}
        />
        <div className="empty-state empty-state-rich route-not-found">
          <Compass size={22} aria-hidden="true" />
          <h1>
            {hasValidWorkspaceRoute
              ? t("unavailable.title", { defaultValue: "This item can't be opened" })
              : t("notFound.title", { defaultValue: "We couldn't find that page" })}
          </h1>
          <p>
            {hasValidWorkspaceRoute
              ? t("unavailable.body", {
                  defaultValue:
                    "This item may be missing, or your current account may not have access. Confirm the link or ask the sender to check its sharing settings.",
                })
              : t("notFound.body", {
                  defaultValue:
                    "The link may be mistyped or out of date. Use the navigation, or jump back to a known place.",
                })}
          </p>
          <div className="empty-state-actions">
            {hasValidWorkspaceRoute ? (
              <>
                {previousHref ? (
                  <Link
                    className="icon-button text-button primary"
                    replace
                    state={null}
                    to={previousHref}
                  >
                    {t("unavailable.previous", { defaultValue: "Return to previous page" })}
                  </Link>
                ) : null}
                {routeWorkspace ? (
                  <button
                    className={`icon-button text-button${previousHref ? "" : " primary"}`}
                    onClick={() => openWorkspaceDashboard(routeWorkspace.id)}
                    type="button"
                  >
                    {t("unavailable.openWorkspace", {
                      defaultValue: "Open {{title}} dashboard",
                      title: routeWorkspace.title,
                    })}
                  </button>
                ) : workspaces.length > 0 ? (
                  <button
                    className="icon-button text-button"
                    onClick={() => {
                      setChosenWorkspaceId(preferredWorkspace?.id ?? null);
                      setWorkspaceChooserOpen(true);
                    }}
                    type="button"
                  >
                    {t("unavailable.selectWorkspace", { defaultValue: "Select a workspace" })}
                  </button>
                ) : (
                  <Link className="icon-button text-button" to="/workspaces/find">
                    {t("workspace.find", { defaultValue: "Find workspaces" })}
                  </Link>
                )}
              </>
            ) : (
              <>
                {routeWorkspace ? (
                  <button
                    className="icon-button text-button primary"
                    onClick={() => openWorkspaceDashboard(routeWorkspace.id)}
                    type="button"
                  >
                    {t("unavailable.openWorkspace", {
                      defaultValue: "Open {{title}} dashboard",
                      title: routeWorkspace.title,
                    })}
                  </button>
                ) : null}
                <Link className={`icon-button text-button${routeWorkspace ? "" : " primary"}`} to="/workspaces/find">
                  {t("workspace.find", { defaultValue: "Find workspaces" })}
                </Link>
                <Link className="icon-button text-button" to="/account">
                  {t("topbar.myPage", { defaultValue: "Account" })}
                </Link>
              </>
            )}
          </div>
        </div>
      </section>
      {workspaceChooserOpen && workspaces.length > 0 ? (
        <Dialog
          labelledBy="unavailable-workspace-title"
          onClose={() => setWorkspaceChooserOpen(false)}
        >
          <section className="dialog-form">
            <header className="dialog-header">
              <div>
                <p className="eyebrow">{t("unavailable.breadcrumb", { defaultValue: "Unavailable" })}</p>
                <h2 id="unavailable-workspace-title">
                  {t("unavailable.selectWorkspace", { defaultValue: "Select a workspace" })}
                </h2>
              </div>
              <DialogCloseButton
                label={t("unavailable.closeChooser", { defaultValue: "Close workspace chooser" })}
                onClick={() => setWorkspaceChooserOpen(false)}
              />
            </header>
            <label className="form-field" htmlFor="unavailable-workspace-select">
              <span>{t("unavailable.workspaces", { defaultValue: "Your workspaces" })}</span>
              <select
                id="unavailable-workspace-select"
                onChange={(event) => setChosenWorkspaceId(event.currentTarget.value)}
                value={chosenWorkspace?.id ?? ""}
              >
                {workspaces.map((workspace) => (
                  <option key={workspace.id} value={workspace.id}>{workspace.title}</option>
                ))}
              </select>
            </label>
            <button
              className="icon-button text-button primary"
              disabled={!chosenWorkspace}
              onClick={() => {
                if (!chosenWorkspace) return;
                openWorkspaceDashboard(chosenWorkspace.id);
              }}
              type="button"
            >
              {chosenWorkspace
                ? t("unavailable.openWorkspace", {
                    defaultValue: "Open {{title}} dashboard",
                    title: chosenWorkspace.title,
                  })
                : t("unavailable.selectWorkspace", { defaultValue: "Select a workspace" })}
            </button>
          </section>
        </Dialog>
      ) : null}
    </div>
  );
}
