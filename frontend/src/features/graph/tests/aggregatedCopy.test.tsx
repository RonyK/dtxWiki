import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import type { WorkspaceGraphScene } from "../../../shared/api/workspaceGraph";
import { WorkspaceGraphScreen } from "../WorkspaceGraphScreen";
import { invalidateWorkspaceGraphCapability } from "../useWorkspaceGraphCapability";
import { workspaceGraphClusterCoverage } from "../workspace/WorkspaceGraphToolbar";

const { getWorkspaceGraphCapabilityMock, queryWorkspaceGraphMock } = vi.hoisted(() => ({
  getWorkspaceGraphCapabilityMock: vi.fn(),
  queryWorkspaceGraphMock: vi.fn(),
}));

vi.mock("../../../shared/api/workspaceGraph", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../shared/api/workspaceGraph")>()),
  queryWorkspaceGraph: queryWorkspaceGraphMock,
  getWorkspaceGraphCapability: getWorkspaceGraphCapabilityMock,
}));

vi.mock("../../../shared/workspace/WorkspaceContext", () => {
  const state = { selectedId: "workspace-one", selectedWorkspace: { id: "workspace-one", role: "owner" } };
  return { useWorkspaceContext: () => state, useOptionalWorkspaceContext: () => state };
});

vi.mock("../../../shared/auth/AuthContext", () => ({
  useAuth: () => ({ user: { user_id: "actor-one" } }),
  useOptionalAuth: () => ({ user: { user_id: "actor-one" } }),
}));

// Copy is the subject here, not the renderer: a bare canvas keeps the screen real.
vi.mock("../workspace/WorkspaceMapCanvas", async () => {
  const React = await import("react");
  return {
    WorkspaceMapCanvas: React.forwardRef(function MockWorkspaceMapCanvas(_props: object, ref) {
      React.useImperativeHandle(ref, () => ({
        centerOn: vi.fn(), fit: vi.fn(), pauseLayout: vi.fn(), pin: vi.fn(() => null), reset: vi.fn(),
        resumeLayout: vi.fn(), setViewport: vi.fn(), zoomIn: vi.fn(), zoomOut: vi.fn(),
      }));
      return <div data-testid="workspace-graph-canvas" />;
    }),
  };
});

type SceneOptions = {
  documents: number;
  clusters?: readonly number[];
  reasons?: WorkspaceGraphScene["omission_reasons"];
};

/** Every non-drawn document sits inside a cluster, as the server aggregates them. */
function scene({ documents, clusters = [], reasons = ["document_budget"] }: SceneOptions): WorkspaceGraphScene {
  const grouped = clusters.reduce((sum, members) => sum + members, 0);
  const nodes: WorkspaceGraphScene["nodes"] = [
    ...Array.from({ length: documents }, (_, index) => ({
      grain: "document" as const, id: `doc-${index}`, entity_kind: "context" as const, entity_id: `page-${index}`,
      label: `Document ${index}`, path: `/pages/${index}`, primary_folder_id: null, primary_folder_label: "None",
      reference_count: 1, evidence_count: 0, also_appears_in: [],
    })),
    ...clusters.map((members, index) => ({
      grain: "cluster" as const, id: `cluster-${index}`, cluster_kind: "link_cluster" as const,
      label: `Linked component · ${members} visible documents`, member_count: members, relation_count: 1,
    })),
  ];
  const aggregated = clusters.length > 0;
  return {
    schema_version: "workspace-graph-scene.v1", workspace_scope_considered: true, head_mode: "published",
    graph_revision: "revision-a", generated_at: "2026-10-18T00:00:00Z", algorithm_version: "workspace-graph.v1",
    metric_basis: "visible references", representation: aggregated ? "aggregated" : "exact",
    completeness: aggregated ? "capped" : "complete", truncated: reasons.length > 0,
    counts: { candidate_documents: documents + grouped, candidate_relations: 1, rendered_documents: documents,
      rendered_clusters: clusters.length, rendered_relations: 1 },
    omission_reasons: reasons, nodes,
    edges: documents > 1 ? [{
      id: "edge-0", source: "doc-0", target: aggregated ? "cluster-0" : "doc-1", relation_kind: "wikilink",
      label: "links to", layer: "knowledge" as const, direction: "directed" as const, aggregate: aggregated,
      relation_count: 1, cross_folder: false,
    }] : [],
    facet_counts: { document_kinds: [], relation_kinds: [], folders: [], quick_filters: [] },
    expansion_cursor: aggregated ? "cursor" : null, warnings: [],
    capabilities: { expand: aggregated, evidence: false, export: true, directory: true, compare: false,
      growth: false, provenance: false },
  };
}

async function renderMap(payload: WorkspaceGraphScene, language: "en" | "ko" = "en") {
  await i18n.changeLanguage(language);
  queryWorkspaceGraphMock.mockResolvedValue(payload);
  render(
    <div className="app-shell">
      <MemoryRouter initialEntries={["/graph"]}><WorkspaceGraphScreen /></MemoryRouter>
    </div>,
  );
  await screen.findByRole("heading", { name: i18n.t("graph:workspaceGraph.list.title") });
  const header = document.querySelector(".workspace-graph-coverage")?.textContent ?? "";
  const banner = Array.from(document.querySelectorAll(".workspace-graph-notice > p"))
    .map((notice) => notice.textContent?.replace(/\s+/g, " ").trim() ?? "").join(" | ");
  return { header, banner };
}

describe("Aggregated Map copy", () => {
  beforeEach(async () => {
    queryWorkspaceGraphMock.mockReset();
    getWorkspaceGraphCapabilityMock.mockReset();
    invalidateWorkspaceGraphCapability();
    window.sessionStorage.clear();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        addEventListener: vi.fn(), addListener: vi.fn(), dispatchEvent: vi.fn(), matches: false, media: query,
        onchange: null, removeEventListener: vi.fn(), removeListener: vi.fn(),
      } satisfies MediaQueryList)),
    });
    await i18n.changeLanguage("en");
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    await i18n.changeLanguage("en");
  });

  it("splits documents shown one by one from documents grouped into clusters, over the visible rows only", () => {
    const payload = scene({ documents: 7, clusters: [40, 3, 2] });
    expect(workspaceGraphClusterCoverage(payload)).toEqual({ shown: 7, grouped: 45, clusters: 3 });
    // A duplicate or unknown id counts for nothing.
    expect(workspaceGraphClusterCoverage(payload, ["doc-0", "doc-1", "cluster-0", "cluster-0", "missing"]))
      .toEqual({ shown: 2, grouped: 40, clusters: 1 });
    // No cluster in the scene, or none among the visible rows: no cluster figures.
    expect(workspaceGraphClusterCoverage(scene({ documents: 5, reasons: [] }))).toBeNull();
    expect(workspaceGraphClusterCoverage(payload, ["doc-0"])).toBeNull();
    expect(workspaceGraphClusterCoverage(payload, [])).toBeNull();
  });

  it("states documents shown and documents grouped into clusters, digit-grouped (header and banner)", async () => {
    const { header, banner } = await renderMap(scene({ documents: 1_200, clusters: [12_345, 3, 2] }));
    expect(header).toBe("1,200 shown · 12,350 in 3 clusters");
    expect(banner).toContain("Some documents are grouped into clusters. 1,200 shown individually, 12,350 grouped into 3 clusters.");
    expect(banner).toContain("The 2,000-document scene budget is reached.");
    // Grouped documents are inside the scene, and a full scene has no room to expand a cluster.
    expect(`${header} ${banner}`).not.toMatch(/outside|expand/i);
  });

  it("keeps the singular form for one cluster", async () => {
    const { header, banner } = await renderMap(scene({ documents: 6, clusters: [492] }));
    expect(header).toBe("6 shown · 492 in 1 cluster");
    expect(banner).toContain("6 shown individually, 492 grouped into 1 cluster.");
  });

  it("writes the same facts in Korean, the cluster count digit-grouped too", async () => {
    const { header, banner } = await renderMap(scene({ documents: 5, clusters: Array.from({ length: 1_200 }, () => 2) }), "ko");
    expect(header).toBe("문서 5개 표시 · 군집 1,200개에 2,400개");
    expect(banner).toContain("일부 문서가 군집으로 묶여 있습니다. 개별 표시 5개, 군집 1,200개에 2,400개 묶음.");
    expect(banner).toContain("2,000개 문서 장면 한도에 도달했습니다.");
  });

  it("keeps the old wording for a truncated scene without clusters", async () => {
    const { header, banner } = await renderMap(scene({ documents: 6, reasons: ["document_budget", "relation_budget"] }));
    expect(header).toBe("6/6 documents · Exact");
    expect(banner).toBe("Scene coverage notice. Some documents are outside the 2,000-document scene budget. "
      + "Some relationships are outside the 6,000-relation scene budget.");
  });

  it("keeps a partial scene's own lead, and claims no document limit when only relations were capped", async () => {
    const partial = scene({ documents: 6, clusters: [492], reasons: ["relation_budget", "projection_partial"] });
    partial.completeness = "partial";
    const { banner } = await renderMap(partial);
    expect(banner.startsWith("This scene is partial. 6 shown individually, 492 grouped into 1 cluster.")).toBe(true);
    expect(banner).toContain("outside the 6,000-relation scene budget");
    expect(banner).not.toContain("-document scene budget");
  });
});
