import type { TFunction } from "i18next";
import { Search, X } from "lucide-react";
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { GraphEdge, GraphNode } from "../../../shared/api/graph";
import {
  RELATION_LIST_PAGE,
  RelationListChip,
  RelationListFoldHeader,
  RelationListMiniMap,
  RelationListMoreButton,
  RelationListOpenLink,
  RelationListSummary,
  formatRelationListCount,
  relationListStrokeSummary,
  type RelationListSummaryItem,
} from "../shared/relationList/parts";
import { RelationFlow } from "../shared/relationList/RelationFlow";
import type { RelationListDirection, RelationListEdgeView, RelationListNodeView } from "../shared/relationList/types";
import { useRovingList, type RovingSelectionOrigin } from "../shared/relationList/useRovingList";
import {
  localListMatches,
  localNodeRelations,
  localSceneRings,
  type LocalSceneAnalysis,
} from "./localGraphList";
import { LocalNodeCue } from "./LocalNodeCue";
import {
  LOCAL_STROKE_ORDER,
  localEdgeView,
  localNodeSearchText,
  localNodeView,
  localRelationSearchText,
  localStroke,
  localStrokeLabel,
  type LocalViewContext,
} from "./localRelationViews";
import { localNodeVisualKind } from "./nodeVisual";
import { useFocusRecovery } from "./useFocusRecovery";

/** Below this list width the seed's relations read top to bottom, like Details. */
const BOW_TIE_MIN_WIDTH = 640;
const NODE_KIND_ORDER = ["context", "library", "agent", "conversation", "folder"] as const;

/**
 * What the reader set in the list: search, open groups and pages. It lives
 * with the graph, so switching to the canvas and back, or between the
 * bow-tie and the flow, or stepping through history keeps it.
 */
export type LocalListMemory = {
  /** The list it belongs to (workspace and current document); another list starts fresh. */
  key: string;
  query: string;
  /** Groups and flow directions the reader opened or closed. */
  toggled: Record<string, boolean>;
  /** Rows shown per paged list, valid for one scope and one search. */
  limits: Record<string, number>;
  limitsScope: string;
};

const emptyListMemory = (key: string): LocalListMemory => ({ key, limits: {}, limitsScope: "", query: "", toggled: {} });

type ListViewProps = {
  active: boolean;
  analysis: LocalSceneAnalysis;
  context: LocalViewContext;
  /** Shown when no relation is listed: why (none, or all filtered out) and the way back. */
  empty?: ReactNode;
  /** Stage status (loading, coverage) the canvas would float; the list head holds it instead. */
  notice?: ReactNode;
  memory: { current: LocalListMemory | null };
  memoryKey: string;
  onBlur: (id: string) => void;
  onFocus: (id: string) => void;
  onSelect: (id: string, origin: RovingSelectionOrigin) => void;
  scopeKey: string;
  selectedId: string | null;
};

type RowHandlers = Pick<ListViewProps, "active" | "onBlur" | "onFocus" | "onSelect" | "selectedId"> & { describedBy: string };

function useListWidth(ref: { current: HTMLElement | null }) {
  const [width, setWidth] = useState<number | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return undefined;
    // Content width: Details opening beside the list adds padding, not width.
    const update = () => {
      // A hidden list (another tab) has no width: keep its layout for the return.
      if (!element.clientWidth) return;
      const style = getComputedStyle(element);
      setWidth(element.clientWidth - parseFloat(style.paddingLeft || "0") - parseFloat(style.paddingRight || "0"));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

/** The sticky head's height, so the bow-tie hub and keyboard scrolling stop below it. */
function useStickyHeadSize(rootRef: { current: HTMLElement | null }, headRef: { current: HTMLElement | null }) {
  useLayoutEffect(() => {
    const root = rootRef.current;
    const head = headRef.current;
    if (!root || !head || typeof ResizeObserver === "undefined") return undefined;
    const update = () => root.style.setProperty("--graph-list-head-size", `${head.offsetHeight}px`);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(head);
    return () => observer.disconnect();
  }, [headRef, rootRef]);
}

/** A list with one Tab entry. Row keys are unique; `targetOf` names what a row selects and highlights. */
function RovingRows<T>({ className, handlers, id, items, keyOf, label, renderRow, scopeKey, targetOf }: {
  className: string;
  handlers: RowHandlers;
  id: string;
  items: readonly T[];
  keyOf: (item: T) => string;
  label: string;
  renderRow: (item: T, props: ReturnType<ReturnType<typeof useRovingList>>) => ReactNode;
  scopeKey: string;
  targetOf: (item: T) => string;
}) {
  const ids = useMemo(() => items.map(keyOf), [items, keyOf]);
  const { onBlur, onFocus, onSelect } = handlers;
  const rowProps = useRovingList({ enabled: handlers.active, ids, onBlur, onFocus, onSelect, scopeKey });
  return <ul aria-describedby={handlers.describedBy} aria-label={label} className={className} id={id}>
    {items.map((item, index) => renderRow(item, rowProps(keyOf(item), index, targetOf(item))))}
  </ul>;
}

/** The seed pill: selecting it selects the seed document itself. */
function SeedHub({ handlers, seed, seedId }: { handlers: RowHandlers; seed: RelationListNodeView; seedId: string }) {
  return <button aria-pressed={handlers.selectedId === seedId} className="relation-list-flow-hub-pill" data-local-graph-id={seedId}
    onClick={(event) => handlers.onSelect(seedId, {
      control: event.currentTarget,
      modality: event.detail === 0 ? "keyboard" : "pointer",
    })} type="button">{seed.cue}<strong title={seed.label}>{seed.label}</strong>{seed.badge}</button>;
}

function OpenOrSpacer({ node }: { node: RelationListNodeView | undefined }) {
  return node?.href ? <RelationListOpenLink href={node.href} label={node.label} tabIndex={-1} />
    : <span aria-hidden="true" className="graph-list-open-spacer" />;
}

/**
 * The Graph tab's list view: the seed's relations first (incoming → seed →
 * outgoing), then one folded group per ring the relation scope added. Every
 * displayed node and relation is reachable here, ten rows at a time.
 */
export function LocalGraphListView({
  active, analysis, context, empty, memory, memoryKey, notice, onBlur, onFocus, onSelect, scopeKey, selectedId,
}: ListViewProps) {
  const { t } = useTranslation("graph");
  const baseId = useId();
  const rootRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const width = useListWidth(rootRef);
  const headRef = useRef<HTMLDivElement>(null);
  useStickyHeadSize(rootRef, headRef);
  // A layout switch, a timeline step or a refresh can drop the focused row:
  // focus its copy in the new layout, else the graph region (Escape still works).
  useFocusRecovery(rootRef, () => rootRef.current?.closest<HTMLElement>("[data-testid='graph-view']") ?? null, selectedId);
  const wide = width === null || width >= BOW_TIE_MIN_WIDTH;
  // Selecting a row opens Details, which can switch the layout (bow-tie to
  // flow): keep the chosen row in view rather than where it was. Only a new
  // selection or a layout switch scrolls; resizing within a layout does not.
  const chosenRef = useRef<HTMLElement | null>(null);
  const select = useCallback((id: string, origin: RovingSelectionOrigin) => {
    chosenRef.current = origin.control;
    onSelect(id, origin);
  }, [onSelect]);
  useLayoutEffect(() => {
    if (!selectedId) return;
    const chosen = chosenRef.current;
    const row = chosen?.isConnected && chosen.dataset.localGraphId === selectedId ? chosen
      : Array.from(rootRef.current?.querySelectorAll<HTMLElement>('[aria-pressed="true"][data-local-graph-id]') ?? [])
        .find((candidate) => candidate.dataset.localGraphId === selectedId);
    row?.scrollIntoView?.({ block: "nearest" });
  }, [selectedId, wide]);
  const [memoryState, setMemoryState] = useState<LocalListMemory>(() =>
    memory.current?.key === memoryKey ? memory.current : emptyListMemory(memoryKey));
  const remembered = memoryState.key === memoryKey ? memoryState : emptyListMemory(memoryKey);
  useEffect(() => {
    memory.current = remembered;
  });
  const remember = useCallback((change: (current: LocalListMemory) => Partial<LocalListMemory>) => setMemoryState((previous) => {
    const current = previous.key === memoryKey ? previous : emptyListMemory(memoryKey);
    return { ...current, ...change(current) };
  }), [memoryKey]);
  const { query, toggled } = remembered;
  // A new search starts every list at its first page.
  const setQuery = useCallback((value: string) => remember(() => ({ limits: {}, query: value })), [remember]);
  const toggle = (id: string, open: boolean) => remember((current) => ({ toggled: { ...current.toggled, [id]: !open } }));
  const limit = (key: string) => remembered.limitsScope === scopeKey ? remembered.limits[key] ?? RELATION_LIST_PAGE : RELATION_LIST_PAGE;
  const showMore = (key: string) => remember((current) => {
    const limits = current.limitsScope === scopeKey ? current.limits : {};
    return { limits: { ...limits, [key]: (limits[key] ?? RELATION_LIST_PAGE) + RELATION_LIST_PAGE }, limitsScope: scopeKey };
  });
  const listId = (key: string) => `${baseId}-${key}`;

  const labelOf = useCallback((id: string) => analysis.nodesById.get(id)?.label ?? "—", [analysis]);
  const nodeView = useCallback((id: string) => {
    const node = analysis.nodesById.get(id);
    return node ? localNodeView(node, context) : undefined;
  }, [analysis, context]);
  const edgeView = useCallback((edge: GraphEdge) => localEdgeView(edge, context, labelOf), [context, labelOf]);
  const matches = useMemo(() => localListMatches(query), [query]);
  const nodeText = useCallback((id: string) => {
    const node = analysis.nodesById.get(id);
    return node ? localNodeSearchText(node, context) : "";
  }, [analysis, context]);
  const relationText = useCallback((edge: GraphEdge) => localRelationSearchText(edgeView(edge)), [edgeView]);
  // A relation matches by its own names or by either end (a node matches by
  // its name and kind names). In a seed's group every row has the seed at one
  // end, so a row matches by the relation or its far end only.
  const edgeMatches = useCallback((edge: GraphEdge) => matches(relationText(edge), nodeText(edge.source), nodeText(edge.target)),
    [matches, nodeText, relationText]);
  const seedRowText = useCallback((edge: GraphEdge, otherId: string) => `${relationText(edge)} ${nodeText(otherId)}`,
    [nodeText, relationText]);
  const seedRowMatches = useCallback((seedId: string) => (edge: GraphEdge) =>
    matches(seedRowText(edge, edge.source === seedId ? edge.target : edge.source)), [matches, seedRowText]);
  const nodeMatches = useCallback((node: GraphNode) => matches(localNodeSearchText(node, context)), [context, matches]);
  const rings = useMemo(() => localSceneRings(analysis), [analysis]);
  const searching = Boolean(query.trim());
  const filtered = useMemo(() => ({
    neighbors: rings.neighbors.filter(edgeMatches),
    ringNodes: rings.ringNodes.filter(nodeMatches),
    ringRelations: rings.ringRelations.filter(edgeMatches),
    unlinkedEdges: rings.unlinkedEdges.filter(edgeMatches),
    unlinkedNodes: rings.unlinkedNodes.filter(nodeMatches),
  }), [edgeMatches, nodeMatches, rings]);
  const seedRelations = useMemo(() => analysis.seeds.map((seedId) => {
    const relations = localNodeRelations(analysis, seedId);
    const rowMatches = seedRowMatches(seedId);
    return {
      all: [...relations.incoming, ...relations.outgoing, ...relations.undirected],
      relations: searching ? {
        incoming: relations.incoming.filter(rowMatches),
        outgoing: relations.outgoing.filter(rowMatches),
        undirected: relations.undirected.filter(rowMatches),
      } : relations,
      seedId,
    };
  }), [analysis, searching, seedRowMatches]);

  // The graph region listens for Escape; a non-empty list search clears first.
  useEffect(() => {
    const input = inputRef.current;
    if (!input) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !input.value) return;
      event.preventDefault();
      event.stopPropagation();
      setQuery("");
    };
    input.addEventListener("keydown", onKeyDown);
    return () => input.removeEventListener("keydown", onKeyDown);
  }, [setQuery]);

  const handlers: RowHandlers = { active, describedBy: `${baseId}-hint`, onBlur, onFocus, onSelect: select, selectedId };
  const strokeSummary = (edges: readonly GraphEdge[]) => <RelationListSummary
    items={relationListStrokeSummary(edges, localStroke, LOCAL_STROKE_ORDER, localStrokeLabel(t))} />;
  const nodeSummary = (nodes: readonly GraphNode[]) => {
    const counts = new Map<string, { count: number; node: GraphNode }>();
    for (const node of nodes) {
      const kind = localNodeVisualKind(node.entity_kind, node.family);
      counts.set(kind, { count: (counts.get(kind)?.count ?? 0) + 1, node: counts.get(kind)?.node ?? node });
    }
    const items: RelationListSummaryItem[] = NODE_KIND_ORDER.filter((kind) => counts.has(kind)).map((kind) => ({
      count: counts.get(kind)!.count,
      cue: <LocalNodeCue node={counts.get(kind)!.node} visuals={context.visuals} />,
      key: kind,
      label: nodeView(counts.get(kind)!.node.id)?.chip.label ?? kind,
    }));
    return <RelationListSummary items={items} />;
  };
  const group = ({ count, defaultOpen = false, id, note, summary, title, body }: {
    body: () => ReactNode; count: number; defaultOpen?: boolean; id: string; note?: string; summary?: ReactNode; title: string;
  }) => {
    if (!count) return null;
    const open = toggled[id] ?? (defaultOpen || searching);
    return <section className="graph-list-group" data-list-group={id} key={id}>
      <RelationListFoldHeader controls={`${baseId}-${id}`} count={count} note={note}
        onToggle={() => toggle(id, open)} open={open} summary={summary} title={title} />
      {open ? <div className="graph-list-group-body" id={`${baseId}-${id}`}>{body()}</div> : null}
    </section>;
  };
  const pagedRows = <T,>(key: string, items: readonly T[], render: (shown: readonly T[]) => ReactNode) => {
    const shown = items.slice(0, limit(key));
    return <>
      {render(shown)}
      <RelationListMoreButton controls={listId(key)} onClick={() => showMore(key)} remaining={items.length - shown.length}
        step={RELATION_LIST_PAGE} />
    </>;
  };
  const nodeRows = (key: string, label: string, nodes: readonly GraphNode[]) => pagedRows(key, nodes, (shown) => (
    <RovingRows className="graph-list-rows" handlers={handlers} id={listId(key)} items={shown} keyOf={nodeKey} label={label}
      renderRow={(node, rowProps) => {
        const view = nodeView(node.id)!;
        const hop = analysis.hop.get(node.id);
        const via = hop !== undefined && hop >= 2 ? analysis.parents.get(node.id) ?? [] : [];
        const viaText = via.length ? `${hop! > 2 ? `${t("graph.list.steps", { count: hop })} · ` : ""}${via.length > 1
          ? t("graph.list.viaMore", { count: via.length - 1, label: labelOf(via[0]) })
          : t("graph.list.via", { label: labelOf(via[0]) })}` : null;
        return <li className="graph-list-row" key={node.id}>
          <button aria-pressed={selectedId === node.id} className="relation-list-row-select graph-list-row-select"
            data-local-graph-id={node.id} type="button" {...rowProps}>
            <span className="relation-list-flow-name">{view.cue}<strong>{view.label}</strong>{view.badge}</span>
            <span className="relation-list-row-meta"><RelationListChip chip={view.chip} />
              {viaText ? <span className="relation-list-row-meta-text">{viaText}</span> : null}</span>
          </button>
          <OpenOrSpacer node={view} />
        </li>;
      }} scopeKey={`${scopeKey}:${key}`} targetOf={nodeKey} />
  ));
  const relationRows = (key: string, label: string, edges: readonly GraphEdge[]) => pagedRows(key, edges, (shown) => (
    <RovingRows className="graph-list-rows graph-list-relation-rows" handlers={handlers} id={listId(key)} items={shown} keyOf={edgeKey}
      label={label}
      renderRow={(edge, rowProps) => {
        const view = edgeView(edge);
        return <li className="graph-list-row graph-list-relation-row" key={edge.id}>
          <button aria-label={view.name} aria-pressed={selectedId === edge.id}
            className="relation-list-row-select graph-list-row-select" data-local-graph-id={edge.id} type="button" {...rowProps}>
            <RelationListMiniMap edge={view} source={nodeView(edge.source)} target={nodeView(edge.target)} />
          </button>
        </li>;
      }} scopeKey={`${scopeKey}:${key}`} targetOf={edgeKey} />
  ));

  const seedGroup = ({ all, relations, seedId }: (typeof seedRelations)[number], index: number) => {
    const seed = nodeView(seedId);
    if (!seed) return null;
    const count = relations.incoming.length + relations.outgoing.length + relations.undirected.length;
    const key = `seed:${seedId}`;
    const seedNode = analysis.nodesById.get(seedId);
    // The seed stays selectable on its own when no row is listed for it: when
    // it has no relations, and when a search matches the seed but none of its
    // rows (the search status counts it, so the list must show it).
    if (!all.length && !searching || searching && !count && !!seedNode && nodeMatches(seedNode)) return <section
      className="graph-list-group" data-list-group={key} key={key}>
      <div className="graph-list-bowtie-hub is-alone"><SeedHub handlers={handlers} seed={seed} seedId={seedId} /></div>
    </section>;
    return group({
      body: () => wide
        ? <BowTie direction={relations} edgeView={edgeView} handlers={handlers} limit={limit} listIdOf={(side) => listId(`seed${index}-${side}`)}
          nodeView={nodeView} onMore={showMore} scopeKey={`${scopeKey}:${key}`} seed={seed} seedId={seedId} t={t} tone={seed.chip.tone} />
        : <RelationFlow
          active={active}
          edgeView={edgeView}
          headingLevel={5}
          hideSearch
          // The bow-tie's pages: Details opening beside the list can switch
          // the layout, and the chosen row must still be on the page.
          limitOf={(side) => limit(`${scopeKey}:${key}:${side}`)}
          navigation="roving"
          node={seed}
          nodeChips
          nodeTone={seed.chip.tone}
          nodeView={nodeView}
          onRowBlur={(_, otherId) => onBlur(otherId)}
          onRowFocus={(_, otherId) => onFocus(otherId)}
          onSelect={select}
          onShowMore={(side) => showMore(`${scopeKey}:${key}:${side}`)}
          onToggleOpen={(side) => toggle(`${key}:${side}`, toggled[`${key}:${side}`] ?? true)}
          openLinkTabIndex={-1}
          openOf={(side) => toggled[`${key}:${side}`] ?? true}
          page={RELATION_LIST_PAGE}
          query={query}
          relations={localNodeRelations(analysis, seedId)}
          rowAttributes={(otherId) => ({ "data-local-graph-id": otherId })}
          scopeKey={scopeKey}
          searchText={seedRowText}
          selectableHub
          selectedId={selectedId}
          strokeLabel={localStrokeLabel(t)}
          texts={{
            empty: t("graph.view.noRelations"),
            incomingNote: t("graph.list.incomingNote"),
            outgoingNote: t("graph.list.outgoingNote"),
            searchLabel: t("graph.list.search"),
            searchPlaceholder: t("relationList.searchPlaceholder"),
            self: t("graph.view.seed"),
            title: t("graph.list.groups.seed"),
            undirectedNote: t("graph.list.undirectedNote"),
          }}
        />,
      count: searching ? count : all.length,
      defaultOpen: index === 0,
      id: key,
      summary: strokeSummary(all),
      title: analysis.seeds.length > 1 ? t("graph.list.groups.seedNamed", { label: seed.label }) : t("graph.list.groups.seed"),
    });
  };

  // Every node a search matches is listed: as a seed (alone when none of its
  // rows match), a seed row's far end, a two-step item or an unlinked item.
  const matchedNodes = searching ? [...analysis.nodesById.values()].filter(nodeMatches).length : 0;
  const counts = (nodes: number, edges: number) => ({
    edges: t("graph.list.relationCount", { count: edges, value: formatRelationListCount(edges) }),
    nodes: t("graph.list.nodeCount", { count: nodes, value: formatRelationListCount(nodes) }),
  });
  // Counted by the rule of the row each relation is listed in.
  const matchedEdges = searching ? analysis.edges.filter((edge) => {
    const seedEnds = [edge.source, edge.target].filter((id) => analysis.seeds.includes(id));
    return seedEnds.length ? seedEnds.some((seedId) => seedRowMatches(seedId)(edge)) : edgeMatches(edge);
  }).length : 0;
  return <section aria-labelledby={`${baseId}-title`} className="graph-list-view" data-testid="graph-list-view"
    data-layout={wide ? "bow-tie" : "flow"} ref={rootRef}>
    <div className="graph-list-head" ref={headRef}>
      <div className="graph-list-title">
        <h4 id={`${baseId}-title`}>{t("graph.list.title")}</h4>
        <span className="graph-list-counts">{t("graph.list.counts", counts(analysis.nodesById.size, analysis.edges.length))}</span>
      </div>
      <div className="relation-list-search graph-list-search">
        <Search aria-hidden="true" size={15} />
        <input aria-label={t("graph.list.search")} onChange={(event) => setQuery(event.currentTarget.value)}
          placeholder={t("relationList.searchPlaceholder")} ref={inputRef} type="search" value={query} />
        {query ? <button aria-label={t("graph.list.clearSearch")} className="relation-list-search-clear"
          onClick={() => { setQuery(""); inputRef.current?.focus(); }} type="button"><X aria-hidden="true" size={14} /></button> : null}
      </div>
      {notice ? <div className="graph-list-notice">{notice}</div> : null}
    </div>
    <p className="visually-hidden" id={`${baseId}-hint`}>{t("graph.relationshipListDescription")}</p>
    {searching ? <p className="graph-list-status" role="status">
      {matchedNodes || matchedEdges ? t("graph.list.status", { ...counts(matchedNodes, matchedEdges), query: query.trim() })
        : t("graph.list.noMatch", { query: query.trim() })}
    </p> : null}
    {!analysis.edges.length ? <div className="graph-list-empty" role="status">{empty ?? <p>{t("graph.view.noRelations")}</p>}</div> : null}
    {seedRelations.map(seedGroup)}
    {group({
      body: () => relationRows("neighbors", t("graph.list.groups.neighbors"), filtered.neighbors),
      count: filtered.neighbors.length, id: "neighbors", note: t("graph.list.groups.neighborsNote"),
      summary: strokeSummary(filtered.neighbors), title: t("graph.list.groups.neighbors"),
    })}
    {group({
      body: () => nodeRows("ringNodes", t("graph.list.groups.ringNodes"), filtered.ringNodes),
      count: filtered.ringNodes.length, id: "ringNodes", note: t("graph.list.groups.ringNodesNote"),
      summary: nodeSummary(filtered.ringNodes), title: t("graph.list.groups.ringNodes"),
    })}
    {group({
      body: () => relationRows("ringRelations", t("graph.list.groups.ringRelations"), filtered.ringRelations),
      count: filtered.ringRelations.length, id: "ringRelations", note: t("graph.list.groups.ringRelationsNote"),
      summary: strokeSummary(filtered.ringRelations), title: t("graph.list.groups.ringRelations"),
    })}
    {group({
      body: () => <>
        {filtered.unlinkedNodes.length ? nodeRows("unlinkedNodes", t("graph.list.groups.unlinked"), filtered.unlinkedNodes) : null}
        {filtered.unlinkedEdges.length ? relationRows("unlinkedEdges", t("graph.list.groups.unlinked"), filtered.unlinkedEdges) : null}
      </>,
      count: filtered.unlinkedNodes.length + filtered.unlinkedEdges.length, id: "unlinked",
      note: t("graph.list.groups.unlinkedNote"), title: t("graph.list.groups.unlinked"),
    })}
  </section>;
}

const nodeKey = (node: GraphNode) => node.id;
const edgeKey = (edge: GraphEdge) => edge.id;

/** Incoming relations on the left point at the seed; outgoing ones leave it to the right. */
function BowTie({ direction, edgeView, handlers, limit, listIdOf, nodeView, onMore, scopeKey, seed, seedId, t, tone }: {
  direction: Record<RelationListDirection, readonly GraphEdge[]>;
  edgeView: (edge: GraphEdge) => RelationListEdgeView;
  handlers: RowHandlers;
  limit: (key: string) => number;
  listIdOf: (side: RelationListDirection) => string;
  nodeView: (id: string) => RelationListNodeView | undefined;
  onMore: (key: string) => void;
  scopeKey: string;
  seed: RelationListNodeView;
  seedId: string;
  t: TFunction<"graph">;
  tone: string;
}) {
  const column = (side: RelationListDirection, note: string) => {
    const edges = direction[side];
    if (!edges.length && side === "undirected") return null;
    const key = `${scopeKey}:${side}`;
    const shown = edges.slice(0, limit(key));
    const otherOf = (edge: GraphEdge) => edge.source === seedId ? edge.target : edge.source;
    const title = t(`relationList.${side}`);
    return <div className="graph-list-bowtie-column" data-direction={side}>
      <div className="graph-list-bowtie-head">
        <span className="relation-list-fold-title">{title}</span>
        <span className="relation-list-fold-count">{formatRelationListCount(edges.length)}</span>
        <span className="relation-list-fold-note">{note}</span>
      </div>
      {edges.length ? <RovingRows className="graph-list-bowtie-list" handlers={handlers} id={listIdOf(side)} items={shown} keyOf={edgeKey}
        label={title}
        renderRow={(edge, rowProps) => {
          const view = edgeView(edge);
          const otherId = otherOf(edge);
          const other = nodeView(otherId);
          return <li className="graph-list-bowtie-row" data-direction={side} key={edge.id}>
            <button aria-label={view.name} aria-pressed={handlers.selectedId === otherId}
              className="relation-list-row-select graph-list-row-select" data-local-graph-id={otherId} type="button" {...rowProps}>
              <span className="relation-list-flow-name">{other?.cue ?? null}<strong>{other?.label ?? "—"}</strong>{other?.badge ?? null}</span>
              <span className="relation-list-row-meta">
                <RelationListChip chip={view.chip} />
                {other ? <RelationListChip chip={other.chip} /> : null}
              </span>
            </button>
            <OpenOrSpacer node={other} />
            <span aria-hidden="true" className="graph-list-bowtie-stroke" data-directed={view.directed} data-visual={view.stroke} />
          </li>;
        }} scopeKey={key} targetOf={otherOf} /> : <p className="graph-list-bowtie-none">—</p>}
      <RelationListMoreButton controls={listIdOf(side)} onClick={() => onMore(key)} remaining={edges.length - shown.length}
        step={RELATION_LIST_PAGE} />
    </div>;
  };
  return <div className="graph-list-bowtie" data-tone={tone}>
    {column("incoming", t("graph.list.incomingNote"))}
    <div className="graph-list-bowtie-hub"><SeedHub handlers={handlers} seed={seed} seedId={seedId} /></div>
    {column("outgoing", t("graph.list.outgoingNote"))}
    {column("undirected", t("graph.list.undirectedNote"))}
  </div>;
}
