import { describe, expect, it } from "vitest";

import { i18n } from "../../../shared/i18n";
import {
  coAccessEvidence,
  coAccessSentence,
  deriveGraphScene,
  graphFacets,
} from "../graphModel";
import { decorateStaticDiagramSvg, graphSceneToMermaid } from "../local/staticDiagram";
import { readGraphTokens } from "../shared/graphTokens";
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
    const originalEdge: GraphEdge = { ...currentEdge, label: "original relationship", provenance: "observed" };
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
    expect(deriveGraphScene(payload, { ...filters, provenances: [currentEdge.provenance] }, 0).edges)
      .toEqual([]);
    expect(deriveGraphScene(payload, filters, 1).edges).toEqual([]);
    expect(deriveGraphScene(payload, filters, 2).edges[0]?.label)
      .toBe("current relationship");
  });

  it("draws an escaped static diagram in the legend vocabulary", () => {
    const payload = graphFixture();
    const scene = deriveGraphScene(payload, graphFacets(payload), null);
    scene.nodes[0] = { ...scene.nodes[0], label: `unsafe "|<tag>` };
    const visuals = { tokens: readGraphTokens(document.documentElement), forcedColors: false };
    const diagram = graphSceneToMermaid(scene, visuals, { iconSlot: true });
    expect(diagram.source).toContain("&quot;&#124;&lt;tag&gt;");
    expect(diagram.source).not.toContain(`"|<tag>`);
    // Seed first; Mermaid lays its title out bold, at the weight it is drawn.
    expect(diagram.nodes).toEqual([
      { kind: "context", seed: true }, { kind: "conversation", seed: false }, { kind: "context", seed: false },
    ]);
    expect(diagram.source).toContain('n0("Mm unsafe');
    expect(diagram.source).toContain("graphEdge0@-->");
    expect(diagram.edges).toEqual([
      { id: "graphEdge0", kind: "citation", directed: true },
      { id: "graphEdge1", kind: "link", directed: true },
    ]);
    expect(diagram.source).toContain(".contextSeed .text-inner-tspan { font-weight: bold; }");
    expect(diagram.source).toContain("classDef contextSeed fill:#7c3aed,stroke:#7c3aed,stroke-width:1.5px,color:#ffffff");
    expect(diagram.source).toContain("classDef conversation fill:#e6f3f9,stroke:#0284c7,stroke-width:1.5px,color:#0f172a");
    expect(diagram.source).toMatch(/^ {2}linkStyle \d+ stroke:#64748b,stroke-width:1.5px$/m);
    expect(diagram.source).toMatch(/^ {2}linkStyle \d+ stroke:#0f9f8f,stroke-width:1.5px,stroke-dasharray:8 3 1.5 3$/m);
    // In the dark graph theme, titles and relationship labels take the graph ink.
    const dark = graphSceneToMermaid(
      scene, { tokens: readGraphTokens(document.documentElement, "dark"), forcedColors: false }, { iconSlot: true },
    ).source;
    expect(dark).toMatch(/classDef conversation fill:#[\da-f]{6},stroke:#38bdf8,stroke-width:1\.5px,color:#e5e7eb/);
    expect(dark).toContain(".edgeLabel text { fill: #e5e7eb !important; }");
    // An exported file has no slot words for a renderer that cannot draw glyphs.
    expect(graphSceneToMermaid(scene, visuals, { iconSlot: false }).source).not.toMatch(/\bMm\b|wrappingWidth/);

    // Inspect the decorated SVG too: Mermaid already supplies direction for
    // stored relationships that have no Local-specific endpoint style.
    const endpointEdges: GraphEdge[] = ["promoted_from", "page_provenance", "wikilink", "tree", "co_access", "searched"]
      .map((edge_kind, index) => ({
        ...payload.edges[0], id: `edge:endpoint-${index}`, label: String(index), edge_kind,
        direction: edge_kind === "co_access" ? "undirected" : "directed",
      }));
    const endpointDiagram = graphSceneToMermaid({ ...scene, edges: endpointEdges }, visuals, { iconSlot: true });
    const svg = `<svg id="static-endpoints" xmlns="http://www.w3.org/2000/svg">
      <defs><marker id="original-arrow"><path d="M0 0L4 2L0 4Z" /></marker></defs>
      ${endpointDiagram.edges.map(({ id }, index) => `<path class="flowchart-link" id="${id}" d="M0 ${index * 10}L40 ${index * 10}"
        ${index === 1 ? 'style="marker-end:url(#original-arrow)"' : 'marker-end="url(#original-arrow)"'}
        marker-start="url(#original-arrow)" />`).join("")}
    </svg>`;
    const decorated = document.createElement("div");
    decorated.innerHTML = decorateStaticDiagramSvg(svg, endpointDiagram.nodes, visuals, endpointDiagram.edges);
    const paths = [...decorated.querySelectorAll<SVGPathElement>("path.flowchart-link")];
    expect(paths[0].getAttribute("marker-end")).toBe("url(#original-arrow)");
    expect(paths[0].style.getPropertyValue("marker-end")).toBe("");
    expect(paths[1].style.getPropertyValue("marker-end")).toBe("url(#original-arrow)");
    expect(paths[0].getAttribute("marker-start")).toBe("url(#original-arrow)");
    expect(paths[1].getAttribute("marker-start")).toBe("url(#original-arrow)");
    const markerShape = (path: SVGPathElement, end: "start" | "end") => {
      const markerId = /#([^)"]+)/.exec(path.style.getPropertyValue(`marker-${end}`))?.[1];
      expect(markerId).toBeDefined();
      return decorated.querySelector<SVGPathElement>(`marker[id="${markerId}"] path`)!;
    };
    expect(markerShape(paths[2], "end").style.fill).toBe("none");
    expect(paths[2].style.getPropertyValue("marker-start")).toBe("none");
    expect(markerShape(paths[3], "start").style.fill).toBe(visuals.tokens.surface);
    expect(markerShape(paths[3], "end").style.fill).toBe("none");
    expect(paths[4].style.getPropertyValue("marker-start")).toBe("none");
    expect(paths[4].style.getPropertyValue("marker-end")).toBe("none");
    expect(markerShape(paths[5], "end").style.fill).toBe(visuals.tokens.edge.action);
    expect(markerShape(paths[5], "end").style.stroke).toBe("none");
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
