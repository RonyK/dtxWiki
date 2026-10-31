import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AUTH_EXPIRED_EVENT, ApiError } from "../../api/client";
import { AuthProvider, useAuth } from "../AuthContext";
import { reviewDraftKey } from "../../lib/reviewDraftStorage";

describe("AuthContext", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("keeps an authenticated session when a later refresh returns 5xx", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ data: userPayload("user@example.com") }))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            error_code: "internal_error",
            message: "upstream unavailable",
            request_id: "req-1",
            warnings: [],
          },
          503,
        ),
      );

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    expect(await screen.findByTestId("status")).toHaveTextContent("authenticated");
    expect(screen.getByTestId("email")).toHaveTextContent("user@example.com");
    window.sessionStorage.setItem("dtxwiki:workspace-graph:v2:actor:workspace:latest", "private state");

    await user.click(screen.getByRole("button", { name: "refresh" }));

    await waitFor(() => {
      expect(screen.getByTestId("status")).toHaveTextContent("authenticated");
      expect(screen.getByTestId("email")).toHaveTextContent("user@example.com");
      expect(screen.getByTestId("session-error")).toHaveTextContent(
        "The server could not complete the request. Please try again.",
      );
    });
    expect(window.sessionStorage.getItem("dtxwiki:workspace-graph:v2:actor:workspace:latest")).toBe("private state");
  });

  it("treats 401 refresh as anonymous without sessionError", async () => {
    window.sessionStorage.setItem("dtxwiki:workspace-graph:workspace-one:latest", "private graph data");
    window.sessionStorage.setItem("unrelated", "keep");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse(
        {
          error_code: "invalid_session",
          message: "session cookie is missing",
          request_id: "req-2",
          warnings: [],
        },
        401,
      ),
    );

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    expect(await screen.findByTestId("status")).toHaveTextContent("anonymous");
    expect(screen.getByTestId("email")).toHaveTextContent("none");
    expect(screen.getByTestId("session-error")).toHaveTextContent("none");
    expect(screen.getByTestId("session-checked")).toHaveTextContent("checked");
    expect(window.sessionStorage.getItem("dtxwiki:workspace-graph:workspace-one:latest")).toBeNull();
    expect(window.sessionStorage.getItem("unrelated")).toBe("keep");
  });

  it("can defer the initial session check on public auth routes", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ data: userPayload("user@example.com") }));

    render(
      <AuthProvider skipInitialSessionCheck>
        <AuthProbe />
      </AuthProvider>,
    );

    expect(screen.getByTestId("status")).toHaveTextContent("anonymous");
    expect(screen.getByTestId("session-checked")).toHaveTextContent("pending");
    expect(fetchSpy).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "refresh" }));

    await waitFor(() => {
      expect(screen.getByTestId("status")).toHaveTextContent("authenticated");
      expect(screen.getByTestId("session-checked")).toHaveTextContent("checked");
    });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("ignores a late pre-login 401 after a successful login transition", async () => {
    const user = userEvent.setup();
    const sessionProbe = deferred<Response>();
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => sessionProbe.promise);

    render(
      <AuthProvider skipInitialSessionCheck>
        <AuthProbe />
      </AuthProvider>,
    );

    await user.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const signal = fetchSpy.mock.calls[0][1]?.signal as AbortSignal;

    await user.click(screen.getByRole("button", { name: "authenticate" }));
    expect(screen.getByTestId("status")).toHaveTextContent("authenticated");
    expect(screen.getByTestId("email")).toHaveTextContent("login@example.com");
    expect(signal.aborted).toBe(true);

    await act(async () => {
      sessionProbe.resolve(
        jsonResponse(
          {
            error_code: "invalid_session",
            message: "the pre-login probe had no cookie",
            request_id: "req-stale-401",
            warnings: [],
          },
          401,
        ),
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId("refresh-settled")).toHaveTextContent("1");
    });
    expect(screen.getByTestId("status")).toHaveTextContent("authenticated");
    expect(screen.getByTestId("email")).toHaveTextContent("login@example.com");
    expect(screen.getByTestId("session-error")).toHaveTextContent("none");
  });

  it("does not let a late session success resurrect a signed-out user", async () => {
    const user = userEvent.setup();
    const sessionProbe = deferred<Response>();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = input.toString();
      if (url.endsWith("/auth/me")) {
        return sessionProbe.promise;
      }
      if (url.endsWith("/auth/logout")) {
        return Promise.resolve(jsonResponse({ data: { logged_out: true } }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <AuthProvider skipInitialSessionCheck>
        <AuthProbe />
      </AuthProvider>,
    );

    await user.click(screen.getByRole("button", { name: "authenticate" }));
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const signal = fetchSpy.mock.calls[0][1]?.signal as AbortSignal;

    await user.click(screen.getByRole("button", { name: "sign out" }));
    await waitFor(() => {
      expect(screen.getByTestId("status")).toHaveTextContent("anonymous");
    });
    expect(signal.aborted).toBe(true);

    await act(async () => {
      sessionProbe.resolve(
        jsonResponse({ data: userPayload("stale-session@example.com") }),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("refresh-settled")).toHaveTextContent("1");
    });
    expect(screen.getByTestId("status")).toHaveTextContent("anonymous");
    expect(screen.getByTestId("email")).toHaveTextContent("none");
  });

  it("applies only the newest session refresh when responses finish out of order", async () => {
    const user = userEvent.setup();
    const firstProbe = deferred<Response>();
    const secondProbe = deferred<Response>();
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => firstProbe.promise)
      .mockImplementationOnce(() => secondProbe.promise);

    render(
      <AuthProvider skipInitialSessionCheck>
        <AuthProbe />
      </AuthProvider>,
    );

    await user.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const firstSignal = fetchSpy.mock.calls[0][1]?.signal as AbortSignal;
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
    expect(firstSignal.aborted).toBe(true);

    await act(async () => {
      secondProbe.resolve(
        jsonResponse({ data: userPayload("latest@example.com") }),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("email")).toHaveTextContent("latest@example.com");
      expect(screen.getByTestId("refresh-settled")).toHaveTextContent("1");
    });

    await act(async () => {
      firstProbe.resolve(
        jsonResponse({ data: userPayload("stale@example.com") }),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("refresh-settled")).toHaveTextContent("2");
    });
    expect(screen.getByTestId("status")).toHaveTextContent("authenticated");
    expect(screen.getByTestId("email")).toHaveTextContent("latest@example.com");
  });

  it("invalidates an outstanding session probe when auth expires elsewhere", async () => {
    const user = userEvent.setup();
    const sessionProbe = deferred<Response>();
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => sessionProbe.promise);

    render(
      <AuthProvider skipInitialSessionCheck>
        <AuthProbe />
      </AuthProvider>,
    );

    await user.click(screen.getByRole("button", { name: "authenticate" }));
    await user.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const signal = fetchSpy.mock.calls[0][1]?.signal as AbortSignal;

    act(() => {
      window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT));
    });
    expect(signal.aborted).toBe(true);
    expect(screen.getByTestId("status")).toHaveTextContent("anonymous");
    expect(screen.getByTestId("email")).toHaveTextContent("none");

    await act(async () => {
      sessionProbe.resolve(
        jsonResponse({ data: userPayload("expired@example.com") }),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("refresh-settled")).toHaveTextContent("1");
    });
    expect(screen.getByTestId("status")).toHaveTextContent("anonymous");
    expect(screen.getByTestId("email")).toHaveTextContent("none");
  });

  it("aborts an outstanding session probe on provider unmount", async () => {
    const user = userEvent.setup();
    const sessionProbe = deferred<Response>();
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => sessionProbe.promise);
    const view = render(
      <AuthProvider skipInitialSessionCheck>
        <AuthProbe />
      </AuthProvider>,
    );

    await user.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1));
    const signal = fetchSpy.mock.calls[0][1]?.signal as AbortSignal;

    view.unmount();

    expect(signal.aborted).toBe(true);
    sessionProbe.resolve(jsonResponse({ data: userPayload("late@example.com") }));
  });

  it("loads signup capability independently and fails closed while refreshing", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ data: { signup_enabled: true } }))
      .mockResolvedValueOnce(jsonResponse({ data: { signup_enabled: false } }));

    render(
      <AuthProvider loadInstanceCapabilities skipInitialSessionCheck>
        <CapabilitiesProbe />
      </AuthProvider>,
    );

    expect(screen.getByTestId("capabilities-checked")).toHaveTextContent("pending");
    expect(screen.getByTestId("signup-enabled")).toHaveTextContent("disabled");
    await waitFor(() => {
      expect(screen.getByTestId("capabilities-checked")).toHaveTextContent(
        "checked",
      );
      expect(screen.getByTestId("signup-enabled")).toHaveTextContent("enabled");
    });

    await user.click(screen.getByRole("button", { name: "refresh capabilities" }));
    await waitFor(() => {
      expect(screen.getByTestId("signup-enabled")).toHaveTextContent("disabled");
    });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("uses the authenticated server-admin flag for maintenance error guidance", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({ data: userPayload("admin@example.com", true) }),
    );

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    expect(await screen.findByTestId("status")).toHaveTextContent("authenticated");

    const error = new ApiError(500, {
      error_code: "database_schema_mismatch",
      message: "private backend detail",
      request_id: "req-schema",
      warnings: [],
    });
    expect(error.message).toContain("dtxwiki bootstrap");
    expect(error.message).toContain("docker compose run --rm init");
    expect(error.message).not.toContain("private backend detail");
  });

  it("keeps initial transient refresh failures out of anonymous state", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse(
        {
          error_code: "internal_error",
          message: "session service unavailable",
          request_id: "req-transient",
          warnings: [],
        },
        503,
      ),
    );

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    expect(await screen.findByTestId("status")).toHaveTextContent(
      "transient_error",
    );
    expect(screen.getByTestId("email")).toHaveTextContent("none");
    expect(screen.getByTestId("session-error")).toHaveTextContent(
      "The server could not complete the request. Please try again.",
    );
  });

  it("clears local auth state even when logout request fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ data: userPayload("user@example.com") }))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            error_code: "internal_error",
            message: "logout failed",
            request_id: "req-3",
            warnings: [],
          },
          500,
        ),
      );

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    expect(await screen.findByTestId("status")).toHaveTextContent("authenticated");

    await user.click(screen.getByRole("button", { name: "sign out" }));

    await waitFor(() => {
      expect(screen.getByTestId("status")).toHaveTextContent("anonymous");
      expect(screen.getByTestId("email")).toHaveTextContent("none");
      expect(screen.getByTestId("session-error")).toHaveTextContent("none");
    });
  });

  it("clears the persisted workspace selection on sign out (WS-05)", async () => {
    // Without this, the next person to sign in on the same browser profile
    // briefly sees the previous user's workspace title/id/role through the
    // stale localStorage key and in-memory snapshot, which outlive the
    // authenticated workspace subtree's identity-keyed remount.
    const user = userEvent.setup();
    const draftKey = reviewDraftKey("user-1", "workspace-1", "review-1");
    sessionStorage.setItem(draftKey, "Private review note");
    sessionStorage.setItem(`${draftKey}.pending`, JSON.stringify({ decisionId: "pending-decision" }));
    sessionStorage.setItem("unrelated.session.preference", "preserve");
    window.localStorage.setItem(
      "dtxwiki.selectedWorkspaceId",
      "00000000-0000-0000-0000-000000000abc",
    );
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ data: userPayload("user@example.com") }))
      .mockResolvedValueOnce(jsonResponse({ data: {} }));

    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>,
    );

    expect(await screen.findByTestId("status")).toHaveTextContent("authenticated");
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBe(
      "00000000-0000-0000-0000-000000000abc",
    );

    await user.click(screen.getByRole("button", { name: "sign out" }));

    await waitFor(() => {
      expect(screen.getByTestId("status")).toHaveTextContent("anonymous");
    });
    expect(window.localStorage.getItem("dtxwiki.selectedWorkspaceId")).toBeNull();
    expect(sessionStorage.getItem(draftKey)).toBeNull();
    expect(sessionStorage.getItem(`${draftKey}.pending`)).toBeNull();
    expect(sessionStorage.getItem("unrelated.session.preference")).toBe("preserve");
  });

  it("purges only Map session storage when auth expires elsewhere", () => {
    window.sessionStorage.setItem("dtxwiki:workspace-graph:v2:actor:workspace:latest", "private state");
    window.sessionStorage.setItem("dtxwiki:workspace-graph:workspace:latest", "legacy private state");
    window.sessionStorage.setItem("unrelated", "keep");
    render(<AuthProvider skipInitialSessionCheck><AuthProbe /></AuthProvider>);
    act(() => window.dispatchEvent(new CustomEvent(AUTH_EXPIRED_EVENT)));
    expect(window.sessionStorage.getItem("dtxwiki:workspace-graph:v2:actor:workspace:latest")).toBeNull();
    expect(window.sessionStorage.getItem("dtxwiki:workspace-graph:workspace:latest")).toBeNull();
    expect(window.sessionStorage.getItem("unrelated")).toBe("keep");
  });

  it("purges Map state when an authenticated session is replaced by another actor", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({
      data: { ...userPayload("first@example.com"), user_id: "00000000-0000-0000-0000-000000000011" },
    }));
    render(<AuthProvider><AuthProbe /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("email")).toHaveTextContent("first@example.com"));
    const key = "dtxwiki:workspace-graph:v2:actor:workspace:latest";
    window.sessionStorage.setItem(key, "first actor state");
    window.sessionStorage.setItem("unrelated", "keep");
    await userEvent.click(screen.getByRole("button", { name: "authenticate" }));
    expect(screen.getByTestId("email")).toHaveTextContent("login@example.com");
    expect(window.sessionStorage.getItem(key)).toBeNull();
    expect(window.sessionStorage.getItem("unrelated")).toBe("keep");
  });

  it("purges Map data before a pending sign-out round trip and again after it completes", async () => {
    const logout = deferred<Response>();
    vi.spyOn(globalThis, "fetch").mockImplementation(() => logout.promise);
    render(<AuthProvider skipInitialSessionCheck><AuthProbe /></AuthProvider>);
    await userEvent.click(screen.getByRole("button", { name: "authenticate" }));
    const key = "dtxwiki:workspace-graph:v2:actor:workspace:latest";
    window.sessionStorage.setItem(key, "private state");
    window.sessionStorage.setItem("unrelated", "keep");
    await userEvent.click(screen.getByRole("button", { name: "sign out" }));
    expect(window.sessionStorage.getItem(key)).toBeNull();
    window.sessionStorage.setItem(key, "late private state");
    await act(async () => logout.resolve(jsonResponse({ data: {} })));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("anonymous"));
    expect(window.sessionStorage.getItem(key)).toBeNull();
    expect(window.sessionStorage.getItem("unrelated")).toBe("keep");
  });
});

function AuthProbe() {
  const {
    refreshSession,
    sessionChecked,
    sessionError,
    setAuthenticatedSession,
    signOut,
    status,
    user,
  } = useAuth();
  const [refreshSettled, setRefreshSettled] = useState(0);
  return (
    <div>
      <div data-testid="status">{status}</div>
      <div data-testid="session-checked">{sessionChecked ? "checked" : "pending"}</div>
      <div data-testid="email">{user?.email ?? "none"}</div>
      <div data-testid="session-error">{sessionError ?? "none"}</div>
      <div data-testid="refresh-settled">{refreshSettled}</div>
      <button
        type="button"
        onClick={() => {
          void refreshSession().finally(() => {
            setRefreshSettled((current) => current + 1);
          });
        }}
      >
        refresh
      </button>
      <button
        type="button"
        onClick={() =>
          setAuthenticatedSession({
            user: userPayload("login@example.com"),
            expires_at: "2026-09-17T00:00:00Z",
          })
        }
      >
        authenticate
      </button>
      <button type="button" onClick={() => void signOut()}>
        sign out
      </button>
    </div>
  );
}

function CapabilitiesProbe() {
  const {
    capabilitiesChecked,
    capabilitiesError,
    refreshCapabilities,
    signupEnabled,
  } = useAuth();
  return (
    <div>
      <div data-testid="capabilities-checked">
        {capabilitiesChecked ? "checked" : "pending"}
      </div>
      <div data-testid="signup-enabled">
        {signupEnabled ? "enabled" : "disabled"}
      </div>
      <div data-testid="capabilities-error">{capabilitiesError ?? "none"}</div>
      <button type="button" onClick={() => void refreshCapabilities()}>
        refresh capabilities
      </button>
    </div>
  );
}

function userPayload(email: string, isServerAdmin = false) {
  return {
    user_id: "00000000-0000-0000-0000-000000000010",
    email,
    display_name: "Test User",
    status: "active",
    is_server_admin: isServerAdmin,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}
