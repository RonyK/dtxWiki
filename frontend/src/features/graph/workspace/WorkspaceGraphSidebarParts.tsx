import { ArrowUpRight, ChevronDown, ChevronRight, FileText, ListPlus, Search, X } from "lucide-react";
import type { TFunction } from "i18next";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { WorkspaceGraphEdge, WorkspaceGraphNode } from "../../../shared/api/workspaceGraph";
import { workspaceRelationVisual, type WorkspaceRelationVisual } from "../map/sceneColumns";
import type { WorkspaceGraphNodeRelations } from "../workspaceGraphModel";
import type { WorkspaceGraphDocumentNavigation } from "./WorkspaceGraphNodeActions";
import { formatCount, workspaceGraphEnumLabel } from "./workspaceGraphLabels";

/** Same shape and colour as the map point and the legend cue. */
export type WorkspaceGraphNodeCueKind = "context" | "library" | "agent" | "cluster";

export function workspaceGraphNodeCueKind(node: WorkspaceGraphNode): WorkspaceGraphNodeCueKind {
  return node.grain === "cluster" ? "cluster" : node.entity_kind;
}

/** Results and the selected node's relations grow by this many rows. */
export const WORKSPACE_GRAPH_RESULT_PAGE = 10;
export const WORKSPACE_GRAPH_RELATION_FLOW_PAGE = 5;

export function WorkspaceGraphNodeCue({ kind }: { kind: WorkspaceGraphNodeCueKind }) {
  return <span aria-hidden="true" className={`workspace-graph-node-cue is-${kind} is-inline`} />;
}

/** A short stroke in the map's line style. */
export function WorkspaceGraphEdgeCue({ visual }: { visual: WorkspaceRelationVisual | null }) {
  return <span aria-hidden="true" className="workspace-graph-edge-cue" data-visual={visual ?? "other"} />;
}

/** Short chip text; the full kind name stays in the tooltip and accessible name. */
export function workspaceGraphNodeChipLabel(node: WorkspaceGraphNode, t: TFunction<"graph">): string {
  const value = node.grain === "cluster" ? node.cluster_kind : node.entity_kind;
  return t(`workspaceGraph.chips.${value}`, { defaultValue: workspaceGraphEnumLabel(value, t) });
}

export function workspaceGraphRelationChipLabel(kind: string, t: TFunction<"graph">): string {
  return t(`workspaceGraph.chips.${kind}`, { defaultValue: workspaceGraphEnumLabel(kind, t) });
}

/** Every name a relation kind is shown with, for the kind-name search. */
export function workspaceGraphRelationNames(kind: string, t: TFunction<"graph">): string {
  return `${workspaceGraphEnumLabel(kind, t)} ${workspaceGraphRelationChipLabel(kind, t)}`;
}

export function WorkspaceGraphNodeChip({ node }: { node: WorkspaceGraphNode }) {
  const { t } = useTranslation("graph");
  const kind = workspaceGraphNodeCueKind(node);
  const full = workspaceGraphEnumLabel(node.grain === "cluster" ? node.cluster_kind : node.entity_kind, t);
  return <span className="workspace-graph-chip" data-node-kind={kind} title={full}>
    <WorkspaceGraphNodeCue kind={kind} />{workspaceGraphNodeChipLabel(node, t)}
  </span>;
}

export function WorkspaceGraphRelationChip({ kind }: { kind: string }) {
  const { t } = useTranslation("graph");
  const visual = workspaceRelationVisual(kind);
  return <span className="workspace-graph-chip" data-visual={visual ?? "other"} title={workspaceGraphEnumLabel(kind, t)}>
    <WorkspaceGraphEdgeCue visual={visual} />{workspaceGraphRelationChipLabel(kind, t)}
  </span>;
}

/** Document icon + arrow: a link, not a button. The row itself still selects on the map. */
export function WorkspaceGraphOpenLink({ node, onNavigate }: {
  node: WorkspaceGraphNode;
  onNavigate: WorkspaceGraphDocumentNavigation;
}) {
  const { t } = useTranslation("graph");
  if (node.grain !== "document") return null;
  const label = t("workspaceGraph.list.openDocument", { label: node.label });
  return <Link aria-label={label} className="workspace-graph-open-link" onClick={onNavigate} title={label} to={node.path}>
    <FileText aria-hidden="true" size={15} /><ArrowUpRight aria-hidden="true" className="workspace-graph-open-arrow" size={11} strokeWidth={2.6} />
  </Link>;
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
  return <button aria-label={label} className="workspace-graph-open-link workspace-graph-list-expand" disabled={busy}
    onClick={onExpand} title={label} type="button">
    <ListPlus aria-hidden="true" size={15} />
  </button>;
}

/** Folded headers keep the count and the composition visible. */
export function WorkspaceGraphFoldHeader({ controls, count, level = "section", note, onToggle, open, summary, title }: {
  controls: string;
  count: number;
  level?: "section" | "group";
  note?: string;
  onToggle: () => void;
  open: boolean;
  summary?: ReactNode;
  title: string;
}) {
  const Chevron = open ? ChevronDown : ChevronRight;
  return <button aria-controls={controls} aria-expanded={open} className="workspace-graph-fold-head"
    data-level={level} onClick={onToggle} type="button">
    <Chevron aria-hidden="true" className="workspace-graph-fold-chevron" size={15} />
    <span className="workspace-graph-fold-title">{title}</span>
    <span className="workspace-graph-fold-count">{formatCount(count)}</span>
    {note ? <span className="workspace-graph-fold-note">{note}</span> : null}
    {summary ? <span className="workspace-graph-fold-summary">{summary}</span> : null}
  </button>;
}

export function WorkspaceGraphNodeSummary({ nodes }: { nodes: readonly WorkspaceGraphNode[] }) {
  const { t } = useTranslation("graph");
  // A 10K scene re-renders often (camera, focus); count once per list.
  const counts = useMemo(() => {
    const result = new Map<WorkspaceGraphNodeCueKind, number>();
    for (const node of nodes) {
      const kind = workspaceGraphNodeCueKind(node);
      result.set(kind, (result.get(kind) ?? 0) + 1);
    }
    return result;
  }, [nodes]);
  return <>{(["context", "library", "agent", "cluster"] as const).filter((kind) => counts.has(kind)).map((kind) => (
    <span className="workspace-graph-fold-summary-item" key={kind}>
      <WorkspaceGraphNodeCue kind={kind} />
      <span className="visually-hidden">{t(`workspaceGraph.chips.${kind}`)}</span>{formatCount(counts.get(kind)!)}
    </span>
  ))}</>;
}

/** One entry per map line style: the map draws every citation kind with the same dashed stroke. */
export function WorkspaceGraphLineSummary({ edges }: { edges: readonly WorkspaceGraphEdge[] }) {
  const { t } = useTranslation("graph");
  const counts = useMemo(() => {
    const result = new Map<WorkspaceRelationVisual | "other", number>();
    for (const edge of edges) {
      const visual = workspaceRelationVisual(edge.relation_kind) ?? "other";
      result.set(visual, (result.get(visual) ?? 0) + 1);
    }
    return result;
  }, [edges]);
  return <>{(["link", "citation", "other"] as const).filter((visual) => counts.has(visual)).map((visual) => (
    <span className="workspace-graph-fold-summary-item" key={visual}>
      <WorkspaceGraphEdgeCue visual={visual === "other" ? null : visual} />
      <span className="visually-hidden">{t(`workspaceGraph.chips.line.${visual}`)}</span>{formatCount(counts.get(visual)!)}
    </span>
  ))}</>;
}

export function WorkspaceGraphMoreButton({ onClick, remaining, step }: {
  onClick: () => void;
  remaining: number;
  step: number;
}) {
  const { t } = useTranslation("graph");
  if (remaining <= 0) return null;
  return <button className="workspace-graph-more" onClick={onClick} type="button">
    {t("workspaceGraph.list.more", { count: Math.min(step, remaining), remaining: formatCount(remaining) })}
  </button>;
}

export function workspaceGraphRelationName(
  edge: WorkspaceGraphEdge,
  labels: ReadonlyMap<string, string>,
  t: TFunction<"graph">,
): string {
  return t(edge.direction === "directed" ? "workspaceGraph.list.relationName" : "workspaceGraph.list.relationNameUndirected", {
    kind: workspaceGraphEnumLabel(edge.relation_kind, t),
    source: labels.get(edge.source) ?? "—",
    target: labels.get(edge.target) ?? "—",
  });
}

/** A relation drawn like the map: source on top, the map's stroke and arrow, target below. */
export function WorkspaceGraphRelationMiniMap({ edge, nodesById }: {
  edge: WorkspaceGraphEdge;
  nodesById: ReadonlyMap<string, WorkspaceGraphNode>;
}) {
  const source = nodesById.get(edge.source);
  const target = nodesById.get(edge.target);
  const visual = workspaceRelationVisual(edge.relation_kind);
  return <span className="workspace-graph-relation-map">
    <span className="workspace-graph-relation-cue">{source ? <WorkspaceGraphNodeCue kind={workspaceGraphNodeCueKind(source)} /> : null}</span>
    <strong className="workspace-graph-relation-end">{source?.label ?? "—"}</strong>
    <span aria-hidden="true" className="workspace-graph-relation-stroke" data-directed={edge.direction === "directed"}
      data-visual={visual ?? "other"} />
    <span className="workspace-graph-relation-kind"><WorkspaceGraphRelationChip kind={edge.relation_kind} /></span>
    <span className="workspace-graph-relation-cue">{target ? <WorkspaceGraphNodeCue kind={workspaceGraphNodeCueKind(target)} /> : null}</span>
    <strong className="workspace-graph-relation-end">{target?.label ?? "—"}</strong>
  </span>;
}

type RelationDirection = keyof WorkspaceGraphNodeRelations;
type FlowStroke = WorkspaceRelationVisual | "other";

const flowStroke = (edge: WorkspaceGraphEdge): FlowStroke => workspaceRelationVisual(edge.relation_kind) ?? "other";
/** Where a branch meets the rail: incoming relations merge below the name line, outgoing ones split above it. */
const FLOW_JOINT: Record<RelationDirection, number> = { incoming: 24, outgoing: 8 };

function FlowRailLine({ from, segment, stroke, to }: {
  from: number | string;
  segment: "above" | "below";
  stroke: FlowStroke;
  to: number | string;
}) {
  return <line className="workspace-graph-flow-stroke" data-segment={segment} data-visual={stroke}
    x1="14" x2="14" y1={from} y2={to} />;
}

/**
 * The rail beside one relation row: its curved branch and the rail stretches
 * above and below the joint. Every stretch belongs to the relation it exists
 * for, so it takes that relation's colour and line type (see the flow).
 */
function FlowRowRail({ above, below, branch, directed, direction }: {
  above: FlowStroke | null;
  below: FlowStroke | null;
  branch: FlowStroke;
  directed: boolean;
  direction: RelationDirection;
}) {
  const joint = FLOW_JOINT[direction];
  return <svg aria-hidden="true" className="workspace-graph-flow-rail" width="34">
    {above ? <FlowRailLine from={0} segment="above" stroke={above} to={joint} /> : null}
    {below ? <FlowRailLine from={joint} segment="below" stroke={below} to="100%" /> : null}
    <path className="workspace-graph-flow-stroke" data-segment="branch" data-visual={branch}
      d={direction === "incoming" ? "M31 16 H22 A8 8 0 0 0 14 24" : `M14 8 A8 8 0 0 0 22 16 H${directed ? 26 : 31}`} />
    {direction === "outgoing" && directed
      ? <path className="workspace-graph-flow-arrow" data-visual={branch} d="M25.5 11.5 L31.5 16 L25.5 20.5 Z" /> : null}
  </svg>;
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
  const baseId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Record<RelationDirection, boolean>>({ incoming: true, outgoing: true });
  const [limits, setLimits] = useState<Record<RelationDirection, number>>({
    incoming: WORKSPACE_GRAPH_RELATION_FLOW_PAGE,
    outgoing: WORKSPACE_GRAPH_RELATION_FLOW_PAGE,
  });
  const self = node.grain === "cluster" ? "Cluster" : "Document";
  const pageLimits = { incoming: WORKSPACE_GRAPH_RELATION_FLOW_PAGE, outgoing: WORKSPACE_GRAPH_RELATION_FLOW_PAGE };
  const changeQuery = (value: string) => {
    setQuery(value);
    setLimits(pageLimits);
  };
  const normalized = query.trim().toLocaleLowerCase();
  const filtered = useMemo(() => {
    if (!normalized) return relations;
    const matches = (edge: WorkspaceGraphEdge, otherId: string) => {
      const other = nodesById.get(otherId);
      return [
        other?.label,
        other?.grain === "document" ? other.primary_folder_label : null,
        workspaceGraphRelationNames(edge.relation_kind, t),
      ].filter(Boolean).join(" ").toLocaleLowerCase().includes(normalized);
    };
    return {
      incoming: relations.incoming.filter((edge) => matches(edge, edge.source)),
      outgoing: relations.outgoing.filter((edge) => matches(edge, edge.target)),
    };
  }, [nodesById, normalized, relations, t]);
  const allRelations = useMemo(() => [...relations.incoming, ...relations.outgoing], [relations]);
  const total = allRelations.length;
  const shownTotal = filtered.incoming.length + filtered.outgoing.length;
  const searchable = total > 0;
  // The Map listens for Escape on its region (native bubbling, before React's
  // root handler); a non-empty relation search is cleared first instead.
  useEffect(() => {
    const input = inputRef.current;
    if (!searchable || !input) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !input.value) return;
      event.preventDefault();
      event.stopPropagation();
      setQuery("");
      setLimits({ incoming: WORKSPACE_GRAPH_RELATION_FLOW_PAGE, outgoing: WORKSPACE_GRAPH_RELATION_FLOW_PAGE });
    };
    input.addEventListener("keydown", onKeyDown);
    return () => input.removeEventListener("keydown", onKeyDown);
  }, [searchable]);

  if (!total) {
    return <section aria-labelledby={`${baseId}-title`} className="workspace-graph-relations">
      <h3 id={`${baseId}-title`}>{t("workspaceGraph.relations.title")}</h3>
      <p className="workspace-graph-relations-empty">{t("workspaceGraph.relations.none")}</p>
    </section>;
  }

  const shown = {
    incoming: open.incoming ? filtered.incoming.slice(0, limits.incoming) : [],
    outgoing: open.outgoing ? filtered.outgoing.slice(0, limits.outgoing) : [],
  };
  // A rail stretch belongs to the relation that makes it exist. Above the node:
  // from a relation's merge down to the next merge (the last one reaches the
  // node). Below it: from the previous split, or the node, down to the
  // relation's own split; nothing is drawn past the last split.
  const intoNode = shown.incoming.length ? flowStroke(shown.incoming[shown.incoming.length - 1]) : null;
  const outOfNode = shown.outgoing.length ? flowStroke(shown.outgoing[0]) : null;
  const row = (edge: WorkspaceGraphEdge, direction: RelationDirection, index: number, visible: readonly WorkspaceGraphEdge[]) => {
    const otherId = direction === "incoming" ? edge.source : edge.target;
    const other = nodesById.get(otherId);
    const next = visible[index + 1];
    const rail = direction === "incoming"
      ? { above: index > 0 ? flowStroke(visible[index - 1]) : null, below: flowStroke(edge) }
      : { above: flowStroke(edge), below: next ? flowStroke(next) : null };
    return <li className="workspace-graph-flow-row" data-direction={direction} data-graph-edge-id={edge.id} key={edge.id}>
      <FlowRowRail above={rail.above} below={rail.below} branch={flowStroke(edge)}
        directed={edge.direction === "directed"} direction={direction} />
      <button aria-label={workspaceGraphRelationName(edge, labels, t)} className="workspace-graph-list-select workspace-graph-flow-select"
        onClick={() => onSelect(otherId)} type="button">
        <span className="workspace-graph-flow-name">
          {other ? <WorkspaceGraphNodeCue kind={workspaceGraphNodeCueKind(other)} /> : null}
          <strong>{other?.label ?? "—"}</strong>
        </span>
        <span className="workspace-graph-row-meta">
          <WorkspaceGraphRelationChip kind={edge.relation_kind} />
          {other?.grain === "document" ? <span className="workspace-graph-row-meta-text">{other.primary_folder_label}</span> : null}
        </span>
      </button>
      {other ? <WorkspaceGraphOpenLink node={other} onNavigate={onNavigate} /> : null}
    </li>;
  };
  const group = (direction: RelationDirection) => {
    const list = filtered[direction];
    const visible = shown[direction];
    const listId = `${baseId}-${direction}`;
    const remaining = open[direction] ? list.length - visible.length : 0;
    return {
      remaining,
      head: <WorkspaceGraphFoldHeader controls={listId} count={list.length} level="group"
        note={t(`workspaceGraph.relations.${direction}Note${self}`)}
        onToggle={() => setOpen((current) => ({ ...current, [direction]: !current[direction] }))}
        open={open[direction]} title={t(`workspaceGraph.relations.${direction}`)} />,
      list: open[direction] ? <ul aria-label={t(`workspaceGraph.relations.${direction}`)} className="workspace-graph-flow-list"
        data-relation-direction={direction} data-shown-count={visible.length} data-total-count={list.length} id={listId}>
        {visible.map((edge, index) => row(edge, direction, index, visible))}
      </ul> : null,
      more: <WorkspaceGraphMoreButton onClick={() => setLimits((current) => ({
        ...current, [direction]: current[direction] + WORKSPACE_GRAPH_RELATION_FLOW_PAGE,
      }))} remaining={remaining} step={WORKSPACE_GRAPH_RELATION_FLOW_PAGE} />,
    };
  };
  const incoming = group("incoming");
  const outgoing = group("outgoing");
  return <section aria-labelledby={`${baseId}-title`} className="workspace-graph-relations"
    onBlurCapture={(event) => {
      const id = event.target.closest<HTMLElement>("[data-graph-edge-id]")?.dataset.graphEdgeId;
      if (event.relatedTarget instanceof Node && event.target.closest("li")?.contains(event.relatedTarget)) return;
      if (id) onBlur(id);
    }}
    onFocusCapture={(event) => {
      const id = event.target.closest<HTMLElement>("[data-graph-edge-id]")?.dataset.graphEdgeId;
      if (id) onFocus(id);
    }}>
    <div className="workspace-graph-relations-head">
      <h3 id={`${baseId}-title`}>{t("workspaceGraph.relations.title")}</h3>
      <span className="workspace-graph-fold-count">{formatCount(total)}</span>
      <span className="workspace-graph-fold-summary"><WorkspaceGraphLineSummary edges={allRelations} /></span>
    </div>
    <div className="workspace-graph-relations-search">
      <Search aria-hidden="true" size={15} />
      <input aria-label={t(`workspaceGraph.relations.search${self}`)}
        onChange={(event) => changeQuery(event.currentTarget.value)}
        placeholder={t("workspaceGraph.relations.searchPlaceholder")} ref={inputRef} type="search" value={query} />
      {query ? <button aria-label={t("workspaceGraph.relations.clearSearch")} className="workspace-graph-relations-clear"
        onClick={() => { changeQuery(""); inputRef.current?.focus(); }} type="button"><X aria-hidden="true" size={14} /></button> : null}
    </div>
    {normalized ? <p className="workspace-graph-relations-status" role="status">
      {shownTotal ? t("workspaceGraph.relations.searchStatus", { shown: formatCount(shownTotal), total: formatCount(total) })
        : t("workspaceGraph.relations.noMatch", { query: query.trim() })}
    </p> : null}
    <div className="workspace-graph-relation-flow" data-node-kind={workspaceGraphNodeCueKind(node)}>
      <div className="workspace-graph-flow-head">{incoming.head}</div>
      {incoming.list}
      {incoming.remaining > 0 ? <div className="workspace-graph-flow-more">
        {intoNode ? <svg aria-hidden="true" className="workspace-graph-flow-rail" width="34">
          <FlowRailLine from={0} segment="below" stroke={intoNode} to="100%" />
        </svg> : null}
        {incoming.more}
      </div> : null}
      <div className="workspace-graph-flow-hub">
        <svg aria-hidden="true" className="workspace-graph-flow-rail" width="34">
          {intoNode ? <>
            <FlowRailLine from={0} segment="above" stroke={intoNode} to={2} />
            <path className="workspace-graph-flow-arrow" data-visual={intoNode} d="M9 1 L19 1 L14 8 Z" />
          </> : null}
          {outOfNode ? <FlowRailLine from="50%" segment="below" stroke={outOfNode} to="100%" /> : null}
        </svg>
        <span className="workspace-graph-flow-hub-pill">
          <WorkspaceGraphNodeCue kind={workspaceGraphNodeCueKind(node)} />
          <strong title={node.label}>{node.label}</strong>
          <span className="workspace-graph-flow-self">{t(`workspaceGraph.relations.self${self}`)}</span>
        </span>
      </div>
      <div className="workspace-graph-flow-head">
        {outOfNode ? <>
          <svg aria-hidden="true" className="workspace-graph-flow-rail" width="34">
            <FlowRailLine from={0} segment="below" stroke={outOfNode} to="100%" />
          </svg>
          <svg aria-hidden="true" className="workspace-graph-flow-rail-arrow" height="8" width="34">
            <path className="workspace-graph-flow-arrow" data-visual={outOfNode} d="M9 0 L19 0 L14 7 Z" />
          </svg>
        </> : null}
        {outgoing.head}
      </div>
      {outgoing.list}
      {outgoing.remaining > 0 ? <div className="workspace-graph-flow-more">{outgoing.more}</div> : null}
    </div>
  </section>;
}

/** A selected relation in Details: the same top-to-bottom reading as the result rows. */
export function WorkspaceGraphRelationEnds({ edge, nodesById, onNavigate }: {
  edge: WorkspaceGraphEdge;
  nodesById: ReadonlyMap<string, WorkspaceGraphNode>;
  onNavigate: WorkspaceGraphDocumentNavigation;
}) {
  const { t } = useTranslation("graph");
  const visual = workspaceRelationVisual(edge.relation_kind);
  const roles = visual === "citation" ? "Citation" : visual === "link" ? "Link" : "";
  const end = (id: string, role: "source" | "target") => {
    const node = nodesById.get(id);
    return <div className="workspace-graph-relation-end-row">
      <span className="workspace-graph-relation-role">{t(`workspaceGraph.details.${role}Role${roles}`, {
        defaultValue: t(`workspaceGraph.details.${role}`),
      })}</span>
      <span className="workspace-graph-flow-name">
        {node ? <WorkspaceGraphNodeCue kind={workspaceGraphNodeCueKind(node)} /> : null}
        <strong>{node?.label ?? "—"}</strong>
      </span>
      {node ? <span className="workspace-graph-row-meta"><WorkspaceGraphNodeChip node={node} />
        {node.grain === "document" ? <span className="workspace-graph-row-meta-text">{node.primary_folder_label}</span> : null}
      </span> : null}
      {node ? <WorkspaceGraphOpenLink node={node} onNavigate={onNavigate} /> : null}
    </div>;
  };
  return <div className="workspace-graph-relation-ends">
    {end(edge.source, "source")}
    <div className="workspace-graph-relation-ends-stroke">
      <span aria-hidden="true" className="workspace-graph-relation-stroke" data-directed={edge.direction === "directed"}
        data-visual={visual ?? "other"} />
      <WorkspaceGraphRelationChip kind={edge.relation_kind} />
    </div>
    {end(edge.target, "target")}
  </div>;
}
