import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphNode } from "../../../../shared/api/graph";
import { graphFixture } from "../../tests/graphFixture";
import { analyzeLocalScene, localListMatches, localNodeRelations, localSceneRings, localSeedPath } from "../localGraphList";

const base = graphFixture();
function node(id: string, options: { folder?: boolean; seed?: boolean; entity?: string } = {}): GraphNode {
  return {
    ...base.nodes[0], id, entity_id: options.entity ?? id, label: id, is_seed: options.seed ?? false,
    entity_kind: options.folder ? "workspace_tree_node" : "context_page",
    family: options.folder ? "structural" : "document",
  };
}
function edge(id: string, source: string, target: string, kind = "wikilink", direction: GraphEdge["direction"] = "directed"): GraphEdge {
  return { ...base.edges[0], id, source, target, edge_kind: kind, label: kind, direction };
}
const ids = (items: readonly { id: string }[]) => items.map(({ id }) => id);

describe("Local graph list model", () => {
  it("places every drawn node and relation relative to the seeds", () => {
    const nodes = Object.freeze([
      node("seed", { seed: true }), node("expanded", { seed: true }), node("incoming"), node("outgoing"),
      node("together"), node("folder", { folder: true }), node("hop2"), node("hop2-alt"), node("island"),
      node("island-peer"), node("alias", { entity: "seed" }), node("unknown"),
    ]);
    const edges = Object.freeze([
      edge("incoming", "incoming", "seed"),
      edge("outgoing", "seed", "outgoing", "citation_page"),
      edge("together", "seed", "together", "co_access", "undirected"),
      edge("membership", "folder", "seed", "tree"),
      edge("between-neighbors", "incoming", "outgoing"),
      edge("to-hop2", "hop2", "outgoing"),
      edge("to-hop2-alt", "together", "hop2"),
      edge("hop2-ring", "hop2", "hop2-alt"),
      edge("islands", "island", "island-peer"),
      edge("seeds", "seed", "expanded"),
      edge("self-alias", "seed", "alias"),
      edge("unknown", "seed", "unknown", "future_private_kind"),
      edge("missing", "seed", "absent"),
    ]);
    const analysis = analyzeLocalScene({ nodes, edges },
      [{ kind: "context_page", id: "seed" }, { kind: "context_page", id: "expanded" }]);

    // Only relations the renderer draws: no self-links (aliases), unknown kinds or missing ends.
    expect(ids(analysis.edges)).not.toEqual(expect.arrayContaining(["self-alias"]));
    expect(ids(analysis.edges)).not.toContain("unknown");
    expect(ids(analysis.edges)).not.toContain("missing");
    expect(analysis.seeds).toEqual(["seed", "expanded"]);
    // Direction is the stored one; the other seed comes first, then names.
    const seed = localNodeRelations(analysis, "seed");
    expect(ids(seed.incoming)).toEqual(["membership", "incoming"]);
    expect(ids(seed.outgoing)).toEqual(["seeds", "outgoing"]);
    expect(ids(seed.undirected)).toEqual(["together"]);
    expect(ids(localNodeRelations(analysis, "folder").outgoing)).toEqual(["membership"]);
    expect(ids(localNodeRelations(analysis, "unknown").incoming)).toEqual([]);

    const rings = localSceneRings(analysis);
    expect(ids(rings.neighbors)).toEqual(["between-neighbors"]);
    expect(ids(rings.ringNodes)).toEqual(["hop2", "hop2-alt"]);
    expect(ids(rings.ringRelations)).toEqual(["to-hop2", "to-hop2-alt", "hop2-ring"]);
    expect(ids(rings.unlinkedNodes)).toEqual(["island", "island-peer", "alias", "unknown"]);
    expect(ids(rings.unlinkedEdges)).toEqual(["islands"]);

    // How a node is reached: from the primary seed, with the other last steps.
    const path = localSeedPath(analysis, "hop2")!;
    expect(path.chain).toEqual(["seed", "outgoing", "hop2"]);
    expect(path.alternatives).toEqual(["together"]);
    expect(path.links.map(ids)).toEqual([["outgoing"], ["to-hop2"]]);
    expect(localSeedPath(analysis, "expanded")!.chain).toEqual(["seed", "expanded"]);
    expect(localSeedPath(analysis, "seed")).toBeNull();
    expect(localSeedPath(analysis, "island")).toBeNull();

    expect(localListMatches(" OUT ")("seed", "outgoing")).toBe(true);
    expect(localListMatches("absent")("seed", null)).toBe(false);
    expect(localListMatches("  ")("anything")).toBe(true);
  });
});
