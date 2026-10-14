import { describe, expect, it } from "vitest";

import { i18n } from "../../../shared/i18n";
import {
  coAccessEvidence,
  coAccessSentence,
  deriveGraphScene,
  graphFacets,
  graphPayloadToNeighbors,
  graphSceneToMermaid,
} from "../graphModel";
import type { GraphEdge, GraphNode, GraphTimelineEvent } from "../../../shared/api/graph";
import { graphFixture } from "./graphFixture";

describe("graphModel", () => {
  it("uses one scene projection to physically remove filtered nodes, edges, and dangling endpoints", () => {
    const payload = graphFixture();
    const all = graphFacets(payload);
    expect(deriveGraphScene(payload, all, null)).toMatchObject({
      nodes: expect.arrayContaining([
        expect.objectContaining({ id: "node:seed" }),
        expect.objectContaining({ id: "node:related" }),
        expect.objectContaining({ id: "node:ask" }),
      ]),
      edges: expect.arrayContaining([
        expect.objectContaining({ id: "edge:wikilink" }),
        expect.objectContaining({ id: "edge:included" }),
      ]),
    });

    const noAsk = deriveGraphScene(payload, {
      ...all,
      nodeFamilies: ["document"],
    }, null);
    expect(noAsk.nodes.map(({ id }) => id)).not.toContain("node:ask");
    expect(noAsk.edges.map(({ id }) => id)).not.toContain("edge:included");

    const onlyObservedUsage = deriveGraphScene(payload, {
      ...all,
      edgeKinds: ["ask_citation"],
      layers: ["evidence"],
      provenances: ["usage"],
      statuses: ["published", "unspecified"],
    }, null);
    expect(onlyObservedUsage.nodes.map(({ id }) => id)).toEqual([
      "node:seed",
      "node:ask",
    ]);
    expect(onlyObservedUsage.edges.map(({ id }) => id)).toEqual(["edge:included"]);

    const noDirection = deriveGraphScene(payload, {
      ...all,
      directions: [],
    }, null);
    expect(noDirection.edges).toEqual([]);
  });

  it("replays stable timeline events without inventing pre-event nodes or edges", () => {
    const payload = graphFixture();
    const filters = graphFacets(payload);
    expect(deriveGraphScene(payload, filters, -1)).toMatchObject({
      nodes: [expect.objectContaining({ id: "node:seed" })],
      edges: [],
    });
    expect(deriveGraphScene(payload, filters, 0).nodes.map(({ id }) => id))
      .toContain("node:related");
    expect(deriveGraphScene(payload, filters, 0).edges).toEqual([]);
    expect(deriveGraphScene(payload, filters, 1).edges.map(({ id }) => id))
      .toEqual(["edge:wikilink"]);
  });

  it("replays historical-only node and edge snapshots without leaking them into the current scene", () => {
    const payload = graphFixture();
    const historicalNode: GraphNode = {
      ...payload.nodes[1],
      id: "node:historical",
      entity_id: "page-historical",
      label: "Removed historical page",
    };
    const historicalEdge: GraphEdge = {
      ...payload.edges[0],
      id: "edge:historical",
      source: payload.nodes[0].id,
      target: historicalNode.id,
    };
    payload.nodes = [payload.nodes[0]];
    payload.edges = [];
    payload.timeline = [
      withSnapshots({
        id: "event:historical-node",
        operation: "node_created",
        action_kind: "created",
        label: "Historical page appeared",
        occurred_at: "2026-07-01T00:00:00Z",
        sequence: 1,
        node_ids: [historicalNode.id],
        edge_ids: [],
        evidence: null,
      }, { nodes: [historicalNode] }),
      withSnapshots({
        id: "event:historical-edge",
        operation: "edge_created",
        action_kind: "referenced",
        label: "Historical link added",
        occurred_at: "2026-07-01T00:01:00Z",
        sequence: 2,
        node_ids: [],
        edge_ids: [historicalEdge.id],
        evidence: null,
      }, { edges: [historicalEdge] }),
      withSnapshots({
        id: "event:historical-edge-removed",
        operation: "edge_removed",
        action_kind: "removed",
        label: "Historical link removed",
        occurred_at: "2026-07-01T00:02:00Z",
        sequence: 3,
        node_ids: [],
        edge_ids: [historicalEdge.id],
        evidence: null,
      }),
    ];

    const filters = graphFacets(payload);
    expect(filters.nodeFamilies).toContain(historicalNode.family);
    expect(filters.edgeKinds).toContain(historicalEdge.edge_kind);
    expect(deriveGraphScene(payload, filters, -1)).toMatchObject({
      nodes: [expect.objectContaining({ id: "node:seed" })],
      edges: [],
    });
    expect(deriveGraphScene(payload, filters, 0).nodes.map(({ id }) => id))
      .toContain(historicalNode.id);
    expect(deriveGraphScene(payload, filters, 1).edges.map(({ id }) => id))
      .toEqual([historicalEdge.id]);
    expect(deriveGraphScene(payload, filters, 2).edges).toEqual([]);
    // A null cursor is the actor-visible current projection. Historical-only
    // snapshots must never leak into it after replay reaches the end.
    expect(deriveGraphScene(payload, filters, null)).toMatchObject({
      nodes: [expect.objectContaining({ id: "node:seed" })],
      edges: [],
    });
  });

  it("keeps observation-free current facts in the partial-history baseline", () => {
    const payload = graphFixture();
    payload.timeline = (payload.timeline ?? [])
      .filter(({ id }) => id !== "event:included");
    const filters = graphFacets(payload);

    expect(deriveGraphScene(payload, filters, -1).edges.map(({ id }) => id))
      .toEqual(["edge:included"]);
    expect(deriveGraphScene(payload, filters, 0).edges.map(({ id }) => id))
      .toEqual(["edge:included"]);
    expect(deriveGraphScene(payload, filters, 1).edges.map(({ id }) => id))
      .toEqual(["edge:included", "edge:wikilink"]);
  });

  it("does not preload future metadata when a stable edge id is removed and re-added", () => {
    const payload = graphFixture();
    const currentEdge = { ...payload.edges[0], label: "current relationship" };
    const originalEdge = { ...currentEdge, label: "original relationship" };
    payload.nodes = payload.nodes.slice(0, 2);
    payload.edges = [currentEdge];
    payload.timeline = [
      withSnapshots({
        id: "event:first-add",
        operation: "edge_created",
        action_kind: "referenced",
        label: "First add",
        occurred_at: "2026-07-01T00:00:00Z",
        sequence: 1,
        node_ids: [],
        edge_ids: [currentEdge.id],
        evidence: null,
      }, { edges: [originalEdge] }),
      withSnapshots({
        id: "event:remove",
        operation: "edge_removed",
        action_kind: "removed",
        label: "Removed",
        occurred_at: "2026-07-01T00:01:00Z",
        sequence: 2,
        node_ids: [],
        edge_ids: [currentEdge.id],
        evidence: null,
      }),
      withSnapshots({
        id: "event:re-add",
        operation: "edge_created",
        action_kind: "referenced",
        label: "Re-added",
        occurred_at: "2026-07-01T00:02:00Z",
        sequence: 3,
        node_ids: [],
        edge_ids: [currentEdge.id],
        evidence: null,
      }, { edges: [currentEdge] }),
    ];
    const filters = graphFacets(payload);

    expect(deriveGraphScene(payload, filters, 0).edges[0]?.label)
      .toBe("original relationship");
    expect(deriveGraphScene(payload, filters, 1).edges).toEqual([]);
    expect(deriveGraphScene(payload, filters, 2).edges[0]?.label)
      .toBe("current relationship");
  });

  it("derives inspector groups and escapes untrusted Mermaid control text", () => {
    const payload = graphFixture();
    const neighbors = graphPayloadToNeighbors(payload);
    expect(neighbors?.backlinks).toEqual([
      expect.objectContaining({ id: "page-related", edge_kind: "wikilink" }),
    ]);
    // Action/evidence edges outside the legacy Context inspector contract stay
    // in Graph View but are not mislabeled as backlinks or citations.
    expect(neighbors?.citations).toEqual([]);
    expect([
      ...(neighbors?.backlinks ?? []),
      ...(neighbors?.outbound ?? []),
      ...(neighbors?.co_access ?? []),
    ]).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ edge_kind: "ask_citation" }),
    ]));

    const scene = deriveGraphScene(payload, graphFacets(payload), null);
    scene.nodes[0] = { ...scene.nodes[0], label: `unsafe "|<tag>` };
    const mermaid = graphSceneToMermaid(scene);
    expect(mermaid).toContain("&quot;&#124;&lt;tag&gt;");
    expect(mermaid).not.toContain(`"|<tag>`);
  });

  it("does not project structural tree edges into the legacy neighbors panel", () => {
    const payload = graphFixture();
    payload.edges = [{ ...payload.edges[0], id: "edge:tree", edge_kind: "tree" }];

    expect(graphPayloadToNeighbors(payload)).toMatchObject({
      backlinks: [],
      outbound: [],
      citations: [],
      co_access: [],
    });
  });

  it("orders inspector groups by rank before slicing", () => {
    const payload = graphFixture();
    const seed = payload.nodes.find((node) => node.is_seed)!;
    // Two ranks tie (b02, b03); the two lowest (b06, b11) fall past the ten.
    const ranks = [0.1, 0.9, 0.5, 0.5, 0.2, 0.8, 0.05, 0.7, 0.6, 0.3, 0.4, 0.01];
    const names = ranks.map((_rank, index) => `b${String(index).padStart(2, "0")}`);
    payload.nodes = [
      seed,
      ...names.map((name) => ({
        ...seed,
        id: `node:${name}`,
        entity_id: `page-${name}`,
        label: `Backlinker ${name}`,
        is_seed: false,
      })),
    ];
    payload.edges = names.map((name, index) => ({
      ...payload.edges[0],
      id: `edge:${name}`,
      source: `node:${name}`,
      target: seed.id,
      rank_score: ranks[index],
    }));

    expect(graphPayloadToNeighbors(payload)?.backlinks?.map((entry) => entry.id)).toEqual([
      "page-b01",
      "page-b05",
      "page-b07",
      "page-b08",
      "page-b02",
      "page-b03",
      "page-b10",
      "page-b09",
      "page-b04",
      "page-b00",
    ]);
  });

  it("ranks a neighbour listed twice by its stronger relation", () => {
    const payload = graphFixture();
    const seed = payload.nodes.find((node) => node.is_seed)!;
    payload.nodes = [
      seed,
      ...["twice", "once"].map((name) => ({
        ...seed,
        id: `node:${name}`,
        entity_id: `page-${name}`,
        label: `Backlinker ${name}`,
        is_seed: false,
      })),
    ];
    // The weaker copy sorts first in edge-id order; the stronger one decides.
    payload.edges = [
      ["edge:a-twice-weak", "node:twice", 0.05],
      ["edge:b-once", "node:once", 0.5],
      ["edge:c-twice-strong", "node:twice", 0.95],
    ].map(([id, source, rank]) => ({
      ...payload.edges[0],
      id: id as string,
      source: source as string,
      target: seed.id,
      rank_score: rank as number,
    }));

    expect(graphPayloadToNeighbors(payload)?.backlinks?.map((entry) => entry.id)).toEqual([
      "page-twice",
      "page-once",
    ]);
  });

  it("preserves bounded metadata when adapting the graph to legacy neighbors", () => {
    const payload = graphFixture();
    payload.truncated = true;
    payload.warnings = ["graph_budget_applied", "future_private_warning"];

    expect(graphPayloadToNeighbors(payload)).toMatchObject({
      truncated: true,
      warnings: ["graph_budget_applied", "future_private_warning"],
    });
  });
});

function withSnapshots(
  event: GraphTimelineEvent,
  snapshots: { edges?: GraphEdge[]; nodes?: GraphNode[] } = {},
) {
  return {
    ...event,
    edge_snapshots: snapshots.edges ?? [],
    node_snapshots: snapshots.nodes ?? [],
  };
}

function coAccessEdge(values: Partial<GraphEdge> = {}): GraphEdge {
  return {
    id: "edge:co-access",
    source: "node:seed",
    target: "node:related",
    edge_kind: "co_access",
    action_kind: null,
    label: "Often read together",
    layer: "activity",
    provenance: "usage",
    direction: "undirected",
    weight: 2.4,
    rank_score: 0.0175,
    aggregate: true,
    occurred_at: "2026-08-24T12:00:00Z",
    first_observed_at: "2026-08-12T12:00:00Z",
    support_session_count: 3,
    ...values,
  };
}

describe("co_access evidence", () => {
  it("counts the sessions and the started days between first and last read", () => {
    expect(coAccessEvidence(coAccessEdge())).toEqual({
      sessions: 3,
      days: 12,
      lastObservedAt: "2026-08-24T12:00:00Z",
    });
    // A partial day is a started day, and reads an hour apart span one day.
    expect(
      coAccessEvidence(coAccessEdge({ occurred_at: "2026-08-24T13:00:00Z" }))?.days,
    ).toBe(13);
    expect(
      coAccessEvidence(
        coAccessEdge({
          first_observed_at: "2026-08-24T11:00:00Z",
          occurred_at: "2026-08-24T12:00:00Z",
        }),
      )?.days,
    ).toBe(1);
  });

  it("has nothing to say for other kinds or an edge without its evidence", () => {
    expect(coAccessEvidence(coAccessEdge({ edge_kind: "wikilink" }))).toBeNull();
    expect(coAccessEvidence(coAccessEdge({ support_session_count: null }))).toBeNull();
    expect(coAccessEvidence(coAccessEdge({ first_observed_at: undefined }))).toBeNull();
  });

  it("reads as one sentence in each locale", () => {
    expect(coAccessSentence(coAccessEdge(), i18n.getFixedT("en", "graph"))).toBe(
      "Read together in 3 sessions over 12 days (last 2026-08-24)",
    );
    expect(
      coAccessSentence(
        coAccessEdge({
          first_observed_at: "2026-08-24T11:00:00Z",
          support_session_count: 1,
        }),
        i18n.getFixedT("en", "graph"),
      ),
    ).toBe("Read together in 1 session over 1 day (last 2026-08-24)");
    expect(coAccessSentence(coAccessEdge(), i18n.getFixedT("ko", "graph"))).toBe(
      "3개 세션에서 12일에 걸쳐 함께 조회됨 (최근 2026-08-24)",
    );
    expect(
      coAccessSentence(coAccessEdge({ edge_kind: "wikilink" }), i18n.getFixedT("en", "graph")),
    ).toBeNull();
  });
});
