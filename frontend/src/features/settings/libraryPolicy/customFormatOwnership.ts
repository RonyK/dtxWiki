import {
  normalizeCustomFormatName,
  normalizeExtensionInput,
  type LibraryFamilyFormatOption,
  type LibraryPolicyCustomFormat,
} from "./model";

export type StableCustomOwner = {
  displayName: string;
  formatId: string;
};

export type StableCustomExtensionOwners = ReadonlyMap<string, StableCustomOwner>;
export type StableCustomDisplayNameOwners = ReadonlyMap<string, StableCustomOwner>;

export type StableCustomOwners = {
  displayNames: StableCustomDisplayNameOwners;
  extensions: StableCustomExtensionOwners;
};

export function snapshotStableCustomOwners(
  customFormats: readonly LibraryPolicyCustomFormat[],
): StableCustomOwners {
  const displayNames = new Map<string, StableCustomOwner>();
  const extensions = new Map<string, StableCustomOwner>();
  for (const format of customFormats) {
    const owner = {
      displayName: format.display_name,
      formatId: format.format_id,
    };
    const displayName = normalizeCustomDisplayNameOwnerKey(format.display_name);
    if (displayName && !displayNames.has(displayName)) {
      displayNames.set(displayName, owner);
    }
    for (const rawExtension of format.extensions) {
      const extension = normalizeExtensionInput(rawExtension);
      if (!extension || extensions.has(extension)) continue;
      extensions.set(extension, owner);
    }
  }
  return { displayNames, extensions };
}

export function withStableBuiltinExtensionOwners(
  owners: StableCustomOwners,
  formats: readonly LibraryFamilyFormatOption[],
): StableCustomOwners {
  const extensions = new Map(owners.extensions);
  for (const format of formats) {
    if (format.workspace_custom || format.format_id.startsWith("custom-")) continue;
    const owner = {
      displayName: format.display_name,
      formatId: format.format_id,
    };
    for (const rawExtension of format.extensions) {
      const extension = normalizeExtensionInput(rawExtension);
      if (!extension || extensions.has(extension)) continue;
      extensions.set(extension, owner);
    }
  }
  return { displayNames: owners.displayNames, extensions };
}

export function stableCustomOwnerConflict(
  extension: string,
  targetFormatId: string | null,
  owners: StableCustomExtensionOwners,
): StableCustomOwner | null {
  const owner = owners.get(normalizeExtensionInput(extension));
  return owner && owner.formatId !== targetFormatId ? owner : null;
}

export function stableCustomNameOwnerConflict(
  displayName: string,
  targetFormatId: string | null,
  owners: StableCustomDisplayNameOwners,
): StableCustomOwner | null {
  const owner = owners.get(normalizeCustomDisplayNameOwnerKey(displayName));
  return owner && owner.formatId !== targetFormatId ? owner : null;
}

function normalizeCustomDisplayNameOwnerKey(value: string) {
  return normalizeCustomFormatName(value).toLocaleLowerCase();
}
