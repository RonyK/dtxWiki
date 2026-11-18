/** What a stored file is, for picking its placeholder icon. */
export type MediaFileCategory = "image" | "audio" | "video" | "document" | "file";

// Text and office-style document formats. Anything else that is not image,
// audio or video falls back to the generic file icon, so a new format never
// needs its own artwork.
const DOCUMENT_MEDIA_TYPE =
  /^(?:text\/|application\/(?:pdf|rtf|msword|epub\+zip|vnd\.ms-|vnd\.openxmlformats-officedocument\.|vnd\.oasis\.opendocument\.))/;

export function mediaFileCategory(mediaType: string): MediaFileCategory {
  const type = mediaType.trim().toLowerCase();
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("audio/")) return "audio";
  if (type.startsWith("video/")) return "video";
  if (DOCUMENT_MEDIA_TYPE.test(type)) return "document";
  return "file";
}

/**
 * The file's extension as plain text ("report.final.PDF" -> "PDF"). Falls
 * back to a short media subtype when the name has no extension, and to an
 * empty label rather than a long vendor subtype. Not translated: extensions
 * read the same in every language.
 */
export function fileExtensionLabel(filename: string, mediaType: string): string {
  const extension = /\.([a-z0-9]{1,8})$/i.exec(filename.trim())?.[1];
  if (extension) return extension.toUpperCase();
  const subtype = mediaType.split("/")[1]?.split(/[;+]/)[0]?.trim() ?? "";
  return /^[a-z0-9]{1,6}$/i.test(subtype) ? subtype.toUpperCase() : "";
}
