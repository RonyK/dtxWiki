import type { SupportedPreviewVariant } from "./PreviewRendererRegistry";

/**
 * Which of the two things a user might want to look at for a Library item.
 * "original" is the source bytes (or the safest available stand-in for
 * them -- a sanitized re-encode, or a bare download action). "converted" is
 * the semantic output a converter produced from that source (Markdown,
 * text, or a table).
 *
 * Grouped by `renderer_contract` rather than by
 * the manifest's `source` field: `source` has a third value ("generated")
 * that belongs on the original side, and the client-synthesized fallback
 * variants (materializeFallbacks in PreviewPane) never carry `source` at
 * all, so filtering on it silently drops the download action and the
 * legacy-compatibility text/markdown fallback.
 */
export type RenditionKind = "original" | "converted";

const ORIGINAL_INLINE_CONTRACTS = new Set([
  "core.pdf.v1",
  "core.image.v1",
  "core.audio.v1",
  "core.video.v1",
]);

const CONVERTED_PRIMARY_CONTRACTS = new Set([
  "core.markdown.v1",
  "core.text.v1",
  "core.table.v1",
]);

const CONVERTED_FALLBACK_CONTRACT = "core.metadata.v1";
const DOWNLOAD_CONTRACT = "core.download.v1";

export type PreviewRenditionBuckets = {
  /** Inline-renderable original candidates. Never includes the download action. */
  original: SupportedPreviewVariant[];
  /**
   * Converted-output candidates. Holds the primary contracts (markdown,
   * text, table) when any exist; falls back to metadata only when there is
   * no other converted output at all (§3-A / D-6).
   */
  converted: SupportedPreviewVariant[];
};

/**
 * Splits an already-filtered candidate list (i.e. `core.download.v1` has
 * already been pulled out by the caller, and any metadata gating for the
 * manifest's status has already been applied) into the two switchable
 * renditions.
 */
export function splitPreviewRenditions(
  candidates: SupportedPreviewVariant[],
): PreviewRenditionBuckets {
  const original: SupportedPreviewVariant[] = [];
  const convertedPrimary: SupportedPreviewVariant[] = [];
  const convertedFallback: SupportedPreviewVariant[] = [];
  for (const candidate of candidates) {
    const contract = candidate.variant.renderer_contract;
    if (contract === DOWNLOAD_CONTRACT) {
      // The download action is handled separately by the caller -- it is
      // an action available from within either rendition's empty state,
      // never a switchable rendition of its own.
      continue;
    }
    if (ORIGINAL_INLINE_CONTRACTS.has(contract)) {
      original.push(candidate);
    } else if (CONVERTED_PRIMARY_CONTRACTS.has(contract)) {
      convertedPrimary.push(candidate);
    } else if (contract === CONVERTED_FALLBACK_CONTRACT) {
      convertedFallback.push(candidate);
    }
  }
  return {
    original,
    converted: convertedPrimary.length > 0 ? convertedPrimary : convertedFallback,
  };
}

/**
 * §3-B: prefer the original when it has an inline candidate (the fix this
 * whole feature exists for -- a converted Markdown no longer hides a
 * successfully rendered PDF/image/audio/video). Otherwise show the
 * converted output. If neither exists, land on "original" so the empty
 * state's download action (if any) has somewhere to live.
 */
export function defaultRendition(buckets: PreviewRenditionBuckets): RenditionKind {
  if (buckets.original.length > 0) return "original";
  if (buckets.converted.length > 0) return "converted";
  return "original";
}

export function otherRendition(rendition: RenditionKind): RenditionKind {
  return rendition === "original" ? "converted" : "original";
}

export type RenditionResolution = {
  rendition: RenditionKind;
  /**
   * True when `preferred` named a rendition with zero candidates *for this
   * specific version*, and the other rendition was shown in its place. The
   * caller should say so (doc §3-C/§3-D) without changing what the viewer
   * preferred -- switching versions again may bring the preferred one back.
   */
  substituted: boolean;
};

/**
 * Separates what the viewer *wants* to look at (`preferred`, persisted for
 * the life of the item) from what the *current* manifest can actually show
 * (doc §3-C). A structurally missing bucket for this version substitutes the
 * other one automatically -- no click required, unlike a renderer exhausted
 * by runtime errors (doc D-4), which stays a manual cross-link in the caller.
 */
export function resolveRendition(
  preferred: RenditionKind | null,
  buckets: PreviewRenditionBuckets,
): RenditionResolution {
  const primary = preferred ?? defaultRendition(buckets);
  if (buckets[primary].length > 0) {
    return { rendition: primary, substituted: false };
  }
  const fallback = otherRendition(primary);
  if (buckets[fallback].length > 0) {
    return { rendition: fallback, substituted: true };
  }
  // Neither bucket has anything for this version. Keep `primary` so the
  // empty-state messaging lands on whichever side the viewer was already
  // looking at (or "original", `defaultRendition`'s own final fallback).
  return { rendition: primary, substituted: false };
}
