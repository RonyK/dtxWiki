import {
  createContext,
  useContext,
  useId,
  useState,
  type ComponentPropsWithoutRef,
  type SyntheticEvent,
} from "react";
import { createPortal } from "react-dom";
import { ImageOff } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ImageViewerDialog } from "../../shared/components/ImageViewerDialog";

export type MarkdownImageProps = ComponentPropsWithoutRef<"img"> & {
  node?: unknown;
  "data-dtx-media-state"?: "ready" | "unavailable";
  "data-dtx-media-reason"?: string;
};

type ImageLoadState = "loading" | "loaded" | "failed";

const MAX_MANUAL_RETRIES = 2;

export type MarkdownImageLinkContextValue = {
  actionsHost: HTMLElement | null;
  mode: "within-link";
};

export const MarkdownImageLinkContext =
  createContext<MarkdownImageLinkContextValue | null>(null);

export function MarkdownImage(props: MarkdownImageProps) {
  const identity = [
    props.src ?? "",
    props["data-dtx-media-state"] ?? "",
    props["data-dtx-media-reason"] ?? "",
  ].join("\u0000");
  return <MarkdownImageState key={identity} {...props} />;
}

function MarkdownImageState({
  node: _node,
  alt = "",
  className,
  crossOrigin: _crossOrigin,
  decoding: _decoding,
  loading: _loading,
  onError,
  onLoad,
  referrerPolicy,
  src,
  "data-dtx-media-state": managedState,
  "data-dtx-media-reason": managedReason,
  ...props
}: MarkdownImageProps) {
  const { t } = useTranslation("pages");
  const linkedImage = useContext(MarkdownImageLinkContext);
  const unavailable = managedState === "unavailable" || !src;
  const [loadState, setLoadState] = useState<ImageLoadState>(
    unavailable ? "failed" : "loading",
  );
  const [manualRetries, setManualRetries] = useState(0);
  const [viewerOpen, setViewerOpen] = useState(false);
  const titleId = useId();
  const source = imageSourceDetails(src);
  const withinAuthoredLink = linkedImage?.mode === "within-link";
  const accessibleName = alt.trim() || t("image.unnamed", { defaultValue: "Image" });
  const canRetry = Boolean(src) && managedState !== "unavailable" &&
    manualRetries < MAX_MANUAL_RETRIES;
  const imageClassName = ["markdown-image", className].filter(Boolean).join(" ");
  const effectiveReferrerPolicy = source?.external
    ? "no-referrer"
    : referrerPolicy;

  const handleLoad = (event: SyntheticEvent<HTMLImageElement>) => {
    setLoadState("loaded");
    onLoad?.(event);
  };
  const handleError = (event: SyntheticEvent<HTMLImageElement>) => {
    setLoadState("failed");
    setViewerOpen(false);
    onError?.(event);
  };
  const retry = () => {
    if (!canRetry) {
      return;
    }
    setManualRetries((current) => current + 1);
    setLoadState("loading");
  };

  if (loadState === "failed") {
    const failureActions = (
      <span className="markdown-image-failure-actions">
        {canRetry ? (
          <button
            aria-label={t("image.retryAria", {
              defaultValue: "Retry loading {{name}}",
              name: accessibleName,
            })}
            className="markdown-image-failure-action"
            type="button"
            onClick={retry}
          >
            {t("image.retry", { defaultValue: "Retry" })}
          </button>
        ) : src && managedState !== "unavailable" ? (
          <span className="markdown-image-retry-limit">
            {t("image.retryLimitReached", {
              defaultValue: "Retry limit reached.",
            })}
          </span>
        ) : null}
        {source ? (
          <a
            aria-label={t("image.openOriginalAria", {
              defaultValue: "Open original image: {{name}}",
              name: accessibleName,
            })}
            className="markdown-image-failure-action"
            href={source.href}
            referrerPolicy={source.external ? "no-referrer" : undefined}
            rel="noopener noreferrer"
            target="_blank"
          >
            {t("image.openOriginal", { defaultValue: "Open original" })}
          </a>
        ) : null}
      </span>
    );
    const failure = (
      <span
        className={[
          "markdown-image-failure",
          linkedImage ? "markdown-image-failure--linked" : "",
        ].filter(Boolean).join(" ")}
        data-dtx-media-reason={managedReason}
        data-dtx-media-state={managedState}
        data-load-state="failed"
        role="status"
      >
        <ImageOff aria-hidden="true" className="markdown-image-failure-icon" size={22} />
        <span className="markdown-image-failure-copy">
          <strong>{t("image.unavailable", {
            defaultValue: "This image could not be displayed.",
          })}</strong>
          {alt.trim() ? (
            <span>{t("image.altText", {
              alt: alt.trim(),
              defaultValue: "Alt text: {{alt}}",
            })}</span>
          ) : null}
          {source?.hostname ? (
            <span>{t("image.sourceHost", {
              defaultValue: "Source: {{hostname}}",
              hostname: source.hostname,
            })}</span>
          ) : null}
        </span>
        {withinAuthoredLink ? null : failureActions}
      </span>
    );

    if (withinAuthoredLink) {
      return (
        <>
          {failure}
          {linkedImage.actionsHost
            ? createPortal(failureActions, linkedImage.actionsHost)
            : null}
        </>
      );
    }

    return failure;
  }

  const image = (
    <img
      {...props}
      alt={alt}
      className={imageClassName}
      data-dtx-media-reason={managedReason}
      data-dtx-media-state={managedState}
      data-load-state={loadState}
      decoding="async"
      key={manualRetries}
      loading="lazy"
      onError={handleError}
      onLoad={handleLoad}
      referrerPolicy={effectiveReferrerPolicy}
      src={src}
    />
  );

  if (withinAuthoredLink) {
    return image;
  }

  return (
    <>
      <button
        aria-expanded={viewerOpen}
        aria-haspopup="dialog"
        aria-label={t("image.expand", {
          defaultValue: "Expand {{name}}",
          name: accessibleName,
        })}
        className="markdown-image-trigger"
        type="button"
        onClick={() => setViewerOpen(true)}
      >
        {image}
      </button>
      {viewerOpen && src ? (
        <ImageViewerDialog
          alt={alt}
          closeLabel={t("image.close", {
            defaultValue: "Close image viewer",
          })}
          referrerPolicy={effectiveReferrerPolicy}
          src={src}
          title={t("image.viewerTitle", {
            defaultValue: "{{name}} viewer",
            name: accessibleName,
          })}
          titleId={titleId}
          onClose={() => setViewerOpen(false)}
        />
      ) : null}
    </>
  );
}

function imageSourceDetails(src: string | undefined) {
  if (!src) {
    return null;
  }
  try {
    const base = typeof window === "undefined"
      ? "http://localhost/"
      : window.location.href;
    const baseUrl = new URL(base);
    const resolved = new URL(src, baseUrl);
    const external = resolved.origin !== baseUrl.origin;
    return {
      external,
      hostname: external ? resolved.hostname : null,
      href: src,
    };
  } catch {
    return null;
  }
}
