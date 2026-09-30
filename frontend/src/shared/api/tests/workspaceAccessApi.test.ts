import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  decideWorkspaceJoinRequest,
  listWorkspaceMembers,
  removeWorkspaceMember,
  updateWorkspaceAccess,
} from "../workspaceAccess";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";

describe("workspace access API", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("binds member cursors to the active query and role filters", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          items: [],
          total: 0,
          limit: 25,
          offset: 0,
          has_more: false,
          next_cursor: null,
        },
      }),
    );

    await listWorkspaceMembers(WORKSPACE_ID, {
      query: "Mina Kim",
      role: "editor",
      cursor: "opaque+cursor",
      limit: 25,
    });

    const url = new URL(fetchMock.mock.calls[0][0].toString());
    expect(url.pathname).toBe(`/api/v1/workspaces/${WORKSPACE_ID}/members`);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: "25",
      query: "Mina Kim",
      role: "editor",
      cursor: "opaque+cursor",
    });
  });

  it("uses optimistic base versions for canonical access updates", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          workspace_id: WORKSPACE_ID,
          updated_at: "2026-07-31T01:00:00Z",
          visibility: "private",
          join_policy: "invite_only",
          pending_join_request_count: 0,
          public_page_count: 0,
          closed_join_request_count: 1,
          cleared_public_page_count: 2,
        },
      }),
    );

    await updateWorkspaceAccess(WORKSPACE_ID, {
      visibility: "private",
      join_policy: "invite_only",
      confirm_access_change: true,
      expected_updated_at: "2026-07-31T00:00:00Z",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/access`,
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          visibility: "private",
          join_policy: "invite_only",
          confirm_access_change: true,
          expected_updated_at: "2026-07-31T00:00:00Z",
        }),
      }),
    );
  });

  it("keeps the landing preference separate from the accessible removal fallback", async () => {
    const defaultWorkspaceId = "00000000-0000-0000-0000-000000000010";
    const fallbackWorkspaceId = "00000000-0000-0000-0000-000000000011";
    const userId = "00000000-0000-0000-0000-000000000012";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          removed_user_id: userId,
          previous_role: "editor",
          default_workspace_id: defaultWorkspaceId,
          fallback_workspace_id: fallbackWorkspaceId,
          member_count: 2,
        },
      }),
    );

    const result = await removeWorkspaceMember(WORKSPACE_ID, userId);

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/members/${userId}`,
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(result).toEqual({
      removed_user_id: userId,
      previous_role: "editor",
      default_workspace_id: defaultWorkspaceId,
      fallback_workspace_id: fallbackWorkspaceId,
      member_count: 2,
    });
  });

  it("sends the current join-request attempt with an owner decision", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          request: {
            request_id: "00000000-0000-0000-0000-000000000002",
            workspace_id: WORKSPACE_ID,
            requester_user_id: "00000000-0000-0000-0000-000000000003",
            requester_email: "person@example.com",
            requester_display_name: "Person",
            status: "approved",
            attempt_no: 4,
            requested_at: "2026-07-31T00:00:00Z",
            updated_at: "2026-07-31T00:01:00Z",
            decided_at: "2026-07-31T00:01:00Z",
            decided_by_user_id: "00000000-0000-0000-0000-000000000004",
            decision_note: null,
            closed_reason: null,
          },
          new_attempt: false,
          membership_created: true,
        },
      }),
    );

    await decideWorkspaceJoinRequest(
      WORKSPACE_ID,
      "00000000-0000-0000-0000-000000000002",
      { decision: "approve", expected_attempt_no: 4 },
    );

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      decision: "approve",
      expected_attempt_no: 4,
    });
  });
});

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
