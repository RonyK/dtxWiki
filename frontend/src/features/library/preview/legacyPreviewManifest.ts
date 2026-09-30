import type { LibraryItemDetail } from "../../../shared/api/library";
import type { PreviewManifest, PreviewVariant } from "./model";

export function createLegacyPreviewManifest(
  detail: LibraryItemDetail,
  previewKind: string | null,
  versionId: string,
): PreviewManifest {
  const version = detail.versions.find((candidate) => candidate.id === versionId) ??
    detail.current_version;
  const artifacts = detail.artifacts.filter(
    (artifact) => artifact.library_item_version_id === versionId,
  );
  const extractedText = artifacts.find((artifact) => artifact.index_text)?.index_text ?? null;
  const resolvedKind = previewKind ?? legacyKind(detail);
  const variants: PreviewVariant[] = [];

  if (resolvedKind === "pdf") {
    const contentUrl = [
      detail.preview_url,
      version?.preview_url,
      ...artifacts.map((artifact) => artifact.preview_url),
      version?.original_url,
    ].find((value): value is string => Boolean(value));
    if (contentUrl) {
      variants.push({
        kind: "pdf",
        media_type: "application/pdf",
        delivery: "range",
        content_url: contentUrl,
        renderer_contract: "core.pdf.v1",
        rank: 10,
        ...(extractedText ? { text: extractedText } : {}),
      });
    }
  }
  if (extractedText) {
    if (resolvedKind === "metadata") {
      variants.push({
        kind: "metadata",
        media_type: "application/json",
        delivery: "inline",
        renderer_contract: "core.metadata.v1",
        rank: variants.length ? 20 : 10,
        text: extractedText,
      });
    } else if (resolvedKind !== "pdf" || variants.length === 0) {
      variants.push({
        kind: resolvedKind === "markdown" ? "markdown" : "text",
        media_type: resolvedKind === "markdown" ? "text/markdown" : "text/plain",
        delivery: "inline",
        renderer_contract:
          resolvedKind === "markdown" ? "core.markdown.v1" : "core.text.v1",
        rank: variants.length ? 20 : 10,
        text: extractedText,
      });
    }
  }

  return {
    schema_version: "1",
    status: variants.length > 0 ? "ready" : "unavailable",
    version_id: versionId,
    selected_preview_attempt_id: null,
    variants,
    fallbacks: ["metadata", "download"],
    ...(resolvedKind === "pdf" && !variants.some(
      (variant) => variant.renderer_contract === "core.pdf.v1",
    )
      ? { unavailable_reason: "legacy_pdf_preview_unavailable" }
      : variants.length === 0
        ? { unavailable_reason: "legacy_preview_unavailable" }
        : {}),
  };
}

function legacyKind(detail: LibraryItemDetail) {
  const format = detail.format_hint.toLowerCase();
  if (format === "pdf" || format === "application/pdf" || /\.pdf(?:$|[?#])/i.test(detail.uri)) {
    return "pdf";
  }
  if (
    format === "json" ||
    format === "application/json" ||
    /\.json(?:$|[?#])/i.test(detail.uri)
  ) {
    return "metadata";
  }
  if (
    format === "markdown" ||
    format === "text/markdown" ||
    /\.(?:md|markdown)(?:$|[?#])/i.test(detail.uri)
  ) {
    return "markdown";
  }
  return "text";
}
