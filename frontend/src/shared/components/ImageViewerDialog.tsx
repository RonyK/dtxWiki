import { X } from "lucide-react";
import { createPortal } from "react-dom";
import type { HTMLAttributeReferrerPolicy } from "react";

import { Dialog } from "./Dialog";

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
 * One image, enlarged to fit the viewport.
 *
 * Extracted from MarkdownImage, which had the only enlargement in the app, so
 * Settings > Attachments could reuse it instead of growing a second one that
 * would drift. Deliberately not a zoom/pan surface: MermaidZoomViewer exists
 * because a diagram is dense enough to need magnifying, while both callers here
 * answer "what is this file?", which fitting it to the viewport answers
 * completely. `Dialog` already supplies Escape, backdrop dismissal, the focus
 * trap, focus restoration and the portal root, so none of that is rebuilt.
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
    <Dialog className="image-viewer-panel" labelledBy={titleId} onClose={onClose}>
      <div className="image-viewer">
        <h2 className="visually-hidden" id={titleId}>
          {title}
        </h2>
        <button
          aria-label={closeLabel}
          className="image-viewer-close icon-button"
          type="button"
          onClick={onClose}
        >
          <X aria-hidden="true" size={18} />
        </button>
        <img
          alt={alt}
          className="image-viewer-content"
          decoding="async"
          loading="lazy"
          referrerPolicy={referrerPolicy}
          src={src}
          onError={onError}
        />
      </div>
    </Dialog>,
    document.body,
  );
}
