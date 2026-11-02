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
export function renderHistoryScene(catalog: GraphScene, frame: GraphScene, previousFrame: GraphScene, event: GraphTimelineEvent | undefined,
  showFuture: boolean, actionMode: boolean, preview = false, caption?: string): GraphRenderScene {
  const nodes = new Map(frame.nodes.map((node) => [node.id, node]));
  const previousNodes = new Map(previousFrame.nodes.map((node) => [node.id, node]));
  const seedIds = new Set(catalog.nodes.filter((node) => node.is_seed).map((node) => node.id));
  const edges = new Map(frame.edges.map((edge) => [edge.id, edge]));
  const rawEventEdges = new Set([...(event?.edge_ids ?? []), ...(event?.edge_snapshots ?? []).map((edge) => edge.id)]);
  // Removed relationships are a separate ghost overlay, absent from frame
  // counts, exports, and the accessible list of active relationships.
  // The filtered pre-removal frame owns both their metadata and visibility.
  // The interval catalog may contain a later re-add of the same stable ID.
  const removalEvent = event?.operation === "edge_removed";
  const removed = new Map((removalEvent
    ? previousFrame.edges.filter((edge) => rawEventEdges.has(edge.id))
    : []).map((edge) => [edge.id, edge]));
  const eventEdges = removalEvent ? new Set(removed.keys()) : rawEventEdges;
  const eventNodes = new Set(removalEvent ? [] : [...(event?.node_ids ?? []), ...(event?.node_snapshots ?? []).map((node) => node.id)]);
  for (const edge of removalEvent ? removed.values() : catalog.edges) {
    if (eventEdges.has(edge.id)) { eventNodes.add(edge.source); eventNodes.add(edge.target); }
  }
  const ghostNodes = new Set([...removed.values()].flatMap((edge) => [edge.source, edge.target]));
  return {
    nodes: catalog.nodes.map((node) => ({
      ...((ghostNodes.has(node.id) ? previousNodes.get(node.id) : undefined) ?? nodes.get(node.id) ?? node),
      hidden: !nodes.has(node.id) && !ghostNodes.has(node.id) && !showFuture && !(preview && eventNodes.has(node.id)),
      muted: !nodes.has(node.id) && !ghostNodes.has(node.id),
      highlighted: eventNodes.has(node.id),
    })),
    edges: catalog.edges.map((edge) => {
      const displayed = removed.get(edge.id) ?? edges.get(edge.id) ?? edge;
      return {
        ...displayed,
        hidden: !edges.has(edge.id) && !removed.has(edge.id) && !showFuture && !(preview && eventEdges.has(edge.id)),
        muted: (!edges.has(edge.id) && !removed.has(edge.id)) || actionMode && !edge.action_kind && !eventEdges.has(edge.id),
        highlighted: eventEdges.has(edge.id),
        eventCaption: eventEdges.has(edge.id) && (seedIds.has(edge.source) || seedIds.has(edge.target)) ? caption : undefined,
        removed: removed.has(edge.id),
        layoutCount: edge.count ?? 1,
        label: eventEdges.has(edge.id) && event ? caption ?? displayed.label : displayed.label,
      };
    }),
  };
}
