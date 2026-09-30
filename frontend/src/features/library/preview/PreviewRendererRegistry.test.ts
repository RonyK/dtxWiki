import { afterEach, describe, expect, it, vi } from "vitest";

import type { PreviewVariant } from "./model";
import { supportedPreviewVariants } from "./PreviewRendererRegistry";

describe("PreviewRendererRegistry", () => {
  afterEach(() => vi.restoreAllMocks());

  it("skips unknown contracts and preserves the manifest fallback order", () => {
    const candidates = supportedPreviewVariants([
      variant("plugin.remote.v1", "application/x-remote", 10),
      variant("core.text.v1", "text/plain", 20, { text: "Safe text" }),
      variant("core.download.v1", "application/octet-stream", 30, {
        content_url: "/download",
      }),
    ]);

    expect(candidates.map((candidate) => candidate.variant.renderer_contract)).toEqual([
      "core.text.v1",
      "core.download.v1",
    ]);
  });

  it("does not hand active SVG content to the image renderer", () => {
    const candidates = supportedPreviewVariants([
      variant("core.image.v1", "image/svg+xml", 10, {
        content_url: "/unsafe.svg",
      }),
      variant("core.image.v1", "image/png", 20, {
        content_url: "/safe.png",
      }),
    ]);

    expect(candidates).toHaveLength(1);
    expect(candidates[0].variant.media_type).toBe("image/png");
  });

  it("keeps native media candidates when canPlayType has no confidence", () => {
    vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockImplementation(
      (mediaType) => mediaType === "audio/mpeg" ? "probably" : "",
    );

    const candidates = supportedPreviewVariants([
      variant("core.video.v1", "video/x-unsupported", 10, {
        content_url: "/unsupported.video",
      }),
      variant("core.audio.v1", "audio/mpeg", 20, {
        content_url: "/supported.mp3",
      }),
    ]);

    expect(candidates).toHaveLength(2);
    expect(candidates.map((candidate) => candidate.variant.media_type)).toEqual([
      "video/x-unsupported",
      "audio/mpeg",
    ]);
  });
});

function variant(
  rendererContract: string,
  mediaType: string,
  rank: number,
  extra: Partial<PreviewVariant> = {},
): PreviewVariant {
  return {
    kind: "test",
    media_type: mediaType,
    delivery: "inline",
    renderer_contract: rendererContract,
    rank,
    ...extra,
  };
}
