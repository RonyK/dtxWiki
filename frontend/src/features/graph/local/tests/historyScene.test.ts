import { describe, expect, it } from "vitest";
import { graphFixture } from "../../tests/graphFixture";
import { deriveGraphScene, graphFacets } from "../../graphModel";
import { buildLocalScene } from "../sceneInput";
import { historySceneCatalog, renderHistoryScene } from "../historyScene";

describe("history scene", () => {
  it("keeps interval geometry stable while removals remain ghosts outside the active scene", () => {
    const payload = graphFixture();
    const edge = payload.edges[0];
    payload.edges = [edge];
    payload.timeline = [
      { id: "added", operation: "edge_created", action_kind: "referenced", label: "Added", occurred_at: "2026-08-02T00:00:00Z", sequence: 1,
        node_ids: [edge.source, edge.target], edge_ids: [edge.id], edge_snapshots: [{ ...edge, count: 9 }] },
      { id: "removed", operation: "edge_removed", action_kind: "removed", label: "Removed", occurred_at: "2026-08-03T00:00:00Z", sequence: 2,
        node_ids: [edge.source, edge.target], edge_ids: [edge.id], edge_snapshots: [] },
    ];
    const filters = graphFacets(payload);
    const catalog = historySceneCatalog(payload, filters);
    const before = renderHistoryScene(catalog, deriveGraphScene(payload, filters, -1), undefined, false, false);
    const added = renderHistoryScene(catalog, deriveGraphScene(payload, filters, 0), payload.timeline[0], false, false);
    const active = deriveGraphScene(payload, filters, 1);
    const removed = renderHistoryScene(catalog, active, payload.timeline[1], false, false);
    expect(before.edges[0].hidden).toBe(true);
    expect(added.edges[0]).toMatchObject({ hidden: false, highlighted: true, removed: false });
    expect(active.edges).toEqual([]);
    expect(removed.edges[0]).toMatchObject({ hidden: false, highlighted: true, removed: true });
    expect(buildLocalScene(before).geometryKey).toBe(buildLocalScene(added).geometryKey);
    expect(buildLocalScene(added).geometryKey).toBe(buildLocalScene(removed).geometryKey);
    const hiddenFilters = { ...filters, edgeKinds: [] };
    const hidden = renderHistoryScene(historySceneCatalog(payload, hiddenFilters), deriveGraphScene(payload, hiddenFilters, 1), payload.timeline[1], false, false);
    expect(hidden.edges).toEqual([]); // A removed ghost never bypasses a filter.
  });
});
