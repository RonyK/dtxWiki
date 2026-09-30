import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cleanupOAuthConnections,
  listOAuthConsentPage,
  listOAuthConsents,
  previewOAuthConnectionCleanup,
  revokeOAuthConsent,
  updateOAuthConsentSettings,
} from "../oauth";

const BASE = "http://127.0.0.1:21080";

describe("oauth api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists OAuth consents with cookie credentials", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        jsonResponse({
          data: [oauthConsentPayload("client-1", "workspace-1")],
        }),
      );

    await expect(listOAuthConsents()).resolves.toEqual([
      oauthConsentPayload("client-1", "workspace-1"),
    ]);
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/oauth/consents`);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: "include",
    });
  });

  it("revokes a URL-shaped CIMD consent through query addressing", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: { revoked: true } }));

    await expect(
      revokeOAuthConsent("https://client.example/oauth/client.json"),
    ).resolves.toEqual({ revoked: true });
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BASE}/oauth/consents?client_id=https%3A%2F%2Fclient.example%2Foauth%2Fclient.json`,
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "DELETE",
      credentials: "include",
    });
  });

  it("passes limit and cursor and exposes the next consent page cursor", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: [oauthConsentPayload("client-2", "workspace-2")],
        next_cursor: "ordered-page-2",
      }),
    );

    await expect(
      listOAuthConsentPage({ limit: 20, cursor: "ordered-page-1" }),
    ).resolves.toEqual({
      data: [oauthConsentPayload("client-2", "workspace-2")],
      next_cursor: "ordered-page-2",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BASE}/oauth/consents?limit=20&cursor=ordered-page-1`,
    );
  });

  it("updates one consent group's future session policy without a workspace param", async () => {
    const consent = oauthConsentPayload("client 1", "workspace-1");
    // Type-only assertion: never invoked, but tsc still checks the directive
    // below, so this fails to compile if the generated request type ever
    // starts accepting a string TTL.
    const rejectsStringTtl = () => {
      // @ts-expect-error Generated request types reject string TTLs at compile time.
      void updateOAuthConsentSettings(consent, { refresh_ttl_seconds: "2592000" });
    };
    void rejectsStringTtl;
    const updated = {
      ...consent,
      refresh_ttl_seconds: 2592000,
      effective_refresh_ttl_seconds: 2592000,
      sliding_enabled: false,
      effective_sliding_enabled: false,
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: updated }));

    await expect(
      updateOAuthConsentSettings(consent, {
        refresh_ttl_seconds: 2592000,
        sliding_enabled: false,
      }),
    ).resolves.toEqual(updated);
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BASE}/oauth/consents?client_id=client+1`,
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "PATCH",
      credentials: "include",
      body: JSON.stringify({
        refresh_ttl_seconds: 2592000,
        sliding_enabled: false,
      }),
    });
  });

  it("replaces a consent group's whole allowed-workspace set", async () => {
    const consent = oauthConsentPayload("client-1", "workspace-1");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          ...consent,
          workspaces: [
            {
              id: "workspace-2",
              title: "Research",
              slug: "research",
              membership_role: "editor",
            },
          ],
        },
      }),
    );

    await expect(
      updateOAuthConsentSettings(consent, {
        workspace_ids: ["workspace-2"],
      }),
    ).resolves.toMatchObject({
      workspaces: [expect.objectContaining({ id: "workspace-2" })],
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BASE}/oauth/consents?client_id=client-1`,
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      body: JSON.stringify({ workspace_ids: ["workspace-2"] }),
    });
  });

  it("previews and runs expired connection cleanup", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () =>
        jsonResponse({
          data: {
            eligible_consent_count: 2,
            affected_mcp_client_count: 2,
            cleaned_consent_count: 0,
          },
        }),
      );

    await previewOAuthConnectionCleanup();
    await cleanupOAuthConnections();
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/oauth/consents/cleanup`);
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: "POST" });
  });
});

// D-01v2: one row is one (user, client) consent GROUP holding a SET of allowed
// workspaces. The singular workspace_id/title/slug triple is gone.
function oauthConsentPayload(clientId: string, workspaceId: string) {
  return {
    client_id: clientId,
    client_name: "Claude Desktop",
    scope: "mcp:read",
    workspaces: [
      {
        id: workspaceId,
        title: "Default workspace",
        slug: "default",
        membership_role: "owner",
      },
    ],
    granted_at: "2026-05-14T00:00:00Z",
    connection_state: "active" as const,
    last_used_at: null,
    next_expiry: null,
    mcp_client_ids: [],
    // Null means "follow the instance default"; the effective_* pair is what
    // the grant actually gets after the instance ceiling is applied.
    refresh_ttl_seconds: null,
    sliding_enabled: true,
    effective_refresh_ttl_seconds: 15552000,
    effective_sliding_enabled: true,
    // MCP-05(b): the instance ceiling the session-length control uses to
    // filter/label its options -- distinct from effective_refresh_ttl_seconds
    // (min(instance, requested)), which is indistinguishable from the
    // ceiling once an explicit consent TTL is smaller.
    instance_max_refresh_ttl_seconds: 15552000,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
