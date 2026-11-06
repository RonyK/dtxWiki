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
  localEdgeSearchText,
  localEdgeView,
  localNodeSearchText,
  localNodeView,
  localStroke,
  localStrokeLabel,
  type LocalViewContext,
} from "./localRelationViews";
import { localNodeVisualKind } from "./nodeVisual";

/** Below this list width the seed's relations read top to bottom, like Details. */
const BOW_TIE_MIN_WIDTH = 640;
const NODE_KIND_ORDER = ["context", "library", "agent", "conversation", "folder"] as const;

type ListViewProps = {
  active: boolean;
  analysis: LocalSceneAnalysis;
  context: LocalViewContext;
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

/** A list with one Tab entry. Row keys are unique; `targetOf` names what a row selects and highlights. */
function RovingRows<T>({ className, handlers, items, keyOf, label, renderRow, scopeKey, targetOf }: {
  className: string;
  handlers: RowHandlers;
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
  return <ul aria-describedby={handlers.describedBy} aria-label={label} className={className}>
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
export function LocalGraphListView({ active, analysis, context, onBlur, onFocus, onSelect, scopeKey, selectedId }: ListViewProps) {
  const { t } = useTranslation("graph");
  const baseId = useId();
  const rootRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const width = useListWidth(rootRef);
  const wide = width === null || width >= BOW_TIE_MIN_WIDTH;
  const [query, setQuery] = useState("");
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const [paging, setPaging] = useState<{ analysis: LocalSceneAnalysis | null; query: string; limits: Record<string, number> }>({
    analysis: null, query: "", limits: {},
  });
  const limit = (key: string) => paging.analysis === analysis && paging.query === query
    ? paging.limits[key] ?? RELATION_LIST_PAGE : RELATION_LIST_PAGE;
  const showMore = (key: string) => setPaging((current) => {
    const limits = current.analysis === analysis && current.query === query ? current.limits : {};
    return { analysis, query, limits: { ...limits, [key]: (limits[key] ?? RELATION_LIST_PAGE) + RELATION_LIST_PAGE } };
  });

  const labelOf = useCallback((id: string) => analysis.nodesById.get(id)?.label ?? "—", [analysis]);
  const nodeView = useCallback((id: string) => {
    const node = analysis.nodesById.get(id);
    return node ? localNodeView(node, context) : undefined;
  }, [analysis, context]);
  const edgeView = useCallback((edge: GraphEdge) => localEdgeView(edge, context, labelOf), [context, labelOf]);
  const matches = useMemo(() => localListMatches(query), [query]);
  const edgeMatches = useCallback((edge: GraphEdge) => matches(localEdgeSearchText(edge, edgeView(edge), labelOf)),
    [edgeView, labelOf, matches]);
  const nodeMatches = useCallback((node: GraphNode) => matches(localNodeSearchText(node, t)), [matches, t]);
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
    return {
      all: [...relations.incoming, ...relations.outgoing, ...relations.undirected],
      relations: searching ? {
        incoming: relations.incoming.filter(edgeMatches),
        outgoing: relations.outgoing.filter(edgeMatches),
        undirected: relations.undirected.filter(edgeMatches),
      } : relations,
      seedId,
    };
  }), [analysis, edgeMatches, searching]);

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
  }, []);

  const handlers: RowHandlers = { active, describedBy: `${baseId}-hint`, onBlur, onFocus, onSelect, selectedId };
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
        onToggle={() => setToggled((current) => ({ ...current, [id]: !open }))} open={open} summary={summary} title={title} />
      {open ? <div className="graph-list-group-body" id={`${baseId}-${id}`}>{body()}</div> : null}
    </section>;
  };
  const pagedRows = <T,>(key: string, items: readonly T[], render: (shown: readonly T[]) => ReactNode) => {
    const shown = items.slice(0, limit(key));
    return <>
      {render(shown)}
      <RelationListMoreButton onClick={() => showMore(key)} remaining={items.length - shown.length} step={RELATION_LIST_PAGE} />
    </>;
  };
  const nodeRows = (key: string, label: string, nodes: readonly GraphNode[]) => pagedRows(key, nodes, (shown) => (
    <RovingRows className="graph-list-rows" handlers={handlers} items={shown} keyOf={nodeKey} label={label}
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
    <RovingRows className="graph-list-rows graph-list-relation-rows" handlers={handlers} items={shown} keyOf={edgeKey} label={label}
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
    // A seed without relations stays selectable beside the empty-state message.
    if (!all.length && !searching) return <section className="graph-list-group" data-list-group={key} key={key}>
      <div className="graph-list-bowtie-hub is-alone"><SeedHub handlers={handlers} seed={seed} seedId={seedId} /></div>
    </section>;
    return group({
      body: () => wide
        ? <BowTie direction={relations} edgeView={edgeView} handlers={handlers} limit={limit} nodeView={nodeView}
          onMore={showMore} scopeKey={`${scopeKey}:${key}`} seed={seed} seedId={seedId} t={t} tone={seed.chip.tone} />
        : <RelationFlow
          active={active}
          edgeView={edgeView}
          headingLevel={5}
          hideSearch
          navigation="roving"
          node={seed}
          nodeChips
          nodeTone={seed.chip.tone}
          nodeView={nodeView}
          onRowBlur={(_, otherId) => onBlur(otherId)}
          onRowFocus={(_, otherId) => onFocus(otherId)}
          onSelect={onSelect}
          openLinkTabIndex={-1}
          page={RELATION_LIST_PAGE}
          query={query}
          relations={localNodeRelations(analysis, seedId)}
          rowAttributes={(otherId) => ({ "data-local-graph-id": otherId })}
          scopeKey={scopeKey}
          searchText={(edge) => localEdgeSearchText(edge, edgeView(edge), labelOf)}
          selectableHub
          selectedId={selectedId}
          strokeLabel={localStrokeLabel(t)}
          texts={{
            empty: t("graph.view.noRelations"),
            incomingNote: t("graph.list.incomingNote"),
            outgoingNote: t("graph.list.outgoingNote"),
            searchLabel: t("graph.list.search"),
            searchPlaceholder: t("graph.list.searchPlaceholder"),
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

  const matchedNodes = searching ? [...analysis.nodesById.values()].filter(nodeMatches).length : 0;
  const matchedEdges = searching ? analysis.edges.filter(edgeMatches).length : 0;
  return <section aria-labelledby={`${baseId}-title`} className="graph-list-view" data-testid="graph-list-view"
    data-layout={wide ? "bow-tie" : "flow"} ref={rootRef}>
    <div className="graph-list-head">
      <div className="graph-list-title">
        <h4 id={`${baseId}-title`}>{t("graph.list.title")}</h4>
        <span className="graph-list-counts">{t("graph.list.counts", {
          edges: formatRelationListCount(analysis.edges.length), nodes: formatRelationListCount(analysis.nodesById.size),
        })}</span>
      </div>
      <div className="relation-list-search graph-list-search">
        <Search aria-hidden="true" size={15} />
        <input aria-label={t("graph.list.search")} onChange={(event) => setQuery(event.currentTarget.value)}
          placeholder={t("graph.list.searchPlaceholder")} ref={inputRef} type="search" value={query} />
        {query ? <button aria-label={t("graph.list.clearSearch")} className="relation-list-search-clear"
          onClick={() => { setQuery(""); inputRef.current?.focus(); }} type="button"><X aria-hidden="true" size={14} /></button> : null}
      </div>
    </div>
    <p className="visually-hidden" id={`${baseId}-hint`}>{t("graph.relationshipListDescription")}</p>
    {searching ? <p className="graph-list-status" role="status">
      {matchedNodes || matchedEdges ? t("graph.list.status", {
        edges: formatRelationListCount(matchedEdges), nodes: formatRelationListCount(matchedNodes), query: query.trim(),
      }) : t("graph.list.noMatch", { query: query.trim() })}
    </p> : null}
    {!analysis.edges.length ? <p className="graph-list-empty">{t("graph.view.noRelations")}</p> : null}
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
function BowTie({ direction, edgeView, handlers, limit, nodeView, onMore, scopeKey, seed, seedId, t, tone }: {
  direction: Record<RelationListDirection, readonly GraphEdge[]>;
  edgeView: (edge: GraphEdge) => RelationListEdgeView;
  handlers: RowHandlers;
  limit: (key: string) => number;
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
      {edges.length ? <RovingRows className="graph-list-bowtie-list" handlers={handlers} items={shown} keyOf={edgeKey} label={title}
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
                {view.note ? <span className="relation-list-row-meta-text">{view.note}</span> : null}
              </span>
            </button>
            <OpenOrSpacer node={other} />
            <span aria-hidden="true" className="graph-list-bowtie-stroke" data-directed={view.directed} data-visual={view.stroke} />
          </li>;
        }} scopeKey={key} targetOf={otherOf} /> : <p className="graph-list-bowtie-none">—</p>}
      <RelationListMoreButton onClick={() => onMore(key)} remaining={edges.length - shown.length} step={RELATION_LIST_PAGE} />
    </div>;
  };
  return <div className="graph-list-bowtie" data-tone={tone}>
    {column("incoming", t("graph.list.incomingNote"))}
    <div className="graph-list-bowtie-hub"><SeedHub handlers={handlers} seed={seed} seedId={seedId} /></div>
    {column("outgoing", t("graph.list.outgoingNote"))}
    {column("undirected", t("graph.list.undirectedNote"))}
  </div>;
}
