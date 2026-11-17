import {
  EyeOff,
  FileIcon,
  FileText,
  Film,
  ImageIcon,
  ImageOff,
  Lock,
  Music,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  fileExtensionLabel,
  mediaFileCategory,
  type MediaFileCategory,
} from "../lib/mediaFile";

/** Why a stored file shows no thumbnail. */
export type MediaPreviewUnavailableReason =
  /** Only versions the viewer cannot read use the file. */
  | "unreadable"
  /** The server-admin view never shows file contents. */
  | "serverScope"
  /** The thumbnail request failed. */
  | "failed";

const CATEGORY_ICONS: Record<MediaFileCategory, LucideIcon> = {
  image: ImageIcon,
  audio: Music,
  video: Film,
  document: FileText,
  file: FileIcon,
};

const REASON_ICONS: Record<MediaPreviewUnavailableReason, LucideIcon> = {
  unreadable: Lock,
  serverScope: EyeOff,
  failed: ImageOff,
};

type Props = {
  filename: string;
  mediaType: string;
  /** Omitted when the file type simply has no still to show. */
  reason?: MediaPreviewUnavailableReason;
  /** The caller's thumbnail box class, so the tile matches a real preview. */
  className: string;
};

/**
 * The thumbnail stand-in: the file's kind as an icon, its extension as text,
 * and a corner badge saying why there is no preview.
 */
export function MediaPreviewPlaceholder({
  filename,
  mediaType,
  reason,
  className,
}: Props) {
  const { t } = useTranslation("pages");
  const category = mediaFileCategory(mediaType);
  const extension = fileExtensionLabel(filename, mediaType);
  const KindIcon = CATEGORY_ICONS[category];
  const ReasonIcon = reason ? REASON_ICONS[reason] : null;
  const name = [t(`mediaPreview.types.${category}`), extension]
    .filter(Boolean)
    .join(" ");
  const explanation = t(`mediaPreview.reasons.${reason ?? "noStill"}`);
  return (
    <span
      aria-label={`${name}. ${explanation}`}
      className={`${className} media-preview-placeholder`}
      data-reason={reason ?? "noStill"}
      role="img"
      title={explanation}
    >
      <KindIcon aria-hidden="true" size={22} strokeWidth={1.75} />
      {extension ? (
        <span aria-hidden="true" className="media-preview-placeholder-extension">
          {extension}
        </span>
      ) : null}
      {ReasonIcon ? (
        <span aria-hidden="true" className="media-preview-placeholder-badge">
          <ReasonIcon size={12} />
        </span>
      ) : null}
    </span>
  );
}
