import { describe, expect, it } from "vitest";
import { i18n } from "../../../../shared/i18n";
import { graphFixture } from "../../tests/graphFixture";
import { timelinePresentation } from "../timelinePresentation";

describe("timeline presentation", () => {
  it("classifies edited versions by their entity, while preserving activity and relationship semantics", () => {
    const payload = graphFixture();
    const seed = payload.nodes[0];
    const version = { ...payload.nodes[1], entity_kind: "context_page_version", label: "Version 2" };
    const edge = { ...payload.edges[0], source: version.id, target: seed.id };
    const nodes = new Map([seed, version].map((node) => [node.id, node]));
    const edges = new Map([[edge.id, edge]]);
    const edited = { ...payload.timeline![1], operation: "action_occurred" as const, action_kind: "edited", edge_ids: [edge.id], node_ids: [] };
    const t = i18n.getFixedT("en", "graph");
    expect(timelinePresentation(edited, "appearance", nodes, edges, t)).toMatchObject({ lane: 0, mark: "version", summary: "Document version edited" });
    // Snapshot-only versions must classify identically; no current node needed.
    expect(timelinePresentation({ ...edited, node_snapshots: [version] }, "appearance", new Map([[seed.id, seed]]), edges, t).lane).toBe(0);
    // A conversation's event reads as that conversation, even when it also
    // carries a version its writes produced.
    nodes.set(version.id, { ...version, entity_kind: "mcp_chat_session", family: "chat", label: "Graph View edit", actor_label: "Codex" });
    expect(timelinePresentation({ ...edited, node_snapshots: [{ ...version, id: "v2" }] }, "action", nodes, edges, t))
      .toMatchObject({ lane: 1, mark: "tool", summary: "Codex · Graph View edit", detail: "This document: Edited" });
    nodes.set(version.id, { ...version, entity_kind: "ask_session", family: "ask" });
    expect(timelinePresentation({ ...edited, action_kind: "included" }, "action", nodes, edges, t)).toMatchObject({ lane: 1, mark: "ask" });
    nodes.set(version.id, { ...version, entity_kind: "context_page" });
    expect(timelinePresentation({ ...edited, operation: "edge_removed", action_kind: "removed" }, "appearance", nodes, edges, t))
      .toMatchObject({ lane: 1, mark: "removed", summary: "Wiki link removed" });
  });
});
