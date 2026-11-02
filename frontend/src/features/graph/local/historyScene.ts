import type { GraphQueryPayload, GraphTimelineEvent } from "../../../shared/api/graph";
import { deriveGraphScene, sortedTimeline, type GraphFilters, type GraphScene } from "../graphModel";
import type { GraphRenderScene } from "../shared/rendererTypes";

/** Only the actor-authorized, filtered interval is allowed into the layout. */
export function historySceneCatalog(payload: GraphQueryPayload, filters: GraphFilters): GraphScene {
  const nodes = new Map<string, GraphScene["nodes"][number]>();
  const edges = new Map<string, GraphScene["edges"][number]>();
  for (const cursor of [-1, ...sortedTimeline(payload).map((_, index) => index), null]) {
    const scene = deriveGraphScene(payload, filters, cursor);
    scene.nodes.forEach((node) => nodes.set(node.id, node));
    scene.edges.forEach((edge) => edges.set(edge.id, edge));
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

/** Playback changes presentation, never the interval's layout topology. */
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
        layoutCount: edge.count ?? 1,
        label: eventEdges.has(edge.id) && event ? caption ?? displayed.label : displayed.label,
      };
    }),
  };
}
