import { beforeEach, describe, expect, it } from "vitest";

import type { WorkspaceGraphDataFilters, WorkspaceGraphScene } from "../../../shared/api/workspaceGraph";
import { workspaceGraphStorageScope } from "../../../shared/lib/workspaceGraphStorage";
import {
  DEFAULT_WORKSPACE_GRAPH_FILTERS,
  clearWorkspaceGraphState,
  countPendingRefineFilters,
  freshWorkspaceGraphState,
  loadWorkspaceGraphState,
  refineFiltersDiffer,
  removeFacetRule,
  saveWorkspaceGraphState,
  updateFacetRule,
  workspaceGraphMetricSize,
  workspaceGraphRenderScene,
  workspaceGraphRequest,
  workspaceGraphOrphanIds,
} from "../workspaceGraphModel";

const scene: WorkspaceGraphScene = {
  schema_version: "workspace-graph-scene.v1",
  workspace_scope_considered: true,
  head_mode: "published",
  graph_revision: "revision-0000000000000001",
  generated_at: "2026-08-05T00:00:00+00:00",
  algorithm_version: "workspace-graph.v1",
  metric_basis: "Referenced by N visible documents",
  representation: "exact",
  completeness: "complete",
  truncated: false,
  counts: {
    candidate_documents: 2,
    candidate_relations: 1,
    rendered_documents: 2,
    rendered_clusters: 0,
    rendered_relations: 1,
  },
  omission_reasons: [],
  nodes: [
    {
      grain: "document",
      id: "context:one",
      entity_kind: "context",
      entity_id: "00000000-0000-4000-8000-000000000001",
      label: "Alpha",
      path: "/pages/alpha",
      primary_folder_id: "00000000-0000-4000-8000-000000000011",
      primary_folder_label: "Research",
      reference_count: 0,
      evidence_count: 3,
      also_appears_in: [],
    },
    {
      grain: "document",
      id: "library:two",
      entity_kind: "library",
      entity_id: "00000000-0000-4000-8000-000000000002",
      label: "Beta",
      path: "/library?item=two",
      primary_folder_id: null,
      primary_folder_label: "No visible placement",
      reference_count: 64,
      evidence_count: 1,
      also_appears_in: [],
    },
  ],
  edges: [
    {
      id: "relation:one-two",
      source: "context:one",
      target: "library:two",
      relation_kind: "wikilink",
      label: null,
      layer: "knowledge",
      direction: "directed",
      aggregate: false,
      relation_count: 1,
      cross_folder: true,
    },
  ],
  facet_counts: {
    document_kinds: [
      { value: "context", label: "Context", count: 1 },
      { value: "library", label: "Library", count: 1 },
    ],
    relation_kinds: [{ value: "wikilink", label: "Wiki link", count: 1 }],
    folders: [{ value: "00000000-0000-4000-8000-000000000011", label: "Research", count: 1 }],
    quick_filters: [],
  },
  expansion_cursor: null,
  warnings: [],
  capabilities: {
    expand: false,
    evidence: true,
    export: true,
    directory: true,
    compare: false,
    growth: false,
    provenance: false,
  },
};

describe("workspaceGraphModel", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("defaults a fresh Map and its first server request to combined knowledge", () => {
    const state = freshWorkspaceGraphState();
    expect(state.lens).toBe("combined");
    expect(workspaceGraphRequest(state).lens).toBe("combined");
  });

  it.each(["links", "evidence", "combined"] as const)(
    "restores an explicitly chosen %s lens only for its actor",
    (lens) => {
      saveWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision, {
        ...freshWorkspaceGraphState(),
        lens,
      });
      const restored = loadWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision);
      expect(restored.lens).toBe(lens);
      expect(workspaceGraphRequest(restored).lens).toBe(lens);
      expect(loadWorkspaceGraphState("workspace-one", "actor-two").lens).toBe("combined");
    },
  );

  it("encodes both visible reference and evidence metrics in the default Combined lens", () => {
    const rendered = workspaceGraphRenderScene(scene, freshWorkspaceGraphState().display);
    for (let i = 0; i < scene.nodes.length; i += 1) {
      const node = scene.nodes[i];
      if (node.grain !== "document") throw new Error("Expected document metric fixture");
      const size = workspaceGraphMetricSize(node.reference_count + node.evidence_count);
      expect(rendered.nodes[i]).toMatchObject({
        metricSized: true,
        nodeWidth: size.width,
        nodeHeight: size.height,
      });
    }
  });

  it("identifies only confirmed unlinked documents and retains a focused document", () => {
    const orphan = { ...scene.nodes[0], id: "orphan", reference_count: 0, evidence_count: 0 };
    const payload = { ...scene, nodes: [...scene.nodes, orphan] } as WorkspaceGraphScene;
    expect([...workspaceGraphOrphanIds(payload)]).toEqual(["orphan"]);
    expect(workspaceGraphOrphanIds({ ...payload, focus: { status: "document", node_id: "orphan" } }))
      .toHaveLength(0);
    const rendered = workspaceGraphRenderScene(payload, freshWorkspaceGraphState().display);
    expect(rendered.nodes.map((node) => node.id)).not.toContain("orphan");
    expect(workspaceGraphRenderScene(payload, { ...freshWorkspaceGraphState().display, show_orphans: true })
      .nodes.map((node) => node.id)).toContain("orphan");
  });

  it("does not call a document an orphan just because an incomplete scene omits its relationships", () => {
    const payload = {
      ...scene,
      nodes: [{ ...scene.nodes[0], reference_count: 0, evidence_count: 0 }],
      edges: [],
    } as WorkspaceGraphScene;
    for (const partial of [
      { ...payload, truncated: true },
      { ...payload, completeness: "capped" as const },
      { ...payload, representation: "aggregated" as const },
    ]) expect(workspaceGraphOrphanIds(partial)).toHaveLength(0);
    // Incoming reference metrics are not outgoing degree: a real outgoing edge
    // keeps its source visible even when both source metrics are zero.
    const outgoing = {
      ...scene,
      nodes: scene.nodes.map((node) => ({ ...node, reference_count: 0, evidence_count: 0 })),
    } as WorkspaceGraphScene;
    expect(workspaceGraphOrphanIds(outgoing)).toHaveLength(0);
    expect(workspaceGraphOrphanIds({
      ...payload,
      nodes: [{ grain: "cluster", id: "cluster:empty", label: "Grouped documents", cluster_kind: "other_cluster", member_count: 2, relation_count: 0 }],
    })).toHaveLength(0);
  });

  it("translates staged filters to the exact bounded wire request", () => {
    const request = workspaceGraphRequest({
      lens: "links",
      dataFilters: {
        ...DEFAULT_WORKSPACE_GRAPH_FILTERS,
        search: "  visible terms  ",
        time_basis: "relation_observed",
        time_from: "2026-08-01T00:00:00.000Z",
        time_to: "2026-08-02T23:59:59.999Z",
        quick_filters: ["cross_folder"],
        facet_rules: [
          { facet: "folder", mode: "only", value: "00000000-0000-4000-8000-000000000011" },
        ],
      },
    });
    expect(request).toEqual({
      schema_version: "workspace-graph-overview.v1",
      head_mode: "published",
      lens: "links",
      document_kinds: ["context", "library"],
      search: "visible terms",
      quick_filters: ["cross_folder"],
      folders: {
        include: [],
        exclude: [],
        only: ["00000000-0000-4000-8000-000000000011"],
      },
      time: {
        basis: "relation_observed",
        from: "2026-08-01T00:00:00.000Z",
        to: "2026-08-02T23:59:59.999Z",
      },
      budget: { documents: 2_000, relations: 6_000 },
    });
    expect(request).not.toHaveProperty("display");
  });

  it("sends a deep-link focus only when one is requested", () => {
    const state = { lens: "links" as const, dataFilters: DEFAULT_WORKSPACE_GRAPH_FILTERS };
    const focusId = "00000000-0000-4000-8000-000000000001";

    expect(workspaceGraphRequest(state, focusId)).toMatchObject({
      focus_entity_id: focusId,
    });
    expect(workspaceGraphRequest(state)).not.toHaveProperty("focus_entity_id");
    expect(workspaceGraphRequest(state, null)).not.toHaveProperty("focus_entity_id");
  });

  it("keeps canvas/list IDs physically aligned and maps document semantics", () => {
    const rendered = workspaceGraphRenderScene(
      scene,
      { show_edges: true, show_labels: true, show_orphans: false },
      "",
      "links",
    );
    expect(rendered.nodes.map((node) => node.id)).toEqual(["context:one", "library:two"]);
    expect(rendered.edges.map((edge) => edge.id)).toEqual(["relation:one-two"]);
    expect(rendered.nodes[0]).toMatchObject({ entityKind: "context_page", metricSized: true });
    expect(rendered.nodes[1]).toMatchObject({ entityKind: "library_item", metricSized: true });
    expect(rendered.nodes[1].nodeWidth).toBeGreaterThan(rendered.nodes[0].nodeWidth ?? 0);
    expect(rendered.edges[0]).toMatchObject({ count: 1, crossFolder: "true" });

    const searched = workspaceGraphRenderScene(
      scene,
      { show_edges: true, show_labels: true, show_orphans: false },
      "alpha",
      "links",
    );
    expect(searched.nodes.map((node) => node.id)).toEqual(["context:one"]);
    expect(searched.edges).toEqual([]);
  });

  it("uses bounded metric sizing only for metric-bearing lenses", () => {
    expect(workspaceGraphMetricSize(0)).toEqual({ width: 58, height: 44 });
    expect(workspaceGraphMetricSize(10_000)).toEqual({ width: 96, height: 68 });
    const comparison = workspaceGraphRenderScene(
      scene,
      { show_edges: true, show_labels: true, show_orphans: false },
      "",
      "compare",
    );
    expect(comparison.nodes.every((node) => node.metricSized === false)).toBe(true);
  });

  it("restores transients only for the exact actor-visible graph revision", () => {
    const state = {
      ...freshWorkspaceGraphState(),
      selectedId: "context:one",
      pinnedIds: ["context:one"],
      pinnedPositions: { "context:one": { x: 12, y: 34 } },
      viewport: { zoom: 1.5, pan: { x: 20, y: 30 } },
    };
    saveWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision, state);
    expect(loadWorkspaceGraphState("workspace-one", "actor-one")).toMatchObject({
      selectedId: null,
      pinnedIds: [],
      pinnedPositions: {},
      viewport: null,
    });
    expect(loadWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision)).toMatchObject({
      selectedId: "context:one",
      pinnedIds: ["context:one"],
      viewport: { zoom: 1.5, pan: { x: 20, y: 30 } },
    });
    expect(loadWorkspaceGraphState("workspace-one", "actor-one", "revision-0000000000000002")).toMatchObject({
      selectedId: null,
      pinnedIds: [],
      viewport: null,
    });
  });

  it("sanitizes malformed saved filters before rebuilding a wire request", () => {
    window.sessionStorage.setItem(
      `${workspaceGraphStorageScope("workspace-corrupt", "actor-one")}latest`,
      JSON.stringify({
        revision: "old-revision",
        state: {
          viewerId: "actor-one",
          lens: "growth",
          dataFilters: {
            search: "x".repeat(250),
            document_kinds: ["context", "unknown", "context"],
            time_basis: "unsupported",
            time_from: "not-a-date",
            time_to: "2026-08-05T00:00:00",
            quick_filters: ["orphans", "unknown", "orphans"],
            facet_rules: [
              null,
              { facet: "folder", mode: "include", value: "not-a-uuid" },
              {
                facet: "folder",
                mode: "include",
                value: "00000000-0000-4000-8000-000000000011",
              },
            ],
          },
          pinnedIds: ["context:one", 42],
          pinnedPositions: { "context:one": { x: "bad", y: 4 } },
          viewport: { zoom: "bad", pan: { x: 1, y: 2 } },
        },
      }),
    );

    const restored = loadWorkspaceGraphState("workspace-corrupt", "actor-one");
    expect(restored).toMatchObject({
      lens: "combined",
      dataFilters: {
        document_kinds: ["context"],
        time_basis: "document_updated",
        time_from: null,
        time_to: null,
        quick_filters: ["orphans"],
        facet_rules: [{
          facet: "folder",
          mode: "include",
          value: "00000000-0000-4000-8000-000000000011",
        }],
      },
      pinnedIds: [],
      pinnedPositions: {},
      viewport: null,
    });
    expect(restored.dataFilters.search).toHaveLength(200);
    expect(() => workspaceGraphRequest(restored)).not.toThrow();
  });

  it.each(["include", "exclude", "only"] as const)(
    "preserves the unplaced folder %s rule across actor-scoped restoration",
    (mode) => {
      const rule = { facet: "folder" as const, mode, value: "unplaced" };
      const state = {
        ...freshWorkspaceGraphState(),
        dataFilters: { ...DEFAULT_WORKSPACE_GRAPH_FILTERS, facet_rules: [rule] },
      };
      saveWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision, state);
      const restored = loadWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision);
      expect(restored.dataFilters.facet_rules).toEqual([rule]);
      expect(workspaceGraphRequest(restored).folders).toEqual({
        include: mode === "include" ? ["unplaced"] : [],
        exclude: mode === "exclude" ? ["unplaced"] : [],
        only: mode === "only" ? ["unplaced"] : [],
      });
      expect(loadWorkspaceGraphState("workspace-one", "actor-two"))
        .toEqual(freshWorkspaceGraphState());
    },
  );

  it("restores mixed UUID and unplaced include rules without admitting malformed sentinels", () => {
    const folderId = "00000000-0000-4000-8000-000000000011";
    const values = [folderId, "unplaced", "Unplaced", "unplaced ", " unplaced", "unplaced-extra"];
    saveWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision, {
      ...freshWorkspaceGraphState(),
      dataFilters: {
        ...DEFAULT_WORKSPACE_GRAPH_FILTERS,
        facet_rules: values.map((value) => ({ facet: "folder", mode: "include", value })),
      },
    });
    const restored = loadWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision);
    expect(workspaceGraphRequest(restored).folders.include).toEqual([folderId, "unplaced"]);
  });

  it("scopes every persisted field to an actor and workspace, not just pins", () => {
    const state = {
      ...freshWorkspaceGraphState(),
      lens: "evidence" as const,
      dataFilters: {
        ...DEFAULT_WORKSPACE_GRAPH_FILTERS,
        search: "private actor-one terms",
        document_kinds: ["library" as const],
        quick_filters: ["orphans" as const],
      },
      display: { show_edges: false, show_labels: false, show_orphans: true },
      selectedId: "context:one",
      pinnedIds: ["context:one"],
      pinnedPositions: { "context:one": { x: 12, y: 34 } },
      viewport: { zoom: 1.5, pan: { x: 20, y: 30 } },
      viewportEngine: "cosmos" as const,
    };
    saveWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision, state);
    expect(loadWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision))
      .toEqual({ ...state, viewerId: "actor-one" });
    expect(loadWorkspaceGraphState("workspace-one", "actor-two", scene.graph_revision))
      .toEqual(freshWorkspaceGraphState());
    expect(loadWorkspaceGraphState("workspace-two", "actor-one", scene.graph_revision))
      .toEqual(freshWorkspaceGraphState());
  });

  it("rejects legacy unowned state and forged scoped owners while preserving unrelated data", () => {
    const legacyKey = "dtxwiki:workspace-graph:workspace-one:latest";
    const forgedKey = `${workspaceGraphStorageScope("workspace-one", "actor-two")}latest`;
    const state = { ...freshWorkspaceGraphState(), viewerId: "actor-one", lens: "evidence" };
    window.sessionStorage.setItem(legacyKey, JSON.stringify({ state }));
    window.sessionStorage.setItem(forgedKey, JSON.stringify({ state }));
    window.sessionStorage.setItem("unrelated", "keep");
    expect(loadWorkspaceGraphState("workspace-one", "actor-two")).toEqual(freshWorkspaceGraphState());
    expect(window.sessionStorage.getItem(legacyKey)).toBeNull();
    expect(window.sessionStorage.getItem(forgedKey)).toBeNull();
    expect(window.sessionStorage.getItem("unrelated")).toBe("keep");
  });

  it("clears only the requested actor/workspace scope and never saves an unowned or mismatched state", () => {
    const state = freshWorkspaceGraphState();
    saveWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision, state);
    saveWorkspaceGraphState("workspace-two", "actor-one", scene.graph_revision, state);
    saveWorkspaceGraphState("workspace-one", "actor-two", scene.graph_revision, state);
    window.sessionStorage.setItem("unrelated", "keep");
    clearWorkspaceGraphState("workspace-one", "actor-one");
    expect(loadWorkspaceGraphState("workspace-one", "actor-one", scene.graph_revision)).toEqual(state);
    expect(loadWorkspaceGraphState("workspace-two", "actor-one", scene.graph_revision).viewerId).toBe("actor-one");
    expect(loadWorkspaceGraphState("workspace-one", "actor-two", scene.graph_revision).viewerId).toBe("actor-two");
    saveWorkspaceGraphState("workspace-three", "", scene.graph_revision, state);
    saveWorkspaceGraphState("workspace-three", "actor-two", scene.graph_revision, { ...state, viewerId: "actor-one" });
    expect(loadWorkspaceGraphState("workspace-three", "actor-two", scene.graph_revision)).toEqual(state);
    expect(window.sessionStorage.getItem("unrelated")).toBe("keep");
  });

  it("keeps folder Only mutually exclusive in staged filter state", () => {
    const include = updateFacetRule(DEFAULT_WORKSPACE_GRAPH_FILTERS, {
      facet: "folder",
      mode: "include",
      value: "folder-a",
    });
    const only = updateFacetRule(include, {
      facet: "folder",
      mode: "only",
      value: "folder-b",
    });
    expect(only.facet_rules).toEqual([
      { facet: "folder", mode: "only", value: "folder-b" },
    ]);
  });
});

describe("pending effective Refine filters", () => {
  const filters = (changes: Partial<WorkspaceGraphDataFilters> = {}): WorkspaceGraphDataFilters => ({
    ...freshWorkspaceGraphState().dataFilters,
    ...changes,
  });

  it("ignores cloned state, search boundary whitespace, ordering and duplicate set entries", () => {
    const active = filters({
      search: "visible terms",
      quick_filters: ["orphans", "cross_folder"],
      facet_rules: [
        { facet: "folder", value: "folder-a", mode: "include" },
        { facet: "folder", value: "unplaced", mode: "exclude" },
      ],
    });
    const draft = filters({
      search: "  visible terms\n",
      document_kinds: ["library", "context", "context"],
      quick_filters: ["cross_folder", "orphans", "orphans"],
      facet_rules: [...active.facet_rules].reverse().concat({ ...active.facet_rules[0] }),
    });
    expect(countPendingRefineFilters(draft, active)).toBe(0);
    expect(refineFiltersDiffer(draft, active)).toBe(false);
  });

  it.each([
    ["", " \t\n", 0], ["Alpha", " Alpha ", 0], ["Alpha", "alpha", 1],
    ["", "new query", 1], ["old query", "", 1],
  ])("compares effective server search %j → %j as %i change", (search, nextSearch, expected) => {
    const active = filters({ search });
    const draft = filters({ search: nextSearch });
    expect(countPendingRefineFilters(draft, active)).toBe(expected);
    expect(refineFiltersDiffer(draft, active)).toBe(expected > 0);
  });

  it("counts the entire document-kind set as one changed dimension", () => {
    expect(countPendingRefineFilters(filters({ document_kinds: ["context"] }), filters())).toBe(1);
    expect(countPendingRefineFilters(filters({ document_kinds: [] }), filters())).toBe(1);
    expect(countPendingRefineFilters(filters({ document_kinds: ["library", "context"] }), filters())).toBe(0);
  });

  it("counts the entire time basis/from/to tuple once and respects omitted bounds", () => {
    const draft = filters({
      time_basis: "relation_observed", time_from: "2026-10-01T00:00:00Z", time_to: "2026-10-08T23:59:59Z",
    });
    expect(countPendingRefineFilters(draft, filters())).toBe(1);
    expect(countPendingRefineFilters(filters({ time_from: "", time_to: "" }), filters())).toBe(0);
    expect(countPendingRefineFilters(filters({ time_to: "2026-10-08T23:59:59Z" }), filters())).toBe(1);
  });

  it("counts each added or removed quick-filter item once", () => {
    const active = filters({ quick_filters: ["orphans", "cross_folder"] });
    const draft = filters({ quick_filters: ["cross_folder", "recently_connected", "recently_connected"] });
    expect(countPendingRefineFilters(draft, active)).toBe(2);
    expect(countPendingRefineFilters(active, draft)).toBe(2);
  });

  it("counts each folder value once for add, remove or mode change", () => {
    const active = filters({ facet_rules: [
      { facet: "folder", value: "folder-a", mode: "include" },
      { facet: "folder", value: "folder-b", mode: "exclude" },
    ] });
    const draft = filters({ facet_rules: [
      { facet: "folder", value: "folder-a", mode: "exclude" },
      { facet: "folder", value: "unplaced", mode: "include" },
    ] });
    expect(countPendingRefineFilters(draft, active)).toBe(3);
    expect(countPendingRefineFilters(active, draft)).toBe(3);
  });

  it("ignores Include/Exclude rules suppressed by an effective Only selection", () => {
    const active = filters({ facet_rules: [
      { facet: "folder", value: "folder-a", mode: "only" },
      { facet: "folder", value: "folder-b", mode: "include" },
      { facet: "folder", value: "folder-c", mode: "exclude" },
    ] });
    const draft = filters({ facet_rules: [
      { facet: "folder", value: "folder-d", mode: "exclude" },
      { facet: "folder", value: "folder-a", mode: "only" },
      { facet: "folder", value: "folder-e", mode: "include" },
      { facet: "folder", value: "folder-a", mode: "only" },
    ] });
    expect(countPendingRefineFilters(draft, active)).toBe(0);
    expect(refineFiltersDiffer(draft, active)).toBe(false);
  });

  it("counts switching to Only by each effective folder-value change", () => {
    const active = filters({ facet_rules: [
      { facet: "folder", value: "folder-a", mode: "include" },
      { facet: "folder", value: "folder-b", mode: "exclude" },
    ] });
    const draft = updateFacetRule(active, { facet: "folder", value: "folder-a", mode: "only" });
    expect(countPendingRefineFilters(draft, active)).toBe(2);
    expect(countPendingRefineFilters(active, draft)).toBe(2);
  });

  it("does not count client document-kind facet rules unused by the wire request", () => {
    const draft = filters({ facet_rules: [{ facet: "document_kind", value: "context", mode: "exclude" }] });
    expect(countPendingRefineFilters(draft, filters())).toBe(0);
    expect(refineFiltersDiffer(draft, filters())).toBe(false);
  });

  it("ignores display and other presentation state", () => {
    const active = { ...filters(), display: { show_edges: true }, selectedId: "old" };
    const draft = { ...filters(), display: { show_edges: false }, selectedId: "new" };
    expect(countPendingRefineFilters(draft, active)).toBe(0);
  });

  it("sums effective dimensions without mutating either input", () => {
    const active = filters({
      quick_filters: ["orphans"],
      facet_rules: [
        { facet: "folder", value: "folder-a", mode: "include" },
        { facet: "folder", value: "folder-b", mode: "exclude" },
      ],
    });
    const draft = filters({
      search: " new terms ", document_kinds: ["library"],
      time_basis: "relation_observed", time_from: "2026-10-01T00:00:00Z", time_to: "2026-10-08T23:59:59Z",
      quick_filters: ["cross_folder"],
      facet_rules: [
        { facet: "folder", value: "folder-a", mode: "exclude" },
        { facet: "folder", value: "folder-c", mode: "include" },
      ],
    });
    const before = JSON.stringify({ active, draft });
    expect(countPendingRefineFilters(draft, active)).toBe(8);
    expect(refineFiltersDiffer(draft, active)).toBe(true);
    expect(JSON.stringify({ active, draft })).toBe(before);
  });

  it("preserves existing exclusive Only update/remove semantics", () => {
    const only = updateFacetRule(filters(), { facet: "folder", value: "folder-a", mode: "only" });
    const include = updateFacetRule(only, { facet: "folder", value: "folder-b", mode: "include" });
    expect(include.facet_rules).toEqual([{ facet: "folder", value: "folder-b", mode: "include" }]);
    expect(removeFacetRule(only, "folder", "folder-a").facet_rules).toEqual([]);
    expect(only.facet_rules).toEqual([{ facet: "folder", value: "folder-a", mode: "only" }]);
  });
});
