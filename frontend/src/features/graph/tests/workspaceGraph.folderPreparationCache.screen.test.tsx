import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import type { WorkspaceGraphScene } from "../../../shared/api/workspaceGraph";
import { WorkspaceGraphScreen } from "../WorkspaceGraphScreen";
import { invalidateWorkspaceGraphCapability } from "../useWorkspaceGraphCapability";
import * as folderScenePreparation from "../map/folderScene";

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

// The renderer is irrelevant here: only the folder metadata the Screen derives from the scene object matters.
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

const RESEARCH = "00000000-0000-4000-8000-000000000011";
const SOURCES = "00000000-0000-4000-8000-000000000012";

describe("Workspace Map folder preparation is computed once per scene", () => {
  beforeEach(async () => {
    queryWorkspaceGraphMock.mockReset();
    getWorkspaceGraphCapabilityMock.mockReset();
    invalidateWorkspaceGraphCapability();
    window.sessionStorage.clear();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        addEventListener: vi.fn(), addListener: vi.fn(), dispatchEvent: vi.fn(), matches: false,
        media: query, onchange: null, removeEventListener: vi.fn(), removeListener: vi.fn(),
      } satisfies MediaQueryList)),
    });
    await i18n.changeLanguage("en");
  });

  afterEach(() => vi.restoreAllMocks());

  const toggle = () => screen.getByRole("button", { name: "Folder context" });
  const folderLabels = () => Array.from(document.querySelectorAll("[data-map-folder-id]"))
    .map((button) => button.textContent ?? "");

  async function renderMap() {
    render(
      <div className="app-shell">
        <MemoryRouter initialEntries={["/graph"]}><WorkspaceGraphScreen /></MemoryRouter>
      </div>,
    );
    await screen.findByTestId("workspace-graph-canvas");
  }

  it("prepares nothing until folder grouping is first enabled, then once however often it is switched", async () => {
    const prepare = vi.spyOn(folderScenePreparation, "prepareWorkspaceMapFolderScene");
    const scene = exactScene();
    queryWorkspaceGraphMock.mockResolvedValue(scene);
    await renderMap();
    fireEvent.change(screen.getByRole("searchbox", { name: "Find in visible map" }), { target: { value: "Alpha" } });
    expect(prepare).not.toHaveBeenCalled();

    await userEvent.click(toggle());
    const labels = ["Research · 1", "Sources · 1"];
    expect(folderLabels()).toEqual(labels);
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(prepare.mock.calls[0][0]).toBe(scene);

    for (const [pressed, expected] of [["false", []], ["true", labels], ["false", []], ["true", labels]] as const) {
      await userEvent.click(toggle());
      expect(toggle()).toHaveAttribute("aria-pressed", pressed);
      expect(folderLabels()).toEqual(expected);
    }
    expect(prepare).toHaveBeenCalledTimes(1);
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(1);
  });
});

function exactScene(): WorkspaceGraphScene {
  const document = (id: string, label: string, entityId: string, folderId: string, kind: "context" | "library") => ({
    grain: "document" as const, id, entity_kind: kind, entity_id: entityId, label,
    path: `/pages/${label.toLocaleLowerCase()}`, primary_folder_id: folderId,
    primary_folder_label: folderId === RESEARCH ? "Research" : "Sources",
    reference_count: 1, evidence_count: 0, also_appears_in: [],
  });
  return {
    schema_version: "workspace-graph-scene.v1", workspace_scope_considered: true, head_mode: "published",
    graph_revision: "revision-0000000000000001", generated_at: "2026-08-05T00:00:00+00:00",
    algorithm_version: "workspace-graph.v1", metric_basis: "distinct_visible_published_wikilink_sources.pre_quick.v1",
    representation: "exact", completeness: "complete", truncated: false,
    counts: { candidate_documents: 2, candidate_relations: 1, rendered_documents: 2, rendered_clusters: 0, rendered_relations: 1 },
    omission_reasons: [],
    nodes: [
      document("context:alpha", "Alpha", "00000000-0000-4000-8000-000000000001", RESEARCH, "context"),
      document("library:beta", "Library Beta", "00000000-0000-4000-8000-000000000002", SOURCES, "library"),
    ],
    edges: [{ id: "edge:alpha-beta", source: "context:alpha", target: "library:beta", relation_kind: "wikilink",
      label: "links to", layer: "knowledge", direction: "directed", aggregate: false, relation_count: 1,
      occurrence_weight: 3, cross_folder: true }],
    facet_counts: {
      document_kinds: [{ value: "context", label: "Context", count: 1 }, { value: "library", label: "Library", count: 1 }],
      relation_kinds: [{ value: "wikilink", label: "Wiki link", count: 1 }],
      folders: [{ value: RESEARCH, label: "Research", count: 1 }, { value: SOURCES, label: "Sources", count: 1 }],
      quick_filters: [],
    },
    expansion_cursor: null, warnings: [],
    capabilities: { expand: false, evidence: false, export: true, directory: true, compare: false, growth: false, provenance: false },
    folder_topology: {
      schema_version: "workspace-graph-folders.v1",
      folders: [
        { id: RESEARCH, label: "Research", parent_folder_id: null, direct_member_count: 1 },
        { id: SOURCES, label: "Sources", parent_folder_id: null, direct_member_count: 1 },
      ],
      memberships: [
        { node_id: "context:alpha", folder_id: RESEARCH, entity_kind: "context", member_count: 1 },
        { node_id: "library:beta", folder_id: SOURCES, entity_kind: "library", member_count: 1 },
      ],
      edge_slices: [{ edge_id: "edge:alpha-beta", source_folder_id: RESEARCH, source_entity_kind: "context",
        target_folder_id: SOURCES, target_entity_kind: "library", relation_count: 1, occurrence_weight: 3 }],
    },
  };
}
