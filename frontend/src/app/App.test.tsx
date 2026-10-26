import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { isValidElement, Suspense, type ReactNode } from "react";
import {
  matchRoutes,
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigationType,
} from "react-router-dom";
import type { RouteObject } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../shared/auth/AuthContext";
import { setLocale } from "../shared/i18n";
import {
  appRoutes,
  isAuthBootstrapPath,
  RequireAuth,
  RequireAskEnabled,
  RequireEditor,
  RootRouteError,
} from "./App";

const { workspaceState } = vi.hoisted(() => ({
  workspaceState: { current: {} as Record<string, unknown> },
}));

describe("auth route bootstrap", () => {
  it.each([
    "/login/",
    "/forgot-password/",
    "/reset-password/",
    "/verify-email/",
  ])("loads capabilities for trailing-slash route %s", (pathname) => {
    expect(isAuthBootstrapPath(pathname)).toBe(true);
  });
});

vi.mock("../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => workspaceState.current,
  useWorkspaceContext: () => workspaceState.current,
  WorkspaceProvider: ({ children }: { children: ReactNode }) => children,
}));

describe("RequireAuth", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows a retryable session error instead of redirecting on initial 5xx", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse(
          {
            error_code: "internal_error",
            message: "session service unavailable",
            request_id: "req-transient",
            warnings: [],
          },
          503,
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({ data: userPayload("user@example.com") }),
      );

    render(
      <AuthProvider>
        <MemoryRouter initialEntries={["/workspaces/ws-1/dashboard"]}>
          <RequireAuth>
            <div>Protected dashboard</div>
          </RequireAuth>
          <LocationProbe />
        </MemoryRouter>
      </AuthProvider>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not verify your session.",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The server could not complete the request. Please try again.",
    );
    expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/dashboard");

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Protected dashboard")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/dashboard");
    });
  });

  it("verifies a deferred public-route session before redirecting", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({ data: userPayload("user@example.com") }),
    );

    render(
      <AuthProvider skipInitialSessionCheck>
        <MemoryRouter initialEntries={["/workspaces/ws-1/dashboard"]}>
          <RequireAuth>
            <div>Protected dashboard</div>
          </RequireAuth>
          <LocationProbe />
        </MemoryRouter>
      </AuthProvider>,
    );

    expect(screen.getByText("Checking session")).toBeInTheDocument();
    expect(await screen.findByText("Protected dashboard")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/dashboard");
    });
  });
});

describe("RequireEditor", () => {
  beforeEach(() => {
    setLocale("en", { persist: false });
  });

  it("shows a spinner before the first workspace fetch resolves", () => {
    // #1 regression: loading starts false, so a full-page load to a write route
    // must wait on `initialized` (not bounce the editor before the list loads).
    workspaceState.current = {
      loading: false,
      initialized: false,
      selectedWorkspace: null,
    };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/agent/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(screen.getByText("Checking access")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/agent/pages/new");
  });

  it("shows a spinner while the workspace list is still loading", () => {
    workspaceState.current = { loading: true, initialized: false, selectedWorkspace: null };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/agent/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(screen.getByText("Checking access")).toBeInTheDocument();
    expect(screen.queryByText("Editor surface")).not.toBeInTheDocument();
  });

  it("localizes the access guard outside English", () => {
    setLocale("ko", { persist: false });
    workspaceState.current = {
      loading: true,
      initialized: false,
      selectedWorkspace: null,
    };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/agent/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
      </MemoryRouter>,
    );
    expect(screen.getByText("접근 권한 확인 중")).toBeInTheDocument();
  });

  it("redirects a resolved zero-membership user instead of hanging (#13)", () => {
    workspaceState.current = {
      loading: false,
      initialized: true,
      selectedWorkspace: null,
    };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/context/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Checking access")).not.toBeInTheDocument();
    expect(screen.queryByText("Editor surface")).not.toBeInTheDocument();
    // C2: editor-gated page routes now bounce to /dashboard (which surfaces the
    // editorRequired banner) rather than the pages landing.
    expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/dashboard");
  });

  it("redirects a viewer from /agent/new to the read-only /agent screen", () => {
    // The bounce target derives from the blocked route: /agent/new has no slug,
    // so a viewer must land on /agent (which they can use), not the pages
    // wildcard.
    workspaceState.current = {
      loading: false,
      initialized: true,
      selectedWorkspace: { role: "viewer" },
    };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/agent/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Editor surface")).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/agent");
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '{"editorRequired":true}',
    );
  });

  it("redirects a viewer from a page write route to /dashboard with the editorRequired reason", () => {
    // C2: a viewer who hits a page write route no longer lands on a random page
    // reader (which dropped the reason); the bounce target is /dashboard, which
    // renders the editorRequired banner from the carried navigation state.
    workspaceState.current = {
      loading: false,
      initialized: true,
      selectedWorkspace: { role: "viewer" },
    };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/context/pages/guide/edit"]}>
        <Routes>
          <Route
            path="/workspaces/:workspaceId/context/pages/:pageId/edit"
            element={
              <RequireEditor>
                <div>Editor surface</div>
              </RequireEditor>
            }
          />
          <Route path="/workspaces/:workspaceId/dashboard" element={<div>Dashboard surface</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Editor surface")).not.toBeInTheDocument();
    expect(screen.getByText("Dashboard surface")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/dashboard");
    expect(screen.getByTestId("location-state")).toHaveTextContent(
      '{"editorRequired":true}',
    );
  });

  it("renders the editor for an editor-role member", () => {
    workspaceState.current = {
      loading: false,
      initialized: true,
      selectedWorkspace: { role: "editor" },
    };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/agent/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(screen.getByText("Editor surface")).toBeInTheDocument();
  });

  it("shows a retryable banner (no redirect) when no workspace loaded", async () => {
    // #8: a first-load failure (no cached workspace) surfaces a retry banner and
    // keeps the route instead of redirecting; Retry calls refreshWorkspaces.
    const user = userEvent.setup();
    const refreshWorkspaces = vi.fn();
    workspaceState.current = {
      loading: false,
      initialized: true,
      error: "boom",
      selectedWorkspace: null,
      refreshWorkspaces,
    };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/agent/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(
      screen.getByText("Could not load your workspaces."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Editor surface")).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/agent/pages/new");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(refreshWorkspaces).toHaveBeenCalledTimes(1);
  });

  it("keeps rendering the editor when a refresh fails but a valid workspace is cached", () => {
    // Regression: `error` persists alongside a valid cached editor workspace
    // after a non-initial transient refresh failure; the guard must NOT block
    // the editor in that case (gate is `error && !selectedWorkspace`).
    workspaceState.current = {
      loading: false,
      initialized: true,
      error: "boom",
      selectedWorkspace: { role: "editor" },
      refreshWorkspaces: () => {},
    };
    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/agent/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(screen.getByText("Editor surface")).toBeInTheDocument();
    expect(
      screen.queryByText("Could not load your workspaces."),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/workspaces/ws-1/agent/pages/new");
  });
});

describe("e2e route-recovery controls", () => {
  // E2E-03: tests/e2e/fixtures.ts clicks a route recovery control when a GET
  // dies with ERR_NETWORK_CHANGED. It used to find them with
  // `button.primary`, which matched RootRouteError but silently missed both
  // banner Retries -- they are `icon-button text-button`, no `primary`. The
  // fixture now addresses these ids (tests/e2e/fixtures.ts RECOVERY_TEST_IDS),
  // so each id has to stay on the control that actually performs recovery,
  // not merely exist somewhere in the tree.
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("tags the session-verification retry, and that control refreshes the session", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse(
          {
            error_code: "internal_error",
            message: "session service unavailable",
            request_id: "req-transient",
            warnings: [],
          },
          503,
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({ data: userPayload("user@example.com") }),
      );

    render(
      <AuthProvider>
        <MemoryRouter initialEntries={["/workspaces/ws-1/dashboard"]}>
          <RequireAuth>
            <div>Protected dashboard</div>
          </RequireAuth>
          <LocationProbe />
        </MemoryRouter>
      </AuthProvider>,
    );

    const retry = await screen.findByTestId("session-verify-retry");
    expect(retry).toHaveAccessibleName("Retry");

    await user.click(retry);

    expect(await screen.findByText("Protected dashboard")).toBeInTheDocument();
  });

  it("tags the workspace-access retry, and that control refreshes the workspaces", async () => {
    const user = userEvent.setup();
    const refreshWorkspaces = vi.fn();
    workspaceState.current = {
      loading: false,
      initialized: true,
      error: "boom",
      selectedWorkspace: null,
      refreshWorkspaces,
    };

    render(
      <MemoryRouter initialEntries={["/workspaces/ws-1/agent/pages/new"]}>
        <RequireEditor>
          <div>Editor surface</div>
        </RequireEditor>
        <LocationProbe />
      </MemoryRouter>,
    );

    const retry = screen.getByTestId("workspace-access-retry");
    expect(retry).toHaveAccessibleName("Retry");

    await user.click(retry);

    expect(refreshWorkspaces).toHaveBeenCalledTimes(1);
  });

  it("tags the root route-error reload, and that control reloads the page", async () => {
    const user = userEvent.setup();
    const reload = vi.fn();
    vi.spyOn(globalThis, "location", "get").mockReturnValue({
      ...globalThis.location,
      reload,
    } as Location);

    render(
      <MemoryRouter>
        <RootRouteError />
      </MemoryRouter>,
    );

    const control = screen.getByTestId("route-error-reload");
    expect(control).toHaveAccessibleName("Load the latest version");

    await user.click(control);

    expect(reload).toHaveBeenCalledTimes(1);
  });
});

describe("appRoutes role gating", () => {
  // Guards route placement using the ACTUAL App.tsx config: every mcp-chats
  // endpoint requires editor+, so the Chats routes must be editor-gated (a
  // viewer would only get a 403 banner plus a misleading empty state).
  it.each(["/workspaces/:workspaceId/chats", "/workspaces/:workspaceId/chats/:chatId"])(
    "mounts %s under RequireAuth + RequireEditor in the real route config",
    (path) => {
      const route = findRoute(appRoutes, path);
      expect(route).not.toBeNull();
      const element = route?.element;
      if (!isValidElement(element)) {
        throw new Error(`route ${path} has no element`);
      }
      expect(element.type).toBe(RequireAuth);
      const child = (element.props as { children?: unknown }).children;
      if (!isValidElement(child)) {
        throw new Error(`route ${path} is not wrapped in RequireEditor`);
      }
      expect(child.type).toBe(RequireEditor);
    },
  );

  it("does not register the /context-map alias (no map feature exists)", () => {
    expect(findRoute(appRoutes, "/context-map")).toBeNull();
  });

  it.each(["/workspaces/ws-1/ask", "/workspaces/ws-1/ask?view=history", "/workspaces/ws-1/ask/"])(
    "registers Ask URL %s behind its experiment gate",
    (path) => {
      const matches = matchRoutes(appRoutes, path);
      const route = matches?.at(-1)?.route;
      expect(route?.path).toBe("/workspaces/:workspaceId/ask");
      const element = route?.element;
      if (!isValidElement<{ children: ReactNode }>(element)) {
        throw new Error("Ask route has no element");
      }
      expect(element.type).toBe(RequireAuth);
      const child = element.props.children;
      expect(isValidElement(child) && child.type === RequireAskEnabled).toBe(true);
    },
  );

  it("keeps the one-time password reset route public", () => {
    const route = findRoute(appRoutes, "/reset-password");
    expect(route).not.toBeNull();
    const element = route?.element;
    if (!isValidElement(element)) {
      throw new Error("/reset-password route has no element");
    }
    expect(element.type).not.toBe(RequireAuth);
  });

  it("keeps workspace discovery behind a browser session", () => {
    const route = findRoute(appRoutes, "/workspaces/find");
    expect(route).not.toBeNull();
    const element = route?.element;
    if (!isValidElement(element)) {
      throw new Error("/workspaces/find route has no element");
    }
    expect(element.type).toBe(RequireAuth);
    const child = (element.props as { children?: unknown }).children;
    expect(isValidElement(child) && child.type === RequireEditor).toBe(false);
  });

  // Slug-only readers remain deleted; permanent links carry their workspace.
  it.each(["/pages/:slug", "/pages/:slug/edit"])(
    "does not register the legacy %s route",
    (path) => {
      expect(findRoute(appRoutes, path)).toBeNull();
    },
  );

  it("routes full page permalinks without a lookup and preserves view state through authentication", async () => {
    vi.restoreAllMocks();
    const pageId = "22222222-2222-4222-8222-222222222222";
    const workspaceId = "11111111-1111-4111-8111-111111111111";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ data: userPayload("user@example.com") }),
    );
    const fullElement = findRoute(appRoutes, "/p/:workspaceId/:pageId")?.element;
    const shortElement = findRoute(appRoutes, "/p/:pageId")?.element;
    if (!isValidElement(fullElement) || !isValidElement(shortElement)) throw new Error("permalink routes missing");
    expect(fullElement.type).toBe(RequireAuth);
    expect(shortElement.type).toBe(RequireAuth);
    function Destination() {
      const location = useLocation();
      const navigationType = useNavigationType();
      return <div data-testid="destination">{location.pathname}{location.search}{location.hash} {navigationType}</div>;
    }
    const view = render(
      <AuthProvider>
        <MemoryRouter initialEntries={[`/p/${workspaceId}/${pageId}?tab=history#design%20details`]}>
          <Suspense fallback={<div>Loading route</div>}>
            <Routes>
              <Route path="/p/:workspaceId/:pageId" element={fullElement} />
              <Route path="/workspaces/:workspaceId/context/pages/:pageId" element={<Destination />} />
            </Routes>
          </Suspense>
        </MemoryRouter>
      </AuthProvider>,
    );
    expect(await screen.findByTestId("destination")).toHaveTextContent(
      `/workspaces/${workspaceId}/context/pages/${pageId}?tab=history#design%20details REPLACE`,
    );
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual(["http://127.0.0.1:21080/api/v1/auth/me"]);
    view.unmount();
    fetchMock.mockClear();
    const shortView = render(
      <AuthProvider>
        <MemoryRouter initialEntries={[`/p/${pageId}`]}>
          <Suspense fallback={<div>Loading route</div>}>
            <Routes><Route path="/p/:pageId" element={shortElement} /></Routes>
          </Suspense>
        </MemoryRouter>
      </AuthProvider>,
    );
    expect(await screen.findByRole("heading", { name: "We couldn't find that page" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual(["http://127.0.0.1:21080/api/v1/auth/me"]);
    shortView.unmount();
    fetchMock.mockClear();
    const conflictView = render(
      <AuthProvider>
        <MemoryRouter initialEntries={[`/p/${workspaceId}/${pageId}?target=${pageId}`]}>
          <Suspense fallback={<div>Loading route</div>}>
            <Routes>
              <Route path="/p/:workspaceId/:pageId" element={fullElement} />
              <Route path="/workspaces/:workspaceId/context/pages/:pageId" element={<Destination />} />
            </Routes>
          </Suspense>
        </MemoryRouter>
      </AuthProvider>,
    );
    expect(await screen.findByRole("heading", { name: "We couldn't find that page" })).toBeInTheDocument();
    expect(screen.queryByTestId("destination")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual(["http://127.0.0.1:21080/api/v1/auth/me"]);
    conflictView.unmount();
    fetchMock.mockClear().mockResolvedValue(jsonResponse({ error_code: "unauthorized", warnings: [] }, 401));
    const anonymousEntry = `/p/${workspaceId}/${pageId}?from=shared#design%20details`;
    render(
      <AuthProvider>
        <MemoryRouter initialEntries={[anonymousEntry]}>
          <Suspense fallback={<div>Loading route</div>}>
            <Routes>
              <Route path="/p/:workspaceId/:pageId" element={fullElement} />
              <Route path="/login" element={<Destination />} />
            </Routes>
          </Suspense>
        </MemoryRouter>
      </AuthProvider>,
    );
    expect(await screen.findByTestId("destination")).toHaveTextContent(`/login?next=${encodeURIComponent(anonymousEntry)} REPLACE`);
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual(["http://127.0.0.1:21080/api/v1/auth/me"]);
  });

  it("mounts the canonical Context page permalink under RequireAuth", () => {
    const route = findRoute(
      appRoutes,
      "/workspaces/:workspaceId/context/pages/:pageId",
    );
    expect(route).not.toBeNull();
    const element = route?.element;
    if (!isValidElement(element)) {
      throw new Error("canonical Context route has no element");
    }
    expect(element.type).toBe(RequireAuth);
  });

  it("mounts the canonical Context edit permalink under RequireAuth + RequireEditor", () => {
    const route = findRoute(
      appRoutes,
      "/workspaces/:workspaceId/context/pages/:pageId/edit",
    );
    expect(route).not.toBeNull();
    const element = route?.element;
    if (!isValidElement(element)) {
      throw new Error("canonical Context edit route has no element");
    }
    expect(element.type).toBe(RequireAuth);
    const child = (element.props as { children?: unknown }).children;
    if (!isValidElement(child)) {
      throw new Error("canonical Context edit route is not wrapped in RequireEditor");
    }
    expect(child.type).toBe(RequireEditor);
  });

  it("removes the workspace-less slug edit route", () => {
    expect(findRoute(appRoutes, "/context/:slug/edit")).toBeNull();
    expect(findRoute(appRoutes, "/workspaces/:workspaceId/context/pages/:pageId/edit")).not.toBeNull();
  });

  it("renders the NotFound screen (not PagesLanding) for an unknown path", async () => {
    // C2: the wildcard "*" route now mounts <NotFoundScreen/> instead of
    // funnelling unknown URLs into the Pages landing. Render the REAL wildcard
    // route element so a mistyped/stale link says "not found" rather than
    // silently becoming "you have no pages".
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: userPayload("user@example.com") }),
    );
    const wildcard = findRoute(appRoutes, "*");
    const element = wildcard?.element;
    if (!isValidElement(element)) {
      throw new Error("wildcard route has no element");
    }

    render(
      <AuthProvider>
        <MemoryRouter initialEntries={["/zzz-nope"]}>
          <Suspense fallback={<div>Loading</div>}>{element}</Suspense>
        </MemoryRouter>
      </AuthProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "We couldn't find that page" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "No context pages yet" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Find workspaces" }),
    ).toHaveAttribute("href", "/workspaces/find");
  });
});

function findRoute(routes: RouteObject[], path: string): RouteObject | null {
  for (const route of routes) {
    if (route.path === path) {
      return route;
    }
    const match = route.children ? findRoute(route.children, path) : null;
    if (match) {
      return match;
    }
  }
  return null;
}

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <div data-testid="location">{location.pathname}</div>
      <div data-testid="location-state">{JSON.stringify(location.state)}</div>
    </>
  );
}

function userPayload(email: string) {
  return {
    user_id: "00000000-0000-0000-0000-000000000010",
    email,
    display_name: "Test User",
    status: "active",
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
