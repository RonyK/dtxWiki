import { afterEach, describe, expect, it, vi } from "vitest";

import { issueAdminPasswordReset, searchAdminUsers } from "../adminUsers";

const BASE = "http://127.0.0.1:21080";

describe("admin users API", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("encodes the protected user search query and pagination", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: { items: [], total: 0, limit: 10, offset: 20 },
      }),
    );

    await searchAdminUsers("Alex + Research", { limit: 10, offset: 20 });

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/admin/users?query=Alex+%2B+Research&limit=10&offset=20`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("sends only delivery and token revocation in the reset request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          user_id: "user-1",
          delivery: "email",
          expires_at: "2026-08-01T03:30:00Z",
          reset_url: null,
          revoke_api_tokens: false,
        },
      }),
    );

    await issueAdminPasswordReset("user-1", {
      delivery: "email",
      revoke_api_tokens: false,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/admin/users/user-1/password-reset`,
      expect.objectContaining({
        credentials: "include",
        method: "POST",
        body: JSON.stringify({
          delivery: "email",
          revoke_api_tokens: false,
        }),
      }),
    );
  });
});

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
