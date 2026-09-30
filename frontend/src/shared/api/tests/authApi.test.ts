import { afterEach, describe, expect, it, vi } from "vitest";

import {
  changePassword,
  confirmPasswordReset,
  createApiToken,
  deleteRevokedApiToken,
  getAuthCapabilities,
  getMe,
  listAccessKeys,
  listApiTokens,
  login,
  logout,
  reauthenticate,
  revokeApiToken,
  signup,
  updateApiTokenWorkspaces,
} from "../auth";

const BASE = "http://127.0.0.1:21080";

describe("auth api", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
  });

  it("calls session endpoints with cookie credentials", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        jsonResponse({ data: sessionPayload("user@example.com") }),
      );

    const session = await login({
      email: "user@example.com",
      password: "correct-password",
      remember_me: false,
    });

    expect(session.user.email).toBe("user@example.com");
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/api/v1/auth/login`);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      credentials: "include",
    });
    // The session-lifetime choice is never implicit on the wire: the server
    // fails closed on an absent flag, so the caller has to state it.
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
      remember_me: false,
    });
  });

  it("maps auth response payloads", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const url = input.toString();
        if (url.endsWith("/signup")) {
          return jsonResponse({ data: sessionPayload("new@example.com") });
        }
        if (url.endsWith("/me")) {
          return jsonResponse({ data: userPayload("new@example.com") });
        }
        if (url.endsWith("/password")) {
          return jsonResponse({ data: sessionPayload("new@example.com") });
        }
        if (url.endsWith("/logout")) {
          return jsonResponse({ data: { logged_out: true } });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    await expect(
      signup({
        email: "new@example.com",
        password: "correct-password",
        display_name: "New User",
        remember_me: true,
      }),
    ).resolves.toMatchObject({ user: { email: "new@example.com" } });
    await expect(getMe()).resolves.toMatchObject({ email: "new@example.com" });
    await expect(
      changePassword({
        current_password: "correct-password",
        new_password: "new-password",
      }),
    ).resolves.toMatchObject({ user: { email: "new@example.com" } });
    await expect(logout()).resolves.toEqual({ logged_out: true });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("passes an AbortSignal through the current-session request", async () => {
    const controller = new AbortController();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: userPayload("user@example.com") }));

    await expect(getMe(controller.signal)).resolves.toMatchObject({
      email: "user@example.com",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/me`,
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  it("reads unauthenticated instance capabilities", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: { signup_enabled: false } }));

    await expect(getAuthCapabilities()).resolves.toEqual({
      signup_enabled: false,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/api/v1/auth/capabilities`);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: "include",
    });
  });

  it("confirms a password reset without creating a session client-side", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: { reset: true } }));

    await expect(
      confirmPasswordReset({
        token: "dtxr_one-time-token",
        new_password: "replacement-password",
      }),
    ).resolves.toEqual({ reset: true });
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BASE}/api/v1/auth/password-reset/confirm`,
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      credentials: "include",
    });
  });

  it("reauthenticates with a password while keeping proof in the HttpOnly cookie", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: { reauthenticated_until: "2026-08-01T03:05:00Z" },
      }),
    );

    await expect(
      reauthenticate({ current_password: "admin-current-password" }),
    ).resolves.toEqual({ reauthenticated_until: "2026-08-01T03:05:00Z" });
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/reauthenticate`,
      expect.objectContaining({
        body: JSON.stringify({ current_password: "admin-current-password" }),
        credentials: "include",
        method: "POST",
      }),
    );
  });

  it("maps API token management payloads", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url.endsWith("/tokens") && !init?.method) {
        return jsonResponse({ data: [apiTokenPayload()] });
      }
      if (url.endsWith("/tokens") && init?.method === "POST") {
        return jsonResponse({
          data: {
            api_token: apiTokenPayload(),
            plain_token: "dtxw_plaintext_once",
          },
        });
      }
      if (url.endsWith("/tokens/token-1")) {
        return jsonResponse({
          data: { ...apiTokenPayload(), revoked_at: "2026-05-14T00:05:00Z" },
        });
      }
      if (url.endsWith("/tokens/token-1/record")) {
        return jsonResponse({ data: { id: "token-1", deleted: true } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    await expect(listApiTokens()).resolves.toHaveLength(1);
    // D-01v2: a key is issued against a SET of allowed workspaces. The MVP UI
    // sends no per-grant `scopes`, so each grant inherits the token ceiling.
    await expect(
      createApiToken({
        workspace_grants: [
          { workspace_id: "workspace-1" },
          { workspace_id: "workspace-2" },
        ],
        name: "CI",
        scopes: ["mcp:read"],
      }),
    ).resolves.toMatchObject({ plain_token: "dtxw_plaintext_once" });
    expect(
      JSON.parse(
        String(
          fetchMock.mock.calls.find(
            ([, init]) => init?.method === "POST",
          )?.[1]?.body,
        ),
      ),
    ).toEqual({
      workspace_grants: [
        { workspace_id: "workspace-1" },
        { workspace_id: "workspace-2" },
      ],
      name: "CI",
      scopes: ["mcp:read"],
    });
    await expect(revokeApiToken("token-1")).resolves.toMatchObject({
      revoked_at: "2026-05-14T00:05:00Z",
    });
    await expect(deleteRevokedApiToken("token-1")).resolves.toEqual({
      id: "token-1",
      deleted: true,
    });
  });

  it("replaces a token's whole allowed-workspace set through PATCH", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: apiTokenPayload() }));

    await expect(
      updateApiTokenWorkspaces("token-1", ["workspace-2", "workspace-3"]),
    ).resolves.toMatchObject({ id: "token-1" });
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/auth/tokens/token-1`,
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          workspace_grants: [
            { workspace_id: "workspace-2" },
            { workspace_id: "workspace-3" },
          ],
        }),
      }),
    );
  });

  it("passes status, search and cursor through the account-scoped key listing", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: [],
        next_cursor: null,
        evaluated_at: "2026-09-09T00:00:00Z",
        total_count: 0,
        filtered_count: 0,
        status_counts: { active: 0, expired: 0, revoked: 0 },
      }),
    );

    await listAccessKeys({ status: "all", q: "ci", cursor: "page-2" });
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BASE}/api/v1/auth/tokens?status=all&limit=20&q=ci&cursor=page-2`,
    );
  });
});

function sessionPayload(email: string) {
  return {
    user: userPayload(email),
    expires_at: "2026-05-14T12:00:00Z",
  };
}

function userPayload(email: string) {
  return {
    user_id: "00000000-0000-0000-0000-000000000010",
    email,
    display_name: "User",
    status: "active",
  };
}

function apiTokenPayload() {
  return {
    id: "token-1",
    name: "CI",
    scopes: ["mcp:read"],
    created_at: "2026-05-14T00:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
