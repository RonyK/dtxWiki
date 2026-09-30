import { ApiError } from "../api/client";

/**
 * Why a media asset could not be deleted, in the terms the reader needs.
 *
 * Lifted out of ContextMediaManagerDialog so the Settings attachments page and
 * the page-level manager map the same statuses to the same words -- they call
 * the same endpoint, so the same 412 should not read as two different problems.
 */
export type MediaDeleteErrorKind = "changed" | "inUse" | "delete";

export function mediaDeleteErrorKind(error: unknown): MediaDeleteErrorKind {
  if (error instanceof ApiError && error.status === 412) {
    // The usage_token this row was listed with no longer matches: somebody
    // referenced or edited the asset between the listing and the click.
    return "changed";
  }
  if (error instanceof ApiError && error.status === 409) {
    // Still referenced by a page version. Reachable even when the row said
    // can_delete: that flag is computed from viewer-MASKED usage, so an asset
    // referenced only by another author's private draft looks free and is not.
    // Masking it the other way would disclose that someone is editing.
    return "inUse";
  }
  return "delete";
}

type ByteLabels = {
  bytes: (value: string) => string;
  kilobytes: (value: string) => string;
  megabytes: (value: string) => string;
};

/**
 * Human byte size, 1024-based.
 *
 * The repo has several byte formatters and they do not agree on the base --
 * some divide by 1000. This is the media one; it keeps the 1024 base the page
 * media manager already shipped with, and keeps its sub-1024 behaviour of
 * printing raw bytes rather than rounding a 70-byte file up to "1 KB", which
 * makes a tiny stray upload look like a real file.
 *
 * The caller supplies the labels rather than a `t` bound to one namespace, so
 * a Settings surface and a page surface can each use their own strings.
 */
export function formatMediaBytes(
  bytes: number,
  language: string,
  labels: ByteLabels,
): string {
  if (bytes < 1024) {
    return labels.bytes(new Intl.NumberFormat(language).format(bytes));
  }
  const oneDecimal = {
    maximumFractionDigits: 1,
    minimumFractionDigits: 1,
  } as const;
  if (bytes < 1024 * 1024) {
    return labels.kilobytes(
      new Intl.NumberFormat(language, oneDecimal).format(bytes / 1024),
    );
  }
  return labels.megabytes(
    new Intl.NumberFormat(language, oneDecimal).format(bytes / (1024 * 1024)),
  );
}
