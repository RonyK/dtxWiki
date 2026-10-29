import { createPortal } from "react-dom";
import type { HTMLAttributeReferrerPolicy } from "react";

import { Dialog } from "./Dialog";
import { ZoomPanViewer } from "./ZoomPanViewer";

type ImageViewerDialogProps = {
  alt: string;
  closeLabel: string;
  referrerPolicy?: HTMLAttributeReferrerPolicy;
  src: string;
  /** Names the dialog for assistive tech; rendered visually hidden. */
  title: string;
  titleId: string;
  onClose: () => void;
  onError?: () => void;
};

/**
 * One image, fitted to the viewer and zoomable.
 *
 * Shared by MarkdownImage and Settings > Attachments. It opens fitted to the
 * viewport and uses the same zoom and pan surface as the Mermaid viewer, since
 * a large screenshot fitted to the window is too small to read. `Dialog`
 * already supplies Escape, backdrop dismissal, the focus trap, focus
 * restoration and the portal root, so none of that is rebuilt.
 */
export function ImageViewerDialog({
  alt,
  closeLabel,
  referrerPolicy,
  src,
  title,
  titleId,
  onClose,
  onError,
}: ImageViewerDialogProps) {
  return createPortal(
    <Dialog className="zoom-viewer-panel" labelledBy={titleId} onClose={onClose}>
      <h2 className="visually-hidden" id={titleId}>
        {title}
      </h2>
      <ZoomPanViewer closeLabel={closeLabel} onClose={onClose}>
        <img
          alt={alt}
          className="zoom-viewer-image"
          decoding="async"
          // A native image drag would steal the pointer from panning.
          draggable={false}
          loading="lazy"
          referrerPolicy={referrerPolicy}
          src={src}
          onError={onError}
        />
      </ZoomPanViewer>
    </Dialog>,
    document.body,
  );
}
