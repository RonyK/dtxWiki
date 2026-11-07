import { ListPlus } from "lucide-react";
import type { TFunction } from "i18next";
import { useCallback, useMemo, type MouseEvent as ReactMouseEvent } from "react";
import { useTranslation } from "react-i18next";

import type { WorkspaceGraphEdge, WorkspaceGraphNode } from "../../../shared/api/workspaceGraph";
import { localEdgeVisualKind } from "../local/edgeKinds";
import { GraphNodeCue, type GraphEdgeCueKind } from "../shared/GraphCues";
import type { GraphNodeVisualKind } from "../shared/graphTokens";
import {
  RELATION_FLOW_PAGE,
  RELATION_LIST_PAGE,
  RelationListChip,
  RelationListFoldHeader,
  RelationListMiniMap,
  RelationListMoreButton,
  RelationListOpenLink,
  RelationListStrokeCue,
  RelationListSummary,
  relationListStrokeSummary,
} from "../shared/relationList/parts";
import { RelationEnds } from "../shared/relationList/RelationEnds";
import { RelationFlow } from "../shared/relationList/RelationFlow";
import type { RelationListEdgeView, RelationListNodeView, RelationListStroke } from "../shared/relationList/types";
import type { WorkspaceGraphNodeRelations } from "../workspaceGraphModel";
import type { WorkspaceGraphDocumentNavigation } from "./WorkspaceGraphNodeActions";
import { workspaceGraphEnumLabel } from "./workspaceGraphLabels";

/** Same shape and colour as the map point and the legend cue. */
export type WorkspaceGraphNodeCueKind = "context" | "library" | "agent" | "cluster";

/** A Map cluster is a grouping, drawn like a folder: the grey ring. */
export const WORKSPACE_GRAPH_CUE_VISUAL = {
  context: "context", library: "library", agent: "agent", cluster: "folder",
} as const satisfies Record<WorkspaceGraphNodeCueKind, GraphNodeVisualKind>;

/**
 * The line style a relation kind has in either graph. The Map canvas draws
 * only links and citations; its lists still name every kind in the shared style.
 */
export type WorkspaceRelationCue = Exclude<GraphEdgeCueKind, "removed">;
export function workspaceRelationCue(kind: string): WorkspaceRelationCue {
  return localEdgeVisualKind(kind) ?? "other";
}

export function workspaceGraphNodeCueKind(node: WorkspaceGraphNode): WorkspaceGraphNodeCueKind {
  return node.grain === "cluster" ? "cluster" : node.entity_kind;
}

/** Results and the selected node's relations grow by these shared page sizes. */
export const WORKSPACE_GRAPH_RESULT_PAGE = RELATION_LIST_PAGE;

const STROKE_ORDER: readonly RelationListStroke[] = ["link", "citation", "coAccess", "membership", "action", "other"];

export function WorkspaceGraphNodeCue({ className = "workspace-graph-node-cue is-inline", kind, size = "inline" }: {
  className?: string;
  kind: WorkspaceGraphNodeCueKind;
  size?: "inline" | "legend";
}) {
  return <GraphNodeCue className={size === "legend" ? undefined : className} kind={WORKSPACE_GRAPH_CUE_VISUAL[kind]} />;
}

/** Short chip text; the full kind name stays in the tooltip and accessible name. */
function workspaceGraphNodeChipLabel(node: WorkspaceGraphNode, t: TFunction<"graph">): string {
  const value = node.grain === "cluster" ? node.cluster_kind : node.entity_kind;
  return t(`workspaceGraph.chips.${value}`, { defaultValue: workspaceGraphEnumLabel(value, t) });
}

function workspaceGraphRelationChipLabel(kind: string, t: TFunction<"graph">): string {
  return t(`workspaceGraph.chips.${kind}`, { defaultValue: workspaceGraphEnumLabel(kind, t) });
}

/** Every name a relation kind is shown with, for the kind-name search. */
export function workspaceGraphRelationNames(kind: string, t: TFunction<"graph">): string {
  return `${workspaceGraphEnumLabel(kind, t)} ${workspaceGraphRelationChipLabel(kind, t)}`;
}

function strokeLabel(t: TFunction<"graph">) {
  return (stroke: RelationListStroke) => t(`relationList.lines.${stroke}`);
}

function workspaceNodeView(node: WorkspaceGraphNode, t: TFunction<"graph">): RelationListNodeView {
  const kind = workspaceGraphNodeCueKind(node);
  return {
    chip: {
      cue: <WorkspaceGraphNodeCue kind={kind} />,
      label: workspaceGraphNodeChipLabel(node, t),
      title: workspaceGraphEnumLabel(node.grain === "cluster" ? node.cluster_kind : node.entity_kind, t),
      tone: kind,
    },
    cue: <WorkspaceGraphNodeCue kind={kind} />,
    href: node.grain === "document" ? node.path : null,
    id: node.id,
    label: node.label,
    meta: node.grain === "document" ? node.primary_folder_label : null,
  };
}

export function workspaceGraphRelationName(
  edge: WorkspaceGraphEdge,
  labels: ReadonlyMap<string, string>,
  t: TFunction<"graph">,
): string {
  return t(edge.direction === "directed" ? "relationList.relationName" : "relationList.relationNameUndirected", {
    kind: workspaceGraphEnumLabel(edge.relation_kind, t),
    source: labels.get(edge.source) ?? "—",
    target: labels.get(edge.target) ?? "—",
  });
}

function workspaceEdgeView(edge: WorkspaceGraphEdge, labels: ReadonlyMap<string, string>, t: TFunction<"graph">): RelationListEdgeView {
  const stroke: RelationListStroke = workspaceRelationCue(edge.relation_kind);
  return {
    chip: {
      cue: <RelationListStrokeCue stroke={stroke} />,
      label: workspaceGraphRelationChipLabel(edge.relation_kind, t),
      title: workspaceGraphEnumLabel(edge.relation_kind, t),
      tone: stroke,
    },
    directed: edge.direction === "directed",
    id: edge.id,
    name: workspaceGraphRelationName(edge, labels, t),
    source: edge.source,
    stroke,
    target: edge.target,
  };
}

export function WorkspaceGraphNodeChip({ node }: { node: WorkspaceGraphNode }) {
  const { t } = useTranslation("graph");
  return <RelationListChip chip={workspaceNodeView(node, t).chip} />;
}

/** Document icon + arrow: a link, not a button. The row itself still selects on the map. */
export function WorkspaceGraphOpenLink({ node, onNavigate }: {
  node: WorkspaceGraphNode;
  onNavigate: WorkspaceGraphDocumentNavigation;
}) {
  if (node.grain !== "document") return null;
  return <RelationListOpenLink href={node.path} label={node.label} onNavigate={onNavigate} />;
}

export function WorkspaceGraphExpandClusterButton({ busy, expanding, node, onExpand }: {
  busy: boolean;
  expanding: boolean;
  node: WorkspaceGraphNode;
  onExpand: () => void;
}) {
  const { t } = useTranslation("graph");
  const label = expanding ? t("workspaceGraph.list.expanding")
    : t("workspaceGraph.list.expandCluster", { label: node.label });
  return <button aria-label={label} className="relation-list-open-link workspace-graph-list-expand" disabled={busy}
    onClick={onExpand} title={label} type="button">
    <ListPlus aria-hidden="true" size={15} />
  </button>;
}

export const WorkspaceGraphFoldHeader = RelationListFoldHeader;
export const WorkspaceGraphMoreButton = RelationListMoreButton;

export function WorkspaceGraphNodeSummary({ nodes }: { nodes: readonly WorkspaceGraphNode[] }) {
  const { t } = useTranslation("graph");
  // A 10K scene re-renders often (camera, focus); count once per list.
  const items = useMemo(() => {
    const counts = new Map<WorkspaceGraphNodeCueKind, number>();
    for (const node of nodes) {
      const kind = workspaceGraphNodeCueKind(node);
      counts.set(kind, (counts.get(kind) ?? 0) + 1);
    }
    return (["context", "library", "agent", "cluster"] as const).filter((kind) => counts.has(kind)).map((kind) => ({
      count: counts.get(kind)!, cue: <WorkspaceGraphNodeCue kind={kind} />, key: kind, label: t(`workspaceGraph.chips.${kind}`),
    }));
  }, [nodes, t]);
  return <RelationListSummary items={items} />;
}

/** One entry per map line style: the map draws every citation kind with the same dashed stroke. */
export function WorkspaceGraphLineSummary({ edges }: { edges: readonly WorkspaceGraphEdge[] }) {
  const { t } = useTranslation("graph");
  const items = useMemo(() => relationListStrokeSummary(edges,
    (edge) => workspaceRelationCue(edge.relation_kind), STROKE_ORDER, strokeLabel(t)), [edges, t]);
  return <RelationListSummary items={items} />;
}

export function WorkspaceGraphRelationMiniMap({ edge, nodesById }: {
  edge: WorkspaceGraphEdge;
  nodesById: ReadonlyMap<string, WorkspaceGraphNode>;
}) {
  const { t } = useTranslation("graph");
  const view = (id: string) => {
    const node = nodesById.get(id);
    return node ? workspaceNodeView(node, t) : undefined;
  };
  return <RelationListMiniMap edge={workspaceEdgeView(edge, new Map(), t)} source={view(edge.source)} target={view(edge.target)} />;
}

/**
 * The selected node's relations at the bottom of Details: incoming relations
 * flow down a rail into this node, outgoing relations leave it below. The
 * search box filters both directions by the other end's name or folder and
 * by the relation kind.
 */
export function WorkspaceGraphRelationFlow({ labels, node, nodesById, onBlur, onFocus, onNavigate, onSelect, relations }: {
  labels: ReadonlyMap<string, string>;
  node: WorkspaceGraphNode;
  nodesById: ReadonlyMap<string, WorkspaceGraphNode>;
  onBlur: (edgeId: string) => void;
  onFocus: (edgeId: string) => void;
  onNavigate: WorkspaceGraphDocumentNavigation;
  onSelect: (nodeId: string) => void;
  relations: WorkspaceGraphNodeRelations;
}) {
  const { t } = useTranslation("graph");
  const self = node.grain === "cluster" ? "Cluster" : "Document";
  const nodeView = useCallback((id: string) => {
    const item = nodesById.get(id);
    return item ? workspaceNodeView(item, t) : undefined;
  }, [nodesById, t]);
  const edgeView = useCallback((edge: WorkspaceGraphEdge) => workspaceEdgeView(edge, labels, t), [labels, t]);
  const searchText = useCallback((edge: WorkspaceGraphEdge, otherId: string) => {
    const other = nodesById.get(otherId);
    return [
      other?.label,
      other?.grain === "document" ? other.primary_folder_label : null,
      workspaceGraphRelationNames(edge.relation_kind, t),
    ].filter(Boolean).join(" ");
  }, [nodesById, t]);
  const flowRelations = useMemo(() => ({ ...relations, undirected: [] }), [relations]);
  return <RelationFlow
    edgeView={edgeView}
    node={workspaceNodeView(node, t)}
    nodeTone={workspaceGraphNodeCueKind(node)}
    nodeView={nodeView}
    onNavigate={onNavigate as (event: ReactMouseEvent<HTMLAnchorElement>) => void}
    onRowBlur={(edgeId) => onBlur(edgeId)}
    onRowFocus={(edgeId) => onFocus(edgeId)}
    onSelect={(otherId) => onSelect(otherId)}
    page={RELATION_FLOW_PAGE}
    relations={flowRelations}
    searchText={searchText}
    strokeLabel={strokeLabel(t)}
    texts={{
      empty: t("workspaceGraph.relations.none"),
      incomingNote: t(`workspaceGraph.relations.incomingNote${self}`),
      outgoingNote: t(`workspaceGraph.relations.outgoingNote${self}`),
      searchLabel: t(`workspaceGraph.relations.search${self}`),
      self: t(`workspaceGraph.relations.self${self}`),
      title: t("workspaceGraph.relations.title"),
    }}
  />;
}

/** A selected relation in Details: the same top-to-bottom reading as the result rows. */
export function WorkspaceGraphRelationEnds({ edge, nodesById, onNavigate }: {
  edge: WorkspaceGraphEdge;
  nodesById: ReadonlyMap<string, WorkspaceGraphNode>;
  onNavigate: WorkspaceGraphDocumentNavigation;
}) {
  const { t } = useTranslation("graph");
  const visual = workspaceRelationCue(edge.relation_kind);
  const roles = visual === "citation" ? "Citation" : visual === "link" ? "Link" : "";
  const role = (end: "source" | "target") => t(`workspaceGraph.details.${end}Role${roles}`, {
    defaultValue: t(`workspaceGraph.details.${end}`),
  });
  return <RelationEnds
    edge={workspaceEdgeView(edge, new Map(), t)}
    nodeView={(id) => {
      const node = nodesById.get(id);
      return node ? workspaceNodeView(node, t) : undefined;
    }}
    onNavigate={onNavigate}
    roles={{ source: role("source"), target: role("target") }}
  />;
}
