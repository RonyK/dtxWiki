import type { components } from "../api/types.gen";

export type ContextImageMediaType =
  components["schemas"]["ContextImageMediaType"];

export type UploadFilePolicy = {
  mediaTypes: readonly string[];
  extensions: readonly string[];
  maxBytes: number;
};

export type UploadFileRejection =
  | { reason: "unsupported_format" }
  | { reason: "too_large"; maxBytes: number };

const EXTENSIONS_BY_MEDIA_TYPE = {
  "image/gif": [".gif"],
  "image/jpeg": [".jpg", ".jpeg"],
  "image/png": [".png"],
  "image/webp": [".webp"],
} satisfies Readonly<Record<ContextImageMediaType, readonly string[]>>;

export function uploadPolicyForMediaTypes(
  mediaTypes: readonly string[],
  maxBytes: number,
): UploadFilePolicy {
  const normalizedMediaTypes = uniqueValues(mediaTypes.map(normalizeMediaType));
  return {
    mediaTypes: normalizedMediaTypes,
    extensions: uniqueValues(
      normalizedMediaTypes.flatMap(
        extensionsForMediaType,
      ),
    ),
    maxBytes,
  };
}

export function uploadAcceptValue(policy: UploadFilePolicy) {
  return uniqueValues([...policy.extensions, ...policy.mediaTypes]).join(",");
}

export function uploadFileRejection(
  file: Pick<File, "name" | "size" | "type">,
  policy: UploadFilePolicy,
): UploadFileRejection | null {
  const mediaType = normalizeMediaType(file.type);
  const filename = file.name.trim().toLowerCase();
  const matchesMediaType =
    mediaType !== "" && policy.mediaTypes.includes(mediaType);
  const matchesExtension = policy.extensions.some(
    (extension) => extension !== "" && filename.endsWith(extension),
  );
  if (!matchesMediaType && !matchesExtension) {
    return { reason: "unsupported_format" };
  }
  if (Number.isFinite(policy.maxBytes) && file.size > policy.maxBytes) {
    return { reason: "too_large", maxBytes: policy.maxBytes };
  }
  return null;
}

export function formatUploadByteLimit(bytes: number) {
  const mebibytes = bytes / (1024 * 1024);
  if (mebibytes >= 1) {
    return `${Number.isInteger(mebibytes) ? mebibytes : mebibytes.toFixed(1)} MiB`;
  }
  return `${Math.max(1, Math.ceil(bytes / 1024))} KiB`;
}

export function isContextImageMediaType(
  mediaType: string,
): mediaType is ContextImageMediaType {
  return Object.prototype.hasOwnProperty.call(
    EXTENSIONS_BY_MEDIA_TYPE,
    mediaType,
  );
}

function extensionsForMediaType(mediaType: string): readonly string[] {
  return isContextImageMediaType(mediaType)
    ? EXTENSIONS_BY_MEDIA_TYPE[mediaType]
    : [];
}

function normalizeMediaType(mediaType: string) {
  return mediaType.trim().toLowerCase();
}

function uniqueValues(values: readonly string[]) {
  return Array.from(new Set(values.filter(Boolean)));
}
