import { Search, X } from "lucide-react";
import { createElement, useEffect, useId, useMemo, useRef, useState, type FocusEvent, type MouseEvent as ReactMouseEvent } from "react";
import { useTranslation } from "react-i18next";

import {
  RELATION_FLOW_PAGE,
  RelationListChip,
  RelationListFoldHeader,
  RelationListMoreButton,
  RelationListOpenLink,
  RelationListSummary,
  formatRelationListCount,
  relationListStrokeSummary,
} from "./parts";
import type {
  RelationListDirection,
  RelationListEdgeView,
  RelationListNodeView,
  RelationListRelations,
  RelationListStroke,
} from "./types";
import { useRovingList, type RovingSelectionOrigin } from "./useRovingList";

const DIRECTIONS: readonly RelationListDirection[] = ["incoming", "outgoing", "undirected"];
const STROKE_ORDER: readonly RelationListStroke[] = ["link", "citation", "coAccess", "membership", "action", "other"];
/** Where a branch meets the rail: incoming relations merge below the name line, outgoing ones split above it. */
const FLOW_JOINT = { incoming: 24, outgoing: 8 } as const;

export type RelationFlowTexts = {
  empty: string;
  incomingNote: string;
  outgoingNote: string;
  searchLabel: string;
  searchPlaceholder?: string;
  self: string;
  title: string;
  undirectedNote?: string;
};

type RelationFlowProps<E> = {
  /** Keep the arrow-key model (one Tab entry per list) instead of one Tab stop per row. */
  active?: boolean;
  edgeView: (edge: E) => RelationListEdgeView;
  headingLevel?: 3 | 4 | 5;
  hideSearch?: boolean;
  /**
   * Rows shown per direction, owned by an enclosing list so another layout of
   * the same relations keeps its pages (with `onShowMore`). Without it the
   * flow pages on its own and resets with its own search.
   */
  limitOf?: (direction: RelationListDirection) => number;
  navigation?: "tab" | "roving";
  node: RelationListNodeView;
  /** Second chip with the other end's kind. */
  nodeChips?: boolean;
  nodeTone: string;
  nodeView: (id: string) => RelationListNodeView | undefined;
  onNavigate?: (event: ReactMouseEvent<HTMLAnchorElement>) => void;
  onRowBlur?: (edgeId: string, otherId: string) => void;
  onRowFocus?: (edgeId: string, otherId: string) => void;
  onSelect: (otherId: string, origin: RovingSelectionOrigin) => void;
  onShowMore?: (direction: RelationListDirection) => void;
  openLinkTabIndex?: number;
  page?: number;
  /** A query owned by an enclosing list; it replaces the flow's own search box. */
  query?: string;
  relations: RelationListRelations<E>;
  rowAttributes?: (otherId: string) => Record<string, string>;
  scopeKey?: string;
  searchText: (edge: E, otherId: string) => string;
  /** The node pill selects the node itself (a list view, not the node's own Details). */
  selectableHub?: boolean;
  selectedId?: string | null;
  strokeLabel: (stroke: RelationListStroke) => string;
  texts: RelationFlowTexts;
};

function FlowRailLine({ from, segment, stroke, to }: {
  from: number | string;
  segment: "above" | "below";
  stroke: RelationListStroke;
  to: number | string;
}) {
  return <line className="relation-list-flow-stroke" data-segment={segment} data-visual={stroke}
    x1="14" x2="14" y1={from} y2={to} />;
}

/**
 * The rail beside one relation row: its curved branch and the rail stretches
 * above and below the joint. Every stretch belongs to the relation it exists
 * for, so it takes that relation's colour and line type.
 */
function FlowRowRail({ above, below, branch, directed, direction }: {
  above: RelationListStroke | null;
  below: RelationListStroke | null;
  branch: RelationListStroke;
  directed: boolean;
  direction: "incoming" | "outgoing";
}) {
  const joint = FLOW_JOINT[direction];
  return <svg aria-hidden="true" className="relation-list-flow-rail" width="34">
    {above ? <FlowRailLine from={0} segment="above" stroke={above} to={joint} /> : null}
    {below ? <FlowRailLine from={joint} segment="below" stroke={below} to="100%" /> : null}
    <path className="relation-list-flow-stroke" data-segment="branch" data-visual={branch}
      d={direction === "incoming" ? "M31 16 H22 A8 8 0 0 0 14 24" : `M14 8 A8 8 0 0 0 22 16 H${directed ? 26 : 31}`} />
    {direction === "outgoing" && directed
      ? <path className="relation-list-flow-arrow" data-visual={branch} d="M25.5 11.5 L31.5 16 L25.5 20.5 Z" /> : null}
  </svg>;
}

/**
 * A node's relations: incoming relations flow down a rail into the node,
 * outgoing relations leave it below, relations without a direction follow.
 * The search filters every direction by the other end and the relation kind.
 */
export function RelationFlow<E>({
  active = true,
  edgeView,
  headingLevel = 3,
  hideSearch = false,
  limitOf,
  navigation = "tab",
  node,
  nodeChips = false,
  nodeTone,
  nodeView,
  onNavigate,
  onRowBlur,
  onRowFocus,
  onSelect,
  onShowMore,
  openLinkTabIndex,
  page = RELATION_FLOW_PAGE,
  query: externalQuery,
  relations,
  rowAttributes,
  scopeKey = "",
  searchText,
  selectableHub = false,
  selectedId,
  strokeLabel,
  texts,
}: RelationFlowProps<E>) {
  const { t } = useTranslation("graph");
  const baseId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [ownQuery, setOwnQuery] = useState("");
  const [open, setOpen] = useState<Record<RelationListDirection, boolean>>({ incoming: true, outgoing: true, undirected: true });
  const pageLimits = { incoming: page, outgoing: page, undirected: page };
  const [ownLimits, setLimits] = useState<Record<RelationListDirection, number>>(pageLimits);
  const limitFor = (direction: RelationListDirection) => limitOf?.(direction) ?? ownLimits[direction];
  const showMore = (direction: RelationListDirection) => {
    if (onShowMore) onShowMore(direction);
    else setLimits((current) => ({ ...current, [direction]: current[direction] + page }));
  };
  const query = externalQuery ?? ownQuery;
  const changeQuery = (value: string) => {
    setOwnQuery(value);
    setLimits(pageLimits);
  };
  const normalized = query.trim().toLocaleLowerCase();
  const otherOf = (edge: E, direction: RelationListDirection) => {
    const view = edgeView(edge);
    return direction === "incoming" ? view.source : direction === "outgoing" ? view.target
      : view.source === node.id ? view.target : view.source;
  };
  const filtered = useMemo(() => {
    if (!normalized) return relations;
    const keep = (direction: RelationListDirection) => relations[direction].filter((edge) =>
      searchText(edge, otherOf(edge, direction)).toLocaleLowerCase().includes(normalized));
    return { incoming: keep("incoming"), outgoing: keep("outgoing"), undirected: keep("undirected") };
    // otherOf only reads edgeView and the node id, both covered below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edgeView, node.id, normalized, relations, searchText]);
  const allRelations = useMemo(() => [...relations.incoming, ...relations.outgoing, ...relations.undirected], [relations]);
  const total = allRelations.length;
  const shownTotal = filtered.incoming.length + filtered.outgoing.length + filtered.undirected.length;
  const searchable = total > 0 && !hideSearch;
  // The graph region listens for Escape (native bubbling, before React's
  // root handler); a non-empty relation search is cleared first instead.
  useEffect(() => {
    const input = inputRef.current;
    if (!searchable || !input) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !input.value) return;
      event.preventDefault();
      event.stopPropagation();
      setOwnQuery("");
      setLimits({ incoming: page, outgoing: page, undirected: page });
    };
    input.addEventListener("keydown", onKeyDown);
    return () => input.removeEventListener("keydown", onKeyDown);
  }, [page, searchable]);

  const shown = {
    incoming: open.incoming ? filtered.incoming.slice(0, limitFor("incoming")) : [],
    outgoing: open.outgoing ? filtered.outgoing.slice(0, limitFor("outgoing")) : [],
    undirected: open.undirected ? filtered.undirected.slice(0, limitFor("undirected")) : [],
  };
  const views = (direction: RelationListDirection) => shown[direction].map((edge) => ({ other: otherOf(edge, direction), view: edgeView(edge) }));
  const rows = { incoming: views("incoming"), outgoing: views("outgoing"), undirected: views("undirected") };
  const roving = navigation === "roving";
  const otherByRow = new Map(DIRECTIONS.flatMap((direction) => rows[direction].map((row) => [row.view.id, row.other] as const)));
  const rovingFor = (direction: RelationListDirection) => ({
    enabled: roving && active && open[direction],
    ids: rows[direction].map((row) => row.view.id),
    onBlur: (otherId: string, edgeId: string) => onRowBlur?.(edgeId, otherId),
    onFocus: (otherId: string, edgeId: string) => onRowFocus?.(edgeId, otherId),
    onSelect: (otherId: string, origin: RovingSelectionOrigin) => onSelect(otherId, origin),
    scopeKey: `${scopeKey}:${node.id}:${direction}`,
  });
  const rovingRows = {
    incoming: useRovingList(rovingFor("incoming")),
    outgoing: useRovingList(rovingFor("outgoing")),
    undirected: useRovingList(rovingFor("undirected")),
  };

  if (!total) {
    return <section aria-labelledby={`${baseId}-title`} className="relation-list-relations">
      {createElement(`h${headingLevel}`, { className: "relation-list-relations-title", id: `${baseId}-title` }, texts.title)}
      <p className="relation-list-relations-empty">{texts.empty}</p>
    </section>;
  }

  const strokeOf = (edge: E) => edgeView(edge).stroke;
  // A rail stretch belongs to the relation that makes it exist. Above the node:
  // from a relation's merge down to the next merge (the last one reaches the
  // node). Below it: from the previous split, or the node, down to the
  // relation's own split; nothing is drawn past the last split.
  const intoNode = shown.incoming.length ? strokeOf(shown.incoming[shown.incoming.length - 1]) : null;
  const outOfNode = shown.outgoing.length ? strokeOf(shown.outgoing[0]) : null;
  const row = (direction: RelationListDirection, index: number) => {
    const { other: otherId, view } = rows[direction][index];
    const other = nodeView(otherId);
    const visible = shown[direction];
    const next = visible[index + 1];
    const rail = direction === "incoming"
      ? { above: index > 0 ? strokeOf(visible[index - 1]) : null, below: view.stroke }
      : direction === "outgoing" ? { above: view.stroke, below: next ? strokeOf(next) : null } : null;
    const rowProps = roving ? rovingRows[direction](view.id, index, otherId) : {
      onClick: (event: ReactMouseEvent<HTMLButtonElement>) => onSelect(otherId, {
        control: event.currentTarget,
        modality: event.detail === 0 ? "keyboard" : "pointer",
      }),
    };
    return <li className="relation-list-flow-row" data-direction={direction} data-graph-edge-id={view.id} key={view.id}>
      {rail && direction !== "undirected" ? <FlowRowRail above={rail.above} below={rail.below} branch={view.stroke}
        directed={view.directed} direction={direction} /> : null}
      <button aria-label={view.name} aria-pressed={selectedId === undefined ? undefined : selectedId === otherId}
        className="relation-list-row-select relation-list-flow-select" type="button" {...rowAttributes?.(otherId)} {...rowProps}>
        <span className="relation-list-flow-name">
          {other?.cue ?? null}
          <strong>{other?.label ?? "—"}</strong>
          {other?.badge ?? null}
        </span>
        <span className="relation-list-row-meta">
          <RelationListChip chip={view.chip} />
          {nodeChips && other ? <RelationListChip chip={other.chip} /> : null}
          {other?.meta ? <span className="relation-list-row-meta-text">{other.meta}</span> : null}
        </span>
      </button>
      {other?.href ? <RelationListOpenLink href={other.href} label={other.label} onNavigate={onNavigate} tabIndex={openLinkTabIndex} /> : null}
    </li>;
  };
  const group = (direction: RelationListDirection) => {
    const list = filtered[direction];
    const listId = `${baseId}-${direction}`;
    const remaining = open[direction] ? list.length - shown[direction].length : 0;
    const title = t(`relationList.${direction}`);
    const note = direction === "incoming" ? texts.incomingNote : direction === "outgoing" ? texts.outgoingNote : texts.undirectedNote;
    return {
      remaining,
      head: <RelationListFoldHeader controls={listId} count={list.length} level="group" note={note}
        onToggle={() => setOpen((current) => ({ ...current, [direction]: !current[direction] }))}
        open={open[direction]} title={title} />,
      list: open[direction] ? <ul aria-label={title} className="relation-list-flow-list"
        data-relation-direction={direction} data-shown-count={shown[direction].length} data-total-count={list.length} id={listId}>
        {shown[direction].map((_, index) => row(direction, index))}
      </ul> : null,
      more: <RelationListMoreButton onClick={() => showMore(direction)} remaining={remaining} step={page} />,
    };
  };
  const incoming = group("incoming");
  const outgoing = group("outgoing");
  const undirected = group("undirected");
  const tabFocus = navigation === "tab" ? {
    onBlurCapture: (event: FocusEvent<HTMLElement>) => {
      const id = event.target.closest<HTMLElement>("[data-graph-edge-id]")?.dataset.graphEdgeId;
      if (event.relatedTarget instanceof Node && event.target.closest("li")?.contains(event.relatedTarget)) return;
      if (id) onRowBlur?.(id, otherByRow.get(id) ?? id);
    },
    onFocusCapture: (event: FocusEvent<HTMLElement>) => {
      const id = event.target.closest<HTMLElement>("[data-graph-edge-id]")?.dataset.graphEdgeId;
      if (id) onRowFocus?.(id, otherByRow.get(id) ?? id);
    },
  } : {};
  return <section aria-labelledby={`${baseId}-title`} className="relation-list-relations" data-embedded={hideSearch || undefined} {...tabFocus}>
    <div className="relation-list-relations-head">
      {createElement(`h${headingLevel}`, { className: "relation-list-relations-title", id: `${baseId}-title` }, texts.title)}
      <span className="relation-list-fold-count">{formatRelationListCount(total)}</span>
      <span className="relation-list-fold-summary">
        <RelationListSummary items={relationListStrokeSummary(allRelations, strokeOf, STROKE_ORDER, strokeLabel)} />
      </span>
    </div>
    {searchable ? <div className="relation-list-search">
      <Search aria-hidden="true" size={15} />
      <input aria-label={texts.searchLabel}
        onChange={(event) => changeQuery(event.currentTarget.value)}
        placeholder={texts.searchPlaceholder ?? t("relationList.searchPlaceholder")} ref={inputRef} type="search" value={ownQuery} />
      {ownQuery ? <button aria-label={t("relationList.clearSearch")} className="relation-list-search-clear"
        onClick={() => { changeQuery(""); inputRef.current?.focus(); }} type="button"><X aria-hidden="true" size={14} /></button> : null}
    </div> : null}
    {normalized && !hideSearch ? <p className="relation-list-relations-status" role="status">
      {shownTotal ? t("relationList.searchStatus", { shown: formatRelationListCount(shownTotal), total: formatRelationListCount(total) })
        : t("relationList.noMatch", { query: query.trim() })}
    </p> : null}
    <div className="relation-list-flow" data-tone={nodeTone}>
      {relations.incoming.length ? <>
        <div className="relation-list-flow-head">{incoming.head}</div>
        {incoming.list}
        {incoming.remaining > 0 ? <div className="relation-list-flow-more">
          {intoNode ? <svg aria-hidden="true" className="relation-list-flow-rail" width="34">
            <FlowRailLine from={0} segment="below" stroke={intoNode} to="100%" />
          </svg> : null}
          {incoming.more}
        </div> : null}
      </> : null}
      <div className="relation-list-flow-hub">
        <svg aria-hidden="true" className="relation-list-flow-rail" width="34">
          {intoNode ? <>
            <FlowRailLine from={0} segment="above" stroke={intoNode} to={2} />
            <path className="relation-list-flow-arrow" data-visual={intoNode} d="M9 1 L19 1 L14 8 Z" />
          </> : null}
          {outOfNode ? <FlowRailLine from="50%" segment="below" stroke={outOfNode} to="100%" /> : null}
        </svg>
        {selectableHub ? <button aria-pressed={selectedId === node.id} className="relation-list-flow-hub-pill"
          type="button" {...rowAttributes?.(node.id)} onClick={(event) => onSelect(node.id, {
            control: event.currentTarget,
            modality: event.detail === 0 ? "keyboard" : "pointer",
          })}>
          {node.cue}
          <strong title={node.label}>{node.label}</strong>
          {node.badge ?? null}
        </button> : <span className="relation-list-flow-hub-pill">
          {node.cue}
          <strong title={node.label}>{node.label}</strong>
          <span className="relation-list-flow-self">{texts.self}</span>
          {node.badge ?? null}
        </span>}
      </div>
      {relations.outgoing.length ? <>
        <div className="relation-list-flow-head">
          {outOfNode ? <>
            <svg aria-hidden="true" className="relation-list-flow-rail" width="34">
              <FlowRailLine from={0} segment="below" stroke={outOfNode} to="100%" />
            </svg>
            <svg aria-hidden="true" className="relation-list-flow-rail-arrow" height="8" width="34">
              <path className="relation-list-flow-arrow" data-visual={outOfNode} d="M9 0 L19 0 L14 7 Z" />
            </svg>
          </> : null}
          {outgoing.head}
        </div>
        {outgoing.list}
        {outgoing.remaining > 0 ? <div className="relation-list-flow-more">{outgoing.more}</div> : null}
      </> : null}
      {relations.undirected.length ? <div className="relation-list-flow-undirected">
        <div className="relation-list-flow-head">{undirected.head}</div>
        {undirected.list}
        {undirected.remaining > 0 ? <div className="relation-list-flow-more">{undirected.more}</div> : null}
      </div> : null}
    </div>
  </section>;
}
