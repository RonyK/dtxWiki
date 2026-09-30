import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getLibraryPreviewManifest,
  PreviewManifestEndpointUnavailableError,
} from "../libraryPreview";

describe("library preview api", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    delete window.__DTXWIKI_RUNTIME_CONFIG__;
  });

  it("reads and validates an exact-version PreviewManifest", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          schema_version: "1",
          status: "ready",
          version_id: "version-1",
          selected_preview_attempt_id: "attempt-1",
          variants: [
            {
              kind: "text",
              media_type: "text/plain",
              delivery: "inline",
              renderer_contract: "core.text.v1",
              rank: 10,
              text: "Preview body",
              byte_size: null,
              width: null,
              height: null,
              duration_ms: null,
              page_count: null,
            },
          ],
          fallbacks: ["metadata", "download"],
        },
      }),
    );

    const manifest = await getLibraryPreviewManifest("workspace-1", "version-1");

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/library/versions/version-1/preview",
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "include" });
    expect(manifest.variants[0].text).toBe("Preview body");
    expect(manifest.variants[0]).not.toHaveProperty("byte_size");
    expect(manifest.variants[0]).not.toHaveProperty("width");
    expect(manifest.variants[0]).not.toHaveProperty("height");
    expect(manifest.variants[0]).not.toHaveProperty("duration_ms");
    expect(manifest.variants[0]).not.toHaveProperty("page_count");
  });

  it.each([404, 405, 501])(
    "marks HTTP %s as an unavailable additive endpoint",
    async (status) => {
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse(
          {
            error_code: "not_found",
            message: "not found",
            request_id: "request-1",
            warnings: [],
          },
          status,
        ),
      );

      await expect(
        getLibraryPreviewManifest("workspace-1", "version-1"),
      ).rejects.toBeInstanceOf(PreviewManifestEndpointUnavailableError);
    },
  );
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
