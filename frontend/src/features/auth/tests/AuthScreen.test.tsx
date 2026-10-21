import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../shared/auth/AuthContext";
import { AuthScreen } from "../AuthScreen";

const BASE = "http://127.0.0.1:21080";

describe("AuthScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({ data: { signup_enabled: true } });
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse(
          {
            error_code: "invalid_session",
            message: "session cookie is missing",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      if (url === `${BASE}/api/v1/auth/signup`) {
        expect(init?.credentials).toBe("include");
        expect(JSON.parse(String(init?.body))).toMatchObject({
          email: "new@example.com",
          password: "correct-password",
          display_name: "New User",
        });
        return jsonResponse({ data: sessionPayload("new@example.com") });
      }
      if (url === `${BASE}/api/v1/auth/login`) {
        expect(init?.credentials).toBe("include");
        expect(JSON.parse(String(init?.body))).toMatchObject({
          email: "login@example.com",
          password: "correct-password",
        });
        return jsonResponse({ data: sessionPayload("login@example.com") });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
  });

  // Name and email wiring is proven by the normalized payload below; the
  // password check is the only one that does not change the sent value.
  it("blocks an invalid password before the authentication request", async () => {
    render(<MemoryRouter initialEntries={["/login?mode=signup"]}><AuthProvider loadInstanceCapabilities><AuthScreen /></AuthProvider></MemoryRouter>);
    await screen.findByRole("heading", { name: "Sign up" });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "new@example.com" } });
    const password = screen.getByLabelText("Password");
    fireEvent.change(password, { target: { value: "😀".repeat(129) } });
    fireEvent.submit(password.closest("form")!);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/auth/signup"))).toHaveLength(0);
    expect(password).toHaveValue("😀".repeat(129));
  });

  it("accepts Unicode byte/code-point boundaries without native email or UTF-16 restrictions", async () => {
    const original = vi.mocked(fetch).getMockImplementation()!;
    let sent: Record<string, unknown> | undefined;
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      if (String(input).endsWith("/auth/signup")) {
        sent = JSON.parse(String(init?.body));
        return jsonResponse({ data: sessionPayload("한글@example.com") });
      }
      return original(input, init);
    });
    render(<MemoryRouter initialEntries={["/login?mode=signup"]}><AuthProvider loadInstanceCapabilities><AuthScreen /></AuthProvider></MemoryRouter>);
    await screen.findByRole("heading", { name: "Sign up" });
    const email = screen.getByLabelText("Email");
    expect(email).toHaveAttribute("type", "text");
    fireEvent.change(email, { target: { value: " 한글@EXAMPLE.COM " } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: " " + "😀".repeat(32) + " " } });
    const password = screen.getByLabelText("Password");
    fireEvent.change(password, { target: { value: "😀".repeat(128) } });
    fireEvent.submit(password.closest("form")!);
    await waitFor(() => expect(sent).toEqual(expect.objectContaining({ email: "한글@example.com", display_name: "😀".repeat(32), password: "😀".repeat(128) })));
  });

  it("rejects raw multiline name paste without sanitizing it into another name", async () => {
    render(<MemoryRouter initialEntries={["/login?mode=signup"]}><AuthProvider loadInstanceCapabilities><AuthScreen /></AuthProvider></MemoryRouter>);
    await screen.findByRole("heading", { name: "Sign up" });
    const name = screen.getByLabelText("Display name");
    expect(fireEvent.paste(name, { clipboardData: { getData: () => "Alice\nBob" } })).toBe(false);
    expect(name).toHaveValue("");
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(fireEvent.keyDown(name, { key: "Enter", isComposing: true })).toBe(false);
  });

  it("directs password recovery to the server operator, not a workspace admin", async () => {
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "Forgot your password? Contact the server operator — workspace admins cannot reset accounts.",
      ),
    ).toBeInTheDocument();
  });

  it("confirms a completed reset on the sign-in screen", async () => {
    render(
      <MemoryRouter initialEntries={["/login?reset=complete"]}>
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("Password reset. Sign in with your new password."),
    ).toBeInTheDocument();
  });

  it("submits signup and shows the account status", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("radio", { name: /^Sign up$/ }));
    await user.type(screen.getByLabelText("Email"), "new@example.com");
    await user.type(screen.getByLabelText("Display name"), "New User");
    await user.type(screen.getByLabelText("Password"), "correct-password");
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    expect(await screen.findByText("Account created")).toBeInTheDocument();
    // L10: the redirect lands a tick after the success message, so poll for it
    // instead of reading the location synchronously (the flaky-under-load case).
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/dashboard?welcome=1",
      ),
    );
  });

  it("uses a full-page load for an OAuth authorize next after login", async () => {
    const user = userEvent.setup();
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    render(
      <MemoryRouter
        initialEntries={[
          "/login?next=%2Foauth%2Fauthorize%3Fclient_id%3Ddyn_1%26state%3Dstate-123",
        ]}
      >
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Email"), "login@example.com");
    await user.type(screen.getByLabelText("Password"), "correct-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    // /oauth/* is served by the backend, so the redirect must be a full-page
    // load rather than SPA navigation onto the router catch-all.
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        "/oauth/authorize?client_id=dyn_1&state=state-123",
      ),
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/login?next=%2Foauth%2Fauthorize%3Fclient_id%3Ddyn_1%26state%3Dstate-123",
    );
  });

  it("invalidates a stale existing-session result when login starts", async () => {
    const user = userEvent.setup();
    const existingSession = deferred<Response>();
    const loginRequest = deferred<Response>();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return Promise.resolve(jsonResponse({ data: { signup_enabled: true } }));
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return existingSession.promise;
      }
      if (url === `${BASE}/api/v1/auth/login`) {
        return loginRequest.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider loadInstanceCapabilities skipInitialSessionCheck>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(
        fetchSpy.mock.calls.some(
          ([input]) => input.toString() === `${BASE}/api/v1/auth/me`,
        ),
      ).toBe(true);
    });
    const existingSessionCall = fetchSpy.mock.calls.find(
      ([input]) => input.toString() === `${BASE}/api/v1/auth/me`,
    );
    const existingSessionSignal = existingSessionCall?.[1]?.signal as AbortSignal;
    await user.type(screen.getByLabelText("Email"), "login@example.com");
    await user.type(screen.getByLabelText("Password"), "correct-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => {
      expect(
        fetchSpy.mock.calls.some(
          ([input]) => input.toString() === `${BASE}/api/v1/auth/login`,
        ),
      ).toBe(true);
    });
    expect(existingSessionSignal.aborted).toBe(true);

    await act(async () => {
      existingSession.resolve(
        jsonResponse({ data: sessionPayload("old-session@example.com").user }),
      );
      await existingSession.promise;
    });
    expect(
      screen.queryByRole("link", { name: "Account security" }),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/login");

    await act(async () => {
      loginRequest.resolve(
        jsonResponse({ data: sessionPayload("login@example.com") }),
      );
    });
    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    });
  });

  it("preserves the next path through signup", async () => {
    const user = userEvent.setup();
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    render(
      <MemoryRouter
        initialEntries={["/login?mode=signup&next=%2Fpages%2Fwelcome"]}
      >
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Email"), "new@example.com");
    await user.type(screen.getByLabelText("Display name"), "New User");
    await user.type(screen.getByLabelText("Password"), "correct-password");
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    expect(await screen.findByText("Account created")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/pages/welcome"),
    );
    expect(assign).not.toHaveBeenCalled();
  });

  it("uses a full-page load for an OAuth authorize next after signup", async () => {
    const user = userEvent.setup();
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    render(
      <MemoryRouter
        initialEntries={[
          "/login?mode=signup&next=%2Foauth%2Fauthorize%3Fclient_id%3Ddyn_1",
        ]}
      >
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Email"), "new@example.com");
    await user.type(screen.getByLabelText("Display name"), "New User");
    await user.type(screen.getByLabelText("Password"), "correct-password");
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    expect(await screen.findByText("Account created")).toBeInTheDocument();
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/oauth/authorize?client_id=dyn_1"),
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/login?mode=signup&next=%2Foauth%2Fauthorize%3Fclient_id%3Ddyn_1",
    );
  });

  it("starts in signup mode from the mode query", async () => {
    render(
      <MemoryRouter initialEntries={["/login?mode=signup"]}>
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Sign up" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Signed out")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: /Keep me signed in/i }),
    ).toBeChecked();
    expect(screen.queryByRole("link", { name: "Account security" })).not.toBeInTheDocument();
    expect(
      screen.getByRole("radiogroup", { name: "Authentication mode" }),
    ).toBeInTheDocument();
  });

  it("previews the password rule on signup but not on login (UI-15)", async () => {
    render(
      <MemoryRouter initialEntries={["/login?mode=signup"]}>
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Sign up" });
    expect(screen.getByText("15–128 characters.")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("radio", { name: /^Sign in$/i }),
    );

    expect(
      screen.queryByText("15–128 characters."),
    ).not.toBeInTheDocument();
  });

  it("fails closed and removes a disabled signup mode while preserving next", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({ data: { signup_enabled: false } });
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse(
          {
            error_code: "invalid_session",
            message: "session cookie is missing",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter
        initialEntries={["/login?mode=signup&next=%2Fpages%2Fwelcome"]}
      >
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Sign-up is not available on this server. Contact the server administrator.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /^Sign up$/ })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/login?next=%2Fpages%2Fwelcome",
      );
    });
  });

  it("hides signup when the capability request fails", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse(
          {
            error_code: "internal_error",
            message: "private server detail",
            request_id: "",
            warnings: [],
          },
          503,
        );
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse(
          {
            error_code: "invalid_session",
            message: "session cookie is missing",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/login?mode=signup"]}>
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "Sign-up is hidden because this server's registration policy could not be checked. You can still sign in.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /^Sign up$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("recovers from a stale signup tab after a backend 403", async () => {
    const user = userEvent.setup();
    let capabilityReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        capabilityReads += 1;
        return jsonResponse({
          data: { signup_enabled: capabilityReads === 1 },
        });
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse(
          {
            error_code: "invalid_session",
            message: "session cookie is missing",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      if (url === `${BASE}/api/v1/auth/signup`) {
        return jsonResponse(
          {
            error_code: "signup_disabled",
            message: "self-service signup is disabled",
            request_id: "",
            warnings: [],
          },
          403,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter
        initialEntries={["/login?mode=signup&next=%2Fpages%2Fwelcome"]}
      >
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Sign up" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Email"), "new@example.com");
    await user.type(screen.getByLabelText("Display name"), "New User");
    await user.type(screen.getByLabelText("Password"), "correct-password");
    await user.click(screen.getByRole("button", { name: "Sign up" }));

    expect(
      await screen.findByText(
        "Sign-up is not available on this server. Contact the server administrator.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveValue("");
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/login?next=%2Fpages%2Fwelcome",
      ),
    );
    expect(capabilityReads).toBe(2);
  });

  it("clears a stale error banner when switching between login and signup", async () => {
    const user = userEvent.setup();
    let sessionReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({ data: { signup_enabled: true } });
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        sessionReads += 1;
        return jsonResponse(
          {
            error_code: "invalid_session",
            message: "session cookie is missing",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      if (url === `${BASE}/api/v1/auth/login`) {
        return jsonResponse(
          {
            error_code: "invalid_credentials",
            message: "Invalid email or password",
            request_id: "",
            warnings: [],
          },
          401,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Email"), "login@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const errorText = "The email or password is incorrect.";
    expect(await screen.findByText(errorText)).toBeInTheDocument();
    await waitFor(() => expect(sessionReads).toBe(2));

    await user.click(screen.getByRole("radio", { name: /^Sign up$/ }));
    expect(screen.queryByText(errorText)).not.toBeInTheDocument();
  });

  it("keeps a rejected sign-in on the form even when a valid cookie survives", async () => {
    // Someone already signed in as A opens /login to switch to B and mistypes
    // B's password. The catch-path reconcile probe legitimately reports A as
    // authenticated; that must not be turned into "welcome back, A".
    const user = userEvent.setup();
    const mountProbe = deferred<Response>();
    let sessionReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return Promise.resolve(jsonResponse({ data: { signup_enabled: true } }));
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        sessionReads += 1;
        // The mount probe is still in flight when the visitor submits; the
        // reconcile probe afterwards resolves against A's surviving cookie.
        return sessionReads === 1
          ? mountProbe.promise
          : Promise.resolve(
              jsonResponse({ data: sessionPayload("already-in@example.com").user }),
            );
      }
      if (url === `${BASE}/api/v1/auth/login`) {
        return Promise.resolve(
          jsonResponse(
            {
              error_code: "invalid_credentials",
              message: "The email or password is incorrect.",
              request_id: "req-wrong-password",
              warnings: [],
            },
            401,
          ),
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider loadInstanceCapabilities skipInitialSessionCheck>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(sessionReads).toBe(1));
    await user.type(screen.getByLabelText("Email"), "switch-to-b@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const errorText = "The email or password is incorrect.";
    expect(await screen.findByText(errorText)).toBeInTheDocument();
    // The reconcile probe ran and reported A as authenticated ...
    await waitFor(() => expect(sessionReads).toBe(2));
    // ... but a rejected credential must not hand the visitor A's session.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled(),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("/login");
    expect(screen.getByText(errorText)).toBeInTheDocument();

    mountProbe.resolve(
      jsonResponse({ data: sessionPayload("already-in@example.com").user }),
    );
  });

  it("redirects authenticated users away from login", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({ data: { signup_enabled: true } });
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({ data: sessionPayload("login@example.com").user });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/dashboard");
    });
  });

  it("sends an already-authenticated visitor to an OAuth next with a full-page load", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/auth/capabilities`) {
        return jsonResponse({ data: { signup_enabled: true } });
      }
      if (url === `${BASE}/api/v1/auth/me`) {
        return jsonResponse({ data: sessionPayload("login@example.com").user });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });

    render(
      <MemoryRouter
        initialEntries={[
          "/login?next=%2Foauth%2Fauthorize%3Fclient_id%3Ddyn_1%26state%3Dstate-123",
        ]}
      >
        <AuthProvider loadInstanceCapabilities>
          <AuthScreen />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        "/oauth/authorize?client_id=dyn_1&state=state-123",
      ),
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/login?next=%2Foauth%2Fauthorize%3Fclient_id%3Ddyn_1%26state%3Dstate-123",
    );
  });
});

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function sessionPayload(email: string) {
  return {
    user: {
      user_id: "00000000-0000-0000-0000-000000000010",
      email,
      display_name: "New User",
      status: "active",
    },
    expires_at: "2026-05-14T12:00:00Z",
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
