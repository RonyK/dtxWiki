import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphNode } from "../../../shared/api/graph";
import { buildSceneNavigation } from "../sceneNavigation";
import { graphFixture } from "./graphFixture";

const base = graphFixture();
function node(id: string, folder = false): GraphNode {
  return {
    ...base.nodes[0], id, entity_id: id, label: id, is_seed: false,
    entity_kind: folder ? "workspace_tree_node" : "context_page",
    family: folder ? "structural" : "document",
  };
}
function edge(id: string, source: string, target: string, kind = "wikilink"): GraphEdge {
  return { ...base.edges[0], id, source, target, edge_kind: kind, label: kind };
}
const ids = (nodes: GraphNode[] | undefined) => nodes?.map(({ id }) => id) ?? [];

describe("Local scene navigation", () => {
  it("uses only rendered direct neighbors, including inbound and undirected relations", () => {
    const nodes = [node("seed"), node("incoming"), node("outgoing"), node("together"), node("hop2"), node("unknown")];
    const result = buildSceneNavigation({ nodes, edges: [
      edge("incoming", "incoming", "seed"),
      edge("outgoing", "seed", "outgoing", "citation_page"),
      { ...edge("together", "seed", "together", "co_access"), direction: "undirected" },
      edge("parallel", "seed", "outgoing", "published"),
      edge("hop2", "outgoing", "hop2"),
      edge("unknown", "seed", "unknown", "future_private_kind"),
      edge("missing", "seed", "absent"),
      edge("self", "seed", "seed"),
    ] });
    expect(ids(result.neighborsByNode.get("seed"))).toEqual(["incoming", "outgoing", "together"]);
    expect(result.neighborsByNode.get("seed")?.[0]).toBe(nodes[1]);
    expect(ids(result.neighborsByNode.get("hop2"))).toEqual(["outgoing"]);
    expect(ids(result.neighborsByNode.get("unknown"))).toEqual([]);
  });

  it("lists direct scene membership and preserves visible secondary placements without guessing primary", () => {
    const nodes = [node("primary", true), node("secondary", true), node("nested", true), node("page"), node("nested-page")];
    const result = buildSceneNavigation({ nodes, edges: [
      edge("primary", "primary", "page", "tree"),
      edge("secondary", "secondary", "page", "tree"),
      edge("duplicate", "secondary", "page", "tree"),
      edge("nested", "primary", "nested", "tree"),
      edge("nested-page", "nested", "nested-page", "tree"),
    ] });
    expect(ids(result.membersByFolder.get("primary"))).toEqual(["page", "nested"]);
    expect(ids(result.membersByFolder.get("secondary"))).toEqual(["page"]);
    expect(ids(result.membersByFolder.get("nested"))).toEqual(["nested-page"]);
    expect(ids(result.foldersByMember.get("page"))).toEqual(["primary", "secondary"]);
    expect(ids(result.foldersByMember.get("nested"))).toEqual(["primary"]);
    expect(result).not.toHaveProperty("primaryFolder");
  });

  it("cannot retain removed endpoints, relationships, or recursive descendants", () => {
    const folder = node("folder", true);
    const child = node("child", true);
    const page = node("page");
    const edges = [edge("child", "folder", "child", "tree"), edge("page", "child", "page", "tree")];
    expect(ids(buildSceneNavigation({ nodes: [folder, child, page], edges }).membersByFolder.get("folder")))
      .toEqual(["child"]);
    const withoutChild = buildSceneNavigation({ nodes: [folder, page], edges });
    expect(ids(withoutChild.membersByFolder.get("folder"))).toEqual([]);
    expect(ids(withoutChild.neighborsByNode.get("page"))).toEqual([]);
    expect(ids(buildSceneNavigation({ nodes: [folder, child, page], edges: [] }).membersByFolder.get("folder")))
      .toEqual([]);
  });

  it("ignores semantic self-links and does not turn non-tree relations into membership", () => {
    const folder = node("folder", true);
    const alias = { ...node("alias"), entity_id: "page" };
    const result = buildSceneNavigation({ nodes: [folder, node("page"), alias], edges: [
      edge("self-alias", "page", "alias"),
      edge("citation", "folder", "page", "citation_page"),
      edge("reverse", "page", "folder", "tree"),
    ] });
    expect(ids(result.neighborsByNode.get("alias"))).toEqual([]);
    expect(ids(result.membersByFolder.get("folder"))).toEqual([]);
    expect(ids(result.foldersByMember.get("page"))).toEqual([]);
  });

  it("indexes a full Local budget in one node and one edge pass without mutating the scene", () => {
    const nodes = Object.freeze(Array.from({ length: 250 }, (_, index) => Object.freeze(node(`node:${index}`))));
    const edges = Object.freeze(Array.from({ length: 750 }, (_, index) => Object.freeze(
      edge(`edge:${index}`, `node:${index % 250}`, `node:${(index + 1) % 250}`),
    )));
    let nodeReads = 0;
    let edgeReads = 0;
    const observedNodes = new Proxy(nodes, { get(target, property, receiver) {
      if (typeof property === "string" && /^\d+$/.test(property)) nodeReads++;
      return Reflect.get(target, property, receiver);
    } });
    const observedEdges = new Proxy(edges, { get(target, property, receiver) {
      if (typeof property === "string" && /^\d+$/.test(property)) edgeReads++;
      return Reflect.get(target, property, receiver);
    } });
    const result = buildSceneNavigation({
      nodes: observedNodes as unknown as GraphNode[],
      edges: observedEdges as unknown as GraphEdge[],
    });
    expect(result.neighborsByNode.size).toBe(250);
    expect(ids(result.neighborsByNode.get("node:0"))).toEqual(["node:1", "node:249"]);
    expect(nodeReads).toBe(250);
    expect(edgeReads).toBe(750);
    expect(nodes[0].label).toBe("node:0");
  });
});
