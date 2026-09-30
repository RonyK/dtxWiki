import { X } from "lucide-react";
import { useEffect, useId, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import { ChipInput } from "../../../shared/components/ChipInput";
import {
  MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS,
  MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH,
  MAX_LIBRARY_EXTENSION_LENGTH,
  formatForExtension,
  invalidCustomFormatExtensions,
  normalizeCustomFormatExtensions,
  normalizeCustomFormatName,
  normalizeExtensionInput,
  unsafeLibraryFormatExtensions,
  validCustomFormatName,
  type LibraryFamilyFormatOption,
  type LibraryPolicyCustomFormat,
} from "./model";
import {
  stableCustomNameOwnerConflict,
  stableCustomOwnerConflict,
  type StableCustomOwners,
} from "./customFormatOwnership";

type FormatAliasGroupListProps = {
  baselineFormats: LibraryFamilyFormatOption[];
  busy: boolean;
  customFormats: LibraryPolicyCustomFormat[];
  formats: LibraryFamilyFormatOption[];
  onRemoveFormat: (formatId: string) => void;
  onInteractionStateChange: (formatId: string, blocked: boolean) => void;
  onUpdateBuiltinFormat: (formatId: string, extensions: string[]) => void;
  onUpdateCustomFormat: (format: LibraryPolicyCustomFormat) => void;
  presetFormatIds: string[];
  selectedFormats: LibraryFamilyFormatOption[];
  stableCustomOwners: StableCustomOwners;
};

export function FormatAliasGroupList({
  baselineFormats,
  busy,
  customFormats,
  formats,
  onRemoveFormat,
  onInteractionStateChange,
  onUpdateBuiltinFormat,
  onUpdateCustomFormat,
  presetFormatIds,
  selectedFormats,
  stableCustomOwners,
}: FormatAliasGroupListProps) {
  const { t } = useTranslation("settings");
  const presetIds = new Set(presetFormatIds);

  if (selectedFormats.length === 0) {
    return (
      <p className="empty-state library-family-alias-groups-empty">
        {t("policy.libraryUpload.families.noFormats", {
          defaultValue: "Use Add formats to choose a registered format or register a custom format.",
        })}
      </p>
    );
  }

  return (
    <div
      aria-label={t("policy.libraryUpload.families.aliasGroupsLabel", {
        defaultValue: "Format alias groups",
      })}
      className="library-family-alias-groups"
      role="list"
    >
      {selectedFormats.map((format) => {
        const customFormat = format.workspace_custom
          ? customFormats.find((candidate) => candidate.format_id === format.format_id) ?? null
          : null;
        return (
          <FormatAliasGroupCard
            baselineFormats={baselineFormats}
            busy={busy}
            customFormat={customFormat}
            customFormats={customFormats}
            format={format}
            formats={formats}
            key={format.format_id}
            onRemove={() => onRemoveFormat(format.format_id)}
            onInteractionStateChange={onInteractionStateChange}
            onUpdateBuiltin={(extensions) => onUpdateBuiltinFormat(
              format.format_id,
              extensions,
            )}
            onUpdateCustom={onUpdateCustomFormat}
            preset={presetIds.has(format.format_id)}
            stableCustomOwners={stableCustomOwners}
          />
        );
      })}
    </div>
  );
}

function FormatAliasGroupCard({
  baselineFormats,
  busy,
  customFormat,
  customFormats,
  format,
  formats,
  onRemove,
  onInteractionStateChange,
  onUpdateBuiltin,
  onUpdateCustom,
  preset,
  stableCustomOwners,
}: {
  baselineFormats: LibraryFamilyFormatOption[];
  busy: boolean;
  customFormat: LibraryPolicyCustomFormat | null;
  customFormats: LibraryPolicyCustomFormat[];
  format: LibraryFamilyFormatOption;
  formats: LibraryFamilyFormatOption[];
  onRemove: () => void;
  onInteractionStateChange: (formatId: string, blocked: boolean) => void;
  onUpdateBuiltin: (extensions: string[]) => void;
  onUpdateCustom: (format: LibraryPolicyCustomFormat) => void;
  preset: boolean;
  stableCustomOwners: StableCustomOwners;
}) {
  const { t } = useTranslation("settings");
  const extensionsHintId = useId();
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editingExtension, setEditingExtension] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const interactionBlocked = editingName !== null || editingExtension || Boolean(error);

  useEffect(() => {
    onInteractionStateChange(format.format_id, interactionBlocked);
    return () => onInteractionStateChange(format.format_id, false);
  }, [format.format_id, interactionBlocked, onInteractionStateChange]);

  function cancelNameEdit() {
    setEditingName(null);
    setError(null);
  }

  function commitName(value: string) {
    if (!customFormat) return;
    const label = normalizeCustomFormatName(value);
    if (
      !label || !validCustomFormatName(label) ||
      label.length > MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH
    ) {
      setError(t("policy.libraryUpload.families.customFormat.errors.invalidName", {
        defaultValue: "Use a name without control characters.",
      }));
      return;
    }
    const stableConflict = stableCustomNameOwnerConflict(
      label,
      customFormat.format_id,
      stableCustomOwners.displayNames,
    );
    if (stableConflict) {
      setError(t("policy.libraryUpload.families.customFormat.errors.stableNameOwnerTransfer", {
        defaultValue: "The name '{{name}}' belonged to {{format}} when these Settings were loaded. Save that format's rename or deletion first, then reopen Settings before assigning the name to another custom format.",
        name: label,
        format: stableConflict.displayName,
      }));
      return;
    }
    const duplicate = customFormats.find((candidate) =>
      candidate.format_id !== customFormat.format_id &&
      candidate.display_name.toLocaleLowerCase() === label.toLocaleLowerCase()
    );
    if (duplicate) {
      setError(t("policy.libraryUpload.families.customFormat.errors.nameConflict", {
        defaultValue: "A custom format named '{{format}}' already exists.",
        format: duplicate.display_name,
      }));
      return;
    }
    onUpdateCustom({ ...customFormat, display_name: label });
    cancelNameEdit();
  }

  function commitExtensions(values: string[]) {
    const extensions = normalizeCustomFormatExtensions(
      values.map((value) => normalizeExtensionInput(value) || value),
    );
    if (extensions.length === 0) {
      setError(t("policy.libraryUpload.families.aliasGroup.lastExtensionHint", {
        defaultValue: "An alias group must keep at least one extension.",
      }));
      return false;
    }
    if (extensions.length > MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS) {
      setError(t("policy.libraryUpload.families.customFormat.errors.tooManyExtensions", {
        defaultValue: "A format can have up to {{count}} extension aliases.",
        count: MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS,
      }));
      return false;
    }
    const invalid = invalidCustomFormatExtensions(extensions);
    if (invalid.length > 0) {
      setError(t("policy.libraryUpload.families.customFormat.errors.invalidExtension", {
        defaultValue: "Invalid extension: {{extensions}}",
        extensions: invalid.join(", "),
      }));
      return false;
    }
    const unsafe = unsafeLibraryFormatExtensions(extensions);
    if (unsafe.length > 0) {
      setError(t("policy.libraryUpload.families.customFormat.errors.unsafeExtension", {
        defaultValue: "Blocked extension: {{extensions}}. Executable, active-content, archive, and macro-enabled aliases cannot be registered.",
        extensions: unsafe.join(", "),
      }));
      return false;
    }

    const previousExtensions = new Set(
      format.extensions.map((extension) => normalizeExtensionInput(extension)),
    );
    const packageOwnedBaselineExtensions = customFormat
      ? new Set<string>()
      : new Set(
        baselineFormats
          .find((candidate) => candidate.format_id === format.format_id)
          ?.extensions.map((extension) => normalizeExtensionInput(extension)) ?? [],
    );
    for (const extension of extensions) {
      if (previousExtensions.has(extension)) continue;
      const packageOwnedAlias = packageOwnedBaselineExtensions.has(extension);
      const stableCustomConflict = packageOwnedAlias
        ? null
        : stableCustomOwnerConflict(
          extension,
          customFormat?.format_id ?? null,
          stableCustomOwners.extensions,
        );
      if (stableCustomConflict) {
        setError(t("policy.libraryUpload.families.customFormat.errors.stableOwnerTransfer", {
          defaultValue: "{{extension}} is reserved by {{format}}. Remove it there before assigning it here.",
          extension,
          format: stableCustomConflict.displayName,
        }));
        return false;
      }
      const stableOwner = packageOwnedAlias
        ? null
        : formatForExtension(
          extension,
          baselineFormats.filter((candidate) => candidate.format_id !== format.format_id),
        );
      if (stableOwner) {
        setError(t("policy.libraryUpload.families.aliasGroup.stableOwnerTransfer", {
          defaultValue: "{{extension}} belonged to {{format}} when this dialog opened. Save its removal first, then reopen Settings before assigning it here.",
          extension,
          format: stableOwner.display_name,
        }));
        return false;
      }
      const owner = packageOwnedAlias
        ? null
        : formatForExtension(
          extension,
          formats.filter((candidate) => candidate.format_id !== format.format_id),
        );
      if (owner) {
        setError(t("policy.libraryUpload.families.customFormat.errors.extensionConflict", {
          defaultValue: "{{extension}} already belongs to {{format}} ({{aliases}}).",
          extension,
          format: owner.display_name,
          aliases: owner.extensions.join(", "),
        }));
        return false;
      }
    }

    if (customFormat) {
      onUpdateCustom({ ...customFormat, extensions });
    } else {
      onUpdateBuiltin(extensions);
    }
    setError(null);
    return true;
  }

  function handleNameKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      commitName(event.currentTarget.value);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      cancelNameEdit();
    }
  }

  return (
    <article
      className="library-family-alias-group"
      id={`library-family-alias-group-${format.format_id}`}
      role="listitem"
    >
      <header className="library-family-alias-group-header">
        <div className="library-family-alias-group-title">
          {editingName !== null && customFormat ? (
            <input
              aria-label={t("policy.libraryUpload.families.aliasGroup.editNameLabel", {
                defaultValue: "Edit alias group name",
              })}
              autoFocus
              disabled={busy}
              maxLength={MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH}
              onBlur={(event) => commitName(event.currentTarget.value)}
              onChange={(event) => setEditingName(event.target.value)}
              onKeyDown={handleNameKeyDown}
              value={editingName}
            />
          ) : customFormat ? (
            <button
              aria-label={t("policy.libraryUpload.families.aliasGroup.editName", {
                defaultValue: "Edit alias group {{format}}",
                format: format.display_name,
              })}
              className="library-family-alias-title-button"
              disabled={busy}
              onClick={() => {
                setEditingName(format.display_name);
                setError(null);
              }}
              type="button"
            >
              {format.display_name}
            </button>
          ) : (
            <strong>{format.display_name}</strong>
          )}
          {preset ? (
            <span
              className="library-family-alias-preset"
              title={t("policy.libraryUpload.families.aliasGroup.presetHint", {
                defaultValue: "This alias group came from the selected family preset.",
              })}
            >
              {t("policy.libraryUpload.families.aliasGroup.preset", {
                defaultValue: "Preset",
              })}
            </span>
          ) : null}
        </div>
        <div className="library-family-alias-group-meta">
          <strong
            className={format.extensions.length === MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS
              ? "warning" : undefined}
            title={t("policy.libraryUpload.families.aliasGroup.extensionsLabelShort", {
              defaultValue: "Extensions",
            })}
          >
            {format.extensions.length} / {MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS}
          </strong>
          <button
            aria-label={t("policy.libraryUpload.families.removeFormatLabel", {
              defaultValue: "Remove {{format}} from this family",
              format: format.display_name,
            })}
            className="library-family-alias-remove"
            disabled={busy}
            onClick={onRemove}
            title={t("policy.libraryUpload.families.removeFormatLabel", {
              defaultValue: "Remove {{format}} from this family",
              format: format.display_name,
            })}
            type="button"
          >
            <X aria-hidden="true" size={18} />
          </button>
        </div>
      </header>

      <div className="library-family-alias-extensions">
        <span className="visually-hidden" id={extensionsHintId}>
          {t("policy.libraryUpload.families.aliasGroup.extensionsHint", {
            defaultValue: "Type an extension and press Enter or comma to add it.",
          })}
        </span>
        <ChipInput
          ariaDescribedBy={extensionsHintId}
          ariaLabel={t("policy.libraryUpload.families.aliasGroup.extensionsLabel", {
            defaultValue: "Extensions for {{format}}",
            format: format.display_name,
          })}
          disabled={busy}
          editableItems
          getEditAriaLabel={(extension) => t(
            "policy.libraryUpload.families.aliasGroup.editExtension",
            {
              defaultValue: "Edit extension {{extension}}",
              extension,
            },
          )}
          getRemoveAriaLabel={(extension) => t(
            "policy.libraryUpload.families.aliasGroup.removeExtension",
            {
              defaultValue: "Remove extension {{extension}}",
              extension,
            },
          )}
          maxItemLength={MAX_LIBRARY_EXTENSION_LENGTH}
          inputDisabled={format.extensions.length >= MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS}
          onChange={commitExtensions}
          onDuplicate={(extension) => setError(t(
            "policy.libraryUpload.families.aliasGroup.duplicateExtension",
            {
              defaultValue: "{{extension}} is already in this group.",
              extension,
            },
          ))}
          onEditingChange={(editing) => {
            setEditingExtension(editing);
            if (editing) setError(null);
          }}
          onItemTooLong={(extension) => setError(t(
            "policy.libraryUpload.families.customFormat.errors.extensionTooLong",
            {
              defaultValue: "{{extension}} is too long. Extensions can contain at most {{count}} characters including the leading dot.",
              count: MAX_LIBRARY_EXTENSION_LENGTH,
              extension,
            },
          ))}
          onQueryChange={() => setError(null)}
          persistentPlaceholder
          placeholder={t("policy.libraryUpload.families.aliasGroup.extensionsPlaceholder", {
            defaultValue: "+ Add extension",
          })}
          stripLeadingHash={false}
          truncateItems={false}
          value={format.extensions}
        />
      </div>
      {error ? (
        <p className="field-error library-family-alias-group-error" role="alert">
          {error}
        </p>
      ) : null}
    </article>
  );
}
