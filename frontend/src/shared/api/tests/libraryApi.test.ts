import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  addLibraryItemVersion,
  fetchLibraryOriginal,
  getLibraryCapabilities,
  getLibrarySemanticMarkdown,
  isProtectedLibraryOriginalUrl,
  libraryUploadAccept,
  listLibraryItems,
  matchingLibraryFormat,
  updateLibrarySemanticMarkdown,
  uploadLibraryFile,
} from "../library";

const CAPABILITIES = {
  policy_revision: "policy-7",
  max_files_per_batch: 4,
  formats: [
    {
      format_id: "pdf",
      category: "document",
      extensions: [".pdf"],
      media_types: ["application/pdf"],
      upload: true,
      conversion: "stored_unconverted",
      preview_kind: "pdf",
      max_bytes: 5_000_000,
    },
    {
      format_id: "exe",
      category: "document",
      extensions: [".exe"],
      media_types: ["application/x-msdownload"],
      upload: false,
      conversion: "denied",
      preview_kind: "metadata",
      max_bytes: 1,
    },
    {
      format_id: "custom-private-bundle",
      display_name: "Private bundle",
      category: "data",
      extensions: [".private", ".priv"],
      media_types: ["application/octet-stream"],
      upload: true,
      conversion: "stored_unconverted",
      preview_kind: "metadata",
      max_bytes: 20_000_000,
    },
  ],
};

describe("library api", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    delete window.__DTXWIKI_RUNTIME_CONFIG__;
  });

  it("reads workspace capabilities and derives accept and matching from them", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: CAPABILITIES }),
    );

    const capabilities = await getLibraryCapabilities("workspace-1");

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/library/capabilities",
    );
    expect(libraryUploadAccept(capabilities)).toBe(
      ".pdf,application/pdf,.private,.priv,application/octet-stream",
    );
    expect(
      matchingLibraryFormat(
        { name: "policy.PDF", type: "application/octet-stream" },
        capabilities,
      )?.format_id,
    ).toBe("pdf");
    expect(
      matchingLibraryFormat(
        { name: "unsafe.exe", type: "application/x-msdownload" },
        capabilities,
      ),
    ).toBeNull();
    expect(
      matchingLibraryFormat(
        { name: "archive.PRIV", type: "" },
        capabilities,
      )?.format_id,
    ).toBe("custom-private-bundle");
  });

  it.each([
    { ...CAPABILITIES, max_files_per_batch: 0 },
    {
      ...CAPABILITIES,
      formats: [{ ...CAPABILITIES.formats[0], upload: "yes" }],
    },
    {
      ...CAPABILITIES,
      formats: [{ ...CAPABILITIES.formats[0], preview_kind: "iframe" }],
    },
  ])("falls back safely when a capability response is malformed", async (data) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ data }));

    const capabilities = await getLibraryCapabilities("workspace-1");

    expect(capabilities.policy_revision).toBe("unavailable");
    expect(libraryUploadAccept(capabilities)).toBe("");
  });

  it("preserves current-version processing status from the Library list", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: [
          {
            id: "item-1",
            uri: "https://example.com/item-1",
            title: "Queued item",
            connector_type: "web",
            format_hint: "pdf",
            trust_level: "trusted",
            lifecycle_status: "active",
            current_version_id: "version-1",
            current_ready_version_id: null,
            processing_status: "queued",
            updated_at: "2026-08-06T00:00:00Z",
          },
        ],
        next_cursor: null,
        total: 1,
        has_more: false,
      }),
    );

    const result = await listLibraryItems("workspace-1");

    expect(result.data[0].processing_status).toBe("queued");
  });

  it("passes the stable operation id and cancellation through the multipart upload request", async () => {
    const controller = new AbortController();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: { id: "item-1" } }),
    );

    await uploadLibraryFile(
      "workspace-1",
      new File(["pdf"], "policy.pdf", { type: "application/pdf" }),
      {
        operationId: "b3ce4dd3-1786-4ea0-8d75-91b17594d019",
        parentNodeId: "folder-1",
        signal: controller.signal,
      },
    );

    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      credentials: "include",
      signal: controller.signal,
    });
    const body = fetchMock.mock.calls[0][1]?.body as FormData;
    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect((body.get("file") as File).name).toBe("policy.pdf");
    expect(body.get("parent_node_id")).toBe("folder-1");
    expect(headers.get("X-DTX-Library-Upload-Operation-Id")).toBe(
      "b3ce4dd3-1786-4ea0-8d75-91b17594d019",
    );
  });

  it("posts a new version onto an existing item with the stable operation id", async () => {
    const controller = new AbortController();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: { id: "item-1", current_version_id: "version-2" } }),
    );

    const detail = await addLibraryItemVersion(
      "workspace-1",
      "item-1",
      new File(["pdf v2"], "policy-v2.pdf", { type: "application/pdf" }),
      {
        operationId: "b3ce4dd3-1786-4ea0-8d75-91b17594d019",
        signal: controller.signal,
      },
    );

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/library/items/item-1/versions",
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      credentials: "include",
      signal: controller.signal,
    });
    const body = fetchMock.mock.calls[0][1]?.body as FormData;
    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect((body.get("file") as File).name).toBe("policy-v2.pdf");
    // Adding a version to an existing item never carries a placement param --
    // only the new-item endpoint places into the tree.
    expect(body.get("parent_node_id")).toBeNull();
    expect(headers.get("X-DTX-Library-Upload-Operation-Id")).toBe(
      "b3ce4dd3-1786-4ea0-8d75-91b17594d019",
    );
    expect(detail.current_version_id).toBe("version-2");
  });

  it("preserves retryability and Retry-After for a failed add-version request", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        error_code: "library_storage_temporarily_unavailable",
        message: "Please retry later.",
        request_id: "request-retry-after",
        warnings: [],
      }), {
        status: 503,
        headers: {
          "Content-Type": "application/json",
          "Retry-After": "7",
        },
      }),
    );

    await expect(addLibraryItemVersion(
      "workspace-1",
      "item-1",
      new File(["pdf v2"], "policy-v2.pdf", { type: "application/pdf" }),
      { operationId: "b3ce4dd3-1786-4ea0-8d75-91b17594d019" },
    )).rejects.toMatchObject({
      retryable: true,
      retryAfterSeconds: 7,
      status: 503,
    });
  });

  it("preserves retryability and Retry-After for typed upload failures", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        error_code: "library_storage_temporarily_unavailable",
        message: "Please retry later.",
        request_id: "request-retry-after",
        warnings: [],
      }), {
        status: 503,
        headers: {
          "Content-Type": "application/json",
          "Retry-After": "7",
        },
      }),
    );

    await expect(uploadLibraryFile(
      "workspace-1",
      new File(["pdf"], "policy.pdf", { type: "application/pdf" }),
      { operationId: "b3ce4dd3-1786-4ea0-8d75-91b17594d019" },
    )).rejects.toMatchObject({
      retryable: true,
      retryAfterSeconds: 7,
      status: 503,
    });
  });

  it("downloads a protected original with bearer and session-cookie auth", async () => {
    window.__DTXWIKI_RUNTIME_CONFIG__ = { apiToken: "runtime-token" };
    const originalPath =
      "/api/v1/workspaces/workspace-1/library/versions/version-1/content";
    const controller = new AbortController();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("source bytes", {
        headers: {
          "Content-Disposition":
            "attachment; filename=policy.pdf; filename*=UTF-8''%EC%A0%95%EC%B1%85.pdf",
          "Content-Type": "application/pdf",
        },
      }),
    );

    const result = await fetchLibraryOriginal(originalPath, controller.signal);

    expect(String(fetchMock.mock.calls[0][0])).toBe(`${BASE}${originalPath}`);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "GET",
      credentials: "include",
      signal: controller.signal,
    });
    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(headers.get("Authorization")).toBe("Bearer runtime-token");
    expect(headers.get("Accept")).toBe("application/octet-stream");
    expect(result.filename).toBe("\uC815\uCC45.pdf");
    expect(result.blob).toMatchObject({ size: 12, type: "application/pdf" });
  });

  it("reads and updates the exact Library version semantic Markdown contract", async () => {
    const controller = new AbortController();
    const payload = {
      library_item_id: "item-1",
      version_id: "version-1",
      base_artifact_id: "artifact-1",
      base_content_sha256: "a".repeat(64),
      overlay_id: null,
      revision: 0,
      source: "base",
      body_markdown: "# Original",
      content_sha256: "b".repeat(64),
      byte_size: 10,
      updated_at: "2026-08-10T00:00:00Z",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ data: payload }))
      .mockResolvedValueOnce(
        jsonResponse({
          data: {
            ...payload,
            overlay_id: "overlay-1",
            revision: 1,
            source: "overlay",
            body_markdown: "# Edited",
          },
        }),
      );

    await getLibrarySemanticMarkdown("workspace-1", "version-1", controller.signal);
    await updateLibrarySemanticMarkdown(
      "workspace-1",
      "version-1",
      {
        body_markdown: "# Edited",
        expected_revision: 0,
        expected_base_artifact_id: "artifact-1",
        expected_base_content_sha256: "a".repeat(64),
      },
      controller.signal,
    );

    const expectedUrl =
      `${BASE}/api/v1/workspaces/workspace-1/library/versions/version-1` +
      "/semantic-markdown";
    expect(String(fetchMock.mock.calls[0][0])).toBe(expectedUrl);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: "include",
      signal: controller.signal,
    });
    expect(String(fetchMock.mock.calls[1][0])).toBe(expectedUrl);
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "PUT",
      credentials: "include",
      signal: controller.signal,
      body: JSON.stringify({
        body_markdown: "# Edited",
        expected_revision: 0,
        expected_base_artifact_id: "artifact-1",
        expected_base_content_sha256: "a".repeat(64),
      }),
    });
  });

  it("rejects non-API original references before fetch so bearer auth cannot leak", async () => {
    window.__DTXWIKI_RUNTIME_CONFIG__ = { apiToken: "runtime-token" };
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const rejectedUrls = [
      "https://attacker.example/original",
      "upload://workspace-1/secret.pdf",
      "storage_ref://library/secret.pdf",
      "/api/v1/workspaces/workspace-1/library/versions/version-1/original",
    ];

    for (const url of rejectedUrls) {
      expect(isProtectedLibraryOriginalUrl(url)).toBe(false);
      await expect(fetchLibraryOriginal(url)).rejects.toThrow(
        "Library original URL is not an authorized API path",
      );
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

const BASE = "http://127.0.0.1:21080";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
