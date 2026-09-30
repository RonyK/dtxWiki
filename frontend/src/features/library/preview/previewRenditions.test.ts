import { describe, expect, it } from "vitest";

import type { SupportedPreviewVariant } from "./PreviewRendererRegistry";
import {
  defaultRendition,
  otherRendition,
  resolveRendition,
  splitPreviewRenditions,
} from "./previewRenditions";

function candidate(
  renderer_contract: string,
  overrides: Record<string, unknown> = {},
): SupportedPreviewVariant {
  return {
    registration: {} as SupportedPreviewVariant["registration"],
    variant: {
      kind: "test",
      media_type: "text/plain",
      delivery: "inline",
      renderer_contract,
      rank: 0,
      ...overrides,
    },
  };
}

describe("splitPreviewRenditions", () => {
  it("puts pdf/image/audio/video in the original bucket", () => {
    const candidates = [
      candidate("core.pdf.v1"),
      candidate("core.image.v1"),
      candidate("core.audio.v1"),
      candidate("core.video.v1"),
    ];
    const buckets = splitPreviewRenditions(candidates);
    expect(buckets.original).toHaveLength(4);
    expect(buckets.converted).toHaveLength(0);
  });

  it("puts markdown/text/table in the converted bucket", () => {
    const candidates = [
      candidate("core.markdown.v1"),
      candidate("core.text.v1"),
      candidate("core.table.v1"),
    ];
    const buckets = splitPreviewRenditions(candidates);
    expect(buckets.original).toHaveLength(0);
    expect(buckets.converted).toHaveLength(3);
  });

  it("uses metadata only when there is no primary converted output (D-6)", () => {
    const withPrimary = splitPreviewRenditions([
      candidate("core.metadata.v1"),
      candidate("core.markdown.v1"),
    ]);
    expect(withPrimary.converted).toEqual([
      expect.objectContaining({
        variant: expect.objectContaining({ renderer_contract: "core.markdown.v1" }),
      }),
    ]);

    const metadataOnly = splitPreviewRenditions([candidate("core.metadata.v1")]);
    expect(metadataOnly.converted).toHaveLength(1);
    expect(metadataOnly.converted[0]?.variant.renderer_contract).toBe(
      "core.metadata.v1",
    );
  });

  it("never treats the download action as a switchable rendition", () => {
    const buckets = splitPreviewRenditions([candidate("core.download.v1")]);
    expect(buckets.original).toHaveLength(0);
    expect(buckets.converted).toHaveLength(0);
  });

  it("separates a sanitized generated re-encode from a semantic preview crop (D-10)", () => {
    // A `source: "generated"` re-encode and a `source: "semantic"` role=preview
    // crop can both claim core.image.v1 for the same item once native preview
    // providers are enabled -- see doc §D-10. Both still belong on the
    // original side; the bucket split doesn't need to arbitrate between them,
    // only keep them off the converted side.
    const buckets = splitPreviewRenditions([
      candidate("core.image.v1", { source: "generated" }),
      candidate("core.image.v1", { source: "semantic" }),
    ]);
    expect(buckets.original).toHaveLength(2);
    expect(buckets.converted).toHaveLength(0);
  });
});

describe("defaultRendition", () => {
  it("prefers original when it has an inline candidate", () => {
    expect(
      defaultRendition({
        original: [candidate("core.pdf.v1")],
        converted: [candidate("core.markdown.v1")],
      }),
    ).toBe("original");
  });

  it("falls back to converted when original has nothing inline", () => {
    expect(
      defaultRendition({ original: [], converted: [candidate("core.markdown.v1")] }),
    ).toBe("converted");
  });

  it("lands on original when both are empty (so a download action has a home)", () => {
    expect(defaultRendition({ original: [], converted: [] })).toBe("original");
  });
});

describe("otherRendition", () => {
  it("flips between the two renditions", () => {
    expect(otherRendition("original")).toBe("converted");
    expect(otherRendition("converted")).toBe("original");
  });
});

describe("resolveRendition", () => {
  const original = [candidate("core.pdf.v1")];
  const converted = [candidate("core.markdown.v1")];

  it("honors an explicit preference when that bucket has a candidate", () => {
    expect(resolveRendition("converted", { original, converted })).toEqual({
      rendition: "converted",
      substituted: false,
    });
  });

  it("falls back to defaultRendition when there is no preference yet", () => {
    expect(resolveRendition(null, { original, converted })).toEqual({
      rendition: "original",
      substituted: false,
    });
  });

  it("auto-substitutes the other bucket when the preferred one is empty for this version, without a click", () => {
    expect(
      resolveRendition("converted", { original, converted: [] }),
    ).toEqual({ rendition: "original", substituted: true });
    expect(
      resolveRendition("original", { original: [], converted }),
    ).toEqual({ rendition: "converted", substituted: true });
  });

  it("keeps the preferred rendition (unsubstituted) when both buckets are empty", () => {
    expect(
      resolveRendition("converted", { original: [], converted: [] }),
    ).toEqual({ rendition: "converted", substituted: false });
  });

  it("lands on original when both are empty and nothing was ever preferred", () => {
    expect(resolveRendition(null, { original: [], converted: [] })).toEqual({
      rendition: "original",
      substituted: false,
    });
  });
});
