import { describe, expect, it } from "vitest";

import { graphFacets } from "../../graphModel";
import { graphFixture } from "../../tests/graphFixture";
import { localSceneScopeKey } from "../sceneScope";

function options(): Parameters<typeof localSceneScopeKey>[0] {
  const payload = graphFixture();
  return {
    workspaceId: "workspace-a", query: payload.query,
    filters: graphFacets(payload), historyMode: "appearance", cursor: null,
  };
}

describe("Local client selection scope", () => {
  it("canonicalizes set-valued query and UI filters without sorting primary seeds", () => {
    const before = options();
    const after = structuredClone(before);
    after.query.layers = [...after.query.layers!].reverse();
    after.query.layers.push(after.query.layers[0]);
    after.filters.nodeFamilies.reverse();
    after.filters.statuses.reverse();
    expect(localSceneScopeKey(after)).toBe(localSceneScopeKey(before));

    before.query.seeds.push({ kind: "context_page", id: "page-related" });
    after.query.seeds = [...before.query.seeds].reverse();
    expect(localSceneScopeKey(after)).not.toBe(localSceneScopeKey(before));
  });

  it.each([
    ["workspace", (value: ReturnType<typeof options>) => { value.workspaceId = "workspace-b"; }],
    ["depth", (value: ReturnType<typeof options>) => { value.query.depth = 2; }],
    ["seed", (value: ReturnType<typeof options>) => { value.query.seeds[0].id = "other-page"; }],
    ["query kind", (value: ReturnType<typeof options>) => { value.query.node_kinds = ["context_page"]; }],
    ["query direction", (value: ReturnType<typeof options>) => { value.query.direction = "in"; }],
    ["time mode", (value: ReturnType<typeof options>) => { value.query.time = { mode: "current" }; }],
    ["time bound", (value: ReturnType<typeof options>) => { value.query.time = { mode: "appearance", from: "2026-08-01T00:00:00Z" }; }],
    ["history mode", (value: ReturnType<typeof options>) => { value.historyMode = "action"; }],
    ["UI filter", (value: ReturnType<typeof options>) => { value.filters.statuses = []; }],
    ["cursor", (value: ReturnType<typeof options>) => { value.cursor = -1; }],
    ["budget", (value: ReturnType<typeof options>) => { value.query.budget = { nodes: 250, edges: 750 }; }],
  ] as const)("fences a changed %s even if visible geometry is equal", (_name, change) => {
    const before = options();
    const after = structuredClone(before);
    change(after);
    expect(localSceneScopeKey(after)).not.toBe(localSceneScopeKey(before));
  });

  it("uses an explicit event identity, not its translated label or the latest watermark", () => {
    const before = options();
    before.cursor = 0;
    before.event = { id: "event-a", occurred_at: "2026-08-01T00:00:00Z", sequence: 1 };
    const after = structuredClone(before);
    after.event!.id = "event-b";
    expect(localSceneScopeKey(after)).not.toBe(localSceneScopeKey(before));
    after.cursor = before.cursor = null;
    expect(localSceneScopeKey(after)).toBe(localSceneScopeKey(before));
  });

  it("ignores payload metadata and object insertion order", () => {
    const payload = graphFixture();
    const before = options();
    const after = options();
    payload.projection_revision++;
    payload.generated_at = "2026-10-09T00:00:00Z";
    payload.nodes[0].label = "새 제목";
    after.query = { ...payload.query, time: { from: null, mode: "appearance" } };
    expect(localSceneScopeKey(after)).toBe(localSceneScopeKey(before));
  });
});
