import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../shared/api/client";
import { readContextPage } from "../../../shared/api/context";
import { ContextEditPermalinkResolver } from "../ContextEditPermalinkResolver";

vi.mock("../../../shared/api/context", () => ({ readContextPage: vi.fn() }));

const selectWorkspaceId = vi.fn();
const contextState: {
  current: {
    selectedId?: string;
    workspaces?: { id: string }[];
    selectWorkspaceId?: typeof selectWorkspaceId;
  };
} = { current: {} };
vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => contextState.current,
}));

const workspaceId = "workspace-1";
const pageId = "00000000-0000-0000-0000-000000000123";

describe("ContextEditPermalinkResolver", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    contextState.current = {};
  });

  it("hands off to the slug-based editor route once the page resolves", async () => {
    vi.mocked(readContextPage).mockResolvedValue({ slug: "current-title" } as never);
    renderResolver(`/workspaces/${workspaceId}/context/pages/${pageId}/edit`);

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/context/current-title/edit",
      ),
    );
    expect(readContextPage).toHaveBeenCalledWith(workspaceId, pageId);
  });

  it("keeps the id permalink stable when the page slug changes", async () => {
    vi.mocked(readContextPage).mockResolvedValue({ slug: "renamed-page" } as never);
    renderResolver(`/workspaces/${workspaceId}/context/pages/${pageId}/edit`);

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/context/renamed-page/edit",
      ),
    );
  });

  it("uses the shared missing-page surface for an unknown id", async () => {
    vi.mocked(readContextPage).mockRejectedValue(
      new ApiError(404, {
        error_code: "not_found",
        message: "page not found",
        request_id: "request-1",
        warnings: [],
      }),
    );
    renderResolver(`/workspaces/${workspaceId}/context/pages/${pageId}/edit`);

    expect(
      await screen.findByRole("heading", { name: "We couldn't find that page" }),
    ).toBeInTheDocument();
  });

  it("uses localized fallback copy for an unexpected resolver failure", async () => {
    vi.mocked(readContextPage).mockRejectedValue(new Error("raw req-secret"));
    renderResolver(`/workspaces/${workspaceId}/context/pages/${pageId}/edit`);

    expect(await screen.findByText("Failed to load page")).toBeInTheDocument();
    expect(screen.queryByText("raw req-secret")).toBeNull();
  });

  it("silently resyncs the ambient workspace when the routed id is already a member", async () => {
    contextState.current = {
      selectedId: "ambient-workspace",
      workspaces: [{ id: workspaceId }, { id: "ambient-workspace" }],
      selectWorkspaceId,
    };
    vi.mocked(readContextPage).mockResolvedValue({ slug: "current-title" } as never);
    renderResolver(`/workspaces/${workspaceId}/context/pages/${pageId}/edit`);

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/context/current-title/edit",
      ),
    );
    expect(selectWorkspaceId).toHaveBeenCalledWith(workspaceId);
  });

  it("does not touch ambient workspace state when the routed id is not a known member", async () => {
    contextState.current = {
      selectedId: "ambient-workspace",
      workspaces: [{ id: "ambient-workspace" }],
      selectWorkspaceId,
    };
    vi.mocked(readContextPage).mockResolvedValue({ slug: "current-title" } as never);
    renderResolver(`/workspaces/unknown-workspace/context/pages/${pageId}/edit`);

    await screen.findByTestId("location");
    expect(selectWorkspaceId).not.toHaveBeenCalled();
  });
});

function renderResolver(initialEntry: string) {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/workspaces/:workspaceId/context/pages/:pageId/edit"
          element={<ContextEditPermalinkResolver />}
        />
        <Route path="/context/:slug/edit" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">
      {decodeURI(location.pathname)}
      {location.search}
    </div>
  );
}
