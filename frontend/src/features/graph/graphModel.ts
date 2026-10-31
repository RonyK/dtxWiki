import type { TFunction } from "i18next";

import type {
  GraphEdge,
  GraphLayer,
  GraphNode,
  GraphNodeFamily,
  GraphQueryPayload,
  GraphTimelineEvent,
} from "../../shared/api/graph";
import type {
  GraphNeighborEntry,
  GraphNeighborsPayload,
} from "../../shared/api/operations";
import { formatVersionTimestamp } from "../../shared/lib/datetime";

export type GraphFilters = {
  nodeFamilies: GraphNodeFamily[];
  layers: GraphLayer[];
  actionKinds: string[];
  edgeKinds: string[];
  directions: GraphRelativeDirection[];
  statuses: string[];
  provenances: GraphEdge["provenance"][];
};

export type GraphRelativeDirection = "in" | "out";

export type GraphActionFamily =
  | "authoring"
  | "governance"
  | "retrieval"
  | "tool"
  | "other";

export type GraphScene = {
  nodes: GraphNode[];
  edges: GraphEdge[];
};

export function graphFacets(payload: GraphQueryPayload) {
  const events = sortedTimeline(payload);
  const allNodes = mergeById(
    events.flatMap(timelineNodeSnapshots),
    payload.nodes,
  );
  const allEdges = mergeById(
    events.flatMap(timelineEdgeSnapshots),
    payload.edges,
  );
  return {
    nodeFamilies: uniqueSorted(
      [
        ...(payload.facets.node_families ?? []),
        ...allNodes.map((node) => node.family),
      ]
        .filter(isGraphNodeFamily),
    ),
    layers: uniqueSorted(
      [
        ...(payload.facets.layers ?? []),
        ...allEdges.map((edge) => edge.layer),
      ]
        .filter(isGraphLayer),
    ),
    actionKinds: uniqueSorted(
      [
        ...(payload.facets.action_kinds ?? []),
        ...allEdges.flatMap((edge) => edge.action_kind ? [edge.action_kind] : []),
      ],
    ),
    edgeKinds: uniqueSorted(
      [
        ...(payload.facets.edge_kinds ?? []),
        ...allEdges.map((edge) => edge.edge_kind),
      ],
    ),
    directions: uniqueSorted(allEdges.flatMap(
      (edge) => relativeDirections(edge, allNodes),
    )),
    statuses: uniqueSorted(
      [
        ...(payload.facets.statuses ?? []),
        ...allNodes.map((node) => node.status ?? "unspecified"),
      ],
    ),
    provenances: uniqueSorted(allEdges.map((edge) => edge.provenance)),
  } satisfies GraphFilters;
}

export function deriveGraphScene(
  payload: GraphQueryPayload,
  filters: GraphFilters,
  timelineCursor: number | null,
): GraphScene {
  const timelineScene = deriveTimelineScene(payload, timelineCursor);
  const familyFilter = new Set(filters.nodeFamilies);
  const layerFilter = new Set(filters.layers);
  const actionFilter = new Set(filters.actionKinds);
  const edgeKindFilter = new Set(filters.edgeKinds);
  const directionFilter = new Set(filters.directions);
  const statusFilter = new Set(filters.statuses);
  const provenanceFilter = new Set(filters.provenances);
  const seedIds = new Set(payload.nodes.filter((node) => node.is_seed).map((node) => node.id));

  const nodes = timelineScene.nodes.filter(
    (node) =>
      familyFilter.has(node.family)
      && statusFilter.has(node.status ?? "unspecified"),
  );
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = timelineScene.edges.filter((edge) =>
    nodeIds.has(edge.source)
      && nodeIds.has(edge.target)
      && layerFilter.has(edge.layer)
      && (!edge.action_kind || actionFilter.has(edge.action_kind))
      && edgeKindFilter.has(edge.edge_kind)
      && relativeDirections(edge, payload.nodes, seedIds)
        .some((direction) => directionFilter.has(direction))
      && provenanceFilter.has(edge.provenance),
  );
  return {
    nodes: stableNodes(nodes),
    edges: stableEdges(edges),
  };
}

export function sortedTimeline(payload: GraphQueryPayload) {
  return [...(payload.timeline ?? [])].sort(compareTimelineEvents);
}

export function compareTimelineEvents(
  left: GraphTimelineEvent,
  right: GraphTimelineEvent,
) {
  return left.occurred_at.localeCompare(right.occurred_at)
    || left.sequence - right.sequence
    || left.id.localeCompare(right.id);
}

export function graphActionFamily(actionKind?: string | null): GraphActionFamily {
  if ([
    "created",
    "edited",
    "published",
    "removed",
    "restored",
    "superseded",
    "promoted",
  ].includes(actionKind ?? "")) return "authoring";
  if ([
    "reviewed",
    "approved",
    "rejected",
    "request_changes",
  ].includes(actionKind ?? "")) return "governance";
  if ([
    "searched",
    "returned",
    "retrieved",
    "included",
    "cited",
    "referenced",
  ].includes(actionKind ?? "")) return "retrieval";
  if (actionKind === "tool_produced") return "tool";
  return "other";
}

export type CoAccessEvidence = {
  sessions: number;
  days: number;
  lastObservedAt: string;
};

const DAY_MS = 86_400_000;

/**
 * What a "read together" edge rests on: how many sessions read both
 * documents close together, and over how many days. Null for every other
 * kind, and for a co_access edge from a server that does not send it.
 */
export function coAccessEvidence(edge: GraphEdge): CoAccessEvidence | null {
  if (edge.edge_kind !== "co_access") return null;
  const sessions = edge.support_session_count;
  const firstObservedAt = edge.first_observed_at;
  const lastObservedAt = edge.occurred_at;
  if (!sessions || !firstObservedAt || !lastObservedAt) return null;
  const span = Date.parse(lastObservedAt) - Date.parse(firstObservedAt);
  if (Number.isNaN(span)) return null;
  // Started days, so two reads an hour apart are "over 1 day", not 0.
  return {
    sessions,
    days: Math.max(1, Math.ceil(span / DAY_MS)),
    lastObservedAt,
  };
}

/** "Read together in 3 sessions over 12 days (last 2026-08-24)". */
export function coAccessSentence(
  edge: GraphEdge,
  t: TFunction<"graph">,
): string | null {
  const evidence = coAccessEvidence(edge);
  if (!evidence) return null;
  return t("graph.coAccess.sentence", {
    sessions: t("graph.coAccess.sessions", { count: evidence.sessions }),
    days: t("graph.coAccess.days", { count: evidence.days }),
    // The date part of the timestamp the rest of the graph shows.
    last: formatVersionTimestamp(evidence.lastObservedAt).split(" ")[0],
  });
}

export function graphPayloadToNeighbors(
  payload: GraphQueryPayload | null,
): GraphNeighborsPayload | null {
  if (!payload) {
    return null;
  }
  const seedIds = new Set(payload.nodes.filter((node) => node.is_seed).map((node) => node.id));
  const nodes = new Map(payload.nodes.map((node) => [node.id, node]));
  const groups: Required<Pick<
    GraphNeighborsPayload,
    "backlinks" | "outbound" | "citations" | "co_access"
  >> = {
    backlinks: [],
    outbound: [],
    citations: [],
    co_access: [],
  };
  // A neighbour listed twice under one key keeps its higher rank.
  const ranks = new Map<string, number>();
  const keyOf = (entry: GraphNeighborEntry) =>
    `${entry.kind}:${entry.id}:${entry.edge_kind}:${entry.direction}`;
  for (const edge of stableEdges(payload.edges)) {
    if (!LEGACY_INSPECTOR_EDGE_KINDS.has(edge.edge_kind)) {
      continue;
    }
    const sourceIsSeed = seedIds.has(edge.source);
    const targetIsSeed = seedIds.has(edge.target);
    if (!sourceIsSeed && !targetIsSeed) {
      continue;
    }
    const neighbor = nodes.get(sourceIsSeed ? edge.target : edge.source);
    if (!neighbor || neighbor.is_seed) {
      continue;
    }
    const entry: GraphNeighborEntry = {
      kind: neighbor.entity_kind,
      id: neighbor.entity_id,
      title: neighbor.label,
      path: neighbor.path ?? null,
      edge_kind: edge.edge_kind,
      direction: sourceIsSeed ? "out" : "in",
    };
    const group = edge.edge_kind === "co_access"
      ? groups.co_access
      : edge.edge_kind === "citation_page" || edge.edge_kind === "citation_source"
        ? groups.citations
        : sourceIsSeed
          ? groups.outbound
          : groups.backlinks;
    const key = keyOf(entry);
    const known = ranks.get(key);
    if (known === undefined) {
      group.push(entry);
    }
    ranks.set(key, Math.max(known ?? 0, edge.rank_score));
  }
  // Highest rank first, so the slice keeps the strongest relations; the sort
  // is stable, so equal ranks keep the stableEdges order.
  const byRank = (entries: GraphNeighborEntry[]) =>
    [...entries].sort(
      (left, right) => (ranks.get(keyOf(right)) ?? 0) - (ranks.get(keyOf(left)) ?? 0),
    );
  return {
    backlinks: byRank(groups.backlinks).slice(0, 10),
    outbound: byRank(groups.outbound).slice(0, 10),
    citations: byRank(groups.citations).slice(0, 5),
    co_access: byRank(groups.co_access).slice(0, 10),
    mermaid: payload.mermaid ?? null,
    truncated: payload.truncated,
    warnings: [...(payload.warnings ?? [])],
  };
}

const LEGACY_INSPECTOR_EDGE_KINDS = new Set([
  "wikilink",
  "citation_page",
  "citation_source",
  "co_access",
]);

export function graphSceneToMermaid(scene: GraphScene) {
  const nodeNames = new Map(
    stableNodes(scene.nodes).map((node, index) => [node.id, `n${index}`]),
  );
  const lines = ["flowchart LR"];
  for (const node of stableNodes(scene.nodes)) {
    lines.push(`  ${nodeNames.get(node.id)}["${mermaidText(node.label)}"]`);
  }
  for (const edge of stableEdges(scene.edges)) {
    const source = nodeNames.get(edge.source);
    const target = nodeNames.get(edge.target);
    if (!source || !target) {
      continue;
    }
    const connector = edge.direction === "undirected" ? "---" : "-->";
    lines.push(`  ${source} ${connector}|${mermaidText(edge.label)}| ${target}`);
  }
  return lines.join("\n");
}

function deriveTimelineScene(
  payload: GraphQueryPayload,
  cursor: number | null,
): GraphScene {
  const events = sortedTimeline(payload);
  if (cursor === null || events.length === 0) {
    return { nodes: payload.nodes, edges: payload.edges };
  }
  const eventNodeIds = new Set(events.flatMap((event) => [
    ...(event.node_ids ?? []),
    ...timelineNodeSnapshots(event).map((node) => node.id),
  ]));
  const eventEdgeIds = new Set(events.flatMap((event) => [
    ...(event.edge_ids ?? []),
    ...timelineEdgeSnapshots(event).map((edge) => edge.id),
  ]));
  // Replay catalogs begin at the current projection and absorb snapshots only
  // when their event is reached. Preloading every snapshot would let metadata
  // from a future remove/re-add cycle leak into an earlier frame that shares
  // the same deterministic entity ID.
  const nodeCatalog = new Map(payload.nodes.map((node) => [node.id, node]));
  const edgeCatalog = new Map(payload.edges.map((edge) => [edge.id, edge]));
  // Current projection facts without a corresponding observation are an
  // honest partial-history baseline. Keeping them visible from frame zero
  // prevents an unexplained bulk jump in the final frame.
  const baselineEdges = payload.edges.filter((edge) => !eventEdgeIds.has(edge.id));
  const visibleNodes = new Set(
    payload.nodes
      .filter((node) => node.is_seed || !eventNodeIds.has(node.id))
      .map((node) => node.id),
  );
  const visibleEdges = new Set(baselineEdges.map((edge) => edge.id));
  for (const edge of baselineEdges) {
    visibleNodes.add(edge.source);
    visibleNodes.add(edge.target);
  }
  for (const event of events.slice(0, Math.max(0, cursor + 1))) {
    for (const snapshot of timelineNodeSnapshots(event)) {
      nodeCatalog.set(snapshot.id, snapshot);
    }
    for (const nodeId of event.node_ids ?? []) {
      if (nodeCatalog.has(nodeId)) visibleNodes.add(nodeId);
    }
    for (const snapshot of timelineEdgeSnapshots(event)) {
      edgeCatalog.set(snapshot.id, snapshot);
    }
    const eventEdges = new Set([
      ...(event.edge_ids ?? []),
      ...timelineEdgeSnapshots(event).map((edge) => edge.id),
    ]);
    for (const edgeId of eventEdges) {
      if (event.operation === "edge_removed") {
        visibleEdges.delete(edgeId);
      } else {
        visibleEdges.add(edgeId);
        const edge = edgeCatalog.get(edgeId);
        if (edge) {
          if (nodeCatalog.has(edge.source)) visibleNodes.add(edge.source);
          if (nodeCatalog.has(edge.target)) visibleNodes.add(edge.target);
        }
      }
    }
  }
  const edges = [...visibleEdges]
    .flatMap((edgeId) => edgeCatalog.get(edgeId) ? [edgeCatalog.get(edgeId)!] : []);
  for (const edge of edges) {
    visibleNodes.add(edge.source);
    visibleNodes.add(edge.target);
  }
  return {
    nodes: [...visibleNodes]
      .flatMap((nodeId) => nodeCatalog.get(nodeId) ? [nodeCatalog.get(nodeId)!] : []),
    edges,
  };
}

function timelineNodeSnapshots(event: GraphTimelineEvent) {
  return event.node_snapshots ?? [];
}

function timelineEdgeSnapshots(event: GraphTimelineEvent) {
  return event.edge_snapshots ?? [];
}

function stableNodes(nodes: GraphNode[]) {
  return [...nodes].sort((left, right) =>
    Number(right.is_seed) - Number(left.is_seed)
      || left.family.localeCompare(right.family)
      || left.label.localeCompare(right.label)
      || left.id.localeCompare(right.id),
  );
}

function stableEdges(edges: GraphEdge[]) {
  return [...edges].sort((left, right) =>
    left.layer.localeCompare(right.layer)
      || left.label.localeCompare(right.label)
      || left.id.localeCompare(right.id),
  );
}

function mergeById<T extends { id: string }>(left: T[], right: T[]) {
  const merged = new Map(left.map((item) => [item.id, item]));
  for (const item of right) {
    merged.set(item.id, item);
  }
  return [...merged.values()];
}

function uniqueSorted<T extends string>(values: T[]) {
  return [...new Set(values)].sort((left, right) => left.localeCompare(right));
}

function relativeDirections(
  edge: GraphEdge,
  nodes: GraphNode[],
  knownSeedIds?: Set<string>,
): GraphRelativeDirection[] {
  const seedIds = knownSeedIds
    ?? new Set(nodes.filter((node) => node.is_seed).map((node) => node.id));
  const sourceIsSeed = seedIds.has(edge.source);
  const targetIsSeed = seedIds.has(edge.target);
  if (edge.direction === "undirected" || sourceIsSeed === targetIsSeed) {
    return ["in", "out"];
  }
  return sourceIsSeed ? ["out"] : ["in"];
}

function mermaidText(value: string) {
  return value
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/\|/g, "&#124;")
    .trim();
}

function isGraphNodeFamily(value: string): value is GraphNodeFamily {
  return ["document", "chat", "ask", "review", "agent", "event", "structural"]
    .includes(value);
}

function isGraphLayer(value: string): value is GraphLayer {
  return ["knowledge", "evidence", "activity", "action"].includes(value);
}
