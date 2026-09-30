import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../client";
import {
  PAGE_AUTOSAVE_BEACON_LIMIT_BYTES,
  autosavePage,
  createPage,
  createPageComment,
  diffPageVersions,
  listPages,
  listPagesPage,
  publishPageVersion,
  readPageBySlug,
  restorePageVersion,
  sendPageAutosave,
  updatePage,
} from "../pages";

const WORKSPACE_ID = "w1";

function pageSummary(id: string) {
  return {
    id,
    slug: id,
    title: id,
    page_type: "overview",
    lifecycle_status: "active",
    tags: [],
    aliases: [],
    current_version_id: "v1",
    updated_at: "2026-05-08T00:00:00Z",
    is_public: false,
  };
}

function pageDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: "p1",
    workspace_id: WORKSPACE_ID,
    slug: "hello",
    title: "Hello",
    page_type: "overview",
    tags: [],
    aliases: [],
    current_version_id: "v1",
    updated_at: "2026-05-08T00:00:00Z",
    content_hash: "a".repeat(64),
    body_md: "# Hello",
    version_id: "v1",
    version_status: "editing",
    parent_version_id: null,
    created_at: "2026-05-08T00:00:00Z",
    editing_revision: 1,
    is_public: false,
    is_alternative: false,
    is_discarded: false,
    is_draft_head: true,
    is_published_head: false,
    is_visible_head: true,
    preserved_as_alternative: false,
    superseded_review_count: 0,
    reconciliation_available: false,
    reconciliation_pending_count: 0,
    reconciliation_pending_count_truncated: false,
    backlink_count: 0,
    children_count: 0,
    ...overrides,
  };
}

describe("pages api", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("sends cookie credentials and create payload", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ data: pageDetail({ id: "p1", slug: "hello", title: "Hello" }) }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    await createPage(WORKSPACE_ID, {
      title: "Hello",
      body_md: "# Hello",
      page_type: "decision",
      tags: ["release"],
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          title: "Hello",
          body_md: "# Hello",
          page_type: "decision",
          tags: ["release"],
        }),
      }),
    );
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBeNull();
    expect(fetchMock.mock.calls[0][1]?.credentials).toBe("include");
  });

  it("throws ApiError for stale conflicts", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "stale_base_version",
          message: "stale",
          request_id: "r1",
          warnings: [],
          server_version_id: "v2",
          server_content_hash: "b".repeat(64),
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(
      updatePage(WORKSPACE_ID, "p1", {
        title: "Hello",
        body_md: "# Hello",
        base_version_id: "v1",
        base_content_hash: "a".repeat(64),
        on_stale: "conflict",
      }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("posts autosave payloads and returns the saved page", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          data: pageDetail({
            current_version_id: "v2",
            content_hash: "b".repeat(64),
            version_id: "v2",
            parent_version_id: "v1",
          }),
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const page = await autosavePage(WORKSPACE_ID, "p1", {
      idempotency_key: "test-key-1",
      title: "Hello",
      body_md: "# Hello",
      base_version_id: "v2",
      base_content_hash: "b".repeat(64),
      session_version_id: "v2",
      expected_editing_revision: 1,
      on_stale: "conflict",
      retry_only: false,
    });

    expect(page.version_id).toBe("v2");
    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages/p1/autosave`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          idempotency_key: "test-key-1",
          title: "Hello",
          body_md: "# Hello",
          base_version_id: "v2",
          base_content_hash: "b".repeat(64),
          session_version_id: "v2",
          expected_editing_revision: 1,
          on_stale: "conflict",
          retry_only: false,
        }),
      }),
    );
  });

  it("publishes a staged page version with the generated request contract", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          data: pageDetail({
            version_id: "v2",
            parent_version_id: "v1",
          }),
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const result = await publishPageVersion(WORKSPACE_ID, "p1", "v2", {
      reason: "Publish from test.",
      publish_despite_findings: false,
      expected_content_hash: "a".repeat(64),
    });

    expect(result.page.version_id).toBe("v2");
    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages/p1/versions/v2/publish`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          reason: "Publish from test.",
          publish_despite_findings: false,
          expected_content_hash: "a".repeat(64),
        }),
      }),
    );
  });

  it("lists pages for a workspace", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ data: [], next_cursor: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await listPages(WORKSPACE_ID);

    expect(result.data).toEqual([]);
    expect(result.next_cursor).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=100`,
    );
  });

  it("follows next_cursor until the page list is exhausted", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            data: [pageSummary("p1"), pageSummary("p2")],
            next_cursor: "cursor-1",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ data: [pageSummary("p3")], next_cursor: null }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );

    const result = await listPages(WORKSPACE_ID);

    expect(result.data.map((entry) => entry.id)).toEqual(["p1", "p2", "p3"]);
    expect(result.next_cursor).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=100`,
    );
    expect(String(fetchMock.mock.calls[1][0])).toBe(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=100&cursor=cursor-1`,
    );
  });

  it("stops following page cursors at the hard request cap", async () => {
    let request = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      request += 1;
      return new Response(
        JSON.stringify({
          data: [pageSummary(`p${request}`)],
          next_cursor: `cursor-${request}`,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });

    const result = await listPages(WORKSPACE_ID);

    expect(fetchMock).toHaveBeenCalledTimes(10);
    expect(result.data).toHaveLength(10);
    expect(result.next_cursor).toBe("cursor-10");
  });

  it("listPagesPage fetches exactly one page and does not follow its own cursor (UI-07)", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          data: [pageSummary("p1"), pageSummary("p2")],
          next_cursor: "cursor-1",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const result = await listPagesPage(WORKSPACE_ID, { limit: 10 });

    expect(result.data.map((entry) => entry.id)).toEqual(["p1", "p2"]);
    expect(result.next_cursor).toBe("cursor-1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=10`,
    );
  });

  it("listPagesPage carries an explicit cursor into the request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ data: [], next_cursor: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await listPagesPage(WORKSPACE_ID, { cursor: "cursor-1", limit: 10 });

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=10&cursor=cursor-1`,
    );
  });

  it("creates page inline comments", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          data: {
            id: "review-1",
            page_id: "p1",
            review_item_id: "review-1",
            change_set_id: "change-1",
            change_operation_id: "operation-1",
            status: "pending",
            risk_level: "low",
            note: "Needs a source.",
            anchor: {
              anchor_text: "Claim",
              context_before: "",
              context_after: "",
              version_id: "v1",
              char_offset_hint: 0,
            },
            created_at: "2026-05-20T00:00:00Z",
            updated_at: "2026-05-20T00:00:00Z",
          },
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    await createPageComment(WORKSPACE_ID, "p1", {
      note: "Needs a source.",
      anchor: {
        anchor_text: "Claim",
        context_before: "",
        context_after: "",
        version_id: "v1",
        char_offset_hint: 0,
      },
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages/p1/comments`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          note: "Needs a source.",
          anchor: {
            anchor_text: "Claim",
            context_before: "",
            context_after: "",
            version_id: "v1",
            char_offset_hint: 0,
          },
        }),
      }),
    );
  });

  it("restores a historical page version", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ data: pageDetail({}) }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const page = await restorePageVersion(WORKSPACE_ID, "p1", "v1");

    expect(page.version_id).toBe("v1");
    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages/p1/restore`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ version_id: "v1" }),
      }),
    );
  });

  it("requests an explicit empty baseline for a first-version diff", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          data: {
            baseline: "empty",
            from_version_id: null,
            to_version_id: "v1",
            from_version_no: 0,
            to_version_no: 1,
            lines: ["--- empty", "+++ v1", "@@ -0,0 +1 @@", "+# First"],
            additions: 1,
            deletions: 0,
            metadata_changes: [],
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const diff = await diffPageVersions(WORKSPACE_ID, "p1", null, "v1");

    expect(diff.from_version_no).toBe(0);
    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages/p1/diff?baseline=empty&to_version_id=v1`,
      expect.any(Object),
    );
  });

  it("reads a page by slug through the direct endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          data: pageDetail({
            id: "p51",
            slug: "page-51",
            title: "Page 51",
            current_version_id: "v51",
            content_hash: "c".repeat(64),
            version_id: "v51",
          }),
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const page = await readPageBySlug(WORKSPACE_ID, "page-51");

    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:21080/api/v1/workspaces/${WORKSPACE_ID}/context/pages/by-slug/page-51`,
      expect.any(Object),
    );
    expect(page?.id).toBe("p51");
  });

  it("returns null when slug lookup returns not found", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error_code: "not_found",
          message: "page not found",
          request_id: "r1",
          warnings: [],
        }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(readPageBySlug(WORKSPACE_ID, "missing")).resolves.toBeNull();
  });

  it("sendPageAutosave uses sendBeacon path for small payloads when unauthenticated", () => {
    const beaconFn = vi.fn().mockReturnValue(true);
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      value: beaconFn,
    });

    const ok = sendPageAutosave(WORKSPACE_ID, "p1", {
      idempotency_key: "test-key-2",
      title: "T",
      body_md: "small body",
      tags: [],
      base_version_id: "v1",
      base_content_hash: "a".repeat(64),
      expected_editing_revision: 1,
      on_stale: "conflict",
      retry_only: false,
    });

    expect(ok).toBe(true);
    expect(beaconFn).toHaveBeenCalledTimes(1);
    expect(beaconFn.mock.calls[0][0]).toContain(
      `/api/v1/workspaces/${WORKSPACE_ID}/context/pages/p1/autosave`,
    );
  });

  it("sendPageAutosave falls back to keepalive fetch when sendBeacon declines", () => {
    const beaconFn = vi.fn().mockReturnValue(false);
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      value: beaconFn,
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("{}", { status: 200 }));

    const ok = sendPageAutosave(WORKSPACE_ID, "p1", {
      idempotency_key: "test-key-3",
      title: "T",
      body_md: "small body",
      tags: [],
      base_version_id: "v1",
      base_content_hash: "a".repeat(64),
      expected_editing_revision: 1,
      on_stale: "conflict",
      retry_only: false,
    });

    expect(ok).toBe(true);
    expect(beaconFn).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      `/api/v1/workspaces/${WORKSPACE_ID}/context/pages/p1/autosave`,
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      keepalive: true,
      method: "POST",
      credentials: "include",
    });
  });

  it("sendPageAutosave reports failure for payloads over the beacon limit", () => {
    // Chromium enforces a ~64 KiB quota on both sendBeacon and keepalive
    // fetch, and blocks synchronous XHR during page dismissal, so an
    // oversized unload autosave cannot be delivered reliably. The function
    // still attempts best-effort delivery but must return false.
    const beaconFn = vi.fn().mockReturnValue(false);
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      value: beaconFn,
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValue(new TypeError("keepalive quota exceeded"));
    const warnMock = vi.spyOn(console, "warn").mockImplementation(() => {});

    const large = "x".repeat(PAGE_AUTOSAVE_BEACON_LIMIT_BYTES + 1000);

    const ok = sendPageAutosave(WORKSPACE_ID, "p1", {
      idempotency_key: "test-key-4",
      title: "T",
      body_md: large,
      tags: [],
      base_version_id: "v1",
      base_content_hash: "a".repeat(64),
      expected_editing_revision: 1,
      on_stale: "conflict",
      retry_only: false,
    });

    expect(ok).toBe(false);
    expect(beaconFn).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ keepalive: true });
    expect(warnMock).toHaveBeenCalled();
  });

  it("sendPageAutosave trusts the browser when it accepts an oversized beacon", () => {
    // Payloads between the conservative 60 KB cap and a UA's actual quota
    // (or in browsers without one) can still be queued; sendBeacon is the
    // authority, so its acceptance wins over the size heuristic.
    const beaconFn = vi.fn().mockReturnValue(true);
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      value: beaconFn,
    });
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const warnMock = vi.spyOn(console, "warn").mockImplementation(() => {});

    const large = "x".repeat(PAGE_AUTOSAVE_BEACON_LIMIT_BYTES + 1000);

    const ok = sendPageAutosave(WORKSPACE_ID, "p1", {
      idempotency_key: "test-key-5",
      title: "T",
      body_md: large,
      tags: [],
      base_version_id: "v1",
      base_content_hash: "a".repeat(64),
      expected_editing_revision: 1,
      on_stale: "conflict",
      retry_only: false,
    });

    expect(ok).toBe(true);
    expect(beaconFn).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(warnMock).toHaveBeenCalled();
  });

  it("sendPageAutosave uses keepalive fetch with the API token when configured", () => {
    vi.stubEnv("VITE_DTXWIKI_API_TOKEN", "custom-token");
    const beaconFn = vi.fn().mockReturnValue(true);
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      value: beaconFn,
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("{}", { status: 200 }));

    try {
      const ok = sendPageAutosave(WORKSPACE_ID, "p1", {
        idempotency_key: "test-key-6",
        title: "T",
        body_md: "small body",
        tags: [],
        base_version_id: "v1",
        base_content_hash: "a".repeat(64),
        expected_editing_revision: 1,
        on_stale: "conflict",
        retry_only: false,
      });

      expect(ok).toBe(true);
      expect(beaconFn).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
      expect(headers.get("Authorization")).toBe("Bearer custom-token");
      expect(fetchMock.mock.calls[0][1]).toMatchObject({ keepalive: true });
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
