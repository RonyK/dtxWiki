import { describe, expect, it } from "vitest";
import { graphFixture } from "../../tests/graphFixture";
import { deriveGraphScene, graphFacets } from "../../graphModel";
import { buildLocalScene } from "../sceneInput";
import { historySceneCatalog, renderHistoryScene } from "../historyScene";

describe("history scene", () => {
  it("keeps interval geometry stable while removals remain ghosts outside the active scene", () => {
    const payload = graphFixture();
    const edge = { ...payload.edges[0], label: "Current relationship", count: 17, occurred_at: "2026-08-04T00:00:00Z" };
    const original = { ...edge, label: "Original relationship", count: 9, provenance: "observed" as const, occurred_at: "2026-08-02T00:00:00Z" };
    payload.edges = [edge];
    payload.timeline = [
      { id: "added", operation: "edge_created", action_kind: "referenced", label: "Added", occurred_at: "2026-08-02T00:00:00Z", sequence: 1,
        node_ids: [edge.source, edge.target], edge_ids: [edge.id], edge_snapshots: [original] },
      { id: "removed", operation: "edge_removed", action_kind: "removed", label: "Removed", occurred_at: "2026-08-03T00:00:00Z", sequence: 2,
        node_ids: [edge.source, edge.target], edge_ids: [edge.id], edge_snapshots: [] },
      { id: "re-added", operation: "edge_created", action_kind: "referenced", label: "Re-added", occurred_at: "2026-08-04T00:00:00Z", sequence: 3,
        node_ids: [edge.source, edge.target], edge_ids: [edge.id], edge_snapshots: [edge] },
    ];
    const filters = graphFacets(payload);
    const catalog = historySceneCatalog(payload, filters);
    const before = renderHistoryScene(catalog, deriveGraphScene(payload, filters, -1), { nodes: [], edges: [] }, undefined, false, false);
    const added = renderHistoryScene(catalog, deriveGraphScene(payload, filters, 0), deriveGraphScene(payload, filters, -1), payload.timeline[0], false, false);
    const active = deriveGraphScene(payload, filters, 1);
    const removed = renderHistoryScene(catalog, active, deriveGraphScene(payload, filters, 0), payload.timeline[1], false, false);
    expect(before.edges[0].hidden).toBe(true);
    expect(added.edges[0]).toMatchObject({ hidden: false, highlighted: true, removed: false });
    expect(active.edges).toEqual([]);
    expect(removed.edges[0]).toMatchObject({ ...original, hidden: false, highlighted: true, removed: true });
    expect(buildLocalScene(before).geometryKey).toBe(buildLocalScene(added).geometryKey);
    expect(buildLocalScene(added).geometryKey).toBe(buildLocalScene(removed).geometryKey);
    const preview = renderHistoryScene(catalog, deriveGraphScene(payload, filters, 2), deriveGraphScene(payload, filters, 0), payload.timeline[1], false, false, true);
    expect(preview.edges[0]).toMatchObject({ ...original, hidden: false, removed: true });
    const hiddenFilters = { ...filters, edgeKinds: [] };
    const hidden = renderHistoryScene(historySceneCatalog(payload, hiddenFilters), deriveGraphScene(payload, hiddenFilters, 1), deriveGraphScene(payload, hiddenFilters, 0), payload.timeline[1], false, false);
    expect(hidden.edges).toEqual([]); // A removed ghost never bypasses a filter.
    const curated = { ...filters, provenances: ["curated" as const] };
    const curatedCatalog = historySceneCatalog(payload, curated);
    const curatedFrame = deriveGraphScene(payload, curated, 1);
    expect(deriveGraphScene(payload, curated, 0).edges).toEqual([]);
    expect(curatedFrame.edges).toEqual([]);
    const excluded = renderHistoryScene(curatedCatalog, curatedFrame, deriveGraphScene(payload, curated, 0), payload.timeline[1], false, false);
    expect(excluded.edges[0]).toMatchObject({ hidden: true, removed: false, highlighted: false });
    const excludedPreview = renderHistoryScene(curatedCatalog, deriveGraphScene(payload, curated, 2), deriveGraphScene(payload, curated, 0), payload.timeline[1], false, false, true);
    expect(excludedPreview.edges[0]).toMatchObject({ ...edge, hidden: false, removed: false, highlighted: false });
  });
});
