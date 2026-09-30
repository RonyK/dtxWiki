import { describe, expect, it } from "vitest";

import {
  normalizePreviewManifest,
  PreviewManifestContractError,
} from "./model";

describe("PreviewManifest", () => {
  it("validates an exact version and sorts variants by rank", () => {
    const manifest = normalizePreviewManifest(
      {
        schema_version: "1",
        status: "ready_partial",
        version_id: "version-1",
        selected_preview_attempt_id: null,
        variants: [
          {
            kind: "future",
            media_type: "application/x-future",
            delivery: "inline",
            renderer_contract: "plugin.future.v1",
            rank: 20,
          },
          {
            kind: "text",
            media_type: "TEXT/PLAIN",
            delivery: "inline",
            renderer_contract: "core.text.v1",
            rank: 10,
            text: "Preview body",
          },
        ],
        fallbacks: ["metadata", "download", "metadata"],
      },
      "version-1",
    );

    expect(manifest.variants.map((variant) => variant.renderer_contract)).toEqual([
      "core.text.v1",
      "plugin.future.v1",
    ]);
    expect(manifest.variants[0].media_type).toBe("text/plain");
    expect(manifest.fallbacks).toEqual(["metadata", "download"]);
  });

  it.each([
    [{ schema_version: "2" }, "unsupported schema_version"],
    [
      {
        schema_version: "1",
        status: "ready",
        version_id: "other-version",
        selected_preview_attempt_id: null,
        variants: [],
        fallbacks: [],
      },
      "does not match",
    ],
  ])("rejects an invalid manifest contract", (value, message) => {
    expect(() => normalizePreviewManifest(value, "version-1")).toThrowError(
      PreviewManifestContractError,
    );
    expect(() => normalizePreviewManifest(value, "version-1")).toThrow(message);
  });

  it("normalizes an optional bounded codec list", () => {
    const manifest = normalizePreviewManifest({
      schema_version: "1",
      status: "ready",
      version_id: "version-1",
      selected_preview_attempt_id: null,
      variants: [
        {
          kind: "video",
          media_type: "video/mp4",
          delivery: "inline",
          renderer_contract: "core.video.v1",
          rank: 10,
          content_url: "/preview/source",
          codecs: [" avc1 ", "mp4a.40.2", "avc1"],
        },
        {
          kind: "audio",
          media_type: "audio/wav",
          delivery: "inline",
          renderer_contract: "core.audio.v1",
          rank: 20,
          content_url: "/preview/source",
          codecs: [],
        },
      ],
      fallbacks: ["download"],
    });

    expect(manifest.variants[0].codecs).toEqual(["avc1", "mp4a.40.2"]);
    expect(manifest.variants[1].codecs).toEqual([]);
  });

  it("rejects an unbounded codec list", () => {
    expect(() => normalizePreviewManifest({
      schema_version: "1",
      status: "ready",
      version_id: "version-1",
      selected_preview_attempt_id: null,
      variants: [{
        kind: "video",
        media_type: "video/mp4",
        delivery: "inline",
        renderer_contract: "core.video.v1",
        rank: 10,
        content_url: "/preview/source",
        codecs: Array.from({ length: 17 }, (_, index) => `codec-${index}`),
      }],
      fallbacks: [],
    })).toThrow("codecs must contain at most 16 entries");
  });

  it("rejects an oversized codec identifier", () => {
    expect(() => normalizePreviewManifest({
      schema_version: "1",
      status: "ready",
      version_id: "version-1",
      selected_preview_attempt_id: null,
      variants: [{
        kind: "video",
        media_type: "video/mp4",
        delivery: "inline",
        renderer_contract: "core.video.v1",
        rank: 10,
        content_url: "/preview/source",
        codecs: ["a".repeat(65)],
      }],
      fallbacks: [],
    })).toThrow("codecs[0] must contain at most 64 characters");
  });
});
