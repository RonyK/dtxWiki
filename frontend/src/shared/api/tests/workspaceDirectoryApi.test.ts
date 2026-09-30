import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cancelWorkspaceJoin,
  listPendingWorkspaceJoins,
  listWorkspaceDirectory,
  requestWorkspaceJoin,
} from "../workspaceDirectory";

const BASE = "http://127.0.0.1:21080";

describe("workspace directory API", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("encodes the directory search and cursor without exposing unrelated fields", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          items: [directoryItem()],
          next_cursor: "next page/+",
        },
      }),
    );

    const data = await listWorkspaceDirectory({
      cursor: "next page/+",
      limit: 10,
      query: "  Research & Design  ",
    });

    expect(data.items).toEqual([directoryItem()]);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspace-directory?q=Research+%26+Design&cursor=next+page%2F%2B&limit=10`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("uses the session-only request and cancellation endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: mutationData("pending") }),
    );

    await requestWorkspaceJoin("workspace-1");
    expect(fetchMock).toHaveBeenLastCalledWith(
      `${BASE}/api/v1/workspaces/workspace-1/join-requests`,
      expect.objectContaining({ method: "POST" }),
    );

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ data: mutationData("cancelled") }),
    );
    await cancelWorkspaceJoin("workspace-1");
    expect(fetchMock).toHaveBeenLastCalledWith(
      `${BASE}/api/v1/workspaces/workspace-1/join-requests/me`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("paginates the dedicated pending-join query to completion", async () => {
    const first = { ...directoryItem(), current_request_status: "pending" as const };
    const second = {
      ...first,
      workspace_id: "workspace-2",
      title: "Second",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ data: { items: [first], next_cursor: "next" } }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ data: { items: [second], next_cursor: null } }),
      );

    await expect(listPendingWorkspaceJoins()).resolves.toEqual([first, second]);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      `${BASE}/api/v1/workspace-directory?pending_only=true&limit=50`,
      `${BASE}/api/v1/workspace-directory?cursor=next&pending_only=true&limit=50`,
    ]);
  });
});

function directoryItem() {
  return {
    workspace_id: "workspace-1",
    title: "Research",
    description: "Shared research notes",
    visibility: "team",
    join_policy: "request",
    current_request_status: null,
  } as const;
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

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
