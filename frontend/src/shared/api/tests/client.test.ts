import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AUTH_EXPIRED_EVENT,
  ApiError,
  apiErrorMessage,
  apiRequest,
  setApiErrorViewerIsServerAdmin,
} from "../client";

describe("api client token", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
    delete window.__DTXWIKI_RUNTIME_CONFIG__;
  });

  it("uses VITE_DTXWIKI_API_TOKEN when set", async () => {
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "custom-token");
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({}));

    await apiRequest("/api/v1/pages");

    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer custom-token");
  });

  it("omits Authorization when env is unset", async () => {
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "");
    vi.stubEnv("PROD", false);
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({}));

    await apiRequest("/api/v1/pages");

    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBeNull();
  });

  it("uses same-origin API and no default token for packaged builds", async () => {
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "");
    vi.stubEnv("PROD", true);
    vi.stubEnv("DEV", false);
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({}));

    await apiRequest("/api/v1/pages");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/pages");
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBeNull();
  });

  it("ignores build-time VITE_* API overrides outside the dev server", async () => {
    // A developer's frontend/.env.local is inlined by Vite; a production
    // bundle must neither send that token nor talk to that origin.
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "leaked-dev-token");
    vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:9999");
    vi.stubEnv("PROD", true);
    vi.stubEnv("DEV", false);
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({}));

    await apiRequest("/api/v1/pages");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/pages");
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBeNull();
  });

  it("honours VITE_* API overrides on the dev server", async () => {
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "dev-token");
    vi.stubEnv("VITE_API_BASE_URL", "http://127.0.0.1:9999/");
    vi.stubEnv("PROD", false);
    vi.stubEnv("DEV", true);
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({}));

    await apiRequest("/api/v1/pages");

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:9999/api/v1/pages");
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer dev-token");
  });

  it("uses backend-provided runtime config when present", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "");
    vi.stubEnv("PROD", true);
    vi.stubEnv("DEV", false);
    window.__DTXWIKI_RUNTIME_CONFIG__ = {
      apiBaseUrl: "http://127.0.0.1:8765/",
      apiToken: "runtime-token",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({}));

    await apiRequest("/api/v1/pages");

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8765/api/v1/pages");
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer runtime-token");
  });

  it("wraps non-json error responses", async () => {
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "custom-token");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("upstream unavailable", { status: 503 }),
    );

    await expect(apiRequest("/api/v1/pages")).rejects.toMatchObject({
      status: 503,
      body: {
        error_code: "internal_error",
        message: "upstream unavailable",
        warnings: [],
      },
    });
  });

  it("passes AbortSignal through to fetch", async () => {
    const controller = new AbortController();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({}));

    await apiRequest("/api/v1/pages", { signal: controller.signal });

    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
  });
});

describe("api client errors", () => {
  afterEach(() => {
    setApiErrorViewerIsServerAdmin(false);
  });

  it("localizes API errors without exposing backend text or request metadata", () => {
    const error = new ApiError(
      500,
      errorBody(
        "database_schema_mismatch",
        "Database schema is out of date. Reset the dev database, run dtxwiki bootstrap, then retry.",
      ),
    );

    expect(error.message).toBe(
      "The database needs an update. Ask a server administrator to run the database upgrade, then retry.",
    );
    expect(error.message).not.toContain("database_schema_mismatch");
    expect(error.message).not.toContain("r1");
    expect(error.message).not.toContain("Reset the dev database");
    expect(apiErrorMessage(error)).toBe(error.message);
    expect(apiErrorMessage("unknown", "Fallback message")).toBe(
      "Fallback message",
    );
    expect(apiErrorMessage(new Error("raw req-secret"), "Fallback message")).toBe(
      "Fallback message",
    );
  });

  it("gives a server administrator exact schema-upgrade commands", () => {
    setApiErrorViewerIsServerAdmin(true);

    const error = new ApiError(
      500,
      errorBody(
        "database_schema_mismatch",
        "raw database schema implementation detail",
      ),
    );

    expect(error.message).toContain("sudo systemctl stop dtxwiki-worker dtxwiki");
    expect(error.message).toContain("/opt/dtxwiki/.venv/bin/dtxwiki bootstrap");
    expect(error.message).toContain("docker compose run --rm init");
    expect(error.message).toContain(".venv/bin/dtxwiki bootstrap");
    expect(error.message).toContain("/health/ready");
    expect(error.message).not.toContain("raw database schema");
  });

  it("keeps unknown backend text private while retaining a support reference", () => {
    const error = new ApiError(
      500,
      errorBody("future_backend_error", "Sensitive backend detail"),
    );

    expect(apiErrorMessage(error)).toBe(
      "Request failed. Please try again.",
    );
    expect(apiErrorMessage(error)).not.toContain("Sensitive backend detail");
  });

  it("keeps internal details private while retaining a support reference", () => {
    const error = new ApiError(
      500,
      errorBody("internal_error", "Sensitive database exception"),
    );

    expect(apiErrorMessage(error)).toBe(
      "The server could not complete the request. Please try again.",
    );
    expect(apiErrorMessage(error)).not.toContain("database exception");
  });

  it("provides an actionable localized message for a known domain error", () => {
    const error = new ApiError(
      409,
      errorBody(
        "duplicate_slug_under_parent",
        "raw duplicate slug implementation detail",
      ),
    );

    expect(apiErrorMessage(error)).toBe(
      "Another item already uses this name in the selected folder.",
    );
    expect(apiErrorMessage(error)).not.toContain("implementation detail");
  });

  it("explains a retryable Library storage failure without exposing backend text", () => {
    const error = new ApiError(
      503,
      errorBody(
        "library_storage_temporarily_unavailable",
        String.raw`C:\private\library.db could not be opened`,
      ),
    );

    expect(apiErrorMessage(error)).toBe(
      "Library storage is temporarily unavailable. Retry with the same file; a successful earlier attempt will not be duplicated.",
    );
    expect(apiErrorMessage(error)).not.toContain("library.db");
  });
});

describe("api client session expiry", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    window.localStorage.clear();
    delete window.__DTXWIKI_RUNTIME_CONFIG__;
  });

  it("clears the persisted workspace, broadcasts, and redirects on a 401", async () => {
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-1");
    window.localStorage.setItem("dtxwiki.selectedWorkspaceSlug", "default");
    const assign = vi.fn();
    vi.stubGlobal("location", {
      ...window.location,
      pathname: "/dashboard",
      search: "?period=7d",
      assign,
    });
    const onExpired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(errorBody("invalid_session", "Session expired"), 401),
    );

    try {
      await expect(apiRequest("/api/v1/pages")).rejects.toMatchObject({
        status: 401,
      });
    } finally {
      window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
    }

    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBeNull();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceSlug")).toBeNull();
    expect(onExpired).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith(
      "/login?next=%2Fdashboard%3Fperiod%3D7d",
    );
  });

  it("does not treat auth-endpoint 401s as an expired session", async () => {
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-1");
    const assign = vi.fn();
    vi.stubGlobal("location", {
      ...window.location,
      pathname: "/dashboard",
      search: "",
      assign,
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(errorBody("unauthorized", "Unauthorized"), 401),
    );

    await expect(apiRequest("/api/v1/auth/me")).rejects.toMatchObject({
      status: 401,
    });

    expect(assign).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-1",
    );
  });

  it("keeps a missing recent-reauth proof separate from session expiry", async () => {
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-1");
    const assign = vi.fn();
    vi.stubGlobal("location", {
      ...window.location,
      pathname: "/settings",
      search: "?section=users",
      assign,
    });
    const onExpired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse(
        errorBody(
          "recent_reauthentication_required",
          "recent verification required",
        ),
        401,
      ),
    );

    try {
      // The directory search and a later admin mutation share the rule.
      for (const path of [
        "/api/v1/admin/users?query=alex&limit=25&offset=0",
        "/api/v1/admin/users/user-1/identities/rebind",
      ]) {
        await expect(apiRequest(path)).rejects.toMatchObject({ status: 401 });
      }
    } finally {
      window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
    }

    expect(onExpired).not.toHaveBeenCalled();
    expect(assign).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-1",
    );
  });

  it("skips session-expiry handling when an API token is configured", async () => {
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "custom-token");
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-1");
    const assign = vi.fn();
    vi.stubGlobal("location", {
      ...window.location,
      pathname: "/dashboard",
      search: "",
      assign,
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(errorBody("invalid_api_token", "Bad token"), 401),
    );

    await expect(apiRequest("/api/v1/pages")).rejects.toMatchObject({
      status: 401,
    });

    expect(assign).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-1",
    );
  });

  it("does not redirect again when already on the login screen", async () => {
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-1");
    const assign = vi.fn();
    vi.stubGlobal("location", {
      ...window.location,
      pathname: "/login",
      search: "?next=%2Fdashboard",
      assign,
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(errorBody("invalid_session", "Session expired"), 401),
    );

    await expect(apiRequest("/api/v1/pages")).rejects.toMatchObject({
      status: 401,
    });

    expect(assign).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBeNull();
  });

  it("leaves non-401 errors untouched", async () => {
    window.localStorage.setItem("dtxwiki.selectedWorkspaceId", "workspace-1");
    const assign = vi.fn();
    vi.stubGlobal("location", {
      ...window.location,
      pathname: "/dashboard",
      search: "",
      assign,
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(errorBody("forbidden", "Forbidden"), 403),
    );

    await expect(apiRequest("/api/v1/pages")).rejects.toMatchObject({
      status: 403,
    });

    expect(assign).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "workspace-1",
    );
  });
});

describe("api client request-path guard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("rejects a workspace-scoped path with an empty id without calling fetch", async () => {
    // A failed workspace resolve used to leave an empty id in screen state, so
    // every later call hit `/workspaces//...`. FastAPI cannot match an empty
    // path parameter, so the user saw a misleading "not found" instead of the
    // real cause. Fail before the network so the cause stays visible.
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(
      apiRequest("/api/v1/workspaces//ask/sessions"),
    ).rejects.toMatchObject({
      body: { error_code: "workspace_not_selected" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects placeholder ids that stringified into the path", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(
      apiRequest("/api/v1/workspaces/undefined/ask/sessions"),
    ).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("flags a non-workspace empty segment separately", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(apiRequest("/api/v1//pages")).rejects.toMatchObject({
      body: { error_code: "invalid_request_path" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("still allows a normal path and a trailing query string", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({}));

    await apiRequest("/api/v1/workspaces/workspace-1/ask/sessions?cursor=abc");

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    "/api/v1/pages/undefined",
    "/api/v1/auth/tokens/null",
    "/api/v1/workspaces/ws-1/tree/nodes/undefined",
    "/api/v1/workspaces/ws-1/ask/sessions/null/turns",
  ])("rejects a stringified JS value in the id position of %s", async (path) => {
    // Narrowing the check to the segment after `workspaces` let every other
    // id-bearing builder through, so `readPage(undefined)` reached the network
    // and came back as a server 404 instead of a clear client-side error.
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(apiRequest(path)).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["/api/v1/pages/by-slug/null", "/api/v1/pages/by-slug/undefined"])(
    "lets %s through: those are legal slugs, not a missing id",
    async (path) => {
      // SLUG_PATTERN accepts the literal strings, so a page titled "null" slugs
      // to `null`. Rejecting them everywhere made that page permanently
      // unopenable, and status 0 dodged the caller's 404 branch so the UI could
      // only tell the user to reload.
      const fetchMock = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValue(jsonResponse({}));

      await apiRequest(path);

      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it("still rejects a placeholder where the workspace id belongs", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(
      apiRequest("/api/v1/workspaces/undefined/ask/sessions"),
    ).rejects.toMatchObject({
      body: { error_code: "workspace_not_selected" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["/", "/api/v1/pages/", "api/v1/pages"])(
    "treats %s as a real path, not a missing id",
    async (path) => {
      // The empty entries a leading and a trailing slash produce say nothing
      // about a missing id, and a relative path has no leading entry to drop
      // at all — the guard used to discard its first real segment instead.
      const fetchMock = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValue(jsonResponse({}));

      await apiRequest(path);

      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );
});

function errorBody(errorCode: string, message: string) {
  return {
    error_code: errorCode,
    message,
    request_id: "r1",
    warnings: [],
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
