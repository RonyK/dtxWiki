import { ListPlus } from "lucide-react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { WorkspaceGraphNode } from "../../../shared/api/workspaceGraph";
import {
  GraphDetailActions, GraphDetailCenterButton, GraphDetailOpenLink, GraphDetailOtherGraphLink, GraphDetailPinButton, type GraphPinState,
} from "../shared/GraphDetailActions";

export type WorkspaceGraphDocumentNavigation = (event: ReactMouseEvent<HTMLAnchorElement>) => void;

/**
 * What a selected node offers, shared by the details panel and the selection
 * card: the graphs' common row (open, center, pin, the Context graph), then
 * the Map's own expand for a cluster, then the card's Details.
 */
export function WorkspaceGraphNodeActions({
  busy,
  canExpand,
  children,
  expanding,
  node,
  onCenter,
  onExpand,
  onNavigate,
  onTogglePin,
  pinState,
}: {
  /** Another expansion request is in flight; this one waits for it. */
  busy: boolean;
  canExpand: boolean;
  /** Extra actions after the node's own, such as the card's Details. */
  children?: ReactNode;
  expanding: boolean;
  node: WorkspaceGraphNode;
  onCenter: (id: string) => void;
  onExpand: (clusterId: string) => void;
  onNavigate: WorkspaceGraphDocumentNavigation;
  onTogglePin: (id: string) => void;
  pinState: GraphPinState;
}) {
  const { t } = useTranslation("graph");
  const document = node.grain === "document" ? node : null;
  return (
    <GraphDetailActions>
      {document ? <GraphDetailOpenLink href={document.path} onNavigate={onNavigate} /> : null}
      <GraphDetailCenterButton onCenter={() => onCenter(node.id)} />
      <GraphDetailPinButton onToggle={() => onTogglePin(node.id)} state={pinState} />
      {document?.entity_kind === "context" ? (
        <GraphDetailOtherGraphLink href={`${document.path}${document.path.includes("?") ? "&" : "?"}tab=graph`}
          label={t("workspaceGraph.details.explore")} onNavigate={onNavigate} />
      ) : null}
      {canExpand ? (
        <button aria-label={t(expanding ? "workspaceGraph.list.expanding" : "workspaceGraph.list.expand")}
          className="workspace-graph-details-expand" disabled={busy || expanding} onClick={() => onExpand(node.id)} type="button">
          <ListPlus aria-hidden="true" size={16} />
          {t(expanding ? "workspaceGraph.list.expanding" : "workspaceGraph.list.expandAction")}
        </button>
      ) : null}
      {children}
    </GraphDetailActions>
  );
}
