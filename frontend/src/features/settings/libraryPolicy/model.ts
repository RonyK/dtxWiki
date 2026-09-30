import type {
  LibraryFormatCapability,
  LibraryFormatFamilyCapability,
  LibraryFormatFamilyPreset,
} from "../../../shared/media/libraryFormatCapabilities";

export const MAX_LIBRARY_FORMAT_FAMILIES = 6;
export const MAX_LIBRARY_FAMILY_NAME_LENGTH = 80;
export const MIN_LIBRARY_FAMILY_BYTES = 1_000;
export const MAX_LIBRARY_POLICY_OVERRIDE_BYTES = 4_294_967_296;
export const MAX_LIBRARY_FAMILY_BYTES = MAX_LIBRARY_POLICY_OVERRIDE_BYTES;
export const MAX_LIBRARY_CUSTOM_FORMATS = 64;
export const MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS = 16;
export const MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH = 80;
export const MAX_LIBRARY_ALIAS_GROUP_RAW_LENGTH = 8_192;
export const MAX_LIBRARY_EXTENSION_LENGTH = 33;

const CUSTOM_FORMAT_EXTENSION_PATTERN = /^\.[a-z0-9][a-z0-9_+-]{0,31}$/;
const CUSTOM_FORMAT_NAME_CONTROL_PATTERN = /[\u0000-\u001f\u007f-\u009f]/;
const UNSAFE_LIBRARY_FORMAT_EXTENSIONS = new Set([
  ".app", ".apk", ".bat", ".cab", ".class", ".cmd", ".com", ".cpl",
  ".desktop", ".dll", ".dmg", ".exe", ".hta", ".ipa", ".iso", ".jar",
  ".lnk", ".msi", ".msix", ".reg", ".scr", ".url", ".war",
  ".bash", ".cjs", ".fish", ".htm", ".html", ".js", ".jse", ".mjs",
  ".php", ".pl", ".ps1", ".psm1", ".py", ".pyw", ".rb", ".sh",
  ".svg", ".vbe", ".vbs", ".wsf", ".wsh", ".xhtml", ".zsh",
  ".7z", ".ar", ".bz2", ".cpio", ".gz", ".lz4", ".rar", ".tar",
  ".tgz", ".xz", ".zip", ".zst", ".zstd",
  ".docm", ".dotm", ".potm", ".ppam", ".ppsm", ".pptm", ".xlam",
  ".xlsb", ".xlsm", ".xltm",
]);

export type LibraryPolicyBuiltinFormatOverrides = Record<
  string,
  { extensions: string[] }
>;

export type LibraryPolicyPresetBaseline = {
  preset_id: string;
  preset_revision: number;
  format_ids: string[];
  format_extensions: Record<string, string[]>;
};

export type LibraryPolicyFormatFamily = {
  family_id: string;
  name: string;
  origin: "preset" | "custom";
  preset_id: string | null;
  preset_revision: number | null;
  preset_baseline: LibraryPolicyPresetBaseline | null;
  format_ids: string[];
  max_upload_bytes: number | null;
};

export function parsePolicyBuiltinFormatOverrides(
  value: unknown,
): LibraryPolicyBuiltinFormatOverrides {
  if (!isRecord(value)) return {};
  const result: LibraryPolicyBuiltinFormatOverrides = {};
  for (const [formatId, candidate] of Object.entries(value)) {
    if (!formatId.trim() || formatId.startsWith("custom-") || !isRecord(candidate)) {
      continue;
    }
    const extensions = normalizeCustomFormatExtensions(candidate.extensions);
    if (
      extensions.length === 0 ||
      extensions.length > MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS ||
      invalidCustomFormatExtensions(extensions).length > 0 ||
      unsafeLibraryFormatExtensions(extensions).length > 0
    ) {
      continue;
    }
    result[formatId] = { extensions };
  }
  return result;
}

export function serializePolicyBuiltinFormatOverrides(
  overrides: LibraryPolicyBuiltinFormatOverrides,
) {
  return Object.fromEntries(
    Object.entries(overrides).map(([formatId, override]) => [
      formatId,
      { extensions: [...override.extensions] },
    ]),
  );
}

export type LibraryFamilyConflict = {
  familyId: string;
  familyName: string;
  formatId: string;
};

export type LibraryPolicyCustomFormat = {
  format_id: string;
  display_name: string;
  extensions: string[];
};

export type LibraryFamilyFormatOption = Pick<
  LibraryFormatCapability,
  "display_name" | "extensions" | "format_id" | "search_aliases"
> & {
  upload_limit_sources?: LibraryFormatCapability["upload_limit_sources"];
  workspace_custom?: boolean;
};

export type EffectiveLibraryFormatLimit = {
  bytes: number;
  source: LibraryFormatCapability["upload_limiting_source"];
};

export function parsePolicyFormatFamilies(value: unknown): LibraryPolicyFormatFamily[] {
  if (!Array.isArray(value)) return [];
  const result: LibraryPolicyFormatFamily[] = [];
  const seen = new Set<string>();
  for (const candidate of value) {
    if (!isRecord(candidate)) continue;
    const familyId = nonEmptyString(candidate.family_id);
    const name = nonEmptyString(candidate.name);
    const formatIds = stringList(candidate.format_ids);
    const rawLimit = candidate.max_upload_bytes;
    const maxUploadBytes = rawLimit === null || rawLimit === undefined
      ? null
      : positiveInteger(rawLimit);
    if (
      !familyId || !name || !formatIds || formatIds.length === 0 ||
      (rawLimit !== null && rawLimit !== undefined && maxUploadBytes === null) ||
      seen.has(familyId)
    ) {
      continue;
    }
    seen.add(familyId);
    const origin = familyOrigin(candidate.origin ?? candidate.source);
    const presetId = nonEmptyString(candidate.preset_id);
    const presetRevision = positiveInteger(candidate.preset_revision);
    const presetBaseline = parsePresetBaseline(candidate.preset_baseline) ?? (
      origin === "preset" && presetId && presetRevision
        ? {
            preset_id: presetId,
            preset_revision: presetRevision,
            format_ids: [...formatIds],
            format_extensions: {},
          }
        : null
    );
    result.push({
      family_id: familyId,
      name,
      origin,
      preset_id: presetId,
      preset_revision: presetRevision,
      preset_baseline: origin === "preset" ? presetBaseline : null,
      format_ids: formatIds,
      max_upload_bytes: maxUploadBytes,
    });
  }
  return result;
}

export function serializePolicyFormatFamilies(families: LibraryPolicyFormatFamily[]) {
  return families.map((family) => ({
    family_id: family.family_id,
    name: family.name.trim(),
    origin: family.origin,
    preset_id: family.preset_id,
    preset_revision: family.preset_revision,
    preset_baseline: family.preset_baseline ? {
      ...family.preset_baseline,
      format_ids: [...family.preset_baseline.format_ids],
      format_extensions: cloneFormatExtensions(
        family.preset_baseline.format_extensions,
      ),
    } : null,
    format_ids: [...family.format_ids],
    max_upload_bytes: family.max_upload_bytes,
  }));
}

export function parsePolicyCustomFormats(value: unknown): LibraryPolicyCustomFormat[] {
  if (!Array.isArray(value)) return [];
  const result: LibraryPolicyCustomFormat[] = [];
  const seenIds = new Set<string>();
  for (const candidate of value) {
    if (!isRecord(candidate)) continue;
    const formatId = nonEmptyString(candidate.format_id)?.toLocaleLowerCase() ?? null;
    const displayName = normalizeCustomFormatName(candidate.display_name);
    const extensions = normalizeCustomFormatExtensions(candidate.extensions);
    if (
      !formatId || !/^custom-[a-z0-9][a-z0-9_-]{0,55}$/.test(formatId) ||
      !displayName || !validCustomFormatName(displayName) ||
      displayName.length > MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH ||
      extensions.length === 0 ||
      extensions.length > MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS ||
      invalidCustomFormatExtensions(extensions).length > 0 ||
      unsafeLibraryFormatExtensions(extensions).length > 0 ||
      seenIds.has(formatId)
    ) continue;
    seenIds.add(formatId);
    result.push({ format_id: formatId, display_name: displayName, extensions });
  }
  return result;
}

export function serializePolicyCustomFormats(formats: LibraryPolicyCustomFormat[]) {
  return formats.map((format) => ({
    format_id: format.format_id,
    display_name: normalizeCustomFormatName(format.display_name),
    extensions: normalizeCustomFormatExtensions(format.extensions),
  }));
}

export function customFormatOption(
  format: LibraryPolicyCustomFormat,
): LibraryFamilyFormatOption {
  return {
    ...format,
    search_aliases: [
      format.display_name,
      ...format.extensions.map((extension) => extension.slice(1)),
    ],
    workspace_custom: true,
  };
}

export function configuredFamilyProjection(
  family: LibraryPolicyFormatFamily,
  projections: LibraryFormatFamilyCapability[],
) {
  return projections.find((candidate) => candidate.family_id === family.family_id) ?? null;
}

export function familyFormats(
  family: Pick<LibraryPolicyFormatFamily, "format_ids">,
  formats: LibraryFamilyFormatOption[],
) {
  const byId = new Map(formats.map((format) => [format.format_id, format]));
  return family.format_ids.flatMap((formatId) => {
    const format = byId.get(formatId);
    return format ? [format] : [];
  });
}

export function familyExtensions(
  family: Pick<LibraryPolicyFormatFamily, "format_ids">,
  formats: LibraryFamilyFormatOption[],
) {
  return Array.from(new Set(
    familyFormats(family, formats).flatMap((format) => format.extensions),
  ));
}

export function familyConflict(
  formatId: string,
  families: LibraryPolicyFormatFamily[],
  currentFamilyId: string | null,
): LibraryFamilyConflict | null {
  const conflict = families.find(
    (family) => family.family_id !== currentFamilyId && family.format_ids.includes(formatId),
  );
  return conflict
    ? { familyId: conflict.family_id, familyName: conflict.name, formatId }
    : null;
}

export function duplicateFamilyName(
  name: string,
  families: LibraryPolicyFormatFamily[],
  currentFamilyId: string | null,
) {
  const normalized = name.trim().toLocaleLowerCase();
  if (!normalized) return null;
  return families.find((family) =>
    family.family_id !== currentFamilyId &&
    family.name.trim().toLocaleLowerCase() === normalized
  ) ?? null;
}

export function normalizeExtensionInput(value: string) {
  const normalized = value.trim().toLocaleLowerCase();
  if (!normalized) return "";
  return normalized.startsWith(".") ? normalized : `.${normalized}`;
}

export function formatForExtension(
  value: string,
  formats: LibraryFamilyFormatOption[],
) {
  const extension = normalizeExtensionInput(value);
  if (!extension) return null;
  return formats.find((format) => format.extensions.some(
    (candidate) => normalizeExtensionInput(candidate) === extension,
  )) ?? null;
}

export function matchingFamilyFormats(
  query: string,
  formats: LibraryFamilyFormatOption[],
  selectedFormatIds: string[],
  limit = 8,
) {
  const normalized = query.trim().toLocaleLowerCase();
  const selected = new Set(selectedFormatIds);
  return formats.filter((format) => {
    if (selected.has(format.format_id)) return false;
    if (!normalized) return true;
    return [
      format.display_name,
      format.format_id,
      ...format.extensions,
      ...format.search_aliases,
    ].some((entry) => entry.toLocaleLowerCase().includes(normalized));
  }).slice(0, limit);
}

export function normalizeCustomFormatName(value: unknown) {
  return typeof value === "string" ? value.trim().replace(/\s+/g, " ") : "";
}

export function validCustomFormatName(value: unknown) {
  return typeof value === "string" && !CUSTOM_FORMAT_NAME_CONTROL_PATTERN.test(value);
}

export function normalizeCustomFormatExtensions(value: unknown): string[] {
  const values = Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string")
    : typeof value === "string" ? value.split(/[\s,]+/) : [];
  return Array.from(new Set(values.map(normalizeExtensionInput).filter(Boolean)));
}

export function invalidCustomFormatExtensions(extensions: string[]) {
  return extensions.filter((extension) => !CUSTOM_FORMAT_EXTENSION_PATTERN.test(extension));
}

export function unsafeLibraryFormatExtensions(extensions: string[]) {
  return extensions.filter((extension) =>
    UNSAFE_LIBRARY_FORMAT_EXTENSIONS.has(normalizeExtensionInput(extension))
  );
}

export function extensionOwner(
  extension: string,
  formats: LibraryFamilyFormatOption[],
) {
  return formatForExtension(extension, formats);
}

export function createCustomFormat(
  displayName: string,
  extensions: string[],
): LibraryPolicyCustomFormat {
  const randomUuid = globalThis.crypto?.randomUUID?.()
    ?.toLocaleLowerCase()
    .replace(/[^a-z0-9_-]/g, "-");
  const suffix = randomUuid || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return {
    format_id: `custom-${suffix}`.slice(0, 63),
    display_name: normalizeCustomFormatName(displayName),
    extensions: normalizeCustomFormatExtensions(extensions),
  };
}

export function familyFromPreset(preset: LibraryFormatFamilyPreset) {
  return {
    family_id: createFamilyId(),
    name: preset.label,
    origin: "preset" as const,
    preset_id: preset.preset_id,
    preset_revision: preset.revision,
    preset_baseline: {
      preset_id: preset.preset_id,
      preset_revision: preset.revision,
      format_ids: [...preset.format_ids],
      format_extensions: cloneFormatExtensions(preset.format_extensions),
    },
    format_ids: [...preset.format_ids],
    max_upload_bytes: null,
  };
}

export function effectiveFormatUploadLimit(
  format: LibraryFormatCapability,
  {
    familyMaxBytes,
    formatMaxBytes,
    workspaceMaxBytes,
  }: {
    familyMaxBytes: number | null;
    formatMaxBytes: number | null;
    workspaceMaxBytes: number;
  },
): EffectiveLibraryFormatLimit {
  const configured: EffectiveLibraryFormatLimit = formatMaxBytes !== null
    ? { bytes: formatMaxBytes, source: "format" }
    : familyMaxBytes !== null
      ? { bytes: familyMaxBytes, source: "family" }
      : { bytes: workspaceMaxBytes, source: "workspace" };
  const instance = format.upload_limit_sources.instance;
  return instance !== undefined && instance < configured.bytes
    ? { bytes: instance, source: "instance" }
    : configured;
}

export function emptyCustomFamily(): LibraryPolicyFormatFamily {
  return {
    family_id: createFamilyId(),
    name: "",
    origin: "custom",
    preset_id: null,
    preset_revision: null,
    preset_baseline: null,
    format_ids: [],
    max_upload_bytes: null,
  };
}

function createFamilyId() {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  return randomUuid ? `family-${randomUuid}` : `family-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function stringList(value: unknown) {
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string")) return null;
  const normalized = value.map((entry) => entry.trim()).filter(Boolean);
  return Array.from(new Set(normalized));
}

function nonEmptyString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function positiveInteger(value: unknown) {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0
    ? value
    : null;
}

function familyOrigin(value: unknown): LibraryPolicyFormatFamily["origin"] {
  return value === "preset" ? value : "custom";
}

function parsePresetBaseline(value: unknown): LibraryPolicyPresetBaseline | null {
  if (!isRecord(value)) return null;
  const presetId = nonEmptyString(value.preset_id);
  const presetRevision = positiveInteger(value.preset_revision);
  const formatIds = stringList(value.format_ids);
  const formatExtensions = normalizeFormatExtensions(
    value.format_extensions,
    formatIds ?? [],
  );
  return presetId && presetRevision && formatIds && formatIds.length > 0
    ? {
        preset_id: presetId,
        preset_revision: presetRevision,
        format_ids: formatIds,
        format_extensions: formatExtensions,
      }
    : null;
}

function normalizeFormatExtensions(
  value: unknown,
  formatIds: string[],
): Record<string, string[]> {
  if (!isRecord(value)) return {};
  const allowedIds = new Set(formatIds);
  return Object.fromEntries(
    Object.entries(value).flatMap(([formatId, rawExtensions]) => {
      if (!allowedIds.has(formatId)) return [];
      const extensions = normalizeCustomFormatExtensions(rawExtensions);
      return (
        extensions.length > 0 &&
        extensions.length <= MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS &&
        invalidCustomFormatExtensions(extensions).length === 0 &&
        unsafeLibraryFormatExtensions(extensions).length === 0
      )
        ? [[formatId, extensions]]
        : [];
    }),
  );
}

function cloneFormatExtensions(value: Record<string, string[]>) {
  return Object.fromEntries(
    Object.entries(value).map(([formatId, extensions]) => [
      formatId,
      [...extensions],
    ]),
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
