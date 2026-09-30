import { lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import { JsonView, defaultStyles } from "react-json-view-lite";
import "react-json-view-lite/dist/index.css";

import { MarkdownView } from "../../pages/MarkdownView";
import type { PreviewTransport } from "./AuthenticatedPreviewTransport";
import type { PreviewManifest } from "./model";

const PdfLibraryPreview = lazy(() =>
  import("../PdfLibraryPreview").then((module) => ({
    default: module.PdfLibraryPreview,
  })),
);

export function LegacyPreviewRenderer({
  manifest,
  title,
  transport,
}: {
  manifest: PreviewManifest;
  title: string;
  transport: PreviewTransport;
}) {
  const { t } = useTranslation("library");
  const variant = manifest.variants[0];
  if (variant?.renderer_contract === "core.pdf.v1" && variant.content_url) {
    const source = transport.pdfSource(variant.content_url);
    return (
      <Suspense fallback={<div className="wiki-empty">{t("reader.pdf.loadingViewer")}</div>}>
        <PdfLibraryPreview
          extractedText={variant.text ?? null}
          fileUrl={source.url}
          httpHeaders={source.httpHeaders}
          title={title}
          withCredentials={source.withCredentials}
        />
      </Suspense>
    );
  }
  const text = variant?.text ?? "";
  if (manifest.unavailable_reason === "legacy_pdf_preview_unavailable") {
    return (
      <div className="library-pdf-fallback">
        <strong>{t("reader.pdfUnavailable")}</strong>
        {text ? (
          <pre className="markdown-snippet">{text}</pre>
        ) : (
          <div className="empty-state">{t("reader.noExtractedView")}</div>
        )}
      </div>
    );
  }
  if (variant?.renderer_contract === "core.markdown.v1") {
    return (
      <div className="library-markdown-preview">
        <MarkdownView
          body={text}
          renderPolicy="untrusted-preview"
          wikilinkMode="plain"
        />
      </div>
    );
  }
  if (variant?.renderer_contract === "core.metadata.v1") {
    const parsed = safeParseJson(text);
    if (parsed !== null && typeof parsed === "object") {
      return (
        <div className="library-json-viewer">
          <JsonView
            aria-label={t("reader.jsonPreview")}
            clickToExpandNode
            data={parsed as Record<string, unknown> | unknown[]}
            shouldExpandNode={(level) => level < 2}
            style={defaultStyles}
          />
        </div>
      );
    }
  }
  return text ? (
    <pre className="markdown-snippet">{text}</pre>
  ) : (
    <div className="empty-state">{t("reader.noExtractedView")}</div>
  );
}

function safeParseJson(value: string) {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return null;
  }
}
