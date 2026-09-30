import { afterEach, describe, expect, it, vi } from "vitest";

import { listAccessKeys } from "../auth";
import { deleteSettingsMcpClient, listSettingsMcpClients } from "../mcpAccess";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

describe("MCP access settings API", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  // D-01v2: MCP clients stay workspace-scoped (they are one workspace's
  // observed caller identities); access keys became account-scoped and moved
  // off the deleted `/workspaces/{id}/settings/access-keys` route.
  it("keeps clients on the workspace route and keys on the account route", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url.includes("/settings/mcp-clients?")) {
        return jsonResponse({
          data: [],
          next_cursor: "client-next",
          total_count: 9,
          filtered_count: 3,
          status_counts: { active: 6, disabled: 3 },
        });
      }
      if (url.includes("/api/v1/auth/tokens?")) {
        return jsonResponse({
          data: [],
          next_cursor: null,
          evaluated_at: "2026-08-04T00:00:00Z",
          total_count: 8,
          filtered_count: 2,
          status_counts: { active: 6, expired: 1, revoked: 1 },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    await expect(
      listSettingsMcpClients(WORKSPACE_ID, {
        status: "disabled",
        q: "Claude Desktop",
        limit: 20,
        cursor: "opaque-client-cursor",
      }),
    ).resolves.toMatchObject({ next_cursor: "client-next", total_count: 9 });
    await expect(
      listAccessKeys({
        status: "inactive",
        limit: 20,
      }),
    ).resolves.toMatchObject({
      evaluated_at: "2026-08-04T00:00:00Z",
      filtered_count: 2,
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients?status=disabled&limit=20&q=Claude+Desktop&cursor=opaque-client-cursor`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `${BASE}/api/v1/auth/tokens?status=inactive&limit=20`,
      expect.objectContaining({ credentials: "include" }),
    );
    // The deleted workspace-scoped listing must never be requested again.
    expect(
      fetchMock.mock.calls.some(([input]) =>
        input.toString().includes("/settings/access-keys"),
      ),
    ).toBe(false);
  });

  it("uses object-specific delete endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({
        data: {
          deleted_client_id: "client-1",
          deleted_at: "2026-08-04T00:00:00Z",
        },
      }),
    );

    await expect(
      deleteSettingsMcpClient(WORKSPACE_ID, "client-1"),
    ).resolves.toMatchObject({ deleted_client_id: "client-1" });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/mcp-clients/client-1`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
