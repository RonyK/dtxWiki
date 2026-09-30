import {
  Component,
  lazy,
  Suspense,
  type ComponentType,
  type ErrorInfo,
  type ReactNode,
} from "react";

import type { PreviewTransport } from "./AuthenticatedPreviewTransport";
import type { PreviewVariant } from "./model";

export type PreviewRendererProps = {
  variant: PreviewVariant;
  title: string;
  transport: PreviewTransport;
  onRendererError(error: unknown): void;
};

type PreviewRendererRegistration = {
  contract: string;
  component: ComponentType<PreviewRendererProps>;
  supports(variant: PreviewVariant): boolean;
};

const lazyRenderer = (exportName: string) =>
  lazy(async () => {
    const renderers = await import("./renderers");
    return {
      default: renderers[exportName as keyof typeof renderers] as ComponentType<PreviewRendererProps>,
    };
  });

export const PREVIEW_RENDERER_REGISTRY: readonly PreviewRendererRegistration[] = [
  registration("core.markdown.v1", "MarkdownPreviewRenderer", hasTextContent),
  registration("core.text.v1", "TextPreviewRenderer", hasTextContent),
  registration("core.table.v1", "TablePreviewRenderer", hasDataContent),
  registration("core.pdf.v1", "PdfPreviewRenderer", isPdfVariant),
  registration("core.image.v1", "ImagePreviewRenderer", isSafeImageVariant),
  registration("core.audio.v1", "AudioPreviewRenderer", isAudioVariant),
  registration("core.video.v1", "VideoPreviewRenderer", isVideoVariant),
  registration("core.metadata.v1", "MetadataPreviewRenderer", hasDataContent),
  registration("core.download.v1", "DownloadPreviewRenderer", hasContentUrl),
] as const;

export type SupportedPreviewVariant = {
  registration: PreviewRendererRegistration;
  variant: PreviewVariant;
};

export function supportedPreviewVariants(
  variants: PreviewVariant[],
): SupportedPreviewVariant[] {
  return variants.flatMap((variant) => {
    const registration = PREVIEW_RENDERER_REGISTRY.find(
      (candidate) => candidate.contract === variant.renderer_contract,
    );
    return registration?.supports(variant) ? [{ registration, variant }] : [];
  });
}

export function RegisteredPreviewRenderer({
  candidate,
  fallback,
  onRendererError,
  title,
  transport,
}: {
  candidate: SupportedPreviewVariant;
  fallback: ReactNode;
  onRendererError(error: unknown): void;
  title: string;
  transport: PreviewTransport;
}) {
  const Renderer = candidate.registration.component;
  return (
    <PreviewErrorBoundary onError={onRendererError}>
      <Suspense fallback={fallback}>
        <Renderer
          onRendererError={onRendererError}
          title={title}
          transport={transport}
          variant={candidate.variant}
        />
      </Suspense>
    </PreviewErrorBoundary>
  );
}

function registration(
  contract: string,
  exportName: string,
  supports: (variant: PreviewVariant) => boolean,
): PreviewRendererRegistration {
  return { contract, component: lazyRenderer(exportName), supports };
}

function hasContentUrl(variant: PreviewVariant) {
  return typeof variant.content_url === "string";
}

function hasTextContent(variant: PreviewVariant) {
  return typeof variant.text === "string" || hasContentUrl(variant);
}

function hasDataContent(variant: PreviewVariant) {
  return Object.hasOwn(variant, "data") || typeof variant.text === "string" || hasContentUrl(variant);
}

function isPdfVariant(variant: PreviewVariant) {
  return variant.media_type === "application/pdf" && hasContentUrl(variant);
}

function isSafeImageVariant(variant: PreviewVariant) {
  return variant.media_type.startsWith("image/") &&
    variant.media_type !== "image/svg+xml" &&
    hasContentUrl(variant);
}

function isAudioVariant(variant: PreviewVariant) {
  return variant.media_type.startsWith("audio/") &&
    hasContentUrl(variant);
}

function isVideoVariant(variant: PreviewVariant) {
  return variant.media_type.startsWith("video/") &&
    hasContentUrl(variant);
}

class PreviewErrorBoundary extends Component<
  { children: ReactNode; onError(error: unknown): void },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, _info: ErrorInfo) {
    this.props.onError(error);
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}
