// Graph View extended checks: run on request (`make test-graph-view`), never
// in PR CI. They pin the edge cases of the expansion transcript on the screen.
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../../shared/api/client";
import type { WorkspaceGraphExpansionOutcome, WorkspaceGraphScene } from "../../../../shared/api/workspaceGraph";
import { i18n } from "../../../../shared/i18n";
import { WorkspaceGraphScreen } from "../../WorkspaceGraphScreen";
import { invalidateWorkspaceGraphCapability } from "../../useWorkspaceGraphCapability";

const { getWorkspaceGraphCapabilityMock, queryWorkspaceGraphMock } = vi.hoisted(() => ({
  getWorkspaceGraphCapabilityMock: vi.fn(),
  queryWorkspaceGraphMock: vi.fn(),
}));

vi.mock("../../../../shared/api/workspaceGraph", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../../shared/api/workspaceGraph")>()),
  queryWorkspaceGraph: queryWorkspaceGraphMock,
  getWorkspaceGraphCapability: getWorkspaceGraphCapabilityMock,
}));

vi.mock("../../../../shared/workspace/WorkspaceContext", () => {
  const state = { selectedId: "workspace-one", selectedWorkspace: { id: "workspace-one", role: "owner" } };
  return { useWorkspaceContext: () => state, useOptionalWorkspaceContext: () => state };
});

vi.mock("../../../../shared/auth/AuthContext", () => ({
  useAuth: () => ({ user: { user_id: "actor-one" } }),
  useOptionalAuth: () => ({ user: { user_id: "actor-one" } }),
}));

// The transcript is the subject, not the renderer: a bare canvas keeps the screen real.
vi.mock("../../workspace/WorkspaceMapCanvas", async () => {
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

/** An aggregated scene: documents on their own plus three-member clusters. */
function scene(clusterIds: readonly string[], options: {
  expansions?: WorkspaceGraphExpansionOutcome[];
  warnings?: string[];
} = {}): WorkspaceGraphScene {
  return {
    schema_version: "workspace-graph-scene.v1", workspace_scope_considered: true, head_mode: "published",
    graph_revision: "revision-a", generated_at: "2026-10-29T00:00:00Z", algorithm_version: "workspace-graph.v1",
    metric_basis: "visible references", representation: "aggregated", completeness: "capped", truncated: true,
    counts: { candidate_documents: 2 + clusterIds.length * 3, candidate_relations: 1, rendered_documents: 2,
      rendered_clusters: clusterIds.length, rendered_relations: 1 },
    omission_reasons: ["document_budget"],
    nodes: [
      ...["doc-a", "doc-b"].map((id) => ({
        grain: "document" as const, id, entity_kind: "context" as const, entity_id: `page-${id}`, label: `Document ${id}`,
        path: `/pages/${id}`, primary_folder_id: null, primary_folder_label: "None", reference_count: 1,
        evidence_count: 0, also_appears_in: [],
      })),
      ...clusterIds.map((id) => ({
        grain: "cluster" as const, id, cluster_kind: "link_cluster" as const, label: `Group ${id}`,
        member_count: 3, relation_count: 1,
      })),
    ],
    edges: [{
      id: "edge-0", source: "doc-a", target: "doc-b", relation_kind: "wikilink", label: "links to",
      layer: "knowledge" as const, direction: "directed" as const, aggregate: false, relation_count: 1, cross_folder: false,
    }],
    facet_counts: { document_kinds: [], relation_kinds: [], folders: [], quick_filters: [] },
    expansion_cursor: null, expansions: options.expansions ?? [], warnings: options.warnings ?? [],
    capabilities: { expand: true, evidence: false, export: true, directory: true, compare: false,
      growth: false, provenance: false },
  };
}

function outcome(clusterId: string, status: WorkspaceGraphExpansionOutcome["status"] = "applied"): WorkspaceGraphExpansionOutcome {
  const found = status !== "missing";
  return {
    cluster_id: clusterId, label: found ? `Group ${clusterId}` : null, member_count: found ? 3 : 0, offset: 0,
    revealed_count: status === "applied" ? 3 : 0, visible_count: status === "applied" ? 3 : 0, folded_count: 0,
    pinned: false, status,
  };
}

async function expand(clusterId: string) {
  const button = document.querySelector<HTMLButtonElement>(`li[data-graph-node-id="${clusterId}"] .workspace-graph-list-expand`);
  expect(button).not.toBeNull();
  await userEvent.click(button!);
}

const chips = () => document.querySelectorAll(".workspace-graph-expanded-groups li");

describe("Graph View expansion transcript (extended)", () => {
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

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("clears expansion chips and notices with their scene while scene warnings stay disclosed", async () => {
    const openIds = Array.from({ length: 8 }, (_, index) => `open-${index}`);
    queryWorkspaceGraphMock
      .mockResolvedValueOnce(scene([...openIds, "spare"], { warnings: ["secondary_placements_capped"] }))
      // The server's transcript already holds the full eight steps.
      .mockResolvedValueOnce(scene(["spare"], {
        expansions: openIds.map((id) => outcome(id)), warnings: ["secondary_placements_capped"],
      }))
      .mockResolvedValueOnce(scene(["open-0", "spare"]))
      .mockResolvedValueOnce(scene(["spare"], { expansions: [outcome("open-0")] }))
      .mockRejectedValueOnce(new ApiError(403, {
        error_code: "forbidden", message: "Access revoked.", request_id: "request-1", warnings: [],
      }));
    render(<div className="app-shell"><MemoryRouter initialEntries={["/graph"]}><WorkspaceGraphScreen /></MemoryRouter></div>);
    await screen.findByRole("heading", { name: "Searchable relationship list" });

    await expand("open-7");
    await waitFor(() => expect(chips()).toHaveLength(8));
    // The expansion report and the scene's own warning are both shown.
    expect(screen.getByText(/Expanded 3 documents from/)).toBeInTheDocument();
    expect(screen.getByText(/secondary folder placements are omitted/)).toBeInTheDocument();

    // A ninth group is refused without a request; the refusal belongs to this scene only.
    await expand("spare");
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/Up to 8 expanded groups stay open together/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Lens" }), "links");
    await waitFor(() => expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(3));
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    expect(chips()).toHaveLength(0);
    expect(screen.queryByText(/Up to 8 expanded groups/)).not.toBeInTheDocument();

    // Losing access clears the map; no group label may stay behind.
    await expand("open-0");
    await waitFor(() => expect(chips()).toHaveLength(1));
    await expand("spare");
    await screen.findByText(i18n.t("graph:workspaceGraph.ui.unavailable"));
    expect(document.querySelector(".workspace-graph-expanded-groups")).toBeNull();
    expect(screen.queryByText(/Group open-0/)).not.toBeInTheDocument();
  });

  it("waits for a pending replay, reports groups it could not reopen, and keeps keyboard focus", async () => {
    queryWorkspaceGraphMock
      .mockResolvedValueOnce(scene(["first", "second", "third"]))
      .mockResolvedValueOnce(scene(["second", "third"], { expansions: [outcome("first")] }))
      .mockResolvedValueOnce(scene(["third"], { expansions: [outcome("first"), outcome("second")] }));
    render(<div className="app-shell"><MemoryRouter initialEntries={["/graph"]}><WorkspaceGraphScreen /></MemoryRouter></div>);
    await screen.findByRole("heading", { name: "Searchable relationship list" });
    await expand("first");
    await waitFor(() => expect(chips()).toHaveLength(1));
    await expand("second");
    await waitFor(() => expect(chips()).toHaveLength(2));

    // With the details panel hidden there is no relationship list to land on.
    await userEvent.click(screen.getByRole("button", { name: "Hide Map details" }));
    let settle: (payload: WorkspaceGraphScene) => void = () => undefined;
    queryWorkspaceGraphMock.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve; }));
    await userEvent.click(screen.getByRole("button", { name: "Collapse “Group second”" }));
    // While the collapse is in flight nothing may start a replay from the old steps.
    expect(screen.getByRole("button", { name: "Collapse “Group first”" })).toBeDisabled();
    expect(queryWorkspaceGraphMock).toHaveBeenCalledTimes(4);

    settle(scene(["second", "third"], { expansions: [outcome("first", "no_capacity")] }));
    await screen.findByText("1 expanded group could not be reopened and was removed.");
    expect(chips()).toHaveLength(0);
    await waitFor(() => expect(document.querySelector(".feature-main.workspace-graph")).toHaveFocus());
  });
});
