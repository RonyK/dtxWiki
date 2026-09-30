import type { components } from "../api/types.gen";

type GeneratedLibraryCapabilities =
  components["schemas"]["LibraryCapabilitiesPayload"];
type GeneratedLibraryFormatCapability =
  components["schemas"]["LibraryFormatCapabilityPayload"];
export type LibraryFormatCapability = Omit<
  GeneratedLibraryFormatCapability,
  "preview_kind" | "upload_limit_sources" | "upload_limiting_source"
> & {
  display_name: string;
  default_extensions: string[];
  extensions_overridden: boolean;
  search_aliases: string[];
  provider_ids: string[];
  policy_enabled: boolean;
  deployment_available: boolean;
  converter_available: boolean;
  admission_state: "ready" | "validator_unavailable" | "denied";
  storage_state: "available" | "available_after_validation" | "denied";
  conversion_state: "available" | "stored_unconverted" | "unavailable";
  preview_state: "ready" | "degraded" | "unavailable";
  render_state: "available" | "unsupported";
  reason: string | null;
  preview_kind: "markdown" | "pdf" | "image" | "audio" | "video" | "table" | "metadata";
  renderer_contract: string;
  max_upload_bytes: number;
  max_convertible_bytes: number | null;
  upload_limit_sources: Partial<
    Record<"workspace" | "family" | "format" | "instance", number>
  >;
  upload_limiting_source: "workspace" | "family" | "format" | "instance";
};
export type LibraryFormatFamilyOrigin = "preset" | "custom";
export type LibraryFormatFamilyPreset = {
  preset_id: string;
  revision: number;
  version_id: string;
  label: string;
  description: string;
  format_ids: string[];
  format_extensions: Record<string, string[]>;
  extensions: string[];
};
export type LibraryFormatFamilyPresetBaseline = {
  preset_id: string;
  preset_revision: number;
  format_ids: string[];
  format_extensions: Record<string, string[]>;
};
export type LibraryFormatFamilyCapability = {
  family_id: string;
  name: string;
  origin: LibraryFormatFamilyOrigin;
  preset_id: string | null;
  preset_revision: number | null;
  preset_baseline: LibraryFormatFamilyPresetBaseline | null;
  format_ids: string[];
  extensions: string[];
  max_upload_bytes: number | null;
  effective_max_upload_bytes: number;
  upload_limiting_source: "workspace" | "family" | "format" | "instance";
  uploadable_count: number;
  preview_ready_count: number;
  unavailable_count: number;
  blockers: string[];
};
export type LibraryFormatCapabilitySummary = {
  total: number;
  uploadable: number;
  preview_ready: number;
  download_only: number;
  unavailable: number;
};
export type LibraryCapabilities = Omit<
  GeneratedLibraryCapabilities,
  "formats" | "catalog_revision" | "policy_schema_version" | "profiles" |
  "current_profile" | "family_presets" | "families" | "summary"
> & {
  catalog_revision?: string;
  policy_schema_version?: 5;
  capability_revision: string;
  family_presets?: LibraryFormatFamilyPreset[];
  families?: LibraryFormatFamilyCapability[];
  summary?: LibraryFormatCapabilitySummary;
  formats: LibraryFormatCapability[];
};

const CATEGORIES = new Set<LibraryFormatCapability["category"]>([
  "text",
  "document",
  "image",
  "audio",
  "video",
  "data",
]);
const CONVERSION_STATES = new Set<LibraryFormatCapability["conversion"]>([
  "available",
  "stored_unconverted",
  "denied",
]);
const PREVIEW_KINDS = new Set<LibraryFormatCapability["preview_kind"]>([
  "markdown",
  "pdf",
  "image",
  "audio",
  "video",
  "table",
  "metadata",
]);
const ADMISSION_STATES = new Set<LibraryFormatCapability["admission_state"]>([
  "ready",
  "validator_unavailable",
  "denied",
]);
const STORAGE_STATES = new Set<LibraryFormatCapability["storage_state"]>([
  "available",
  "available_after_validation",
  "denied",
]);
const CAPABILITY_CONVERSION_STATES = new Set<LibraryFormatCapability["conversion_state"]>([
  "available",
  "stored_unconverted",
  "unavailable",
]);
const PREVIEW_STATES = new Set<LibraryFormatCapability["preview_state"]>([
  "ready",
  "degraded",
  "unavailable",
]);
const RENDER_STATES = new Set<LibraryFormatCapability["render_state"]>([
  "available",
  "unsupported",
]);
export function normalizeLibraryCapabilities(
  value: unknown,
  fallback: LibraryCapabilities,
): LibraryCapabilities {
  if (!isRecord(value)) return cloneCapabilities(fallback);
  const policyRevision = nonEmptyString(value.policy_revision);
  const capabilityRevision =
    nonEmptyString(value.capability_revision) ?? policyRevision;
  const maxFilesPerBatch = positiveInteger(value.max_files_per_batch);
  if (
    !policyRevision ||
    maxFilesPerBatch === null ||
    !Array.isArray(value.formats)
  ) {
    return cloneCapabilities(fallback);
  }

  const formats: LibraryFormatCapability[] = [];
  const formatIds = new Set<string>();
  for (const candidate of value.formats) {
    const format = normalizeFormat(candidate);
    if (!format || formatIds.has(format.format_id)) {
      return cloneCapabilities(fallback);
    }
    formatIds.add(format.format_id);
    formats.push(format);
  }
  return {
    catalog_revision: nonEmptyString(value.catalog_revision) ?? capabilityRevision ?? policyRevision,
    policy_revision: policyRevision,
    capability_revision: capabilityRevision ?? policyRevision,
    policy_schema_version: 5,
    max_files_per_batch: maxFilesPerBatch,
    family_presets: normalizeFamilyPresets(value.family_presets, formats),
    families: normalizeFamilies(value.families, formats),
    summary: normalizeSummary(value.summary, formats),
    formats,
  };
}

function normalizeFormat(value: unknown): LibraryFormatCapability | null {
  if (!isRecord(value)) return null;
  const formatId = nonEmptyString(value.format_id);
  const category = enumValue(value.category, CATEGORIES);
  const conversion = enumValue(value.conversion, CONVERSION_STATES);
  const previewKind = enumValue(value.preview_kind, PREVIEW_KINDS);
  const maxBytes = positiveInteger(value.max_bytes);
  const maxUploadBytes = positiveInteger(value.max_upload_bytes) ?? maxBytes;
  const maxConvertibleBytes = value.max_convertible_bytes === null
    ? null
    : positiveInteger(value.max_convertible_bytes);
  const extensions = stringList(value.extensions, normalizeExtension);
  const defaultExtensions = stringList(value.default_extensions, normalizeExtension);
  const mediaTypes = stringList(value.media_types, normalizeMediaType);
  if (
    !formatId ||
    !category ||
    !conversion ||
    !previewKind ||
    maxBytes === null ||
    maxUploadBytes === null ||
    extensions === null ||
    mediaTypes === null ||
    typeof value.upload !== "boolean"
  ) {
    return null;
  }
  const normalizedDefaults = defaultExtensions ?? extensions;
  return {
    format_id: formatId,
    display_name: nonEmptyString(value.display_name) ?? formatId.toUpperCase(),
    category,
    extensions,
    default_extensions: normalizedDefaults,
    extensions_overridden: typeof value.extensions_overridden === "boolean"
      ? value.extensions_overridden
      : !sameStringSet(extensions, normalizedDefaults),
    media_types: mediaTypes,
    search_aliases: stringList(value.search_aliases, (entry) => entry.trim()) ?? [
      formatId,
      ...extensions.map((entry) => entry.replace(/^\./, "")),
      ...mediaTypes,
    ],
    provider_ids: stringList(value.provider_ids, (entry) => entry.trim()) ?? [],
    upload: value.upload,
    policy_enabled: typeof value.policy_enabled === "boolean"
      ? value.policy_enabled
      : value.upload,
    deployment_available: typeof value.deployment_available === "boolean"
      ? value.deployment_available
      : value.upload,
    converter_available: typeof value.converter_available === "boolean"
      ? value.converter_available
      : conversion === "available",
    admission_state: enumValue(value.admission_state, ADMISSION_STATES) ??
      (value.deployment_available === false ? "denied" : "ready"),
    storage_state: enumValue(value.storage_state, STORAGE_STATES) ??
      (value.upload ? "available" : "denied"),
    conversion_state: enumValue(value.conversion_state, CAPABILITY_CONVERSION_STATES) ??
      (conversion === "denied" ? "unavailable" : conversion),
    preview_state: enumValue(value.preview_state, PREVIEW_STATES) ??
      (conversion === "available" && (previewKind === "markdown" || previewKind === "table")
        ? "ready"
        : "unavailable"),
    render_state: enumValue(value.render_state, RENDER_STATES) ?? "available",
    reason: nonEmptyString(value.reason),
    conversion,
    preview_kind: previewKind,
    renderer_contract: nonEmptyString(value.renderer_contract) ?? `core.${previewKind}.v1`,
    max_bytes: maxBytes,
    max_upload_bytes: maxUploadBytes,
    max_convertible_bytes: maxConvertibleBytes,
    upload_limit_sources: normalizeLimitSources(value.upload_limit_sources, maxUploadBytes),
    upload_limiting_source: limitSource(value.upload_limiting_source),
  };
}

function normalizeLimitSources(value: unknown, fallback: number) {
  if (!isRecord(value)) return { workspace: fallback };
  const result: LibraryFormatCapability["upload_limit_sources"] = {};
  for (const key of ["workspace", "family", "format", "instance"] as const) {
    const limit = positiveInteger(value[key]);
    if (limit !== null) result[key] = limit;
  }
  return Object.keys(result).length ? result : { workspace: fallback };
}

function limitSource(value: unknown): LibraryFormatCapability["upload_limiting_source"] {
  return value === "family" || value === "format" || value === "instance"
    ? value
    : "workspace";
}

function cloneCapabilities(value: LibraryCapabilities): LibraryCapabilities {
  return {
    ...value,
    family_presets: value.family_presets?.map((preset) => ({
      ...preset,
      format_ids: [...preset.format_ids],
      format_extensions: cloneFormatExtensions(preset.format_extensions),
      extensions: [...preset.extensions],
    })),
    families: value.families?.map((family) => ({
      ...family,
      preset_baseline: family.preset_baseline ? {
        ...family.preset_baseline,
        format_ids: [...family.preset_baseline.format_ids],
        format_extensions: cloneFormatExtensions(
          family.preset_baseline.format_extensions,
        ),
      } : null,
      format_ids: [...family.format_ids],
      extensions: [...family.extensions],
      blockers: [...family.blockers],
    })),
    summary: value.summary ? { ...value.summary } : undefined,
    formats: value.formats.map((format) => ({
      ...format,
      extensions: [...format.extensions],
      default_extensions: [...format.default_extensions],
      media_types: [...format.media_types],
      search_aliases: [...format.search_aliases],
      provider_ids: [...format.provider_ids],
    })),
  };
}

function normalizeFamilies(
  value: unknown,
  formats: LibraryFormatCapability[],
): LibraryFormatFamilyCapability[] {
  if (!Array.isArray(value)) return [];
  const byFormatId = new Map(formats.map((format) => [format.format_id, format]));
  const result: LibraryFormatFamilyCapability[] = [];
  const seen = new Set<string>();
  for (const candidate of value) {
    if (!isRecord(candidate)) continue;
    const familyId = nonEmptyString(candidate.family_id);
    const name = nonEmptyString(candidate.name);
    const formatIds = stringList(candidate.format_ids, (entry) => entry.trim());
    if (!familyId || !name || !formatIds || formatIds.length === 0 || seen.has(familyId)) continue;
    const selected = formatIds.map((formatId) => byFormatId.get(formatId)).filter(
      (format): format is LibraryFormatCapability => Boolean(format),
    );
    const extensions = stringList(candidate.extensions, normalizeExtension)
      ?? Array.from(new Set(selected.flatMap((format) => format.extensions)));
    const configuredLimit = candidate.max_upload_bytes === null
      ? null
      : positiveInteger(candidate.max_upload_bytes);
    if (candidate.max_upload_bytes !== null && configuredLimit === null) continue;
    const effectiveLimit = positiveInteger(candidate.effective_max_upload_bytes)
      ?? Math.min(...selected.map((format) => format.max_upload_bytes), 50_000_000);
    const blockers = stringList(candidate.blockers, (entry) => entry.trim()) ?? [];
    seen.add(familyId);
    result.push({
      family_id: familyId,
      name,
      origin: familyOrigin(candidate.origin),
      preset_id: nonEmptyString(candidate.preset_id),
      preset_revision: positiveInteger(candidate.preset_revision),
      preset_baseline: normalizePresetBaseline(candidate.preset_baseline),
      format_ids: Array.from(new Set(formatIds)),
      extensions,
      max_upload_bytes: configuredLimit,
      effective_max_upload_bytes: effectiveLimit,
      upload_limiting_source: familyLimitSource(candidate.upload_limiting_source),
      uploadable_count: nonNegativeInteger(candidate.uploadable_count)
        ?? selected.filter((format) => format.upload).length,
      preview_ready_count: nonNegativeInteger(candidate.preview_ready_count)
        ?? selected.filter((format) => format.preview_state === "ready").length,
      unavailable_count: nonNegativeInteger(candidate.unavailable_count)
        ?? selected.filter((format) => !format.upload).length,
      blockers,
    });
  }
  return result;
}

function normalizeFamilyPresets(
  value: unknown,
  formats: LibraryFormatCapability[],
): LibraryFormatFamilyPreset[] {
  if (!Array.isArray(value)) return [];
  const byFormatId = new Map(formats.map((format) => [format.format_id, format]));
  const presets: LibraryFormatFamilyPreset[] = [];
  const seen = new Set<string>();
  for (const candidate of value) {
    if (!isRecord(candidate)) continue;
    const presetId = nonEmptyString(candidate.preset_id);
    const revision = positiveInteger(candidate.revision ?? candidate.preset_revision);
    const label = nonEmptyString(candidate.label) ?? nonEmptyString(candidate.name);
    const description = nonEmptyString(candidate.description) ?? "";
    const formatIds = stringList(candidate.format_ids, (entry) => entry.trim());
    if (
      !presetId || revision === null || !label || !formatIds ||
      formatIds.length === 0 || seen.has(presetId)
    ) continue;
    const extensions = stringList(candidate.extensions, normalizeExtension)
      ?? Array.from(new Set(formatIds.flatMap(
        (formatId) => byFormatId.get(formatId)?.extensions ?? [],
      )));
    const formatExtensions = normalizeFormatExtensions(
      candidate.format_extensions,
      formatIds,
      byFormatId,
    );
    seen.add(presetId);
    presets.push({
      preset_id: presetId,
      revision,
      version_id: nonEmptyString(candidate.version_id) ?? `${presetId}-v${revision}`,
      label,
      description,
      format_ids: Array.from(new Set(formatIds)),
      format_extensions: formatExtensions,
      extensions,
    });
  }
  return presets;
}

function normalizePresetBaseline(
  value: unknown,
): LibraryFormatFamilyPresetBaseline | null {
  if (!isRecord(value)) return null;
  const presetId = nonEmptyString(value.preset_id);
  const presetRevision = positiveInteger(value.preset_revision);
  const formatIds = stringList(value.format_ids, (entry) => entry.trim());
  const formatExtensions = normalizeFormatExtensions(
    value.format_extensions,
    formatIds ?? [],
  );
  return presetId && presetRevision && formatIds && formatIds.length > 0
    ? {
        preset_id: presetId,
        preset_revision: presetRevision,
        format_ids: Array.from(new Set(formatIds)),
        format_extensions: formatExtensions,
      }
    : null;
}

function normalizeFormatExtensions(
  value: unknown,
  formatIds: string[],
  formatsById?: Map<string, LibraryFormatCapability>,
) {
  const record = isRecord(value) ? value : {};
  return Object.fromEntries(formatIds.flatMap((formatId) => {
    const configured = stringList(record[formatId], normalizeExtension);
    const extensions = configured?.length
      ? configured
      : formatsById?.get(formatId)?.default_extensions ?? [];
    return extensions.length > 0 ? [[formatId, [...extensions]]] : [];
  }));
}

function cloneFormatExtensions(value: Record<string, string[]>) {
  return Object.fromEntries(
    Object.entries(value).map(([formatId, extensions]) => [
      formatId,
      [...extensions],
    ]),
  );
}

function sameStringSet(left: string[], right: string[]) {
  if (left.length !== right.length) return false;
  const expected = new Set(right);
  return left.every((value) => expected.has(value));
}

function normalizeSummary(
  value: unknown,
  formats: LibraryFormatCapability[],
): LibraryFormatCapabilitySummary {
  if (isRecord(value)) {
    const total = nonNegativeInteger(value.total);
    const uploadable = nonNegativeInteger(value.uploadable);
    const previewReady = nonNegativeInteger(value.preview_ready);
    const downloadOnly = nonNegativeInteger(value.download_only);
    const unavailable = nonNegativeInteger(value.unavailable);
    if (total !== null && uploadable !== null && previewReady !== null && downloadOnly !== null && unavailable !== null) {
      return { total, uploadable, preview_ready: previewReady, download_only: downloadOnly, unavailable };
    }
  }
  return {
    total: formats.length,
    uploadable: formats.filter((format) => format.upload).length,
    preview_ready: formats.filter((format) => format.preview_state === "ready").length,
    download_only: formats.filter((format) => format.upload && format.preview_state !== "ready").length,
    unavailable: formats.filter((format) => format.policy_enabled && !format.upload).length,
  };
}

function familyLimitSource(value: unknown): LibraryFormatFamilyCapability["upload_limiting_source"] {
  return value === "family" || value === "format" || value === "instance"
    ? value
    : "workspace";
}

function familyOrigin(value: unknown): LibraryFormatFamilyOrigin {
  return value === "preset" ? value : "custom";
}

function stringList(
  value: unknown,
  normalize: (entry: string) => string,
): string[] | null {
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) {
    return null;
  }
  const normalized = value.map(normalize);
  if (normalized.some((entry) => entry === "")) return null;
  return Array.from(new Set(normalized));
}

function normalizeExtension(value: string) {
  const normalized = value.trim().toLowerCase();
  if (!normalized) return "";
  return normalized.startsWith(".") ? normalized : `.${normalized}`;
}

function normalizeMediaType(value: string) {
  return value.trim().toLowerCase();
}

function positiveInteger(value: unknown) {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value > 0
    ? value
    : null;
}

function nonNegativeInteger(value: unknown) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

function nonEmptyString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function enumValue<T extends string>(value: unknown, allowed: Set<T>): T | null {
  return typeof value === "string" && allowed.has(value as T)
    ? (value as T)
    : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
