import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createAuthenticatedPreviewTransport,
  normalizePreviewContentPath,
  UnsafePreviewContentUrlError,
} from "./AuthenticatedPreviewTransport";

// getApiBaseUrl() reads import.meta.env, which vitest fixes to a DEV build for
// the whole file. A production build (the packaged wheel, and the E2E harness,
// which pins VITE_API_BASE_URL="") resolves it to "" instead, so it has to be
// substitutable to test that shape at all. Undefined override == the real
// function, which is what every other test in this file uses.
const { apiBaseUrl } = vi.hoisted(() => ({
  apiBaseUrl: { override: undefined as string | undefined },
}));
vi.mock("../../../shared/api/client", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../shared/api/client")>();
  return {
    ...actual,
    getApiBaseUrl: () => apiBaseUrl.override ?? actual.getApiBaseUrl(),
  };
});

const scope = {
  workspaceId: "workspace-1",
  itemId: "item-1",
  versionId: "version-1",
};
const previewPath =
  "/api/v1/workspaces/workspace-1/library/versions/version-1/preview/source";

describe("authenticated preview transport", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.__DTXWIKI_RUNTIME_CONFIG__ = { apiToken: "preview-token" };
  });

  it("adds authentication, cookies, ranges, and cancellation to scoped requests", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("content"));
    const controller = new AbortController();
    const transport = createAuthenticatedPreviewTransport(scope);

    await transport.fetch(previewPath, {
      accept: "text/plain",
      range: "bytes=0-127",
      signal: controller.signal,
    });

    const [url, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(url).toBe(`http://127.0.0.1:21080${previewPath}`);
    expect(init).toMatchObject({
      credentials: "include",
      method: "GET",
      signal: controller.signal,
    });
    expect(headers.get("Authorization")).toBe("Bearer preview-token");
    expect(headers.get("Range")).toBe("bytes=0-127");
    expect(headers.get("Accept")).toBe("text/plain");
  });

  it.each([
    "https://viewer.example/preview",
    `${previewPath}?token=secret`,
    "/api/v1/workspaces/other-workspace/library/versions/version-1/preview/source",
    "/api/v1/workspaces/workspace-1/library/versions/other-version/preview/source",
    "/api/v1/workspaces/workspace-1/library/versions/version-1/preview/%2e%2e/source",
  ])("rejects an unscoped or unsafe URL before fetching", async (value) => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const transport = createAuthenticatedPreviewTransport(scope);

    await expect(transport.fetch(value)).rejects.toBeInstanceOf(
      UnsafePreviewContentUrlError,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("allows legacy item routes only when compatibility is explicit", () => {
    const legacyPath =
      "/api/v1/workspaces/workspace-1/library/item-1/original";

    expect(normalizePreviewContentPath(legacyPath, scope)).toBeNull();
    expect(
      normalizePreviewContentPath(legacyPath, {
        ...scope,
        allowLegacyRoutes: true,
      }),
    ).toBe(legacyPath);
  });

  it("uses native media only when same-origin cookies can carry authentication", () => {
    const bearerTransport = createAuthenticatedPreviewTransport(scope);
    expect(bearerTransport.mediaSource(previewPath)).toBeNull();

    window.__DTXWIKI_RUNTIME_CONFIG__ = {};
    const cookieTransport = createAuthenticatedPreviewTransport(scope);
    expect(cookieTransport.mediaSource(previewPath)).toBe(
      `http://127.0.0.1:21080${previewPath}`,
    );
  });
});

describe("preview content paths in a single-origin production build", () => {
  beforeEach(() => {
    // What frontend/src/shared/api/client.ts returns whenever
    // import.meta.env.DEV is false: DEFAULT_PACKAGED_BASE_URL is "".
    apiBaseUrl.override = "";
  });
  afterEach(() => {
    apiBaseUrl.override = undefined;
  });

  it("accepts an absolute content URL on the document origin", () => {
    // Regression: this used to evaluate `new URL("")`, which throws a
    // TypeError outside the try/catch guarding the parse two lines above.
    expect(
      normalizePreviewContentPath(
        `${window.location.origin}${previewPath}`,
        scope,
      ),
    ).toBe(previewPath);
  });

  it("still rejects an absolute content URL from another origin", () => {
    expect(
      normalizePreviewContentPath(`https://viewer.example${previewPath}`, scope),
    ).toBeNull();
  });
});
