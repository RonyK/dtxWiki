import { describe, expect, it } from "vitest";
import type { GraphTimelineEvent } from "../../../../shared/api/graph";
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
    const added = renderHistoryScene(catalog, deriveGraphScene(payload, filters, 0), deriveGraphScene(payload, filters, 0), payload.timeline[0], false, false);
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
    const creationPreview = renderHistoryScene(catalog, active, deriveGraphScene(payload, filters, 0), payload.timeline[0], false, false, true);
    expect(creationPreview.edges[0]).toMatchObject({ ...original, hidden: false, highlighted: true, removed: false });
    expect(buildLocalScene(creationPreview).geometryKey).toBe(buildLocalScene(removed).geometryKey);
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
    const excludedCreation = renderHistoryScene(curatedCatalog, curatedFrame, deriveGraphScene(payload, curated, 0), payload.timeline[0], false, false, true);
    expect(excludedCreation.edges[0]).toMatchObject({ hidden: true, highlighted: false, eventCaption: undefined });
    const excludedPreview = renderHistoryScene(curatedCatalog, deriveGraphScene(payload, curated, 2), deriveGraphScene(payload, curated, 0), payload.timeline[1], false, false, true);
    expect(excludedPreview.edges[0]).toMatchObject({ ...edge, hidden: false, removed: false, highlighted: false });
  });

  it("previews admitted action items and their metadata without revealing filtered siblings or nodes", () => {
    const payload = graphFixture();
    const excludedCurrent = { ...payload.edges[0], count: 17 };
    const admittedCurrent = { ...payload.edges[1], provenance: "curated" as const, count: 23 };
    const excludedSnapshot = { ...excludedCurrent, provenance: "observed" as const, count: 9 };
    const admittedSnapshot = { ...admittedCurrent, label: "Recorded inclusion", count: 7 };
    const historicalNode = { ...payload.nodes[2], label: "Recorded question", status: "draft" };
    payload.edges = [excludedCurrent, admittedCurrent];
    const event: GraphTimelineEvent = {
      id: "action", operation: "action_occurred", action_kind: "included", label: "Included", occurred_at: "2026-08-03T00:01:00Z", sequence: 1,
      node_ids: [historicalNode.id], node_snapshots: [historicalNode], edge_ids: [excludedCurrent.id, admittedCurrent.id],
      edge_snapshots: [excludedSnapshot, admittedSnapshot],
    };
    payload.timeline = [event];
    const filters = { ...graphFacets(payload), provenances: ["curated" as const] };
    const catalog = historySceneCatalog(payload, filters);
    const frame = deriveGraphScene(payload, filters, -1);
    const eventFrame = deriveGraphScene(payload, filters, 0);
    expect(frame.edges).toEqual([]);
    expect(eventFrame.edges.map((edge) => edge.id)).toEqual([admittedCurrent.id]);
    const before = renderHistoryScene(catalog, frame, eventFrame, undefined, false, true);
    const preview = renderHistoryScene(catalog, frame, eventFrame, event, false, true, true);
    expect(preview.edges.find((edge) => edge.id === excludedCurrent.id)).toMatchObject({ hidden: true, highlighted: false, eventCaption: undefined });
    expect(preview.edges.find((edge) => edge.id === admittedCurrent.id)).toMatchObject({ ...admittedSnapshot, hidden: false, highlighted: true, layoutCount: 23 });
    expect(preview.nodes.find((node) => node.id === historicalNode.id)).toMatchObject({ ...historicalNode, hidden: false, highlighted: true });
    expect(buildLocalScene(preview).geometryKey).toBe(buildLocalScene(before).geometryKey);

    const hiddenFilters = { ...filters, statuses: filters.statuses.filter((status) => status !== "draft") };
    const hidden = renderHistoryScene(historySceneCatalog(payload, hiddenFilters), deriveGraphScene(payload, hiddenFilters, -1),
      deriveGraphScene(payload, hiddenFilters, 0), event, false, true, true);
    expect(hidden.nodes.find((node) => node.id === historicalNode.id)).toMatchObject({ hidden: true, highlighted: false });
    expect(hidden.edges.find((edge) => edge.id === admittedCurrent.id)).toMatchObject({ hidden: true, highlighted: false });

    const future = renderHistoryScene(catalog, frame, eventFrame, event, true, true, true);
    expect(future.edges.find((edge) => edge.id === excludedCurrent.id)).toMatchObject({ ...excludedCurrent, hidden: false, highlighted: false });
  });
});
