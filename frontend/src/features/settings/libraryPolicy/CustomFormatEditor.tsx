import { codePointLength, inputTextError, textInputLimit } from "../../../shared/lib/inputPolicy";
import { useMemo, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import { ChipInput } from "../../../shared/components/ChipInput";
import { AdvancedSection } from "../../../shared/components/AdvancedSection";
import {
  MAX_LIBRARY_ALIAS_GROUP_RAW_LENGTH,
  MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS,
  MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH,
  MAX_LIBRARY_CUSTOM_FORMATS,
  MAX_LIBRARY_EXTENSION_LENGTH,
  createCustomFormat,
  extensionOwner,
  invalidCustomFormatExtensions,
  normalizeCustomFormatExtensions,
  normalizeCustomFormatName,
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

type CustomFormatEditorProps = {
  busy: boolean;
  customFormats: LibraryPolicyCustomFormat[];
  formats: LibraryFamilyFormatOption[];
  initialExtensions?: string;
  initialFormat?: LibraryPolicyCustomFormat | null;
  onCancel: () => void;
  onSave: (format: LibraryPolicyCustomFormat) => void;
  onBulkAdd?: (query: string) => void;
  onInputChange?: () => void;
  stableCustomOwners: StableCustomOwners;
  titleId?: string;
};

export function CustomFormatEditor({
  busy,
  customFormats,
  formats,
  initialExtensions = "",
  initialFormat = null,
  onCancel,
  onBulkAdd,
  onInputChange,
  onSave,
  stableCustomOwners,
  titleId,
}: CustomFormatEditorProps) {
  const { t } = useTranslation("settings");
  const [name, setName] = useState(initialFormat?.display_name ?? "");
  const [extensions, setExtensions] = useState(
    normalizeCustomFormatExtensions(
      initialFormat?.extensions ?? initialExtensions,
    ),
  );
  const [extensionInputError, setExtensionInputError] = useState<string | null>(null);
  const [rawSourceOpen, setRawSourceOpen] = useState(false);
  const [rawSource, setRawSource] = useState("");
  const hasRawSource = Boolean(rawSource.trim());
  const normalizedName = normalizeCustomFormatName(name);
  const malformedExtensions = invalidCustomFormatExtensions(extensions);
  const unsafeExtensions = unsafeLibraryFormatExtensions(extensions);
  const otherFormats = useMemo(
    () => formats.filter((format) => format.format_id !== initialFormat?.format_id),
    [formats, initialFormat?.format_id],
  );
  const extensionConflicts = extensions.flatMap((extension) => {
    if (stableCustomOwnerConflict(
      extension,
      initialFormat?.format_id ?? null,
      stableCustomOwners.extensions,
    )) return [];
    const owner = extensionOwner(extension, otherFormats);
    return owner ? [{ extension, owner }] : [];
  });
  const stableOwnerConflicts = extensions.flatMap((extension) => {
    const owner = stableCustomOwnerConflict(
      extension,
      initialFormat?.format_id ?? null,
      stableCustomOwners.extensions,
    );
    return owner ? [{ extension, owner }] : [];
  });
  const nameConflict = customFormats.find((format) =>
    format.format_id !== initialFormat?.format_id &&
    format.display_name.toLocaleLowerCase() === normalizedName.toLocaleLowerCase()
  );
  const stableNameOwnerConflict = stableCustomNameOwnerConflict(
    normalizedName,
    initialFormat?.format_id ?? null,
    stableCustomOwners.displayNames,
  );
  const atFormatLimit = !initialFormat && customFormats.length >= MAX_LIBRARY_CUSTOM_FORMATS;
  const invalid =
    !normalizedName || Boolean(inputTextError(name, Number.MAX_SAFE_INTEGER)) ||
    !validCustomFormatName(name) ||
    codePointLength(normalizedName) > MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH ||
    extensions.length === 0 ||
    extensions.length > MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS ||
    malformedExtensions.length > 0 ||
    unsafeExtensions.length > 0 ||
    Boolean(extensionInputError) ||
    extensionConflicts.length > 0 ||
    stableOwnerConflicts.length > 0 ||
    Boolean(stableNameOwnerConflict) ||
    Boolean(nameConflict) ||
    atFormatLimit;

  function save() {
    if (busy) return;
    if (hasRawSource && onBulkAdd) {
      onBulkAdd(rawSource);
      return;
    }
    if (invalid) return;
    onSave(initialFormat ? {
      ...initialFormat,
      display_name: normalizedName,
      extensions,
    } : createCustomFormat(normalizedName, extensions));
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (
      event.key !== "Enter" ||
      event.nativeEvent.isComposing ||
      !(event.target instanceof HTMLInputElement)
    ) {
      return;
    }
    // The inline editor lives inside the Family form. Keep Enter scoped to
    // Custom Format registration so it can never submit the outer Family.
    event.preventDefault();
    event.stopPropagation();
    save();
  }

  function handleRawSourceOpenChange(open: boolean) {
    setRawSourceOpen(open);
    if (!open) {
      setRawSource("");
      onInputChange?.();
    }
  }

  return (
    <fieldset className="library-custom-format-editor">
      <legend id={titleId}>{initialFormat
        ? t("policy.libraryUpload.families.customFormat.editTitle", {
            defaultValue: "Edit custom format",
          })
        : t("policy.libraryUpload.families.customFormat.title", {
            defaultValue: "Register custom format",
          })}</legend>
      <p>{t("policy.libraryUpload.families.customFormat.hint", {
        defaultValue: "Group aliases that identify the same format, such as .md and .markdown.",
      })}</p>
      <label htmlFor={`${titleId ?? "inline"}-custom-format-name`}>
        <span>{t("policy.libraryUpload.families.customFormat.name", {
          defaultValue: "Format name",
        })}</span>
        <input
          autoFocus
          disabled={busy || hasRawSource}
          id={`${titleId ?? "inline"}-custom-format-name`}
          {...textInputLimit(MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH, true, (value) => value.trim().replace(/\s+/g, " "))}
          onChange={(event) => {
            setName(event.target.value);
            onInputChange?.();
          }}
          onKeyDown={handleKeyDown}
          value={name}
        />
      </label>
      <div className="library-custom-format-extension-field">
        <span id={`${titleId ?? "inline"}-custom-format-extensions-label`}>
          {t("policy.libraryUpload.families.customFormat.extensions", {
            defaultValue: "Extension aliases",
          })}
        </span>
        <ChipInput
          ariaDescribedBy={`${titleId ?? "inline"}-custom-format-extensions-hint`}
          ariaLabel={t("policy.libraryUpload.families.customFormat.extensions", {
            defaultValue: "Extension aliases",
          })}
          disabled={busy || hasRawSource}
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
          id={`${titleId ?? "inline"}-custom-format-extensions`}
          maxItemLength={MAX_LIBRARY_EXTENSION_LENGTH}
          onChange={(value) => {
            setExtensions(normalizeCustomFormatExtensions(value));
            setExtensionInputError(null);
            onInputChange?.();
          }}
          onItemTooLong={(extension) => setExtensionInputError(t(
            "policy.libraryUpload.families.customFormat.errors.extensionTooLong",
            {
              defaultValue: "{{extension}} is too long. Extensions can contain at most {{count}} characters including the leading dot.",
              count: MAX_LIBRARY_EXTENSION_LENGTH,
              extension,
            },
          ))}
          onQueryChange={() => setExtensionInputError(null)}
          placeholder={t("policy.libraryUpload.families.customFormat.extensionsPlaceholder", {
            defaultValue: ".foo, .bar",
          })}
          stripLeadingHash={false}
          truncateItems={false}
          value={extensions}
        />
      </div>
      <p
        className="meta-line"
        id={`${titleId ?? "inline"}-custom-format-extensions-hint`}
      >
        {t("policy.libraryUpload.families.customFormat.extensionsHint", {
          defaultValue: "Add 1–{{count}} aliases. Press Enter or comma after each extension.",
          count: MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS,
        })}
      </p>
      {!hasRawSource && extensionInputError ? (
        <p className="field-error" role="alert">{extensionInputError}</p>
      ) : null}
      {!hasRawSource && malformedExtensions.length > 0 ? (
        <p className="field-error" role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.invalidExtension", {
            defaultValue: "Invalid extension: {{extensions}}",
            extensions: malformedExtensions.join(", "),
          })}
        </p>
      ) : null}
      {!hasRawSource && unsafeExtensions.length > 0 ? (
        <p className="field-error" role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.unsafeExtension", {
            defaultValue: "Blocked extension: {{extensions}}. Executable, active-content, archive, and macro-enabled aliases cannot be registered.",
            extensions: unsafeExtensions.join(", "),
          })}
        </p>
      ) : null}
      {name && !validCustomFormatName(name) ? (
        <p className="field-error" role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.invalidName", {
            defaultValue: "Use a name without control characters.",
          })}
        </p>
      ) : null}
      {extensionConflicts.map(({ extension, owner }) => (
        <p className="field-error" key={`${extension}-${owner.format_id}`} role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.extensionConflict", {
            defaultValue: "{{extension}} already belongs to {{format}} ({{aliases}}).",
            extension,
            format: owner.display_name,
            aliases: owner.extensions.join(", "),
          })}
        </p>
      ))}
      {stableOwnerConflicts.map(({ extension, owner }) => (
        <p className="field-error" key={`${extension}-${owner.formatId}`} role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.stableOwnerTransfer", {
            defaultValue: "{{extension}} belonged to {{format}} when these Settings were loaded. Save its removal first, then reopen Settings to assign it to another custom format.",
            extension,
            format: owner.displayName,
          })}
        </p>
      ))}
      {nameConflict ? (
        <p className="field-error" role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.nameConflict", {
            defaultValue: "A custom format named '{{format}}' already exists.",
            format: nameConflict.display_name,
          })}
        </p>
      ) : null}
      {stableNameOwnerConflict ? (
        <p className="field-error" role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.stableNameOwnerTransfer", {
            defaultValue: "The name '{{name}}' belonged to {{format}} when these Settings were loaded. Save that format's rename or deletion first, then reopen Settings before assigning the name to another custom format.",
            name: normalizedName,
            format: stableNameOwnerConflict.displayName,
          })}
        </p>
      ) : null}
      {extensions.length > MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS ? (
        <p className="field-error" role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.tooManyExtensions", {
            defaultValue: "A format can have up to {{count}} extension aliases.",
            count: MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS,
          })}
        </p>
      ) : null}
      {atFormatLimit ? (
        <p className="field-error" role="alert">
          {t("policy.libraryUpload.families.customFormat.errors.formatLimit", {
            defaultValue: "This Workspace can register up to {{count}} custom formats.",
            count: MAX_LIBRARY_CUSTOM_FORMATS,
          })}
        </p>
      ) : null}
      {!initialFormat ? (
        <div className="library-custom-format-bulk-source">
          <div className="library-custom-format-mode-divider" role="separator">
            <span>{t("policy.libraryUpload.families.customFormat.orAddInBulk", {
              defaultValue: "or add several groups in bulk",
            })}</span>
          </div>
          <AdvancedSection
            label={t("actions.showRaw")}
            onOpenChange={handleRawSourceOpenChange}
            open={rawSourceOpen}
          >
            <label htmlFor={`${titleId ?? "inline"}-custom-format-raw`}>
              <span>{t("policy.libraryUpload.families.customFormat.rawSource", {
                defaultValue: "Raw source (add / register)",
              })}</span>
              <textarea
                aria-describedby={`${titleId ?? "inline"}-custom-format-raw-hint`}
                className="library-custom-format-raw-source"
                disabled={busy || Boolean(name.trim() || extensions.length)}
                id={`${titleId ?? "inline"}-custom-format-raw`}
                maxLength={MAX_LIBRARY_ALIAS_GROUP_RAW_LENGTH}
                onChange={(event) => {
                  setRawSource(event.target.value);
                  onInputChange?.();
                }}
                placeholder="[alias](.ext1,.ext2), [other](.ext3)"
                rows={3}
                value={rawSource}
              />
            </label>
            <p
              className="meta-line"
              id={`${titleId ?? "inline"}-custom-format-raw-hint`}
            >
              {t("policy.libraryUpload.families.customFormat.rawSourceHint", {
                defaultValue: "Registered aliases add existing formats; unknown groups register Workspace custom formats (up to {{count}} characters).",
                count: MAX_LIBRARY_ALIAS_GROUP_RAW_LENGTH,
              })}
            </p>
          </AdvancedSection>
        </div>
      ) : null}
      <div className="library-custom-format-actions">
        <button
          className="icon-button text-button"
          disabled={busy}
          onClick={onCancel}
          type="button"
        >
          {t("actions.cancel", { defaultValue: "Cancel" })}
        </button>
        <button
          className="icon-button primary"
          disabled={busy || (hasRawSource ? !onBulkAdd : invalid)}
          onClick={save}
          type="button"
        >
          {hasRawSource
            ? t("policy.libraryUpload.families.customFormat.bulkAdd", {
                defaultValue: "Add or register groups",
              })
            : initialFormat
            ? t("policy.libraryUpload.families.customFormat.update", {
                defaultValue: "Update format",
              })
            : t("policy.libraryUpload.families.customFormat.add", {
                defaultValue: "Register format",
              })}
        </button>
      </div>
    </fieldset>
  );
}
