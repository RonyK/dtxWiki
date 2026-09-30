import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceDirectoryItem } from "../../../shared/api/workspaceDirectory";
import { i18n } from "../../../shared/i18n";
import { WorkspaceDirectoryScreen } from "../WorkspaceDirectoryScreen";

const api = vi.hoisted(() => ({
  cancelWorkspaceJoin: vi.fn(),
  listWorkspaceDirectory: vi.fn(),
  requestWorkspaceJoin: vi.fn(),
}));

vi.mock("../../../shared/api/workspaceDirectory", () => api);

describe("WorkspaceDirectoryScreen", () => {
  beforeEach(() => {
    api.cancelWorkspaceJoin.mockReset();
    api.listWorkspaceDirectory.mockReset();
    api.requestWorkspaceJoin.mockReset();
  });

  it("renders only the safe directory fields and distinct request policies", async () => {
    api.listWorkspaceDirectory.mockResolvedValue({
      items: [
        directoryItem(),
        directoryItem({
          workspace_id: "workspace-2",
          title: "Private Invitations",
          description: null,
          visibility: "public",
          join_policy: "invite_only",
        }),
      ],
      next_cursor: null,
    });

    renderScreen();

    expect(
      screen.getByRole("status", { name: "Loading workspace directory" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Research Guild" }))
      .toBeInTheDocument();
    expect(screen.getByText("Shared research notes")).toBeInTheDocument();
    expect(screen.getByText("Team")).toBeInTheDocument();
    expect(screen.getByText("Requests open")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request to join" }))
      .toBeInTheDocument();
    expect(screen.getByText("Invite only")).toBeInTheDocument();
    expect(
      screen.getByText("Ask a workspace owner for an invitation."),
    ).toBeInTheDocument();
    expect(screen.queryByText("user@example.com")).not.toBeInTheDocument();
    expect(screen.queryByText("workspace-1")).not.toBeInTheDocument();
  });

  it("requests, announces, and cancels a pending join request", async () => {
    const user = userEvent.setup();
    api.listWorkspaceDirectory.mockResolvedValue({
      items: [directoryItem()],
      next_cursor: null,
    });
    api.requestWorkspaceJoin.mockResolvedValue(mutationData("pending"));
    api.cancelWorkspaceJoin.mockResolvedValue(mutationData("cancelled"));

    renderScreen();

    await user.click(
      await screen.findByRole("button", { name: "Request to join" }),
    );
    expect(api.requestWorkspaceJoin).toHaveBeenCalledWith("workspace-1");
    expect(await screen.findByText("Requested")).toBeInTheDocument();
    expect(screen.getByText("Join request sent for Research Guild."))
      .toHaveClass("visually-hidden");

    await user.click(screen.getByRole("button", { name: "Cancel request" }));
    expect(api.cancelWorkspaceJoin).toHaveBeenCalledWith("workspace-1");
    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request again" }))
      .toBeInTheDocument();
  });

  it("searches by name and renders the search-specific empty state", async () => {
    const user = userEvent.setup();
    api.listWorkspaceDirectory.mockResolvedValue({ items: [], next_cursor: null });

    renderScreen();

    await screen.findByRole("heading", {
      name: "No discoverable workspaces yet",
    });
    await user.type(
      screen.getByRole("searchbox", { name: "Search workspaces" }),
      "Research",
    );
    await user.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => {
      expect(api.listWorkspaceDirectory).toHaveBeenLastCalledWith(
        expect.objectContaining({ query: "Research" }),
      );
    });
    expect(await screen.findByRole("heading", { name: "No matching workspaces" }))
      .toBeInTheDocument();
  });

  it("offers a fresh join request for a since-removed approved membership, with Korean copy", async () => {
    // WS-01: the directory only ever returns workspaces the viewer is NOT a
    // member of. An "approved" row therefore means "approved in the past,
    // then removed" — the dead "Refresh my workspaces" action must not
    // appear; the only meaningful action is requesting again.
    await i18n.changeLanguage("ko");
    api.listWorkspaceDirectory.mockResolvedValueOnce({
      items: [directoryItem({ current_request_status: "approved" })],
      next_cursor: null,
    });
    api.requestWorkspaceJoin.mockResolvedValue(mutationData("pending"));
    const user = userEvent.setup();

    renderScreen();

    expect(await screen.findByRole("heading", { name: "워크스페이스 찾기" }))
      .toBeInTheDocument();
    expect(await screen.findByText("승인됨")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "내 워크스페이스 새로고침" }),
    ).not.toBeInTheDocument();
    const requestAgain = screen.getByRole("button", { name: "다시 요청" });
    expect(requestAgain).toBeInTheDocument();

    await user.click(requestAgain);
    expect(api.requestWorkspaceJoin).toHaveBeenCalledWith("workspace-1");
  });

  it("keeps load errors localized and retryable", async () => {
    const user = userEvent.setup();
    api.listWorkspaceDirectory
      .mockRejectedValueOnce(new Error("raw backend failure req-123"))
      .mockResolvedValueOnce({ items: [], next_cursor: null });

    renderScreen();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load the workspace directory.",
    );
    expect(screen.queryByText(/raw backend failure/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", {
      name: "No discoverable workspaces yet",
    })).toBeInTheDocument();
  });
});

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/workspaces/find"]}>
      <WorkspaceDirectoryScreen />
    </MemoryRouter>,
  );
}

function directoryItem(
  overrides: Partial<WorkspaceDirectoryItem> = {},
): WorkspaceDirectoryItem {
  return { ...directoryItemBase(), ...overrides };
}

function directoryItemBase() {
  return {
    workspace_id: "workspace-1",
    title: "Research Guild",
    description: "Shared research notes",
    visibility: "team" as const,
    join_policy: "request" as const,
    current_request_status: null as
      | "pending"
      | "approved"
      | "rejected"
      | "cancelled"
      | "closed"
      | null,
  };
}

function mutationData(status: "pending" | "cancelled") {
  return {
    request: {
      request_id: "request-1",
      workspace_id: "workspace-1",
      requester_user_id: "user-1",
      requester_email: "user@example.com",
      requester_display_name: "User",
      status,
      attempt_no: 1,
      requested_at: "2026-07-31T00:00:00Z",
      updated_at: "2026-07-31T00:00:00Z",
      decided_at: null,
      decided_by_user_id: null,
      decision_note: null,
      closed_reason: null,
    },
    new_attempt: status === "pending",
    membership_created: false,
  };
}
