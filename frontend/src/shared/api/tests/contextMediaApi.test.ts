import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  contextMediaAssetIdFromReference,
  contextMediaCapabilitiesAreCurrent,
  contextMediaPageVersionContentUrl,
  contextMediaPublicContentUrl,
  contextMediaUploadPolicy,
  deletePageContextMedia,
  getContextMediaCapabilities,
  PENDING_CONTEXT_MEDIA_CAPABILITIES,
  listPageContextMedia,
} from "../contextMedia";

const ASSET_ID = "123e4567-e89b-12d3-a456-426614174000";

describe("context media api paths", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    delete window.__DTXWIKI_RUNTIME_CONFIG__;
  });

  it("reads and normalizes workspace Context media capabilities", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          policy_revision: "context-images-v2",
          uploadable_media_types: [
            "IMAGE/JPEG",
            "image/png",
            "image/gif",
            "IMAGE/WEBP",
            "image/jpeg",
          ],
          max_upload_bytes: 2048,
          max_files_per_batch: 10,
          max_batch_bytes: 16_384,
          max_parallel_uploads: 2,
        },
      }),
    );

    const capabilities = await getContextMediaCapabilities("workspace/one");

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "http://127.0.0.1:21080/api/v1/workspaces/workspace%2Fone/context/media/capabilities",
    );
    expect(capabilities).toEqual({
      policy_revision: "context-images-v2",
      uploadable_media_types: [
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
      ],
      max_upload_bytes: 2048,
      max_files_per_batch: 10,
      max_batch_bytes: 16_384,
      max_parallel_uploads: 2,
    });
    expect(contextMediaUploadPolicy(capabilities)).toMatchObject({
      mediaTypes: ["image/jpeg", "image/png", "image/gif", "image/webp"],
      extensions: [".jpg", ".jpeg", ".png", ".gif", ".webp"],
      maxBytes: 2048,
    });
    expect(contextMediaCapabilitiesAreCurrent(capabilities)).toBe(true);
  });

  it("uses compatibility batch defaults when additive fields are absent", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          policy_revision: "context-images-v1",
          uploadable_media_types: ["image/png"],
          max_upload_bytes: 2048,
        },
      }),
    );

    await expect(getContextMediaCapabilities("workspace-1")).resolves.toMatchObject({
      max_files_per_batch: 1,
      max_batch_bytes: 10 * 1024 * 1024,
      max_parallel_uploads: 1,
    });
  });

  it("fails closed with pending capabilities for an invalid response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: { policy_revision: "broken", max_upload_bytes: 0 } }),
    );

    await expect(getContextMediaCapabilities("workspace-1")).resolves.toEqual(
      PENDING_CONTEXT_MEDIA_CAPABILITIES,
    );
    expect(PENDING_CONTEXT_MEDIA_CAPABILITIES).toEqual({
      max_batch_bytes: 0,
      max_files_per_batch: 0,
      max_parallel_uploads: 0,
      max_upload_bytes: 0,
      policy_revision: "",
      uploadable_media_types: [],
    });
    expect(contextMediaCapabilitiesAreCurrent(
      PENDING_CONTEXT_MEDIA_CAPABILITIES,
    )).toBe(false);
    expect(contextMediaUploadPolicy(PENDING_CONTEXT_MEDIA_CAPABILITIES)).toEqual({
      extensions: [],
      maxBytes: 0,
      mediaTypes: [],
    });
  });

  it.each([
    ["unknown-only", ["image/avif"]],
    ["mixed known and unknown", ["image/png", "image/avif"]],
  ])("fails closed for %s capability media types", async (_label, mediaTypes) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          policy_revision: "context-images-schema-drift",
          uploadable_media_types: mediaTypes,
          max_upload_bytes: 2048,
          max_files_per_batch: 10,
          max_batch_bytes: 16_384,
          max_parallel_uploads: 2,
        },
      }),
    );

    const capabilities = await getContextMediaCapabilities("workspace-1");

    expect(capabilities).toEqual(PENDING_CONTEXT_MEDIA_CAPABILITIES);
    expect(contextMediaCapabilitiesAreCurrent(capabilities)).toBe(false);
  });

  it("lists used and unused page media with the typed usage filter", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: [mediaAsset("history_only")] }),
    );

    await expect(
      listPageContextMedia("workspace-1", "page/one", "used"),
    ).resolves.toEqual([
      expect.objectContaining({ id: ASSET_ID, usage_state: "history_only" }),
    ]);
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "http://127.0.0.1:21080/api/v1/workspaces/workspace-1/context/pages/page%2Fone/media?usage=used",
    );
  });

  it("deletes page media with the opaque usage token in If-Match", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          id: ASSET_ID,
          association_state: "tombstoned",
          gc_state: "queued",
        },
      }),
    );

    await expect(
      deletePageContextMedia("workspace-1", ASSET_ID, "cmu1_opaque"),
    ).resolves.toMatchObject({ id: ASSET_ID, gc_state: "queued" });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "DELETE" });
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("If-Match")).toBe(
      "cmu1_opaque",
    );
  });

  it("parses only the exact logical asset reference", () => {
    expect(contextMediaAssetIdFromReference(`dtx-media://asset/${ASSET_ID}`)).toBe(
      ASSET_ID,
    );
    expect(
      contextMediaAssetIdFromReference(`dtx-media://asset/${ASSET_ID}/content`),
    ).toBeNull();
    expect(
      contextMediaAssetIdFromReference(`dtx-media://asset/${ASSET_ID}?download=1`),
    ).toBeNull();
    expect(contextMediaAssetIdFromReference("dtx-media://asset/not-a-uuid")).toBeNull();
    expect(
      contextMediaAssetIdFromReference(` dtx-media://asset/${ASSET_ID}`),
    ).toBeNull();
    expect(
      contextMediaAssetIdFromReference(`DTX-MEDIA://asset/${ASSET_ID}`),
    ).toBeNull();
  });

  it("builds the scoped page-version and public content paths", () => {
    expect(
      contextMediaPageVersionContentUrl(
        "workspace/zero",
        "page/one",
        "version/two",
        ASSET_ID,
      ),
    ).toBe(
      `/api/v1/workspaces/workspace%2Fzero/context/pages/page%2Fone/versions/version%2Ftwo/media/${ASSET_ID}/content`,
    );
    expect(contextMediaPublicContentUrl("team docs", "release/notes", ASSET_ID)).toBe(
      `/api/v1/public/w/team%20docs/pages/release%2Fnotes/media/${ASSET_ID}`,
    );
  });

  it("refuses to build a content path for a non-UUID asset id", () => {
    expect(() =>
      contextMediaPageVersionContentUrl("workspace-1", "page-1", "version-1", "../blob"),
    ).toThrow("Context media asset id was invalid.");
  });
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mediaAsset(usageState: "active" | "history_only" | "unused") {
  return {
    id: ASSET_ID,
    logical_ref: `dtx-media://asset/${ASSET_ID}`,
    original_filename: "diagram.png",
    media_type: "image/png",
    bytes: 128,
    association_state: "active",
    usage_state: usageState,
    latest_draft_reference_count: 0,
    current_published_reference_count: 0,
    historical_reference_count: usageState === "history_only" ? 1 : 0,
    can_delete: usageState === "unused",
    usage_token: "cmu1_opaque",
    content_url: `/api/v1/workspaces/workspace-1/context/pages/page-1/versions/v1/media/${ASSET_ID}/content`,
  };
}
