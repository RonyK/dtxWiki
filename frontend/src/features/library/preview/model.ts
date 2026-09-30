export const PREVIEW_MANIFEST_SCHEMA_VERSION = "1" as const;

export const PREVIEW_STATUSES = [
  "queued",
  "generating",
  "ready",
  "ready_partial",
  "unavailable",
  "failed",
  "timed_out",
] as const;

export type PreviewStatus = (typeof PREVIEW_STATUSES)[number];

export type PreviewVariant = {
  id?: string;
  kind: string;
  media_type: string;
  delivery: string;
  content_url?: string;
  renderer_contract: string;
  rank: number;
  text?: string;
  data?: unknown;
  filename?: string;
  title?: string;
  alt?: string;
  captions_url?: string;
  poster_url?: string;
  truncated?: boolean;
  byte_size?: number;
  width?: number;
  height?: number;
  duration_seconds?: number;
  duration_ms?: number;
  page_count?: number;
  codecs?: string[];
  source?: "generated" | "semantic" | "original";
  provider_id?: string;
  provider_version?: string;
};

export type PreviewManifest = {
  schema_version: typeof PREVIEW_MANIFEST_SCHEMA_VERSION;
  status: PreviewStatus;
  version_id: string;
  selected_preview_attempt_id: string | null;
  variants: PreviewVariant[];
  fallbacks: string[];
  unavailable_reason?: string;
  download_url?: string;
  is_current_preview?: boolean;
};

const PREVIEW_STATUS_SET = new Set<string>(PREVIEW_STATUSES);
const MAX_VARIANTS = 64;
const MAX_FALLBACKS = 32;
const MAX_CODECS = 16;
const MAX_CODEC_LENGTH = 64;

export class PreviewManifestContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PreviewManifestContractError";
  }
}

export function normalizePreviewManifest(
  value: unknown,
  expectedVersionId?: string,
): PreviewManifest {
  if (!isRecord(value)) {
    throw contractError("manifest must be an object");
  }
  const schemaVersion = requiredString(value.schema_version, "schema_version");
  if (schemaVersion !== PREVIEW_MANIFEST_SCHEMA_VERSION) {
    throw contractError(`unsupported schema_version: ${schemaVersion}`);
  }
  const status = requiredString(value.status, "status");
  if (!PREVIEW_STATUS_SET.has(status)) {
    throw contractError(`unsupported status: ${status}`);
  }
  const versionId = requiredString(value.version_id, "version_id");
  if (expectedVersionId !== undefined && versionId !== expectedVersionId) {
    throw contractError("manifest version_id does not match the requested version");
  }
  if (!Array.isArray(value.variants) || value.variants.length > MAX_VARIANTS) {
    throw contractError(`variants must contain at most ${MAX_VARIANTS} entries`);
  }
  if (!Array.isArray(value.fallbacks) || value.fallbacks.length > MAX_FALLBACKS) {
    throw contractError(`fallbacks must contain at most ${MAX_FALLBACKS} entries`);
  }

  const variants = value.variants
    .map((variant, index) => normalizeVariant(variant, index))
    .sort((left, right) => left.rank - right.rank);
  const fallbacks = Array.from(
    new Set(
      value.fallbacks.map((fallback, index) =>
        requiredString(fallback, `fallbacks[${index}]`),
      ),
    ),
  );
  const selectedAttempt = value.selected_preview_attempt_id;
  if (selectedAttempt !== null && typeof selectedAttempt !== "string") {
    throw contractError("selected_preview_attempt_id must be a string or null");
  }

  return {
    schema_version: PREVIEW_MANIFEST_SCHEMA_VERSION,
    status: status as PreviewStatus,
    version_id: versionId,
    selected_preview_attempt_id: selectedAttempt,
    variants,
    fallbacks,
    ...optionalStringField(value, "unavailable_reason"),
    ...optionalStringField(value, "download_url"),
    ...(typeof value.is_current_preview === "boolean"
      ? { is_current_preview: value.is_current_preview }
      : {}),
  };
}

function normalizeVariant(value: unknown, index: number): PreviewVariant {
  if (!isRecord(value)) {
    throw contractError(`variants[${index}] must be an object`);
  }
  const rank = value.rank;
  if (typeof rank !== "number" || !Number.isFinite(rank)) {
    throw contractError(`variants[${index}].rank must be finite`);
  }
  const variant: PreviewVariant = {
    kind: requiredString(value.kind, `variants[${index}].kind`),
    media_type: requiredString(
      value.media_type,
      `variants[${index}].media_type`,
    ).toLowerCase(),
    delivery: requiredString(value.delivery, `variants[${index}].delivery`),
    renderer_contract: requiredString(
      value.renderer_contract,
      `variants[${index}].renderer_contract`,
    ),
    rank,
  };

  for (const field of [
    "id",
    "content_url",
    "text",
    "filename",
    "title",
    "alt",
    "captions_url",
    "poster_url",
    "provider_id",
    "provider_version",
  ] as const) {
    Object.assign(variant, optionalStringField(value, field));
  }
  if (Object.hasOwn(value, "data")) variant.data = value.data;
  if (typeof value.truncated === "boolean") variant.truncated = value.truncated;
  if (Object.hasOwn(value, "codecs")) {
    if (!Array.isArray(value.codecs) || value.codecs.length > MAX_CODECS) {
      throw contractError(`variants[${index}].codecs must contain at most ${MAX_CODECS} entries`);
    }
    variant.codecs = Array.from(new Set(value.codecs.map((codec, codecIndex) => {
      const normalized = requiredString(
        codec,
        `variants[${index}].codecs[${codecIndex}]`,
      ).trim();
      if (normalized.length > MAX_CODEC_LENGTH) {
        throw contractError(
          `variants[${index}].codecs[${codecIndex}] must contain at most ${MAX_CODEC_LENGTH} characters`,
        );
      }
      return normalized;
    })));
  }
  for (const field of [
    "byte_size",
    "width",
    "height",
    "duration_seconds",
    "duration_ms",
    "page_count",
  ] as const) {
    const candidate = value[field];
    if (candidate === undefined || candidate === null) continue;
    if (typeof candidate !== "number" || !Number.isFinite(candidate) || candidate < 0) {
      throw contractError(`variants[${index}].${field} must be non-negative`);
    }
    variant[field] = candidate;
  }
  if (
    value.source === "generated" ||
    value.source === "semantic" ||
    value.source === "original"
  ) {
    variant.source = value.source;
  }
  return variant;
}

function optionalStringField<T extends string>(
  value: Record<string, unknown>,
  field: T,
): Partial<Record<T, string>> {
  const candidate = value[field];
  if (candidate === undefined || candidate === null) return {};
  if (typeof candidate !== "string" || candidate.trim() === "") {
    throw contractError(`${field} must be a non-empty string`);
  }
  return { [field]: candidate } as Partial<Record<T, string>>;
}

function requiredString(value: unknown, field: string) {
  if (typeof value !== "string" || value.trim() === "") {
    throw contractError(`${field} must be a non-empty string`);
  }
  return value;
}

function contractError(message: string) {
  return new PreviewManifestContractError(`Invalid PreviewManifest: ${message}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
