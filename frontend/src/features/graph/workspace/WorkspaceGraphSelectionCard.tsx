import { PanelBottomOpen, X } from "lucide-react";
import type { RefObject } from "react";
import { useTranslation } from "react-i18next";

import type { WorkspaceGraphEdge, WorkspaceGraphNode } from "../../../shared/api/workspaceGraph";
import { GraphDetailActions, GraphDetailCenterButton, type GraphPinState } from "../shared/GraphDetailActions";
import { WorkspaceGraphNodeActions, type WorkspaceGraphDocumentNavigation } from "./WorkspaceGraphNodeActions";
import { formatCount, workspaceGraphEnumLabel, workspaceGraphNodeKind, workspaceGraphNodeLocation } from "./workspaceGraphLabels";

/** Non-modal summary of a map selection for the sheet layout. */
export function WorkspaceGraphSelectionCard({
  busy,
  canExpand,
  detailsRef,
  edge,
  expanding,
  labels,
  node,
  onCenter,
  onClear,
  onDetails,
  onExpand,
  onNavigate,
  onTogglePin,
  pinState,
}: {
  busy: boolean;
  canExpand: boolean;
  detailsRef: RefObject<HTMLButtonElement | null>;
  edge: WorkspaceGraphEdge | null;
  expanding: boolean;
  labels: ReadonlyMap<string, string>;
  node: WorkspaceGraphNode | null;
  onCenter: (id: string) => void;
  onClear: () => void;
  onDetails: () => void;
  onExpand: (clusterId: string) => void;
  onNavigate: WorkspaceGraphDocumentNavigation;
  onTogglePin: (id: string) => void;
  pinState: GraphPinState;
}) {
  const { t } = useTranslation("graph");
  if (!node && !edge) return null;
  const title = node ? node.label : workspaceGraphEnumLabel(edge!.relation_kind, t);
  const summary = node?.grain === "cluster"
    ? [workspaceGraphEnumLabel(node.cluster_kind, t),
      `${t("workspaceGraph.details.members")} ${formatCount(node.member_count)}`,
      `${t("workspaceGraph.details.relations")} ${formatCount(node.relation_count)}`].join(" · ")
    : node
      ? [workspaceGraphEnumLabel(workspaceGraphNodeKind(node), t),
        workspaceGraphNodeLocation(node) || t("workspaceGraph.folders.unplaced"),
        ...node.grain === "document" ? [t("workspaceGraph.details.referencedByValue", { count: node.reference_count })] : [],
      ].join(" · ")
      : `${labels.get(edge!.source) ?? "—"} → ${labels.get(edge!.target) ?? "—"}`;
  const details = (
    <button className="workspace-graph-selection-card-details" onClick={onDetails} ref={detailsRef} type="button">
      <PanelBottomOpen aria-hidden="true" size={16} />{t("workspaceGraph.card.details")}
    </button>
  );
  return (
    <section aria-label={t("graph.details.heading")} className="workspace-graph-selection-card">
      <div className="workspace-graph-selection-card-head">
        <strong title={title}>{title}</strong>
        <button aria-label={t("graph.details.clearSelection")} className="icon-button"
          onClick={onClear} title={t("graph.details.clearSelection")} type="button">
          <X aria-hidden="true" size={16} />
        </button>
      </div>
      <p title={summary}>{summary}</p>
      {node ? (
        <WorkspaceGraphNodeActions busy={busy} canExpand={canExpand} expanding={expanding} node={node}
          onCenter={onCenter} onExpand={onExpand} onNavigate={onNavigate} onTogglePin={onTogglePin} pinState={pinState}>
          {details}
        </WorkspaceGraphNodeActions>
      ) : (
        <GraphDetailActions>
          <GraphDetailCenterButton onCenter={() => onCenter(edge!.id)} />
          {details}
        </GraphDetailActions>
      )}
    </section>
  );
}
