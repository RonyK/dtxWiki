import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useWorkspaceRoute, workspacePath } from "../../shared/lib/workspaceRoutes";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { useLabsPreferences } from "../../shared/hooks/useLabsPreferences";
import { NotFoundScreen } from "../errors/NotFoundScreen";
import {
  invalidateWorkspaceGraphCapability,
  useWorkspaceGraphCapability,
} from "./useWorkspaceGraphCapability";

/**
 * Render the Knowledge Map only for owners after workspace Labs activation.
 *
 * Other members use the same unavailable surface as other experiments.
 * Once activated, a shared /graph link explains the workspace's availability.
 * Until activation is saved, the canvas and its scene query never mount.
 */
export function WorkspaceGraphGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation("graph");
  const { selectedId: ambientWorkspaceId } = useWorkspaceContext();
  const route = useWorkspaceRoute();
  const selectedId = route?.workspaceId ?? ambientWorkspaceId;
  const { preferences, ready: labsReady, status: labsStatus } = useLabsPreferences();
  const { status, capability, enabled } = useWorkspaceGraphCapability(
    selectedId,
  );

  if (labsStatus === "loading") {
    return <StatusBanner tone="info">{t("workspaceGraph.gate.checking")}</StatusBanner>;
  }

  if (!labsReady || (capability && !capability.can_manage)) {
    return <NotFoundScreen />;
  }

  if (preferences.knowledgeMap && enabled) {
    return <>{children}</>;
  }

  let body: ReactNode;
  if (status === "error") {
    body = (
      <StatusBanner
        tone="warning"
        action={(
          <button
            className="workspace-graph-error-action"
            onClick={() => invalidateWorkspaceGraphCapability(selectedId)}
            type="button"
          >
            {t("workspaceGraph.ui.retry")}
          </button>
        )}
      >
        {t("workspaceGraph.gate.checkFailed")}
      </StatusBanner>
    );
  } else if (!capability) {
    body = <StatusBanner tone="info">{t("workspaceGraph.gate.checking")}</StatusBanner>;
  } else if (capability.workspace_map.reason === "service_unavailable") {
    body = (
      <StatusBanner tone="warning">
        {t("workspaceGraph.gate.serviceUnavailable")}
      </StatusBanner>
    );
  } else {
    body = (
      <StatusBanner
        tone="info"
        action={capability.can_manage ? (
          <Link className="workspace-graph-error-action" to={selectedId ? `${workspacePath(selectedId, "settings")}?section=labs` : "/workspaces/find"}>
            {t("workspaceGraph.gate.openSettings")}
          </Link>
        ) : undefined}
      >
        {t(capability.can_manage
          ? "workspaceGraph.gate.disabledOwner"
          : "workspaceGraph.gate.disabledMember")}
      </StatusBanner>
    );
  }

  return (
    <div className="workspace-screen single-column workspace-graph-screen">
      <section
        aria-labelledby="workspace-graph-title"
        className="feature-main workspace-graph"
      >
        <Breadcrumbs items={[{ label: t("workspaceGraph.ui.title") }]} />
        <header className="feature-header workspace-graph-header">
          <div>
            <p className="eyebrow">{t("workspaceGraph.ui.eyebrow")}</p>
            <h1 id="workspace-graph-title">{t("workspaceGraph.ui.title")}</h1>
          </div>
        </header>
        {body}
      </section>
    </div>
  );
}
