import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useParams } from "react-router-dom";

import { ApiError, apiErrorMessage, apiRequest } from "../../shared/api/client";
import { getSettingsPolicies, updateSettingsPolicyBatch, type SettingsPolicies } from "../../shared/api/operations";
import type { WorkspaceListResponse, WorkspaceSummary } from "../../shared/api/workspace";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { workspacePath } from "../../shared/lib/workspaceRoutes";
import { SettingsDirtyProvider } from "./SettingsDirtyRegistry";
import { WorkspaceSettingsSection } from "./SettingsScreen";

type LoadedWorkspace = {
  workspace: WorkspaceSummary;
  workspaces: WorkspaceSummary[];
  defaultWorkspaceId: string | null;
  policies: SettingsPolicies;
};

export function WorkspaceScopedSettingsScreen() {
  const { workspaceId = "" } = useParams();
  return <ScopedWorkspace key={workspaceId} workspaceId={workspaceId} />;
}

function ScopedWorkspace({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation("settings");
  const { hash } = useLocation();
  const [loaded, setLoaded] = useState<LoadedWorkspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState<{ keys: string[]; label: string } | null>(null);
  const [deleted, setDeleted] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        // The shared list helper resolves and persists the ambient selection.
        const response = await apiRequest<WorkspaceListResponse>("/api/v1/workspaces", { signal: controller.signal });
        const workspace = response.data.workspaces.find((candidate) => candidate.id === workspaceId && candidate.role);
        if (!workspace) throw new Error(t("workspaceScoped.unavailable"));
        const policies = await getSettingsPolicies(workspaceId);
        if (!controller.signal.aborted) {
          setLoaded({ workspace, policies, workspaces: response.data.workspaces, defaultWorkspaceId: response.data.default_workspace_id ?? null });
        }
      } catch {
        if (!controller.signal.aborted) setError(t("workspaceScoped.unavailable"));
      }
    })();
    return () => controller.abort();
  }, [workspaceId, t]);

  const isLoaded = loaded !== null;
  useEffect(() => {
    if (isLoaded && !deleted && hash === "#workspace-danger-title") {
      document.getElementById("workspace-danger-title")?.scrollIntoView?.();
    }
  }, [isLoaded, deleted, hash]);

  return (
    <main className="workspace-screen single-column">
      <section className="feature-main feature-main--reader settings-content">
      <Link to={`${workspacePath(workspaceId, "settings")}?section=workspace`}>
        {t("workspaceScoped.back")}
      </Link>
      {deleted ? (
        <p role="status">{t("workspaceScoped.deleted")}</p>
      ) : error ? (
        <StatusBanner tone="warning">{error}</StatusBanner>
      ) : !loaded ? (
        <RouteLoading label={t("workspaceScoped.loading")} />
      ) : (
        <SettingsDirtyProvider>
          <h1>{loaded.workspace.title}</h1>
          {saveError ? <StatusBanner tone="warning">{saveError}</StatusBanner> : null}
          <WorkspaceSettingsSection
            workspace={loaded.workspace}
            workspaceLoading={false}
            defaultWorkspaceId={loaded.defaultWorkspaceId}
            workspaces={loaded.workspaces}
            onDefaultChanged={async (id) => {
              await apiRequest("/api/v1/users/me/default-workspace", { method: "PATCH", body: JSON.stringify({ workspace_id: id }) });
              setLoaded((current) => current ? { ...current, defaultWorkspaceId: id } : current);
            }}
            policies={loaded.policies}
            busyKey={busyKey}
            onFieldsSave={async (group, updates, label) => {
              if (!updates.length) return false;
              setBusyKey(`${group}:*`);
              setSaveError(null);
              try {
                const policies = await updateSettingsPolicyBatch(workspaceId, {
                  group,
                  expected_revision: loaded.policies[group].revision ?? "0".repeat(64),
                  changes: Object.fromEntries(updates.map(({ field, value }) => [field.key, value])),
                });
                setLoaded((current) => current ? { ...current, policies } : current);
                setSaveNotice({ keys: updates.map(({ field }) => field.key), label });
                return true;
              } catch (caught) {
                if (caught instanceof ApiError && caught.body.error_code === "settings_revision_conflict") {
                  setSaveError(t("policy.revisionConflict"));
                  try {
                    const policies = await getSettingsPolicies(workspaceId);
                    setLoaded((current) => current ? { ...current, policies } : current);
                  } catch {
                    setSaveError(t("workspaceScoped.unavailable"));
                  }
                } else {
                  setSaveError(apiErrorMessage(caught, t("errors.requestFailed")));
                }
                return false;
              } finally {
                setBusyKey(null);
              }
            }}
            saveNotice={saveNotice}
            readOnly={(loaded.workspace.role ?? "").toLowerCase() !== "owner"}
            onPoliciesChanged={(policies) => setLoaded((current) => current ? { ...current, policies } : current)}
            onSlugChanged={async (slug) => {
              setLoaded((current) => current ? { ...current, workspace: { ...current.workspace, slug } } : current);
            }}
            onDelete={async (_workspace, options) => {
              const query = options.purgeContents ? "?purge=true" : "";
              await apiRequest(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}${query}`, {
                method: "DELETE",
                headers: options.purgeGrant ? { "X-Purge-Grant": options.purgeGrant } : undefined,
              });
              setDeleted(true);
            }}
          />
        </SettingsDirtyProvider>
      )}
      </section>
    </main>
  );
}
