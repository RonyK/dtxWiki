import { FileText, ListPlus, LocateFixed, Network } from "lucide-react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { WorkspaceGraphNode } from "../../../shared/api/workspaceGraph";

export type WorkspaceGraphDocumentNavigation = (event: ReactMouseEvent<HTMLAnchorElement>) => void;

/** What a selected node offers, shared by the details panel and the selection card. */
export function WorkspaceGraphNodeActions({
  busy,
  canExpand,
  children,
  expanding,
  node,
  onCenter,
  onExpand,
  onNavigate,
}: {
  /** Another expansion request is in flight; this one waits for it. */
  busy: boolean;
  canExpand: boolean;
  /** Extra actions after the node's own, such as the card's Details. */
  children?: ReactNode;
  expanding: boolean;
  node: WorkspaceGraphNode;
  onCenter?: (id: string) => void;
  onExpand: (clusterId: string) => void;
  onNavigate: WorkspaceGraphDocumentNavigation;
}) {
  const { t } = useTranslation("graph");
  const center = onCenter ? <WorkspaceGraphCenterButton onCenter={() => onCenter(node.id)} /> : null;
  if (node.grain === "cluster") {
    if (!canExpand && !center && !children) return null;
    return (
      <div className="workspace-graph-detail-actions">
        {canExpand ? (
          <button aria-label={t(expanding ? "workspaceGraph.list.expanding" : "workspaceGraph.list.expand")}
            className="workspace-graph-details-expand" disabled={busy || expanding} onClick={() => onExpand(node.id)} type="button">
            <ListPlus aria-hidden="true" size={16} />
            {t(expanding ? "workspaceGraph.list.expanding" : "workspaceGraph.list.expandAction")}
          </button>
        ) : null}
        {center}
        {children}
      </div>
    );
  }
  return (
    <div className="workspace-graph-detail-actions">
      <Link className="workspace-graph-detail-open"
        onClick={onNavigate} title={t("workspaceGraph.details.openDocument")} to={node.path}>
        <FileText aria-hidden="true" size={16} />{t("workspaceGraph.list.open")}
      </Link>
      {center}
      {node.entity_kind === "context" ? (
        <Link onClick={onNavigate}
          title={t("workspaceGraph.details.explore")} to={`${node.path}${node.path.includes("?") ? "&" : "?"}tab=graph`}>
          <Network aria-hidden="true" size={16} />{t("workspaceGraph.details.exploreAction")}
        </Link>
      ) : null}
      {children}
    </div>
  );
}

export function WorkspaceGraphCenterButton({ onCenter }: { onCenter: () => void }) {
  const { t } = useTranslation("graph");
  return (
    <button className="workspace-graph-center-map" onClick={onCenter} title={t("workspaceGraph.details.centerMap")} type="button">
      <LocateFixed aria-hidden="true" size={16} />{t("workspaceGraph.details.centerAction")}
    </button>
  );
}
