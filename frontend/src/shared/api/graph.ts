import { apiRequest } from "./client";
import type { components } from "./types.gen";

export type GraphBudget = components["schemas"]["GraphBudget"];
export type GraphCapabilities = components["schemas"]["GraphCapabilities"];
export type GraphEdge = components["schemas"]["GraphEdge"];
export type GraphEvidenceRef = components["schemas"]["GraphEvidenceRef"];
export type GraphFacets = components["schemas"]["GraphFacets"];
export type GraphHistoryMetadata = components["schemas"]["GraphHistoryMetadata"];
export type GraphNode = components["schemas"]["GraphNode"];
export type GraphQueryPayload = components["schemas"]["GraphQueryPayload"];
export type GraphQueryRequest = components["schemas"]["GraphQueryRequest"];
export type GraphSeed = components["schemas"]["GraphSeed"];
export type GraphTimelineEvent = components["schemas"]["GraphTimelineEvent"];

export type GraphHistoryMode = NonNullable<GraphQueryRequest["time"]>["mode"];
export type GraphDepth = 1 | 2;
export type GraphNodeFamily = GraphNode["family"];
export type GraphLayer = GraphEdge["layer"];

export async function queryGraph(
  workspaceId: string,
  request: GraphQueryRequest,
  init: Pick<RequestInit, "signal"> = {},
) {
  const response = await apiRequest<{ data: GraphQueryPayload }>(
    `/api/v1/workspaces/${workspaceId}/graph/query`,
    {
      method: "POST",
      body: JSON.stringify(request),
      signal: init.signal,
    },
  );
  return response.data;
}

export function contextGraphQuery(
  pageId: string,
  mode: GraphHistoryMode = "current",
  depth: GraphDepth = 2,
): GraphQueryRequest {
  return {
    schema_version: "dtxwiki.graph-query.v1",
    seeds: [{ kind: "context_page", id: pageId }],
    scope: "local",
    preset: "all",
    layers: ["knowledge", "evidence", "activity", "action"],
    node_families: [],
    node_kinds: [],
    edge_kinds: [],
    action_kinds: [],
    direction: "both",
    // Historical observation modes retain their one-hop contract until their
    // own depth-two scene semantics are implemented server-side.
    depth: mode === "current" ? depth : 1,
    time: { mode },
    budget: mode === "current"
      ? { nodes: 250, edges: 750 }
      : { nodes: 50, edges: 150 },
    cursor: null,
  };
}
