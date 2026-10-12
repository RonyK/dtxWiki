import { ChevronLeft, ChevronRight, Minus, Plus } from "lucide-react";
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { useTranslation } from "react-i18next";
import "react-pdf/dist/Page/TextLayer.css";

// react-pdf rasterises the page at exactly the `width` it is given AND lays the
// selectable text layer out to match, so a fixed width does not survive a
// narrow pane: `max-width: 100%` squeezes the canvas while the text layer keeps
// the original width. Measured on a booted app at the previous fixed 720:
// the canvas rendered 248px wide on a 360px viewport while the text layer
// stayed 720px, which is the 460px of empty width the reading pane scrolled
// sideways to reveal -- and it covered both pager bars, so neither could be
// clicked at 1440, 1280, 834, 768 or 430px wide. Track the pane instead.
const PDF_MAX_PAGE_WIDTH = 720;
const PDF_MIN_PAGE_WIDTH = 180;

// Fitting the page to the pane makes it legible on a phone only in the sense
// that all of it is on screen: an A4 page lands at 278px wide on a 390px
// viewport, and there was no way to magnify it. Browser pinch zoom is not an
// answer -- it scales the whole app and enlarges a 278px raster, so the text
// just gets blurry. These steps multiply the fitted width and react-pdf
// re-rasterises at the new size, so the glyphs stay sharp and the text layer
// keeps following them. Above 100% the page outgrows its pane and the existing
// `overflow: auto` on .library-pdf-document pans it.
const PDF_ZOOM_STEPS = [1, 1.25, 1.5, 2, 3] as const;

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

type PdfLibraryPreviewProps = {
  fileUrl: string;
  title: string;
  extractedText: string | null;
  httpHeaders?: Record<string, string>;
  onLoadError?: (error: Error) => void;
  withCredentials?: boolean;
};

export function PdfLibraryPreview({
  fileUrl,
  title,
  extractedText,
  httpHeaders,
  onLoadError,
  withCredentials = false,
}: PdfLibraryPreviewProps) {
  const { t } = useTranslation("library");
  const [numPages, setNumPages] = useState<number | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const documentRef = useRef<HTMLDivElement | null>(null);
  const pageRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [pageWidth, setPageWidth] = useState(PDF_MAX_PAGE_WIDTH);
  const [zoomStep, setZoomStep] = useState(0);
  const zoom = PDF_ZOOM_STEPS[zoomStep];
  const zoomPercent = Math.round(zoom * 100);
  const pdfOptions = useMemo(
    () => ({
      ...(httpHeaders ? { httpHeaders } : {}),
      withCredentials,
    }),
    [httpHeaders, withCredentials],
  );

  // Layout effect so the first painted frame is already the right size, and
  // measured off the document's content box rather than the viewport: the
  // reading pane's width depends on the rail, the inspector and the breakpoint,
  // none of which a media query here could see. The element is a scroll
  // container (`overflow: auto`), so its own width never depends on the page
  // inside it and this cannot feed back on itself.
  useLayoutEffect(() => {
    const node = documentRef.current;
    if (!node || typeof ResizeObserver === "undefined") {
      return undefined;
    }
    const measure = () => {
      const styles = getComputedStyle(node);
      const inner =
        node.clientWidth -
        Number.parseFloat(styles.paddingLeft || "0") -
        Number.parseFloat(styles.paddingRight || "0");
      if (!Number.isFinite(inner)) {
        return;
      }
      const next = Math.max(
        PDF_MIN_PAGE_WIDTH,
        Math.min(PDF_MAX_PAGE_WIDTH, Math.floor(inner)),
      );
      setPageWidth((current) => (current === next ? current : next));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  function handleLoadSuccess({ numPages: nextNumPages }: { numPages: number }) {
    setNumPages(nextNumPages);
    setPageNumber((current) => Math.min(current, nextNumPages));
  }

  function alignTextLayerWithCanvas() {
    const canvas = canvasRef.current;
    const textLayer = pageRef.current?.querySelector<HTMLElement>(".textLayer");
    if (!canvas || !textLayer) return;
    // react-pdf floors the viewport in JavaScript for the canvas, while pdf.js
    // rounds its text layer in CSS. Firefox can round the same scale down one
    // pixel further. Use the rendered canvas's CSS dimensions for both layers;
    // either layer can finish first, including after a resize, zoom or page turn.
    textLayer.style.width = canvas.style.width;
    textLayer.style.height = canvas.style.height;
  }

  return (
    <div
      className="library-pdf-viewer"
      aria-label={t("reader.pdf.previewFor", {
        title,
        defaultValue: "PDF preview for {{title}}",
      })}
    >
      <div className="library-pdf-toolbar">
        <strong>{title}</strong>
        <div className="library-pdf-toolbar-controls">
          <div className="library-pdf-zoom">
            <button
              aria-label={t("reader.pdf.zoomOutAria", {
                defaultValue: "Zoom out of the PDF page",
              })}
              className="library-pdf-zoom-button"
              disabled={zoomStep <= 0}
              onClick={() => setZoomStep((current) => Math.max(0, current - 1))}
              type="button"
            >
              <Minus size={14} aria-hidden="true" />
            </button>
            <span
              aria-label={t("reader.pdf.zoomLevelAria", {
                defaultValue: "PDF zoom level",
              })}
              className="library-pdf-zoom-level"
            >
              {zoomPercent}%
            </span>
            <button
              aria-label={t("reader.pdf.zoomInAria", {
                defaultValue: "Zoom in on the PDF page",
              })}
              className="library-pdf-zoom-button"
              disabled={zoomStep >= PDF_ZOOM_STEPS.length - 1}
              onClick={() =>
                setZoomStep((current) =>
                  Math.min(PDF_ZOOM_STEPS.length - 1, current + 1),
                )
              }
              type="button"
            >
              <Plus size={14} aria-hidden="true" />
            </button>
          </div>
          <span className="library-pdf-page-indicator">
            {numPages
              ? t("reader.pdf.pageWithTotal", {
                  current: pageNumber,
                  total: numPages,
                  defaultValue: "Page {{current}} / {{total}}",
                })
              : t("reader.pdf.pageCurrent", {
                  current: pageNumber,
                  defaultValue: "Page {{current}}",
                })}
          </span>
        </div>
      </div>
      <div className="library-pdf-stage">
        <button
          aria-label={t("reader.pdf.previousAria", {
            defaultValue: "Previous PDF page",
          })}
          className="library-pdf-pager library-pdf-pager-prev"
          disabled={pageNumber <= 1}
          onClick={() => setPageNumber((current) => Math.max(1, current - 1))}
          type="button"
        >
          <ChevronLeft size={20} aria-hidden="true" />
        </button>
        <div className="library-pdf-document" ref={documentRef}>
          <Document
            file={fileUrl}
            options={pdfOptions}
            loading={(
              <div className="wiki-empty">
                {t("reader.pdf.loading", {
                  defaultValue: "Loading PDF preview…",
                })}
              </div>
            )}
            error={<PdfFallback extractedText={extractedText} />}
            onLoadError={onLoadError}
            onLoadSuccess={handleLoadSuccess}
          >
            <Page
              canvasRef={canvasRef}
              inputRef={pageRef}
              onRenderSuccess={alignTextLayerWithCanvas}
              onRenderTextLayerSuccess={alignTextLayerWithCanvas}
              pageNumber={pageNumber}
              renderAnnotationLayer={false}
              renderTextLayer
              width={Math.round(pageWidth * zoom)}
            />
          </Document>
        </div>
        <button
          aria-label={t("reader.pdf.nextAria", { defaultValue: "Next PDF page" })}
          className="library-pdf-pager library-pdf-pager-next"
          disabled={numPages === null || pageNumber >= numPages}
          onClick={() =>
            setPageNumber((current) =>
              numPages === null ? current : Math.min(numPages, current + 1),
            )
          }
          type="button"
        >
          <ChevronRight size={20} aria-hidden="true" />
        </button>
      </div>
      {extractedText ? (
        <details className="library-pdf-extracted">
          <summary>
            {t("reader.pdf.extractedText", { defaultValue: "Extracted text" })}
          </summary>
          <pre className="markdown-snippet">{extractedText}</pre>
        </details>
      ) : null}
    </div>
  );
}

function PdfFallback({ extractedText }: { extractedText: string | null }) {
  const { t } = useTranslation("library");
  return (
    <div className="library-pdf-fallback">
      <strong>{t("reader.pdfUnavailable")}</strong>
      <p>
        {t("reader.pdf.fallback", {
          defaultValue: "Open the source file or use the extracted text below.",
        })}
      </p>
      {extractedText ? (
        <pre className="markdown-snippet">{extractedText}</pre>
      ) : (
        <div className="empty-state">{t("reader.noExtractedView")}</div>
      )}
    </div>
  );
}
