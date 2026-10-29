import { randomUUID } from "node:crypto";
import { readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page } from "./fixtures";

import { authenticatePage, backendBaseUrl, currentWorkspace, viteDevServerOrigin, workspaceUrl } from "./auth";

/**
 * Topology gate for the E2E runner itself.
 *
 * The packaged wheel serves the SPA and the API from one FastAPI app on one
 * origin. The suite used to prove a shape nobody ships: Playwright's baseURL
 * was a Vite dev server on a second port while the bundle was built with an
 * absolute VITE_API_BASE_URL, so every page held two Chromium socket pools and
 * one host network-change notification flushed both. That is what
 * tests/e2e/network_retry.ts and the ERR_NETWORK_CHANGED recovery in
 * fixtures.ts were written to paper over.
 *
 * No other spec can catch a regression back to two origins: they navigate with
 * relative paths and assert on rendered product behaviour, which is identical
 * under either topology. Hence these assertions are about origins and bundle
 * shape rather than about a screen.
 *
 * Untagged on purpose — the subject is the harness, not engine behaviour, so
 * Chromium alone is the right coverage under the standard profile.
 */
const backendOrigin = new URL(backendBaseUrl).origin;
const e2eStaticRoot = (() => {
  const root = process.env.DTXWIKI_E2E_STATIC_ROOT;
  if (!root) {
    throw new Error(
      "DTXWIKI_E2E_STATIC_ROOT must be set by tests/e2e/run-e2e.mjs. " +
        "Run `npm run test:e2e`, do not invoke `playwright test` directly.",
    );
  }
  return root;
})();

// Every non-data: request and every WebSocket the page opens, so the origin
// assertions read one complete log instead of a pre-filtered subset.
function watchOrigins(page: Page) {
  const requestUrls: string[] = [];
  const webSocketUrls: string[] = [];
  page.on("request", (request) => {
    const url = request.url();
    if (url.startsWith("data:") || url.startsWith("blob:")) {
      return;
    }
    requestUrls.push(url);
  });
  page.on("websocket", (webSocket) => webSocketUrls.push(webSocket.url()));
  return {
    requestUrls,
    webSocketUrls,
    foreign: () =>
      requestUrls.filter((url) => new URL(url).origin !== backendOrigin),
    fromBackend: (prefix: string) =>
      requestUrls.filter((url) => url.startsWith(`${backendOrigin}${prefix}`)),
  };
}

// The module entry of the bundle THIS run built, read off disk.
function builtEntrySource() {
  const indexHtml = readFileSync(
    path.join(e2eStaticRoot, "index.html"),
    "utf8",
  );
  const match = indexHtml.match(/<script[^>]*\stype="module"[^>]*\ssrc="([^"]+)"/);
  expect(
    match,
    `no module entry in ${e2eStaticRoot}/index.html; the vite build step in tests/e2e/run-e2e.mjs did not produce a bundle`,
  ).not.toBeNull();
  return match![1];
}

test("the browser loads the production bundle from the backend origin and contacts no other", async ({
  page,
}) => {
  const watch = watchOrigins(page);

  await page.goto("/");

  // Reaching the sign-in screen means the document, its chunks and the
  // /auth/me probe have all been issued, so the recorded requests are the
  // complete boot sequence rather than a snapshot of the first hop.
  await expect(page).toHaveURL(/\/login\?next=%2F$/);
  await expect(page.getByRole("heading", { name: "Sign In" })).toBeVisible();

  expect(watch.foreign()).toEqual([]);
  // An independent probe, not a filter of an array the line above already
  // proved empty. The inequality guard keeps it from going vacuous if the two
  // ports are ever configured to the same value.
  expect(viteDevServerOrigin).not.toBe(backendOrigin);
  expect(
    watch.requestUrls.filter((url) => url.startsWith(viteDevServerOrigin)),
  ).toEqual([]);
  // A Vite dev client opens an HMR socket; a production bundle opens none.
  expect(watch.webSocketUrls).toEqual([]);
  expect(new URL(page.url()).origin).toBe(backendOrigin);

  const moduleSources = await page
    .locator('script[type="module"]')
    .evaluateAll((elements) =>
      elements.map((element) => element.getAttribute("src") ?? ""),
    );
  // A dev server injects /@vite/client alongside an unhashed /src/main.tsx
  // entry. One hashed, origin-relative entry is the production shape.
  expect(moduleSources).toHaveLength(1);
  expect(moduleSources[0]).toMatch(/^\/assets\/index-[A-Za-z0-9_-]+\.js$/);
  // Content-derived name, so this catches a bundle whose CONTENT differs from
  // the one this run built. It does NOT prove the file came from this run's
  // directory — see the static-root identity test below for why.
  expect(moduleSources[0]).toBe(builtEntrySource());
});

test("the backend serves the static root this run built, not a stale checked-in bundle", async ({
  page,
}) => {
  // Vite asset names are content hashes, so a backend/static left behind by an
  // earlier `make bundle-frontend` of an identical tree carries the SAME entry
  // name as a fresh build — measured, not assumed: on a clean tree here both
  // said index-C2CN2AVy.js. Whenever that holds, the name comparison in the
  // first test cannot tell the two directories apart, so dropping
  // DTXWIKI_STATIC_ROOT from playwright.config.ts goes unnoticed on every
  // machine that has a backend/static (gitignored, so most of them).
  //
  // A marker file can tell them apart: it exists in exactly one directory, and
  // ImmutableStaticFiles resolves /assets/* against its mounted directory on
  // every request. Written after the server booted, removed straight after.
  const assetsDir = path.join(e2eStaticRoot, "assets");
  expect(
    statSync(assetsDir, { throwIfNoEntry: false })?.isDirectory() ?? false,
    `${assetsDir} must exist: the vite build step in tests/e2e/run-e2e.mjs builds into DTXWIKI_E2E_STATIC_ROOT`,
  ).toBe(true);

  const markerName = `e2e-static-root-probe-${randomUUID()}.txt`;
  const markerBody = `static-root=${e2eStaticRoot}`;
  const markerPath = path.join(assetsDir, markerName);
  writeFileSync(markerPath, markerBody, { encoding: "utf8", flag: "wx" });
  try {
    const marker = await page.request.get(`/assets/${markerName}`);
    expect(
      marker.status(),
      "the backend is serving some other directory as its static root: " +
        "DTXWIKI_STATIC_ROOT in playwright.config.ts is not reaching it",
    ).toBe(200);
    expect(await marker.text()).toBe(markerBody);
  } finally {
    rmSync(markerPath, { force: true });
  }
});

test("the backend answers SPA deep links without shadowing the API namespace", async ({
  page,
}) => {
  // Asserted over the wire rather than through a navigation: an authenticated
  // route redirects to /login client-side, which would hide whether the server
  // returned index.html or a 404 for the deep link itself.
  const deepLink = await page.request.get("/w/missing-workspace/pages/missing");
  expect(deepLink.status()).toBe(200);
  expect(deepLink.headers()["content-type"]).toContain("text/html");

  const unknownApiRoute = await page.request.get("/api/v1/definitely-not-a-route");
  expect(unknownApiRoute.status()).toBe(404);
});

test("lazily imported route chunks and authenticated API traffic stay on the one origin", async ({
  page,
}, testInfo) => {
  // The anonymous boot above only ever fetches the entry chunk and the two
  // unauthenticated auth probes. Every other chunk (SettingsScreen,
  // markdown-renderer, mermaid, cytoscape — separate manualChunks entries in
  // frontend/vite.config.ts) is dynamically imported behind RequireAuth, so a
  // second origin reachable only through `base` or a CDN asset host would not
  // show up there.
  const watch = watchOrigins(page);

  await authenticatePage(page, testInfo, "single-origin-authenticated");
  const workspace = await currentWorkspace(page.request);
  await page.goto(workspaceUrl(workspace.id, "settings"));
  await expect(
    page.getByRole("heading", { name: "Workspace", exact: true }),
  ).toBeVisible();

  expect(watch.foreign()).toEqual([]);
  expect(watch.webSocketUrls).toEqual([]);
  // Guards the assertion above against passing because nothing loaded: a
  // rendered authenticated screen must have pulled at least one chunk beyond
  // the entry and issued at least one API call.
  expect(watch.fromBackend("/assets/").length).toBeGreaterThan(1);
  expect(watch.fromBackend("/api/").length).toBeGreaterThan(0);
});
