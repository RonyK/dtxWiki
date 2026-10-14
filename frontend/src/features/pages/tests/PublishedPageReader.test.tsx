import { render, screen, waitFor, within } from "@testing-library/react";
import {
  createMemoryRouter,
  MemoryRouter,
  Outlet,
  Route,
  RouterProvider,
  Routes,
  useLocation,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { appRoutes } from "../../../app/App";
import { i18n } from "../../../shared/i18n";
import { formatDisplayDateTime } from "../../../shared/lib/datetime";
import { PublishedPageReader } from "../PublishedPageReader";

const BASE = "http://127.0.0.1:21080";
const PUBLIC_URL = `${BASE}/api/v1/public/w/acme/pages/welcome`;
const CONTEXT_MEDIA_ASSET_ID = "0dd36725-7342-43c7-858e-4d3e6f96f128";

const publicPage = {
  id: "p1",
  slug: "welcome",
  title: "Welcome",
  body_md:
    "# Welcome\n\nThis is the **public** body.\n\nSee [[Secret Page]] for more.\n\n## Section\n\n" +
    `![Public diagram](dtx-media://asset/${CONTEXT_MEDIA_ASSET_ID})`,
  updated_at: "2026-05-30T00:00:00Z",
  exposure_scope: "indexed_web",
};

// Renders the public route the way App.tsx wires it: a PublicFrame layout with a
// "Sign in" link, nested under the /w/:workspaceSlug/pages/:slug route, plus a
// /login sentinel so we can assert anonymous visitors are NOT redirected there.
function renderPublicRoute(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <LocationProbe />
      <Routes>
        <Route
          element={
            <div className="public-frame">
              <a href="/login">Sign in</a>
              <Outlet />
            </div>
          }
        >
          <Route
            path="/w/:workspaceSlug/pages/:slug"
            element={<PublishedPageReader />}
          />
        </Route>
        <Route path="/login" element={<div>Login screen</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

describe("PublishedPageReader", () => {
  beforeEach(async () => {
    vi.restoreAllMocks();
    await i18n.changeLanguage("en");
  });

  afterEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("renders the title and body for an anonymous visitor without redirecting to login", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = String(input);
        if (url === PUBLIC_URL) {
          // The public read must be anonymous: no cookies, no bearer token.
          expect(init?.credentials).toBe("omit");
          const headers = new Headers(init?.headers);
          expect(headers.has("Authorization")).toBe(false);
          return jsonResponse({ data: publicPage });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      });

    renderPublicRoute("/w/acme/pages/welcome");

    expect(
      await screen.findByRole("heading", { name: "Welcome", level: 1 }),
    ).toBeInTheDocument();
    const main = screen.getByRole("main", { name: "Published page" });
    const updatedAt = formatDisplayDateTime(publicPage.updated_at, "en");
    expect(within(main).getByText(`Last updated ${updatedAt}`)).toHaveAttribute(
      "title",
      updatedAt,
    );

    const article = document.querySelector(".markdown-body");
    expect(article).not.toBeNull();
    expect(within(article as HTMLElement).getByText(/public/)).toBeInTheDocument();
    expect(
      within(article as HTMLElement).getByRole("heading", { name: "Section" }),
    ).toBeInTheDocument();
    expect(
      within(article as HTMLElement).getByRole("img", { name: "Public diagram" }),
    ).toHaveAttribute(
      "src",
      `/api/v1/public/w/acme/pages/welcome/media/${CONTEXT_MEDIA_ASSET_ID}`,
    );

    // Stays on the public route — no redirect to /login.
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/w/acme/pages/welcome",
    );
    expect(screen.queryByText("Login screen")).not.toBeInTheDocument();
    // A "Sign in" affordance is present in the public frame.
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();

    // The anonymous reader must NEVER fetch the authenticated workspace page list
    // or any internal endpoint — only the single public read.
    const calledUrls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(calledUrls).toEqual([PUBLIC_URL]);
    expect(calledUrls.some((url) => url.includes("/api/v1/pages"))).toBe(false);
    expect(calledUrls.some((url) => url.includes("/api/v1/workspaces"))).toBe(false);
  });

  it.each([
    { language: "en", label: "Loading published page" },
    { language: "ko", label: "게시된 페이지를 불러오는 중" },
  ])("localizes the loading state in $language", async ({ language, label }) => {
    await i18n.changeLanguage(language);
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () => new Promise<Response>(() => undefined),
    );

    renderPublicRoute("/w/acme/pages/welcome");

    expect(screen.getByRole("status", { name: label })).toHaveTextContent(label);
  });

  it("renders Korean reader ARIA and locale-aware updated time", async () => {
    await i18n.changeLanguage("ko");
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ data: publicPage }),
    );

    renderPublicRoute("/w/acme/pages/welcome");

    const main = await screen.findByRole("main", { name: "게시된 페이지" });
    const updatedAt = formatDisplayDateTime(publicPage.updated_at, "ko");
    expect(within(main).getByText(`마지막 업데이트 ${updatedAt}`)).toHaveAttribute(
      "title",
      updatedAt,
    );
  });

  it("renders wikilinks as plain text (no in-app links, no target enumeration)", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ data: publicPage }),
    );

    renderPublicRoute("/w/acme/pages/welcome");

    const article = await waitFor(() => {
      const node = document.querySelector(".markdown-body");
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });
    // The [[Secret Page]] wikilink renders as plain text, not an anchor, so the
    // public reader leaks no in-app /pages link and probes no private target.
    expect(within(article).getByText(/Secret Page/)).toBeInTheDocument();
    expect(article.querySelector("a.wikilink")).toBeNull();
    expect(article.querySelector('a[href*="/pages/"]')).toBeNull();
    expect(article.querySelector('a[href*="/w/"]')).toBeNull();
  });

  it("keeps an indexed public page discoverable", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ data: publicPage }),
    );

    renderPublicRoute("/w/acme/pages/welcome");
    await screen.findByRole("heading", { name: "Welcome", level: 1 });

    expect(document.head.querySelector('meta[name="robots"]')).toBeNull();
  });

  it("sets robots:noindex for an unlisted page and removes it on unmount", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({
        data: { ...publicPage, exposure_scope: "unlisted_web" },
      }),
    );

    const { unmount } = renderPublicRoute("/w/acme/pages/welcome");
    await screen.findByRole("heading", { name: "Welcome", level: 1 });

    const meta = await waitFor(() => {
      const node = document.head.querySelector(
        'meta[name="robots"][data-dtx-public-page-robots="true"]',
      );
      expect(node?.getAttribute("content")).toBe("noindex, nofollow");
      return node;
    });
    expect(meta).not.toBeNull();

    unmount();
    expect(
      document.head.querySelector(
        'meta[name="robots"][data-dtx-public-page-robots="true"]',
      ),
    ).toBeNull();
  });

  it("mounts the public route OUTSIDE auth in the real app router config", async () => {
    // Guards route placement using the ACTUAL App.tsx routes (the local frame in
    // the other tests can't catch a public route accidentally moved under
    // RequireAuth/AppFrame, which would trigger a session check + /login redirect).
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const url = String(input);
        if (url === PUBLIC_URL) {
          return jsonResponse({ data: publicPage });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      });

    const router = createMemoryRouter(appRoutes, {
      initialEntries: ["/w/acme/pages/welcome"],
    });
    render(<RouterProvider router={router} />);

    // The published reader renders for an anonymous visitor through the real
    // route tree, and stays on the public path (no redirect to /login).
    expect(
      await screen.findByRole(
        "heading",
        { name: "Welcome", level: 1 },
        { timeout: 4000 },
      ),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/w/acme/pages/welcome");
    // The PublicFrame "Sign in" link carries ?next= so the visitor returns to
    // this public page after login instead of landing on /dashboard.
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/login?next=%2Fw%2Facme%2Fpages%2Fwelcome",
    );
    // Only the public read was fetched — no auth/session call entered the path.
    expect(fetchMock.mock.calls.map((call) => String(call[0]))).toEqual([PUBLIC_URL]);
  });

  it("localizes the real public frame chrome in Korean", async () => {
    await i18n.changeLanguage("ko");
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ data: publicPage }),
    );
    const router = createMemoryRouter(appRoutes, {
      initialEntries: ["/w/acme/pages/welcome"],
    });

    render(<RouterProvider router={router} />);

    await screen.findByRole("heading", { name: "Welcome", level: 1 });
    expect(screen.getByRole("link", { name: "로그인" })).toHaveAttribute(
      "href",
      "/login?next=%2Fw%2Facme%2Fpages%2Fwelcome",
    );
  });

  it("shows a minimal not-found state with a Sign in link on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ error_code: "not_found", message: "Not found" }, 404),
    );

    renderPublicRoute("/w/acme/pages/missing");

    expect(
      await screen.findByRole("heading", { name: "Page not available" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/private or does not exist/)).toBeInTheDocument();
    // The 404 "Sign in" link preserves the attempted path (the page may simply
    // be private to this visitor) so login returns them here, not /dashboard.
    expect(
      within(screen.getByRole("main")).getByRole("link", { name: "Sign in" }),
    ).toHaveAttribute("href", "/login?next=%2Fw%2Facme%2Fpages%2Fmissing");
    // Still no redirect to login.
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/w/acme/pages/missing",
    );
    expect(screen.queryByText("Login screen")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(
        document.head.querySelector('meta[name="robots"]')?.getAttribute("content"),
      ).toBe("noindex, nofollow");
    });
  });

  it("localizes the Korean not-found state and reader Sign in action", async () => {
    await i18n.changeLanguage("ko");
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      jsonResponse({ error_code: "not_found", message: "Not found" }, 404),
    );

    renderPublicRoute("/w/acme/pages/missing");

    const main = await screen.findByRole("main", { name: "게시된 페이지" });
    expect(
      within(main).getByRole("heading", { name: "페이지를 이용할 수 없습니다" }),
    ).toBeInTheDocument();
    expect(
      within(main).getByText("이 페이지는 비공개이거나 존재하지 않습니다."),
    ).toBeInTheDocument();
    expect(within(main).getByRole("link", { name: "로그인" })).toHaveAttribute(
      "href",
      "/login?next=%2Fw%2Facme%2Fpages%2Fmissing",
    );
  });

  it.each([
    {
      language: "en",
      title: "Page not available",
      description: "This page could not be loaded. Try again later.",
    },
    {
      language: "ko",
      title: "페이지를 이용할 수 없습니다",
      description: "페이지를 불러오지 못했습니다. 잠시 후 다시 시도하세요.",
    },
  ])(
    "shows a localized generic error without raw server details in $language",
    async ({ language, title, description }) => {
      await i18n.changeLanguage(language);
      vi.spyOn(globalThis, "fetch").mockRejectedValue(
        new Error("database host leaked; request_id=secret-123"),
      );

      renderPublicRoute("/w/acme/pages/unavailable");

      expect(await screen.findByRole("heading", { name: title })).toBeInTheDocument();
      expect(screen.getByText(description)).toBeInTheDocument();
      expect(screen.queryByText(/database host|secret-123/i)).toBeNull();
    },
  );
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
