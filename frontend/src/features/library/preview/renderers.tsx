import {
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type SyntheticEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { JsonView, defaultStyles } from "react-json-view-lite";
import "react-json-view-lite/dist/index.css";

import { MarkdownView } from "../../pages/MarkdownView";
import { PreviewRendererFailure } from "./previewErrors";
import type { PreviewRendererProps } from "./PreviewRendererRegistry";

const PdfLibraryPreview = lazy(() =>
  import("../PdfLibraryPreview").then((module) => ({
    default: module.PdfLibraryPreview,
  })),
);

export function MarkdownPreviewRenderer(props: PreviewRendererProps) {
  const content = usePreviewText(props);
  if (content === null) return <PreviewLoading />;
  return (
    <div className="library-markdown-preview">
      <MarkdownView
        body={content}
        renderPolicy="untrusted-preview"
        wikilinkMode="plain"
      />
      {props.variant.truncated ? <PreviewTruncatedNotice /> : null}
    </div>
  );
}

export function TextPreviewRenderer(props: PreviewRendererProps) {
  const content = usePreviewText(props);
  if (content === null) return <PreviewLoading />;
  return (
    <>
      <pre className="markdown-snippet">{content}</pre>
      {props.variant.truncated ? <PreviewTruncatedNotice /> : null}
    </>
  );
}

export function TablePreviewRenderer(props: PreviewRendererProps) {
  if (props.variant.media_type === "text/markdown") {
    return <MarkdownTablePreviewRenderer {...props} />;
  }
  return <StructuredTablePreviewRenderer {...props} />;
}

function MarkdownTablePreviewRenderer(props: PreviewRendererProps) {
  const content = usePreviewText(props);
  if (content === null) return <PreviewLoading />;
  return (
    <div className="library-markdown-preview library-table-preview">
      <MarkdownView
        body={content}
        renderPolicy="untrusted-preview"
        wikilinkMode="plain"
      />
      {props.variant.truncated ? <PreviewTruncatedNotice /> : null}
    </div>
  );
}

function StructuredTablePreviewRenderer(props: PreviewRendererProps) {
  const resource = usePreviewResource(props, "application/json, text/csv, text/tab-separated-values");
  const data = props.variant.data ?? parseStructuredResource(resource);
  const table = normalizeTable(data);
  useReportResourceError(resource, props.onRendererError);
  if (resource.loading || resource.error) return <PreviewLoading />;
  if (!table) {
    throw new Error("Unsupported table preview payload");
  }
  return (
    // A horizontally scrollable data table must remain keyboard-focusable.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <div className="library-preview-table-scroll" tabIndex={0}>
      <table className="library-preview-table">
        <thead>
          <tr>{table.columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {table.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {table.columns.map((column, columnIndex) => (
                <td key={`${column}-${columnIndex}`}>{displayCell(row[columnIndex])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {props.variant.truncated ? <PreviewTruncatedNotice /> : null}
    </div>
  );
}

export function PdfPreviewRenderer({
  onRendererError,
  title,
  transport,
  variant,
}: PreviewRendererProps) {
  if (!variant.content_url) return null;
  const source = transport.pdfSource(variant.content_url);
  return (
    <Suspense fallback={<PreviewLoading />}>
      <PdfLibraryPreview
        extractedText={variant.text ?? null}
        fileUrl={source.url}
        httpHeaders={source.httpHeaders}
        onLoadError={onRendererError}
        title={title}
        withCredentials={source.withCredentials}
      />
    </Suspense>
  );
}

export function ImagePreviewRenderer({
  onRendererError,
  title,
  transport,
  variant,
}: PreviewRendererProps) {
  const errorReported = useRef(false);
  const src = variant.content_url
    ? transport.mediaSource(variant.content_url)
    : null;
  useEffect(() => {
    if (!variant.content_url || src || errorReported.current) return;
    errorReported.current = true;
    onRendererError(new PreviewRendererFailure("authentication"));
  }, [onRendererError, src, variant.content_url]);
  if (!variant.content_url) return null;
  if (!src) return <PreviewLoading />;
  return (
    <figure className="library-image-preview">
      <img
        alt={variant.alt ?? title}
        crossOrigin="use-credentials"
        height={variant.height}
        onError={() => {
          if (errorReported.current) return;
          errorReported.current = true;
          onRendererError(new PreviewRendererFailure("runtime"));
        }}
        src={src}
        width={variant.width}
      />
      {variant.title ? <figcaption>{variant.title}</figcaption> : null}
    </figure>
  );
}

export function AudioPreviewRenderer(props: PreviewRendererProps) {
  return <NativeMediaPreview {...props} kind="audio" />;
}

export function VideoPreviewRenderer(props: PreviewRendererProps) {
  return <NativeMediaPreview {...props} kind="video" />;
}

export function MetadataPreviewRenderer(props: PreviewRendererProps) {
  const { t } = useTranslation("library");
  const resource = usePreviewResource(props, "application/json, text/plain");
  useReportResourceError(resource, props.onRendererError);
  if (resource.loading || resource.error) return <PreviewLoading />;
  const data = props.variant.data ?? parseStructuredResource(resource);
  if (data !== null && typeof data === "object") {
    return (
      <div className="library-json-viewer">
        <JsonView
          aria-label={t("reader.preview.metadataAria")}
          clickToExpandNode
          data={data as Record<string, unknown> | unknown[]}
          shouldExpandNode={(level) => level < 2}
          style={defaultStyles}
        />
      </div>
    );
  }
  if (typeof data === "string") return <pre className="markdown-snippet">{data}</pre>;
  throw new Error("Unsupported metadata preview payload");
}

export function DownloadPreviewRenderer({ transport, variant }: PreviewRendererProps) {
  const { t } = useTranslation("library");
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState(false);
  if (!variant.content_url) return null;

  const download = async () => {
    setDownloading(true);
    setError(false);
    try {
      const response = await transport.fetch(variant.content_url!, {
        accept: "application/octet-stream",
      });
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = safeFilename(variant.filename ?? "library-preview");
      anchor.click();
      queueMicrotask(() => URL.revokeObjectURL(url));
    } catch {
      setError(true);
    } finally {
      setDownloading(false);
    }
  };
  return (
    <div className="library-preview-download">
      <button
        aria-busy={downloading}
        className="icon-button text-button"
        disabled={downloading}
        type="button"
        onClick={() => void download()}
      >
        {downloading ? t("reader.preview.downloading") : t("reader.preview.download")}
      </button>
      {error ? <p role="alert">{t("reader.preview.downloadFailed")}</p> : null}
    </div>
  );
}

function NativeMediaPreview(
  {
    kind,
    onRendererError,
    title,
    transport,
    variant,
  }: PreviewRendererProps & { kind: "audio" | "video" },
) {
  const { t } = useTranslation("library");
  const errorReported = useRef(false);
  const src = variant.content_url
    ? transport.mediaSource(variant.content_url)
    : null;
  const playbackType = mediaPlaybackType(
    variant.media_type,
    variant.codecs,
  );
  const playbackSupport = typeof document === "undefined"
    ? ""
    : document.createElement(kind).canPlayType(playbackType);
  const poster = variant.poster_url
    ? transport.mediaSource(variant.poster_url) ?? undefined
    : undefined;
  const captions = variant.captions_url
    ? transport.mediaSource(variant.captions_url) ?? undefined
    : undefined;
  useEffect(() => {
    if (!variant.content_url || src || errorReported.current) return;
    errorReported.current = true;
    onRendererError(new PreviewRendererFailure("authentication"));
  }, [onRendererError, src, variant.content_url]);
  if (!variant.content_url) return null;
  if (!src) return <PreviewLoading />;

  const reportError = (event: SyntheticEvent<HTMLMediaElement>) => {
    if (errorReported.current) return;
    errorReported.current = true;
    const mediaErrorCode = event.currentTarget.error?.code ?? null;
    const failureKind = playbackSupport === "" ||
      mediaErrorCode === 3 ||
      mediaErrorCode === 4
      ? "codec"
      : "runtime";
    onRendererError(new PreviewRendererFailure(failureKind));
  };
  if (kind === "audio") {
    return (
      // Audio captions/transcripts are surfaced only when a provider supplies them.
      // eslint-disable-next-line jsx-a11y/media-has-caption
      <audio
        aria-label={t("reader.preview.audioAria", { title })}
        controls
        crossOrigin="use-credentials"
        preload="metadata"
        src={src}
        onError={reportError}
      />
    );
  }
  return (
    // Captions are provider-owned. Missing captions remain explicit rather than
    // inventing a track or sending media to an external transcription viewer.
    // eslint-disable-next-line jsx-a11y/media-has-caption
    <video
      aria-label={t("reader.preview.videoAria", { title })}
      controls
      crossOrigin="use-credentials"
      poster={poster}
      preload="metadata"
      src={src}
      onError={reportError}
    >
      {captions ? <track default kind="captions" src={captions} /> : null}
    </video>
  );
}

function mediaPlaybackType(mediaType: string, codecs: string[] | undefined) {
  return codecs && codecs.length > 0
    ? `${mediaType}; codecs="${codecs.join(",")}"`
    : mediaType;
}

type ResourceState<T> = { loading: boolean; value: T | null; error: unknown };

function usePreviewText(props: PreviewRendererProps) {
  const resource = usePreviewResource(props, props.variant.media_type || "text/plain");
  useReportResourceError(resource, props.onRendererError);
  if (typeof props.variant.text === "string") return props.variant.text;
  return resource.value;
}

function usePreviewResource(
  { transport, variant }: PreviewRendererProps,
  accept: string,
): ResourceState<string> {
  const [state, setState] = useState<ResourceState<string>>(() => ({
    loading: Boolean(variant.content_url && variant.text === undefined),
    value: variant.text ?? null,
    error: null,
  }));
  useEffect(() => {
    if (typeof variant.text === "string" || !variant.content_url) {
      setState({ loading: false, value: variant.text ?? null, error: null });
      return;
    }
    const controller = new AbortController();
    setState({ loading: true, value: null, error: null });
    void transport.fetch(variant.content_url, { accept, signal: controller.signal })
      .then((response) => response.text())
      .then((value) => setState({ loading: false, value, error: null }))
      .catch((error) => {
        if (!controller.signal.aborted) setState({ loading: false, value: null, error });
      });
    return () => controller.abort();
  }, [accept, transport, variant.content_url, variant.text]);
  return state;
}

function useReportResourceError<T>(
  resource: ResourceState<T>,
  onRendererError: (error: unknown) => void,
) {
  useEffect(() => {
    if (resource.error) onRendererError(resource.error);
  }, [onRendererError, resource.error]);
}

function parseStructuredResource(resource: ResourceState<string>) {
  if (resource.value === null) return null;
  try {
    return JSON.parse(resource.value) as unknown;
  } catch {
    return resource.value;
  }
}

function normalizeTable(value: unknown) {
  if (
    value === null ||
    typeof value !== "object" ||
    !Object.hasOwn(value, "columns") ||
    !Object.hasOwn(value, "rows")
  ) {
    return null;
  }
  const candidate = value as { columns: unknown; rows: unknown };
  if (
    !Array.isArray(candidate.columns) ||
    !candidate.columns.every((column) => typeof column === "string") ||
    !Array.isArray(candidate.rows) ||
    !candidate.rows.every((row) => Array.isArray(row))
  ) {
    return null;
  }
  return {
    columns: candidate.columns as string[],
    rows: candidate.rows as unknown[][],
  };
}

function displayCell(value: unknown) {
  if (value === null || value === undefined) return "";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function safeFilename(value: string) {
  return value.replace(/\\/g, "/").split("/").at(-1)?.replace(/[\u0000-\u001f\u007f]/g, "") || "library-preview";
}

function PreviewLoading() {
  const { t } = useTranslation("library");
  return <div className="wiki-empty">{t("reader.preview.loading")}</div>;
}

function PreviewTruncatedNotice() {
  const { t } = useTranslation("library");
  return <p className="library-preview-notice">{t("reader.preview.truncated")}</p>;
}
