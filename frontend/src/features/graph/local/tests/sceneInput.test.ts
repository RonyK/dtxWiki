import { describe, expect, it } from "vitest";

import type { GraphRenderEdge, GraphRenderNode, GraphRenderScene } from "../../shared/rendererTypes";
import { buildLocalScene } from "../sceneInput";

function node(id: string, entityId: string, isSeed = false): GraphRenderNode {
  return {
    id,
    entity_kind: "context_page",
    entity_id: entityId,
    family: "document",
    label: id,
    is_seed: isSeed,
  };
}

function edge(id: string, source: string, target: string, edgeKind = "wikilink", count = 1): GraphRenderEdge {
  return {
    id,
    source,
    target,
    label: edgeKind,
    layer: "knowledge",
    edge_kind: edgeKind,
    direction: "directed",
    count,
  };
}

function scene(nodes: GraphRenderNode[], edges: GraphRenderEdge[]): GraphRenderScene {
  return { nodes, edges };
}

describe("visible Local scene input", () => {
  it("uses stable entity keys while preserving graph IDs only for UI selection", () => {
    const result = buildLocalScene(scene(
      [node("projection:A", "a", true), node("projection:B", "b")],
      [edge("ephemeral-edge-id", "projection:A", "projection:B")],
    ));
    expect(result.keyByGraphId.get("projection:A")).toBe("context_page:a");
    expect(result.graphIdByKey.get("context_page:b")).toBe("projection:B");
    expect(result.layout.nodes.map(({ key }) => key)).toEqual(["context_page:a", "context_page:b"]);
    expect(result.layout.nodes[0].isSeed).toBe(true);
    expect(result.layout.edges).toEqual([{
      sourceKey: "context_page:a",
      targetKey: "context_page:b",
      kind: "link",
      count: 1,
    }]);
    expect(JSON.stringify(result.layout)).not.toContain("ephemeral-edge-id");
  });

  it("never lets an absent endpoint or unknown relationship alter layout", () => {
    const visible = scene(
      [node("a", "a", true), node("b", "b")],
      [edge("visible", "a", "b")],
    );
    const withHiddenDecoys = scene(visible.nodes, [
      ...visible.edges,
      edge("hidden-target", "a", "hidden"),
      edge("unknown-kind", "a", "b", "future_private_kind"),
    ]);
    expect(buildLocalScene(withHiddenDecoys).layout).toEqual(buildLocalScene(visible).layout);
    expect(buildLocalScene(withHiddenDecoys).edges).toHaveLength(1);
  });

  it("keeps genuine backend action occurrences in both canvas edges and force topology", () => {
    const actions = ["created", "edited", "published", "included", "cited"];
    const sourceByAction = { created: "v1", edited: "v2", published: "audit", included: "ask", cited: "ask" };
    const input = scene(
      [
        node("seed", "seed", true),
        { ...node("v1", "version-1"), entity_kind: "context_page_version" },
        { ...node("v2", "version-2"), entity_kind: "context_page_version" },
        { ...node("audit", "audit-1"), entity_kind: "audit_event", family: "event" },
        { ...node("ask", "ask-1"), entity_kind: "ask_turn", family: "ask" },
      ],
      actions.map((kind) => ({
        ...edge(`action:${kind}`, sourceByAction[kind as keyof typeof sourceByAction], "seed", kind),
        action_kind: kind,
        layer: kind === "cited" ? "evidence" : "action",
      })),
    );
    const result = buildLocalScene(input);
    expect(result.edges.map(({ graphId }) => graphId).sort())
      .toEqual(input.edges.map(({ id }) => id).sort());
    expect(result.edges.every(({ kind }) => kind === "action")).toBe(true);
    expect(result.layout.edges).toHaveLength(4);
    expect(result.layout.edges.reduce((count, edge) => count + edge.count, 0)).toBe(5);
    expect(result.layout.edges.find(({ sourceKey }) => sourceKey === "ask_turn:ask-1"))
      .toEqual({ kind: "action", sourceKey: "ask_turn:ask-1", targetKey: "context_page:seed", count: 2 });
  });

  it("does not disguise stored citations as actions or infer unknown kinds from layer/action metadata", () => {
    const result = buildLocalScene(scene(
      [node("seed", "seed", true), node("event", "event")],
      [
        { ...edge("stored-citation", "event", "seed", "ask_citation"), action_kind: "included", layer: "evidence" },
        { ...edge("future-event", "event", "seed", "unknown_future_action"), action_kind: "created", layer: "action" },
      ],
    ));
    expect(result.edges).toMatchObject([{ graphId: "stored-citation", kind: "citation" }]);
    expect(result.edges).toHaveLength(1);
  });

  it("is deterministic under node/edge permutation and projection ID changes", () => {
    const first = scene(
      [node("a1", "a", true), node("b1", "b"), node("c1", "c")],
      [edge("row1", "a1", "b1"), edge("row2", "b1", "c1", "co_access", 3)],
    );
    const second = scene(
      [node("c2", "c"), node("b2", "b"), node("a2", "a", true)],
      [edge("new-row2", "b2", "c2", "co_access", 3), edge("new-row1", "a2", "b2")],
    );
    expect(buildLocalScene(second).layout).toEqual(buildLocalScene(first).layout);
  });

  it("aggregates visible occurrence counts without depending on edge UUIDs", () => {
    const result = buildLocalScene(scene(
      [node("a", "a"), node("b", "b")],
      [edge("one", "a", "b", "citation_page", 2), edge("two", "a", "b", "citation_page", 4)],
    ));
    expect(result.layout.edges).toEqual([{
      sourceKey: "context_page:a",
      targetKey: "context_page:b",
      kind: "citation",
      count: 6,
    }]);
    expect(result.layout.nodes[0].radiusPx).toBeGreaterThan(3);
  });
});

describe("Local wire geometry identity", () => {
  const original = scene(
    [node("a", "a", true), node("b", "b"), node("c", "c")],
    [edge("ab", "a", "b", "citation_page"), edge("bc", "b", "c", "co_access", 3)],
  );
  const copy = (): GraphRenderScene => ({
    nodes: original.nodes.map((entry) => ({ ...entry })),
    edges: original.edges.map((entry) => ({ ...entry })),
  });

  it("retains exact wire geometry across cloned, reordered presentation metadata", () => {
    const next = copy();
    next.nodes.reverse();
    next.edges.reverse();
    next.nodes = next.nodes.map((entry) => ({ ...entry, label: `New ${entry.id}`, status: "draft" }));
    next.edges = next.edges.map((entry) => ({ ...entry, label: `New ${entry.id}`, layer: "evidence" }));
    const before = buildLocalScene(original);
    const after = buildLocalScene(next);
    expect(before.geometryKey).toEqual(expect.any(String));
    expect(after.geometryKey).toBe(before.geometryKey);
    expect(after.layout).toEqual(before.layout);
    expect(after.nodes.find(({ graphId }) => graphId === "b"))
      .toMatchObject({ label: "New b", status: "draft" });
  });

  it.each([
    ["node wire ID", (next: GraphRenderScene) => {
      next.nodes[0].id = "new-a";
      next.edges[0].source = "new-a";
    }],
    ["edge wire ID", (next: GraphRenderScene) => { next.edges[0].id = "new-ab"; }],
    ["node semantic identity", (next: GraphRenderScene) => { next.nodes[1].entity_id = "new-b"; }],
    ["seed", (next: GraphRenderScene) => { next.nodes[1].is_seed = true; }],
    ["endpoint", (next: GraphRenderScene) => { next.edges[0].target = "c"; }],
    ["raw kind with the same visual kind", (next: GraphRenderScene) => { next.edges[0].edge_kind = "citation_source"; }],
    ["visual kind", (next: GraphRenderScene) => { next.edges[0].edge_kind = "tree"; }],
    ["direction", (next: GraphRenderScene) => { next.edges[0].direction = "undirected"; }],
    ["occurrence count and derived radius", (next: GraphRenderScene) => { next.edges[0].count = 5; }],
  ] as const)("distinguishes a changed %s", (_name, mutate) => {
    const next = copy();
    mutate(next);
    expect(buildLocalScene(next).geometryKey).not.toBe(buildLocalScene(original).geometryKey);
  });

  it("distinguishes reversed undirected wire endpoints even when force inputs agree", () => {
    const first = copy();
    first.edges[0].direction = "undirected";
    const second = { ...first, edges: first.edges.map((entry) => ({ ...entry })) };
    [second.edges[0].source, second.edges[0].target] = [second.edges[0].target, second.edges[0].source];
    expect(buildLocalScene(second).layout).toEqual(buildLocalScene(first).layout);
    expect(buildLocalScene(second).geometryKey).not.toBe(buildLocalScene(first).geometryKey);
  });

  it("distinguishes occurrence counts after node radius reaches its cap", () => {
    const first = copy();
    first.edges[0].count = 100;
    const second = { ...first, edges: first.edges.map((entry) => ({ ...entry })) };
    second.edges[0].count = 101;
    expect(buildLocalScene(second).layout.nodes).toEqual(buildLocalScene(first).layout.nodes);
    expect(buildLocalScene(second).geometryKey).not.toBe(buildLocalScene(first).geometryKey);
  });

  it.each([
    ["duplicate semantic nodes", (next: GraphRenderScene) => { next.nodes.push({ ...next.nodes[1], id: "alias-b" }); }],
    ["duplicate node wire IDs", (next: GraphRenderScene) => { next.nodes.push({ ...next.nodes[1], entity_id: "alias-b" }); }],
    ["duplicate edge wire IDs", (next: GraphRenderScene) => { next.edges.push({ ...next.edges[0] }); }],
    ["missing entity identity", (next: GraphRenderScene) => { delete next.nodes[1].entity_id; }],
  ] as const)("does not admit a fast path for %s", (_name, mutate) => {
    const next = copy();
    mutate(next);
    expect(buildLocalScene(next).geometryKey).toBeNull();
  });

  it("keeps hidden endpoint and unknown-kind decoys outside admitted geometry", () => {
    const next = copy();
    next.edges.push(edge("hidden", "a", "hidden"), edge("unknown", "a", "b", "future_private_kind"));
    expect(buildLocalScene(next).geometryKey).toBe(buildLocalScene(original).geometryKey);
  });
});
