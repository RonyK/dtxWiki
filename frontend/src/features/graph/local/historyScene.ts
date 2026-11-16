import type { GraphQueryPayload, GraphTimelineEvent } from "../../../shared/api/graph";
import { filterGraphScene, visitGraphTimelineFrames, type GraphFilters, type GraphScene } from "../graphModel";
import type { GraphRenderScene } from "../shared/rendererTypes";

/** Analyze visible changes and collect filtered records in one forward replay. */
export function analyzeGraphHistory(
  payload: GraphQueryPayload,
  filters: GraphFilters,
  applyVisibility: (scene: GraphScene) => GraphScene = (scene) => scene,
): { visibleChanges: boolean[]; catalog: GraphScene } {
  const nodes = new Map<string, GraphScene["nodes"][number]>();
  const edges = new Map<string, GraphScene["edges"][number]>();
  const visibleChanges: boolean[] = [];
  let previous: GraphScene | undefined;
  const collect = (scene: GraphScene) => {
    scene.nodes.forEach((node) => nodes.set(node.id, node));
    scene.edges.forEach((edge) => edges.set(edge.id, edge));
  };
  visitGraphTimelineFrames(payload, filters, (cursor, scene) => {
    // Catalog membership precedes visibility, just as the original catalog did.
    collect(scene);
    const visible = applyVisibility(scene);
    if (cursor >= 0) visibleChanges.push(!sameGraphScene(previous!, visible));
    previous = visible;
  });
  // Current metadata wins without sorting or replaying the timeline again.
  collect(filterGraphScene({ nodes: payload.nodes, edges: payload.edges }, filters, payload.nodes));
  return { visibleChanges, catalog: { nodes: [...nodes.values()], edges: [...edges.values()] } };
}

/**
 * Same order and same content. Frames share their unchanged node and edge
 * objects, so identity settles almost every pair and JSON only the rest.
 */
function sameGraphScene(left: GraphScene, right: GraphScene) {
  const same = <T,>(a: readonly T[], b: readonly T[]) => a.length === b.length
    && a.every((item, index) => item === b[index] || JSON.stringify(item) === JSON.stringify(b[index]));
  return same(left.nodes, right.nodes) && same(left.edges, right.edges);
}

/** Resolve the visible frame and event preview against the interval catalog. */
export function renderHistoryScene(catalog: GraphScene, frame: GraphScene, eventFrame: GraphScene, event: GraphTimelineEvent | undefined,
  showFuture: boolean, actionMode: boolean, preview = false, caption?: string): GraphRenderScene {
  const nodes = new Map(frame.nodes.map((node) => [node.id, node]));
  const seedIds = new Set(catalog.nodes.filter((node) => node.is_seed).map((node) => node.id));
  const edges = new Map(frame.edges.map((edge) => [edge.id, edge]));
  const rawEventEdges = new Set([...(event?.edge_ids ?? []), ...(event?.edge_snapshots ?? []).map((edge) => edge.id)]);
  // Every event uses its own filtered frame: before a removal, after other
  // operations. The replay cursor and interval catalog may hold different
  // metadata for the same stable ID, so neither can authorize a preview.
  const removalEvent = event?.operation === "edge_removed";
  const eventEdges = new Map(eventFrame.edges.filter((edge) => rawEventEdges.has(edge.id)).map((edge) => [edge.id, edge]));
  const eventNodeIds = new Set(removalEvent ? [] : [...(event?.node_ids ?? []), ...(event?.node_snapshots ?? []).map((node) => node.id)]);
  for (const edge of eventEdges.values()) {
    eventNodeIds.add(edge.source); eventNodeIds.add(edge.target);
  }
  const eventNodes = new Map(eventFrame.nodes.filter((node) => eventNodeIds.has(node.id)).map((node) => [node.id, node]));
  // Removal ghosts stay outside active counts, exports, and accessible lists.
  const ghostNodes = new Set(removalEvent ? eventNodes.keys() : []);
  return {
    nodes: catalog.nodes.map((node) => ({
      ...(((removalEvent || preview) ? eventNodes.get(node.id) : undefined) ?? nodes.get(node.id) ?? node),
      hidden: !nodes.has(node.id) && !ghostNodes.has(node.id) && !showFuture && !(preview && eventNodes.has(node.id)),
      muted: !nodes.has(node.id) && !ghostNodes.has(node.id),
      highlighted: eventNodes.has(node.id),
    })),
    edges: catalog.edges.map((edge) => {
      const eventEdge = eventEdges.get(edge.id);
      const removed = removalEvent && Boolean(eventEdge);
      const displayed = ((removed || preview) ? eventEdge : undefined) ?? edges.get(edge.id) ?? edge;
      return {
        ...displayed,
        hidden: !edges.has(edge.id) && !removed && !showFuture && !(preview && eventEdge),
        muted: (!edges.has(edge.id) && !removed) || actionMode && !displayed.action_kind && !eventEdge,
        highlighted: eventEdges.has(edge.id),
        eventCaption: eventEdge && (seedIds.has(displayed.source) || seedIds.has(displayed.target)) ? caption : undefined,
        removed,
        label: eventEdges.has(edge.id) && event ? caption ?? displayed.label : displayed.label,
      };
    }),
  };
}
