import { useCallback, useId, type MouseEvent as ReactMouseEvent } from "react";
import { useTranslation } from "react-i18next";

import type { GraphEdge, GraphNode } from "../../../shared/api/graph";
import { domainLabel } from "../../../shared/lib/domainLabel";
import { RelationListChip } from "../shared/relationList/parts";
import { RelationEnds } from "../shared/relationList/RelationEnds";
import { RelationFlow } from "../shared/relationList/RelationFlow";
import type { RovingSelectionOrigin } from "../shared/relationList/useRovingList";
import { localNodeRelations, localSeedPath, type LocalSceneAnalysis } from "./localGraphList";
import {
  localEdgeSearchText,
  localEdgeView,
  localNodeView,
  localSelfKind,
  localStroke,
  localStrokeLabel,
  type LocalViewContext,
} from "./localRelationViews";

type Select = (id: string, origin: RovingSelectionOrigin) => void;

/** Under the title: the kind chip and status, then the location. */
export function LocalNodeMeta({ context, node }: { context: LocalViewContext; node: GraphNode }) {
  const { t } = useTranslation("graph");
  const view = localNodeView(node, context);
  return <div className="graph-details-meta">
    <span className="graph-details-meta-line">
      <RelationListChip chip={view.chip} />
      {view.badge}
      {node.status ? <span className="graph-details-status">{domainLabel("graph", "graphStatus", node.status, t)}</span> : null}
    </span>
    {node.path ? <span className="graph-details-location" title={node.path}>{node.path}</span> : null}
  </div>;
}

/**
 * How the selected node is reached from the seed: one shortest path drawn
 * like the map (seed / stroke + kind / … / this node). Arrows follow the
 * stored direction, so a relation into the seed points up. Below it, every
 * other relation between this node and a seed. List rows that touch a seed
 * select the far document, so this card is where any seed relation opens
 * outside the canvas: one between two expanded seeds, or one from an expanded
 * seed to a document the path reaches another way.
 */
export function LocalSeedPath({ analysis, context, node, onSelect }: {
  analysis: LocalSceneAnalysis;
  context: LocalViewContext;
  node: GraphNode;
  onSelect: Select;
}) {
  const { t } = useTranslation("graph");
  const titleId = useId();
  const path = localSeedPath(analysis, node.id);
  const onPath = new Set(path?.links.flat().map((edge) => edge.id));
  const seeds = new Set(analysis.seeds);
  const seedLinks = analysis.edges.filter((edge) => !onPath.has(edge.id)
    && ((edge.source === node.id && seeds.has(edge.target)) || (edge.target === node.id && seeds.has(edge.source))));
  if (!path && !seedLinks.length) return null;
  const labelOf = (id: string) => analysis.nodesById.get(id)?.label ?? "—";
  const select = (id: string) => (event: ReactMouseEvent<HTMLButtonElement>) => onSelect(id, {
    control: event.currentTarget,
    modality: event.detail === 0 ? "keyboard" : "pointer",
  });
  const links = seedLinks.length ? <ul aria-label={t("graph.seedPath.otherSeeds")} className="graph-seed-links">
    {seedLinks.map((edge) => {
      const otherId = edge.source === node.id ? edge.target : edge.source;
      const other = localNodeView(analysis.nodesById.get(otherId)!, context);
      const edgeView = localEdgeView(edge, context, labelOf);
      return <li key={edge.id}>
        <button aria-label={edgeView.name} className="graph-seed-link" data-local-graph-id={edge.id}
          onClick={select(edge.id)} title={edgeView.name} type="button">
          <RelationListChip chip={edgeView.chip} />
          {edge.direction === "directed"
            ? <span aria-hidden="true" className="graph-seed-link-direction">{edge.source === node.id ? "→" : "←"}</span> : null}
          {other.cue}<strong>{other.label}</strong>
        </button>
      </li>;
    })}
  </ul> : null;
  if (!path) {
    return <section aria-labelledby={titleId} className="graph-seed-path" data-tone={localNodeView(node, context).chip.tone}>
      <h5 id={titleId}>{t("graph.seedPath.otherSeeds")}</h5>
      {links}
    </section>;
  }
  const alternatives = path.alternatives.map(labelOf);
  return <section aria-labelledby={titleId} className="graph-seed-path" data-tone={localNodeView(node, context).chip.tone}>
    <h5 id={titleId}>
      {t("graph.details.seedRelationships")}
      <span className="graph-seed-path-steps">{path.steps === 1 ? t("graph.seedPath.direct")
        : t("graph.seedPath.steps", { steps: path.steps, via: path.steps - 1 })}</span>
    </h5>
    <ol>
      {path.chain.map((id, index) => {
        const item = analysis.nodesById.get(id)!;
        const view = localNodeView(item, context);
        const links = path.links[index] ?? [];
        const self = id === node.id;
        const content = <>
          {view.cue}<strong>{view.label}</strong>
          {self ? <span className="relation-list-flow-self">{t(`graph.relations.self.${localSelfKind(node)}`)}</span>
            // The path starts at a seed; every node between it and this one is passed through.
            : index === 0 ? view.badge : <span className="graph-seed-path-via">{t("graph.seedPath.via")}</span>}
        </>;
        return <li key={id}>
          {self ? <span className="graph-seed-path-node">{content}</span>
            : <button className="graph-seed-path-node" data-local-graph-id={id} type="button"
              onClick={select(id)}>{content}</button>}
          {links.length ? <div className="graph-seed-path-link">
            <span aria-hidden="true" className="relation-list-map-stroke" data-directed={links[0].direction === "directed"}
              data-upward={links[0].direction === "directed" && links[0].source === path.chain[index + 1]}
              data-visual={localStroke(links[0])} />
            <span className="graph-seed-path-chips">{links.map((edge) => {
              const edgeView = localEdgeView(edge, context, labelOf);
              // The chip selects the relation itself: the list's seed rows select
              // documents, so this is where a seed relation's Details open.
              return <button aria-label={edgeView.name} className="graph-seed-path-relation" data-local-graph-id={edge.id}
                key={edge.id} onClick={select(edge.id)} title={edgeView.name} type="button"><RelationListChip chip={edgeView.chip} /></button>;
            })}</span>
          </div> : null}
        </li>;
      })}
    </ol>
    {alternatives.length ? <p className="graph-seed-path-more">{t(alternatives.length > 2 ? "graph.seedPath.alternativesMore" : "graph.seedPath.alternatives", {
      count: alternatives.length, labels: alternatives.slice(0, 2).join(", "),
    })}</p> : null}
    {links ? <p aria-hidden="true" className="graph-seed-links-title">{t("graph.seedPath.otherSeeds")}</p> : null}
    {links}
  </section>;
}

/** The selected node's relations in Details: the shared flow with this graph's keyboard model. */
export function LocalRelationFlow({ active, analysis, context, node, onBlur, onFocus, onSelect, scopeKey }: {
  active: boolean;
  analysis: LocalSceneAnalysis;
  context: LocalViewContext;
  node: GraphNode;
  onBlur: (id: string) => void;
  onFocus: (id: string) => void;
  onSelect: Select;
  scopeKey: string;
}) {
  const { t } = useTranslation("graph");
  const labelOf = useCallback((id: string) => analysis.nodesById.get(id)?.label ?? "—", [analysis]);
  const nodeView = useCallback((id: string) => {
    const item = analysis.nodesById.get(id);
    return item ? localNodeView(item, context) : undefined;
  }, [analysis, context]);
  const edgeView = useCallback((edge: GraphEdge) => localEdgeView(edge, context, labelOf), [context, labelOf]);
  const self = localSelfKind(node);
  const view = localNodeView(node, context);
  return <RelationFlow
    active={active}
    edgeView={edgeView}
    headingLevel={5}
    navigation="roving"
    node={view}
    nodeChips
    nodeTone={view.chip.tone}
    nodeView={nodeView}
    onRowBlur={(_, otherId) => onBlur(otherId)}
    onRowFocus={(_, otherId) => onFocus(otherId)}
    onSelect={onSelect}
    openLinkTabIndex={-1}
    relations={localNodeRelations(analysis, node.id)}
    rowAttributes={(otherId) => ({ "data-local-graph-id": otherId })}
    scopeKey={scopeKey}
    searchText={(edge) => localEdgeSearchText(edge, edgeView(edge), labelOf)}
    strokeLabel={localStrokeLabel(t)}
    texts={{
      empty: t("graph.relations.none"),
      incomingNote: t(`graph.relations.incomingNote.${self}`),
      outgoingNote: t(`graph.relations.outgoingNote.${self}`),
      searchLabel: t(`graph.relations.search.${self}`),
      searchPlaceholder: t("graph.list.searchPlaceholder"),
      self: t(`graph.relations.self.${self}`),
      title: t("graph.relations.title"),
      undirectedNote: t("graph.relations.undirectedNote"),
    }}
  />;
}

/** A selected relation: source on top, the map's stroke and kind, target below; each end selects and opens. */
export function LocalRelationEnds({ analysis, context, edge, onSelect }: {
  analysis: LocalSceneAnalysis;
  context: LocalViewContext;
  edge: GraphEdge;
  onSelect: Select;
}) {
  const { t } = useTranslation("graph");
  const labelOf = (id: string) => analysis.nodesById.get(id)?.label ?? "—";
  const stroke = localStroke(edge);
  const roles = stroke === "link" ? "Link" : stroke === "citation" ? "Citation" : stroke === "membership" ? "Membership" : "";
  return <RelationEnds
    edge={localEdgeView(edge, context, labelOf)}
    nodeView={(id) => {
      const item = analysis.nodesById.get(id);
      return item ? localNodeView(item, context) : undefined;
    }}
    onSelectEnd={onSelect}
    roles={{
      source: t(roles ? `graph.roles.source${roles}` : "graph.roles.source"),
      target: t(roles ? `graph.roles.target${roles}` : "graph.roles.target"),
    }}
    rowAttributes={(id) => ({ "data-local-graph-id": id })}
  />;
}

