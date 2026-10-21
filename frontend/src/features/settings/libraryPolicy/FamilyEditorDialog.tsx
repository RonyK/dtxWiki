import { codePointLength, inputTextError, textInputLimit } from "../../../shared/lib/inputPolicy";
import type { TFunction } from "i18next";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { useTranslation } from "react-i18next";

import { Dialog } from "../../../shared/components/Dialog";
import type {
  LibraryFormatCapability,
  LibraryFormatFamilyPreset,
} from "../../../shared/media/libraryFormatCapabilities";
import {
  MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS,
  MAX_LIBRARY_CUSTOM_FORMATS,
  MAX_LIBRARY_ALIAS_GROUP_RAW_LENGTH,
  MAX_LIBRARY_FAMILY_BYTES,
  MAX_LIBRARY_FAMILY_NAME_LENGTH,
  MAX_LIBRARY_FORMAT_FAMILIES,
  MIN_LIBRARY_FAMILY_BYTES,
  customFormatOption,
  createCustomFormat,
  duplicateFamilyName,
  emptyCustomFamily,
  familyConflict,
  familyFormats,
  familyFromPreset,
  invalidCustomFormatExtensions,
  matchingFamilyFormats,
  normalizeExtensionInput,
  serializePolicyBuiltinFormatOverrides,
  unsafeLibraryFormatExtensions,
  type LibraryFamilyFormatOption,
  type LibraryPolicyBuiltinFormatOverrides,
  type LibraryPolicyCustomFormat,
  type LibraryPolicyFormatFamily,
} from "./model";
import {
  parseAliasGroupInput,
  type AliasGroupInputError,
} from "./aliasGroupInput";
import { CustomFormatEditor } from "./CustomFormatEditor";
import {
  snapshotStableCustomOwners,
  stableCustomNameOwnerConflict,
  stableCustomOwnerConflict,
  withStableBuiltinExtensionOwners,
} from "./customFormatOwnership";
import { FormatAliasGroupList } from "./FormatAliasGroupList";
import { UploadLimitControl, byteSizeDraftBytes } from "./UploadLimitControl";

function exactFamilyFormat(
  query: string,
  formats: LibraryFamilyFormatOption[],
) {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return null;
  const normalizedExtension = normalizeExtensionInput(normalized);
  const matches = formats.filter((format) => [
    format.display_name,
    format.format_id,
    ...format.extensions,
    ...format.search_aliases,
  ].some((value) => {
    const candidate = value.trim().toLocaleLowerCase();
    return candidate === normalized ||
      (candidate.startsWith(".") && candidate === normalizedExtension);
  }));
  return matches.length === 1 ? matches[0] : null;
}

export function FamilyEditorDialog({
  baselineBuiltinFormatOverrides,
  baselineCustomFormats,
  builtinFormatOverrides,
  busy,
  customFormats,
  families,
  formatOverrideIds,
  formats,
  initialFamily,
  isServerAdmin,
  onCancel,
  onApplyCustomFormats,
  onOpenDeploymentSettings,
  onRevealConflict,
  onSave,
  presets,
  workspaceDefaultBytes,
}: {
  baselineBuiltinFormatOverrides: LibraryPolicyBuiltinFormatOverrides;
  baselineCustomFormats: LibraryPolicyCustomFormat[];
  builtinFormatOverrides: LibraryPolicyBuiltinFormatOverrides;
  busy: boolean;
  customFormats: LibraryPolicyCustomFormat[];
  families: LibraryPolicyFormatFamily[];
  formatOverrideIds: string[];
  formats: LibraryFormatCapability[];
  initialFamily: LibraryPolicyFormatFamily | null;
  isServerAdmin: boolean;
  onCancel: () => void;
  onApplyCustomFormats: (customFormats: LibraryPolicyCustomFormat[]) => void;
  onOpenDeploymentSettings: () => void;
  onRevealConflict: (familyId: string) => void;
  onSave: (
    family: LibraryPolicyFormatFamily,
    customFormats: LibraryPolicyCustomFormat[],
    builtinFormatOverrides: LibraryPolicyBuiltinFormatOverrides,
  ) => void;
  presets: LibraryFormatFamilyPreset[];
  workspaceDefaultBytes: number;
}) {
  const { t } = useTranslation("settings");
  const titleId = useId();
  const formId = useId();
  const helpId = useId();
  const errorId = useId();
  const listboxId = useId();
  const customFormatEditorTitleId = useId();
  const addFormatsTitleId = useId();
  const aliasGroupsTitleId = useId();
  const customFormatListId = useId();
  const registeredFormatListboxRef = useRef<HTMLDivElement>(null);
  const registeredFormatPickerRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<LibraryPolicyFormatFamily>(() =>
    initialFamily ? cloneFamily(initialFamily) : emptyCustomFamily()
  );
  const [initialFamilyDraft] = useState(() => cloneFamily(draft));
  const [startKind, setStartKind] = useState<"preset" | "custom">(
    initialFamily?.origin === "preset" ? "preset" : "custom",
  );
  const [selectedPresetId, setSelectedPresetId] = useState(
    initialFamily?.preset_id ?? presets[0]?.preset_id ?? "",
  );
  const usedPresetIds = useMemo(
    () => new Set(
      families.flatMap((family) =>
        family.origin === "preset" && family.preset_id ? [family.preset_id] : []
      ),
    ),
    [families],
  );
  const [presetBaseline, setPresetBaseline] = useState<DialogPresetBaseline | null>(() =>
    presetBaselineFromFamily(initialFamily)
  );
  const [extensionError, setExtensionError] = useState<string | null>(null);
  const [presetAliasConflict, setPresetAliasConflict] = useState<{
    fingerprint: string;
    message: string;
  } | null>(null);
  const [registeredFormatQuery, setRegisteredFormatQuery] = useState("");
  const [registeredFormatFocused, setRegisteredFormatFocused] = useState(false);
  const [activeRegisteredFormatIndex, setActiveRegisteredFormatIndex] = useState(-1);
  const [draftBuiltinFormatOverrides, setDraftBuiltinFormatOverrides] = useState(
    () => cloneBuiltinFormatOverrides(builtinFormatOverrides),
  );
  const [draftCustomFormats, setDraftCustomFormats] = useState<LibraryPolicyCustomFormat[]>(
    () => customFormats.map((format) => ({ ...format, extensions: [...format.extensions] })),
  );
  const [customFormatEditorOpen, setCustomFormatEditorOpen] = useState(false);
  const [customFormatListOpen, setCustomFormatListOpen] = useState(false);
  const [customFormatInitialExtensions, setCustomFormatInitialExtensions] = useState("");
  const [editingCustomFormatId, setEditingCustomFormatId] = useState<string | null>(null);
  const [aliasEditorResetVersion, setAliasEditorResetVersion] = useState(0);
  const [blockedAliasGroupIds, setBlockedAliasGroupIds] = useState<Set<string>>(
    () => new Set(),
  );
  const handleAliasInteractionStateChange = useCallback((
    formatId: string,
    blocked: boolean,
  ) => {
    setBlockedAliasGroupIds((current) => {
      if (current.has(formatId) === blocked) return current;
      const next = new Set(current);
      if (blocked) next.add(formatId);
      else next.delete(formatId);
      return next;
    });
  }, []);
  const initialCustomFormatIds = useMemo(
    () => new Set(customFormats.map((format) => format.format_id)),
    [customFormats],
  );
  const customFormatsDirty = JSON.stringify(draftCustomFormats) !== JSON.stringify(customFormats);
  const builtinFormatOverridesDirty = JSON.stringify(
    serializePolicyBuiltinFormatOverrides(draftBuiltinFormatOverrides),
  ) !== JSON.stringify(
    serializePolicyBuiltinFormatOverrides(baselineBuiltinFormatOverrides),
  );
  const hasNewCustomFormats = draftCustomFormats.some(
    (format) => !initialCustomFormatIds.has(format.format_id),
  );
  const familyDraftDirty = JSON.stringify(draft) !== JSON.stringify(initialFamilyDraft);
  const availableFormats = useMemo(() => {
    const byId = new Map<string, LibraryFamilyFormatOption>(
      formats
        .filter((format) => !format.format_id.startsWith("custom-"))
        .map((format) => {
          const override = draftBuiltinFormatOverrides[format.format_id];
          return [format.format_id, {
            ...format,
            extensions: [...(
              override?.extensions ?? format.default_extensions ?? format.extensions
            )],
            extensions_overridden: Boolean(override),
          }] as const;
        }),
    );
    for (const format of draftCustomFormats) {
      byId.set(format.format_id, customFormatOption(format));
    }
    return [...byId.values()];
  }, [draftBuiltinFormatOverrides, draftCustomFormats, formats]);
  const selectedFormats = useMemo(
    () => familyFormats(draft, availableFormats),
    [availableFormats, draft],
  );
  const registeredFormatSuggestions = useMemo(
    () => matchingFamilyFormats(
      registeredFormatQuery,
      availableFormats,
      [],
    ),
    [availableFormats, registeredFormatQuery],
  );
  const exactRegisteredFormat = useMemo(
    () => exactFamilyFormat(registeredFormatQuery, availableFormats),
    [availableFormats, registeredFormatQuery],
  );
  const unknownRegisteredExtension = useMemo(() => {
    const normalized = normalizeExtensionInput(registeredFormatQuery);
    return normalized && /^[.][a-z0-9][a-z0-9+_-]*$/i.test(normalized) &&
      formatsForExtension(normalized, availableFormats).length === 0 ? normalized : null;
  }, [availableFormats, registeredFormatQuery]);
  useEffect(() => {
    if (activeRegisteredFormatIndex < 0) return;
    const activeOption = registeredFormatListboxRef.current
      ?.querySelector<HTMLElement>("[aria-selected='true']");
    activeOption?.scrollIntoView?.({ block: "nearest" });
  }, [activeRegisteredFormatIndex, registeredFormatSuggestions]);
  useEffect(() => {
    if (!customFormatEditorOpen) return;
    registeredFormatPickerRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [customFormatEditorOpen]);
  const baselineFormatOptions = useMemo(() => {
    const byId = new Map<string, LibraryFamilyFormatOption>(
      formats.map((format) => {
        const baselineOverride = baselineBuiltinFormatOverrides[format.format_id];
        return [format.format_id, {
          ...format,
          extensions: [...(
            baselineOverride?.extensions ?? format.default_extensions ?? format.extensions
          )],
          extensions_overridden: Boolean(baselineOverride),
        }];
      }),
    );
    for (const format of baselineCustomFormats) {
      byId.set(format.format_id, customFormatOption(format));
    }
    return [...byId.values()];
  }, [baselineBuiltinFormatOverrides, baselineCustomFormats, formats]);
  const stableCustomOwners = useMemo(
    () => withStableBuiltinExtensionOwners(
      snapshotStableCustomOwners(baselineCustomFormats),
      baselineFormatOptions,
    ),
    [baselineCustomFormats, baselineFormatOptions],
  );
  const presetTransitionFingerprint = JSON.stringify({
    builtinFormatOverrides: serializePolicyBuiltinFormatOverrides(draftBuiltinFormatOverrides),
    customFormats: draftCustomFormats,
    draft,
    selectedPresetId,
    startKind,
  });
  useEffect(() => {
    if (
      presetAliasConflict &&
      presetAliasConflict.fingerprint !== presetTransitionFingerprint
    ) {
      setPresetAliasConflict(null);
    }
  }, [presetAliasConflict, presetTransitionFingerprint]);
  const projectedFamilies = useMemo(
    () => initialFamily
      ? families.map((family) => family.family_id === draft.family_id ? draft : family)
      : families,
    [draft, families, initialFamily],
  );
  const instanceHardCap = useMemo(() => {
    const selectedCaps = selectedFormats.flatMap((format) => {
      const limit = format.upload_limit_sources?.instance;
      return typeof limit === "number" ? [limit] : [];
    });
    const catalogCaps = formats.flatMap((format) => {
      const limit = format.upload_limit_sources?.instance;
      return typeof limit === "number" ? [limit] : [];
    });
    const candidates = selectedCaps.length > 0 ? selectedCaps : catalogCaps;
    return candidates.length > 0 ? Math.min(...candidates) : null;
  }, [formats, selectedFormats]);
  const nameConflict = duplicateFamilyName(draft.name, families, draft.family_id);
  const savedPresetUnavailable = startKind === "preset" && presetBaseline !== null && !presets.some(
    (preset) => preset.preset_id === presetBaseline.preset_id &&
      preset.revision === presetBaseline.preset_revision,
  );
  const savedPresetOptionValue = `saved:${presetBaseline?.preset_id ?? selectedPresetId}:${presetBaseline?.preset_revision ?? "unknown"}`;
  const presetAliasesModified = presetBaseline
    ? Object.entries(presetBaseline.format_extensions).some(([formatId, extensions]) => {
        const format = availableFormats.find((candidate) => candidate.format_id === formatId);
        return format ? !sameStringSet(format.extensions, extensions) : false;
      })
    : false;
  const presetModified = startKind === "preset" && presetBaseline !== null && (
    draft.origin !== "preset" ||
    !sameStringSet(draft.format_ids, presetBaseline.format_ids) ||
    presetAliasesModified
  );
  const formatConflicts = draft.format_ids.flatMap((formatId) => {
    const conflict = familyConflict(formatId, families, draft.family_id);
    return conflict ? [conflict] : [];
  });
  const limitInvalid = draft.max_upload_bytes !== null && (
    !Number.isInteger(draft.max_upload_bytes) ||
    draft.max_upload_bytes < MIN_LIBRARY_FAMILY_BYTES ||
    draft.max_upload_bytes > MAX_LIBRARY_FAMILY_BYTES
  );
  const atFamilyLimit = initialFamily === null && families.length >= MAX_LIBRARY_FORMAT_FAMILIES;
  const invalid = !draft.name.trim() || codePointLength(draft.name.trim().replace(/\s+/g, " ")) > MAX_LIBRARY_FAMILY_NAME_LENGTH || Boolean(inputTextError(draft.name, Number.MAX_SAFE_INTEGER)) ||
    draft.format_ids.length === 0 ||
    Boolean(nameConflict) || formatConflicts.length > 0 ||
    limitInvalid || atFamilyLimit || customFormatEditorOpen || blockedAliasGroupIds.size > 0 ||
    Boolean(presetAliasConflict);
  const blockedReason = blockedAliasGroupIds.size > 0
    ? t("policy.libraryUpload.families.blockedReason", {
        defaultValue: "Finish the highlighted extension edit before saving.",
      })
    : null;

  function selectPreset(presetId: string) {
    const preset = presets.find((candidate) => candidate.preset_id === presetId);
    if (!preset) return false;
    const baseline = presetBaselineFromPreset(preset);
    const next = withPresetBaseline(localizedPresetFamily(preset, t), baseline);
    const nextCustomFormats = retainedDraftCustomFormats(next.format_ids);
    setSelectedPresetId(presetId);
    setStartKind("preset");
    setDraftCustomFormats(nextCustomFormats);
    setDraftBuiltinFormatOverrides(cloneBuiltinFormatOverrides(builtinFormatOverrides));
    setPresetBaseline(baseline);
    setDraft((current) => ({ ...next, family_id: current.family_id }));
    setAliasEditorResetVersion((current) => current + 1);
    setExtensionError(null);
    setPresetAliasConflict(null);
    return true;
  }

  function chooseStartKind(next: "preset" | "custom") {
    if (next === "preset" && presets.length > 0) {
      const selectedLivePreset = presets.find(
        (preset) => preset.preset_id === selectedPresetId,
      );
      selectPreset(selectedLivePreset?.preset_id ?? presets[0].preset_id);
      return;
    }
    setStartKind(next);
    setPresetBaseline(null);
    setDraftBuiltinFormatOverrides(cloneBuiltinFormatOverrides(builtinFormatOverrides));
    const nextDraft = withPresetBaseline(initialFamily ? {
        ...draft,
        origin: "custom",
        preset_id: null,
        preset_revision: null,
      } : {
        ...emptyCustomFamily(),
        family_id: draft.family_id,
      }, null);
    discardUnreferencedNewCustomFormats(nextDraft.format_ids);
    setDraft(nextDraft);
    setAliasEditorResetVersion((current) => current + 1);
    setExtensionError(null);
    setPresetAliasConflict(null);
  }

  function customizeFamily(current: LibraryPolicyFormatFamily) {
    if (startKind === "preset" && presetBaseline) {
      return withPresetBaseline({
        ...current,
        origin: "preset",
        preset_id: presetBaseline.preset_id,
        preset_revision: presetBaseline.preset_revision,
      }, presetBaseline);
    }
    return withPresetBaseline({
      ...current,
      origin: "custom",
      preset_id: null,
      preset_revision: null,
    }, null);
  }

  function discardUnreferencedNewCustomFormats(referencedFormatIds: string[]) {
    const referenced = new Set(referencedFormatIds);
    setDraftCustomFormats((current) => current.filter((format) =>
      initialCustomFormatIds.has(format.format_id) || referenced.has(format.format_id)
    ));
  }

  function retainedDraftCustomFormats(referencedFormatIds: string[]) {
    const referenced = new Set(referencedFormatIds);
    return draftCustomFormats.filter((format) =>
      initialCustomFormatIds.has(format.format_id) || referenced.has(format.format_id)
    );
  }

  function blockPresetAliasChange(
    baseline: DialogPresetBaseline,
    nextBuiltinFormatOverrides: LibraryPolicyBuiltinFormatOverrides,
    nextCustomFormats: LibraryPolicyCustomFormat[],
  ) {
    const stableConflict = findStablePresetAliasConflict(
      baseline.format_extensions,
      baselineFormatOptions,
      formats,
    );
    if (stableConflict) {
      setPresetAliasConflict({
        fingerprint: presetTransitionFingerprint,
        message: t("policy.libraryUpload.families.aliasGroup.stableOwnerTransfer", {
          defaultValue: "{{extension}} belonged to {{format}} when this dialog opened. Save its removal first, then reopen Settings before assigning it here.",
          extension: stableConflict.extension,
          format: stableConflict.owner.display_name,
        }),
      });
      return true;
    }

    const effectiveConflict = findEffectiveAliasConflict(
      projectEffectiveFormatOptions(formats, nextBuiltinFormatOverrides, nextCustomFormats),
      formats,
    );
    if (effectiveConflict) {
      setPresetAliasConflict({
        fingerprint: presetTransitionFingerprint,
        message: t("policy.libraryUpload.families.customFormat.errors.extensionConflict", {
          defaultValue: "{{extension}} already belongs to {{format}} ({{aliases}}).",
          aliases: effectiveConflict.owner.extensions.join(", "),
          extension: effectiveConflict.extension,
          format: effectiveConflict.owner.display_name,
        }),
      });
      return true;
    }
    return false;
  }

  function addFormat(format: LibraryFamilyFormatOption) {
    const conflict = familyConflict(format.format_id, families, draft.family_id);
    if (conflict) {
      setExtensionError(t("policy.libraryUpload.families.errors.extensionConflict", {
        defaultValue: "{{extension}} is already in '{{family}}'. Remove it there before adding it here.",
        extension: format.extensions[0] ?? format.format_id,
        family: conflict.familyName,
      }));
      return;
    }
    setDraft((current) => ({
      ...customizeFamily(current),
      format_ids: [...new Set([...current.format_ids, format.format_id])],
    }));
    setRegisteredFormatQuery("");
    setActiveRegisteredFormatIndex(-1);
    setExtensionError(null);
  }

  function handleRegisteredFormatKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return;
    const registeredFormatOptionCount = registeredFormatSuggestions.length +
      (unknownRegisteredExtension ? 1 : 0);
    if (event.key === "ArrowDown" && registeredFormatOptionCount > 0) {
      event.preventDefault();
      setActiveRegisteredFormatIndex((current) =>
        current < 0 ? 0 : (current + 1) % registeredFormatOptionCount
      );
    } else if (event.key === "ArrowUp" && registeredFormatOptionCount > 0) {
      event.preventDefault();
      setActiveRegisteredFormatIndex((current) =>
        current < 0
          ? registeredFormatOptionCount - 1
          : (current - 1 + registeredFormatOptionCount) % registeredFormatOptionCount
      );
    } else if (event.key === "Enter" && registeredFormatQuery.trim()) {
      event.preventDefault();
      event.stopPropagation();
      const explicitlySelectedFormat = activeRegisteredFormatIndex >= 0 &&
          activeRegisteredFormatIndex < registeredFormatSuggestions.length
        ? registeredFormatSuggestions[activeRegisteredFormatIndex]
        : null;
      if (explicitlySelectedFormat) addFormat(explicitlySelectedFormat);
      else if (
        unknownRegisteredExtension &&
        activeRegisteredFormatIndex === registeredFormatSuggestions.length
      ) beginCustomFormat(unknownRegisteredExtension);
      else if (exactRegisteredFormat) addFormat(exactRegisteredFormat);
      else if (unknownRegisteredExtension) beginCustomFormat(unknownRegisteredExtension);
    } else if (event.key === "Escape" && registeredFormatQuery) {
      event.preventDefault();
      setRegisteredFormatQuery("");
      setActiveRegisteredFormatIndex(-1);
    }
  }

  function commitExtensionInput(query: string) {
    if (query.length > MAX_LIBRARY_ALIAS_GROUP_RAW_LENGTH) {
      setExtensionError(t("policy.libraryUpload.families.inputErrors.rawTooLong", {
        defaultValue: "Raw source can contain up to {{count}} characters.",
        count: MAX_LIBRARY_ALIAS_GROUP_RAW_LENGTH,
      }));
      return false;
    }
    const parsed = parseAliasGroupInput(query);
    if (!parsed.ok) {
      setExtensionError(aliasGroupInputErrorMessage(parsed.error, t));
      return false;
    }
    const unsafeExtensions = unsafeLibraryFormatExtensions(
      parsed.groups.flatMap((group) => group.extensions),
    );
    if (unsafeExtensions.length > 0) {
      setExtensionError(t("policy.libraryUpload.families.inputErrors.unsafeExtension", {
        defaultValue: "{{extensions}} cannot be registered because executable, active-content, archive, and macro-enabled aliases are blocked.",
        extensions: unsafeExtensions.join(", "),
      }));
      return false;
    }
    const remainingCustomFormatCount = MAX_LIBRARY_CUSTOM_FORMATS - draftCustomFormats.length;
    const newGroupCount = parsed.groups.filter((group) => group.extensions.every(
      (extension) => formatsForExtension(extension, availableFormats).length === 0,
    )).length;
    if (
      parsed.groups.length > MAX_LIBRARY_CUSTOM_FORMATS ||
      newGroupCount > remainingCustomFormatCount
    ) {
      setExtensionError(t("policy.libraryUpload.families.inputErrors.tooManyGroups", {
        defaultValue: "This source would register {{count}} new groups, but only {{remaining}} Workspace custom format slots remain.",
        count: newGroupCount,
        remaining: remainingCustomFormatCount,
      }));
      return false;
    }

    let nextCustomFormats = draftCustomFormats.map((format) => ({
      ...format,
      extensions: [...format.extensions],
    }));
    const nextFormatIds = [...draft.format_ids];
    let candidates: LibraryFamilyFormatOption[] = [...availableFormats];

    for (const group of parsed.groups) {
      const ownersByExtension = group.extensions.map((extension) =>
        formatsForExtension(extension, candidates)
      );
      const ambiguousExtensionIndex = ownersByExtension.findIndex((owners) => owners.length > 1);
      if (ambiguousExtensionIndex >= 0) {
        const extension = group.extensions[ambiguousExtensionIndex];
        setExtensionError(t("policy.libraryUpload.families.inputErrors.ambiguousExtension", {
          defaultValue: "{{extension}} matches more than one registered format ({{formats}}). Choose a specific suggestion.",
          extension,
          formats: ownersByExtension[ambiguousExtensionIndex]
            .map((owner) => owner.display_name)
            .join(", "),
        }));
        return false;
      }
      const owners = ownersByExtension.map((matches) => matches[0] ?? null);
      const registeredOwners = Array.from(new Map(
        owners.filter((owner): owner is LibraryFamilyFormatOption => owner !== null)
          .map((owner) => [owner.format_id, owner]),
      ).values());
      let format: LibraryFamilyFormatOption;

      if (registeredOwners.length > 1 || (registeredOwners.length === 1 && owners.some((owner) => owner === null))) {
        setExtensionError(t("policy.libraryUpload.families.inputErrors.mixedOwners", {
          defaultValue: "All extensions in one alias group must belong to the same format. Split registered and new extensions into separate groups.",
        }));
        return false;
      }

      if (registeredOwners.length === 1) {
        format = registeredOwners[0];
        if (
          parsed.explicit &&
          group.label.toLocaleLowerCase() !== format.display_name.toLocaleLowerCase()
        ) {
          setExtensionError(t("policy.libraryUpload.families.inputErrors.registeredName", {
            defaultValue: "{{extensions}} already belong to {{format}}. Use that registered group name instead of {{label}}.",
            extensions: group.extensions.join(", "),
            format: format.display_name,
            label: group.label,
          }));
          return false;
        }
      } else {
        const stableConflict = group.extensions.flatMap((extension) => {
          const owner = stableCustomOwnerConflict(
            extension,
            null,
            stableCustomOwners.extensions,
          );
          return owner ? [{ extension, owner }] : [];
        })[0];
        if (stableConflict) {
          setExtensionError(t("policy.libraryUpload.families.customFormat.errors.stableOwnerTransfer", {
            defaultValue: "{{extension}} belonged to {{format}} when these Settings were loaded. Save its removal first, then reopen Settings to assign it to another custom format.",
            extension: stableConflict.extension,
            format: stableConflict.owner.displayName,
          }));
          return false;
        }
        if (nextCustomFormats.length >= MAX_LIBRARY_CUSTOM_FORMATS) {
          setExtensionError(t("policy.libraryUpload.families.customFormat.errors.formatLimit", {
            defaultValue: "This Workspace can register up to {{count}} custom formats.",
            count: MAX_LIBRARY_CUSTOM_FORMATS,
          }));
          return false;
        }
        const stableNameConflict = stableCustomNameOwnerConflict(
          group.label,
          null,
          stableCustomOwners.displayNames,
        );
        if (stableNameConflict) {
          setExtensionError(t("policy.libraryUpload.families.customFormat.errors.stableNameOwnerTransfer", {
            defaultValue: "The name '{{name}}' belonged to {{format}} when these Settings were loaded. Save that format's rename or deletion first, then reopen Settings before assigning the name to another custom format.",
            name: group.label,
            format: stableNameConflict.displayName,
          }));
          return false;
        }
        if (
          group.extensions.length > MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS ||
          invalidCustomFormatExtensions(group.extensions).length > 0
        ) {
          setExtensionError(t("policy.libraryUpload.families.inputErrors.invalid_extension", {
            defaultValue: "Enter valid extensions such as .foo or .archive.",
          }));
          return false;
        }
        const duplicateName = nextCustomFormats.find((candidate) =>
          candidate.display_name.toLocaleLowerCase() === group.label.toLocaleLowerCase()
        );
        if (duplicateName) {
          setExtensionError(t("policy.libraryUpload.families.customFormat.errors.nameConflict", {
            defaultValue: "A custom format named '{{format}}' already exists.",
            format: duplicateName.display_name,
          }));
          return false;
        }
        const customFormat = createCustomFormat(group.label, group.extensions);
        nextCustomFormats = [...nextCustomFormats, customFormat];
        format = customFormatOption(customFormat);
        candidates = [...candidates, format];
      }

      const conflict = familyConflict(format.format_id, families, draft.family_id);
      if (conflict) {
        setExtensionError(t("policy.libraryUpload.families.errors.extensionConflict", {
          defaultValue: "{{extension}} is already in '{{family}}'. Remove it there before adding it here.",
          extension: format.extensions[0] ?? format.format_id,
          family: conflict.familyName,
        }));
        return false;
      }
      if (!nextFormatIds.includes(format.format_id)) nextFormatIds.push(format.format_id);
    }

    setDraftCustomFormats(nextCustomFormats);
    setDraft((current) => ({ ...customizeFamily(current), format_ids: nextFormatIds }));
    setExtensionError(null);
    return true;
  }

  function beginCustomFormat(initialExtensions = "", formatId: string | null = null) {
    setCustomFormatInitialExtensions(initialExtensions);
    setEditingCustomFormatId(formatId);
    setCustomFormatEditorOpen(true);
    setCustomFormatListOpen(false);
    setExtensionError(null);
  }

  function cancelCustomFormat() {
    setCustomFormatEditorOpen(false);
    setCustomFormatInitialExtensions("");
    setEditingCustomFormatId(null);
  }

  function removeDraftCustomFormat(formatId: string) {
    setDraftCustomFormats((current) => current.filter(
      (format) => format.format_id !== formatId,
    ));
    if (!draft.format_ids.includes(formatId)) return;
    setDraft((current) => ({
      ...customizeFamily(current),
      format_ids: current.format_ids.filter((candidate) => candidate !== formatId),
    }));
  }

  function updateBuiltinFormat(formatId: string, extensions: string[]) {
    const format = formats.find((candidate) => candidate.format_id === formatId);
    if (!format) return;
    const defaults = format.default_extensions ?? format.extensions;
    setDraftBuiltinFormatOverrides((current) => {
      const next = cloneBuiltinFormatOverrides(current);
      if (sameStringSet(extensions, defaults)) {
        delete next[formatId];
      } else {
        next[formatId] = { extensions: [...extensions] };
      }
      return next;
    });
    setDraft((current) => customizeFamily(current));
    setExtensionError(null);
    setPresetAliasConflict(null);
  }

  function restorePresetDefaults() {
    if (!presetBaseline) return;
    const currentBuiltins = draft.format_ids.filter(
      (formatId) => !formatId.startsWith("custom-"),
    );
    const baselineIds = new Set(presetBaseline.format_ids);
    const nextBuiltinFormatOverrides = cloneBuiltinFormatOverrides(draftBuiltinFormatOverrides);
    for (const formatId of currentBuiltins) {
      if (baselineIds.has(formatId)) continue;
      const openedOverride = builtinFormatOverrides[formatId];
      if (openedOverride) {
        nextBuiltinFormatOverrides[formatId] = { extensions: [...openedOverride.extensions] };
      } else {
        delete nextBuiltinFormatOverrides[formatId];
      }
    }
    const restoredBuiltinFormatOverrides = applyPresetAliases(
      nextBuiltinFormatOverrides,
      presetBaseline,
      formats,
    );
    const nextCustomFormats = retainedDraftCustomFormats(presetBaseline.format_ids);
    if (blockPresetAliasChange(
      presetBaseline,
      restoredBuiltinFormatOverrides,
      nextCustomFormats,
    )) return;
    setDraftBuiltinFormatOverrides(restoredBuiltinFormatOverrides);
    setDraftCustomFormats(nextCustomFormats);
    setDraft((current) => withPresetBaseline({
      ...current,
      origin: "preset",
      preset_id: presetBaseline.preset_id,
      preset_revision: presetBaseline.preset_revision,
      format_ids: [...presetBaseline.format_ids],
    }, presetBaseline));
    setAliasEditorResetVersion((current) => current + 1);
    setExtensionError(null);
    setPresetAliasConflict(null);
  }

  return (
    <Dialog
      className="library-family-editor-dialog"
      dismissible={!busy}
      footer={(
        <div className="modal-actions">
          {invalid && blockedReason ? (
            <p className="library-family-submit-blocked-reason" role="status">
              {blockedReason}
              {blockedAliasGroupIds.size > 0 ? (
                <button
                  className="text-button"
                  onClick={() => {
                    const formatId = blockedAliasGroupIds.values().next().value as string | undefined;
                    const card = formatId
                      ? document.getElementById(`library-family-alias-group-${formatId}`)
                      : null;
                    card?.scrollIntoView({ block: "center" });
                    card?.querySelector<HTMLInputElement>("input")?.focus();
                  }}
                  type="button"
                >
                  {t("policy.libraryUpload.families.revealBlockedReason", {
                    defaultValue: "Review it →",
                  })}
                </button>
              ) : null}
            </p>
          ) : null}
          <button className="icon-button" disabled={busy} onClick={onCancel} type="button">
            {t("actions.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={busy || invalid}
            form={formId}
            type="submit"
          >
            {initialFamily
              ? t("policy.libraryUpload.families.update", { defaultValue: "Update family" })
              : t("policy.libraryUpload.families.add", { defaultValue: "Add family" })}
          </button>
        </div>
      )}
      labelledBy={titleId}
      onClose={onCancel}
    >
      <form
        className="library-family-editor"
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          if (!invalid && !busy) {
            onSave(
              { ...draft, name: draft.name.trim() },
              draftCustomFormats,
              draftBuiltinFormatOverrides,
            );
          }
        }}
      >
        <header className="library-family-editor-heading">
          <h2 id={titleId}>
            {initialFamily
              ? t("policy.libraryUpload.families.editTitle", {
                  defaultValue: "Edit format family",
                })
              : t("policy.libraryUpload.families.addTitle", {
                  defaultValue: "Add format family",
                })}
          </h2>
          <p id={helpId}>
            {t("policy.libraryUpload.families.editorHint", {
              defaultValue: "Start from a preset or build a family from Workspace formats.",
            })}
          </p>
        </header>

        <fieldset className="library-family-start-options" data-family-editor-section="start-from">
          <legend>
            {t("policy.libraryUpload.families.startLabel", { defaultValue: "Start from" })}
          </legend>
          <label>
            <input
              checked={startKind === "custom"}
              disabled={busy}
              name="library-family-start"
              onChange={() => chooseStartKind("custom")}
              type="radio"
            />
            <span>{t("policy.libraryUpload.families.startCustom", { defaultValue: "Custom" })}</span>
          </label>
          <label>
            <input
              checked={startKind === "preset"}
              disabled={busy || presets.length === 0}
              name="library-family-start"
              onChange={() => chooseStartKind("preset")}
              type="radio"
            />
            <span>{t("policy.libraryUpload.families.startPreset", { defaultValue: "Preset" })}</span>
          </label>
        </fieldset>

        {startKind === "preset" ? (
          <label className="library-family-field" htmlFor="library-family-preset">
            <span>
              {t("policy.libraryUpload.families.presetLabel", {
                defaultValue: "Family preset",
              })}
            </span>
            <select
              autoFocus={!initialFamily}
              disabled={busy}
              id="library-family-preset"
              onChange={(event) => {
                if (event.target.value !== savedPresetOptionValue) {
                  selectPreset(event.target.value);
                }
              }}
              value={savedPresetUnavailable ? savedPresetOptionValue : selectedPresetId}
            >
              {savedPresetUnavailable ? (
                <option value={savedPresetOptionValue}>
                  {usedPresetIds.has(presetBaseline?.preset_id ?? selectedPresetId)
                    ? t("policy.libraryUpload.families.presetInUse", {
                        defaultValue: "{{preset}} — In use",
                        preset: t("policy.libraryUpload.families.savedPresetSnapshot", {
                          defaultValue: "Saved preset snapshot (revision {{revision}})",
                          revision: presetBaseline?.preset_revision ?? "?",
                        }),
                      })
                    : t("policy.libraryUpload.families.savedPresetSnapshot", {
                        defaultValue: "Saved preset snapshot (revision {{revision}})",
                        revision: presetBaseline?.preset_revision ?? "?",
                      })}
                </option>
              ) : null}
              {presets.map((preset) => {
                const label = t(`policy.libraryUpload.familyPresets.names.${preset.preset_id}`, {
                  defaultValue: preset.label,
                });
                return (
                  <option key={preset.version_id} value={preset.preset_id}>
                    {usedPresetIds.has(preset.preset_id)
                      ? t("policy.libraryUpload.families.presetInUse", {
                          defaultValue: "{{preset}} — In use",
                          preset: label,
                        })
                      : label}
                  </option>
                );
              })}
            </select>
          </label>
        ) : null}
        {startKind === "preset" && presets.length === 0 && !savedPresetUnavailable ? (
          <p className="field-error" role="alert">
            {t("policy.libraryUpload.families.noPresets", {
              defaultValue: "No verified family presets are available from this server.",
            })}
          </p>
        ) : null}
        {savedPresetUnavailable ? (
          <p className="meta-line" role="status">
            {t("policy.libraryUpload.families.presetUnavailable", {
              defaultValue: "This saved preset snapshot is no longer offered by the server. You can still edit it or restore its saved baseline.",
            })}
          </p>
        ) : null}
        {startKind === "preset" && presetBaseline ? (
          <div className="library-family-preset-state">
            <span className={presetModified ? "status-pill warning" : "status-pill"}>
              {presetModified
                ? t("policy.libraryUpload.families.presetModified", {
                    defaultValue: "Modified from preset",
                  })
                : t("policy.libraryUpload.families.aliasGroup.preset", {
                    defaultValue: "Preset",
                  })}
            </span>
            <button
              className="icon-button text-button"
              disabled={busy || !presetModified}
              onClick={restorePresetDefaults}
              type="button"
            >
              {t("policy.libraryUpload.families.restorePreset", {
                defaultValue: "Restore preset defaults",
              })}
            </button>
          </div>
        ) : null}

        <div data-family-editor-section="family-name">
          <label className="library-family-field" htmlFor="library-family-name">
            <span>
              {t("policy.libraryUpload.families.nameLabel", {
                defaultValue: "Format family name",
              })}
            </span>
            <input
              autoFocus={Boolean(initialFamily) || startKind === "custom"}
              disabled={busy}
              id="library-family-name"
              {...textInputLimit(MAX_LIBRARY_FAMILY_NAME_LENGTH, true, (value) => value.trim().replace(/\s+/g, " "))}
              onChange={(event) => setDraft((current) => ({
                ...current,
                name: event.target.value,
              }))}
              value={draft.name}
            />
          </label>
          {nameConflict ? (
            <p className="field-error" role="alert">
              {t("policy.libraryUpload.families.errors.duplicateName", {
                defaultValue: "A family named '{{family}}' already exists.",
                family: nameConflict.name,
              })}
            </p>
          ) : null}
        </div>

        <div data-family-editor-section="upload-limit">
          <div className="library-family-section-heading">
            <strong>{t("policy.libraryUpload.families.limitLabel", {
              defaultValue: "Upload limit",
            })}</strong>
          </div>
          <div className="library-upload-limit-panel">
            <UploadLimitControl
              busy={busy || draft.max_upload_bytes === null}
              fieldId="library-family-max-upload-bytes"
              label={t("policy.libraryUpload.families.customLimitLabel", {
                defaultValue: "Family upload limit",
              })}
              maxBytes={MAX_LIBRARY_FAMILY_BYTES}
              onChange={(next) => setDraft((current) => ({
                ...current,
                max_upload_bytes: byteSizeDraftBytes(next),
              }))}
              value={draft.max_upload_bytes ?? workspaceDefaultBytes}
            />
            <label className="checkbox-field">
              <input
                checked={draft.max_upload_bytes === null}
                disabled={busy}
                onChange={(event) => setDraft((current) => ({
                  ...current,
                  max_upload_bytes: event.target.checked
                    ? null
                    : current.max_upload_bytes ?? workspaceDefaultBytes,
                }))}
                type="checkbox"
              />
              <span>{t("policy.libraryUpload.families.useWorkspaceLimit", {
                defaultValue: "Use workspace upload limit",
              })}</span>
            </label>
            {draft.max_upload_bytes !== null ? (
              <>
                <p className="library-policy-effective">
                  {t("policy.libraryUpload.families.customLimitHint", {
                    defaultValue: "This format-specific default may be higher or lower than the Workspace default.",
                  })}
                </p>
                {instanceHardCap !== null ? (
                  <>
                    <p className="library-policy-effective">
                      {t("policy.libraryUpload.families.hardCapHint", {
                        defaultValue: "{{value}} is the most you can set here — this dtxWiki server won't accept more for these formats.",
                        value: formatByteLimit(instanceHardCap),
                      })}
                    </p>
                    <p className="library-policy-effective">
                      {isServerAdmin ? (
                        <button
                          className="text-button"
                          onClick={onOpenDeploymentSettings}
                          type="button"
                        >
                          {t("policy.libraryUpload.families.hardCapAdminLink", {
                            defaultValue: "Raise it in deployment settings →",
                          })}
                        </button>
                      ) : (
                        t("policy.libraryUpload.families.hardCapAdminNote", {
                          defaultValue: "A server admin can raise it in the server's deployment settings.",
                        })
                      )}
                    </p>
                  </>
                ) : null}
              </>
            ) : null}
            <p className="library-policy-effective">
              {t("policy.libraryUpload.families.workspaceLimitReference", {
                defaultValue: "Workspace upload limit: {{value}}",
                value: formatByteLimit(workspaceDefaultBytes),
              })}
            </p>
          </div>
          {limitInvalid ? (
            <p className="field-error" role="alert">
              {t("policy.libraryUpload.families.errors.invalidLimit", {
                defaultValue: "The family limit must be between 1 KB and 4 GiB.",
              })}
            </p>
          ) : null}
        </div>

        <div className="library-family-extension-editor">
          <div
            className="library-family-section-heading"
            data-family-editor-section="add-formats"
          >
            <strong id={addFormatsTitleId}>
              {t("policy.libraryUpload.families.addFormatsLabel", {
                defaultValue: "Add formats",
              })}
            </strong>
            <small>
              {t("policy.libraryUpload.families.addFormatsHint", {
                defaultValue: "Enter an extension, format name, or alias. Registered formats are reused automatically; an unknown extension can be registered here.",
              })}
            </small>
          </div>
          <div className="library-family-registered-format-picker" ref={registeredFormatPickerRef}>
            {customFormatEditorOpen ? (
              <CustomFormatEditor
                busy={busy}
                customFormats={draftCustomFormats}
                formats={availableFormats}
                initialExtensions={customFormatInitialExtensions}
                initialFormat={editingCustomFormatId
                  ? draftCustomFormats.find(
                      (format) => format.format_id === editingCustomFormatId,
                    ) ?? null
                  : null}
                key={editingCustomFormatId ?? "new-custom-format"}
                onCancel={cancelCustomFormat}
                onSave={(format) => {
                  const editingExisting = draftCustomFormats.some(
                    (candidate) => candidate.format_id === format.format_id,
                  );
                  setDraftCustomFormats((current) => editingExisting
                    ? current.map((candidate) =>
                        candidate.format_id === format.format_id ? format : candidate
                      )
                    : [...current, format]
                  );
                  if (!editingExisting) addFormat(customFormatOption(format));
                  cancelCustomFormat();
                }}
                onBulkAdd={(query) => {
                  if (commitExtensionInput(query)) {
                    cancelCustomFormat();
                  }
                }}
                onInputChange={() => setExtensionError(null)}
                stableCustomOwners={stableCustomOwners}
                titleId={customFormatEditorTitleId}
              />
            ) : (
              <div
                onBlur={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
                    setRegisteredFormatFocused(false);
                  }
                }}
              >
                <div className="library-family-registered-format-row">
                  <label htmlFor="library-family-registered-format">
                    <span className="visually-hidden">{t("policy.libraryUpload.families.findRegisteredLabel", {
                      defaultValue: "Add formats",
                    })}</span>
                    <input
                      aria-activedescendant={activeRegisteredFormatIndex >= 0
                        ? activeRegisteredFormatIndex < registeredFormatSuggestions.length
                          ? `${listboxId}-${registeredFormatSuggestions[activeRegisteredFormatIndex]?.format_id}`
                          : `${listboxId}-register-custom`
                        : undefined}
                      aria-autocomplete="list"
                      aria-controls={registeredFormatFocused && (
                        registeredFormatSuggestions.length > 0 || unknownRegisteredExtension
                      )
                        ? listboxId
                        : undefined}
                      aria-describedby={errorId}
                      aria-expanded={registeredFormatFocused &&
                        (registeredFormatSuggestions.length > 0 || Boolean(unknownRegisteredExtension))}
                      aria-invalid={Boolean(extensionError || presetAliasConflict)}
                      autoComplete="off"
                      disabled={busy}
                      id="library-family-registered-format"
                      onChange={(event) => {
                        setRegisteredFormatQuery(event.target.value);
                        setActiveRegisteredFormatIndex(-1);
                        setExtensionError(null);
                      }}
                      onFocus={() => setRegisteredFormatFocused(true)}
                      onKeyDown={handleRegisteredFormatKeyDown}
                      placeholder={t("policy.libraryUpload.families.findRegisteredPlaceholder", {
                        defaultValue: "Extension or format name",
                      })}
                      role="combobox"
                      value={registeredFormatQuery}
                    />
                  </label>
                  <button
                    className="icon-button library-family-custom-format-add"
                    disabled={busy || draftCustomFormats.length >= MAX_LIBRARY_CUSTOM_FORMATS}
                    onClick={() => beginCustomFormat()}
                    type="button"
                  >
                    {t("policy.libraryUpload.families.customFormat.addAction", {
                      defaultValue: "Register custom format",
                    })}
                  </button>
                  <button
                    aria-controls={customFormatListId}
                    aria-expanded={customFormatListOpen}
                    className="icon-button text-button library-family-custom-format-manage"
                    disabled={busy || draftCustomFormats.length === 0}
                    onClick={() => setCustomFormatListOpen((open) => !open)}
                    type="button"
                  >
                    {t("policy.libraryUpload.families.customFormat.manageAction", {
                      defaultValue: "Manage custom formats ({{count}})",
                      count: draftCustomFormats.length,
                    })}
                  </button>
                </div>
                {registeredFormatFocused &&
                  (registeredFormatSuggestions.length > 0 || unknownRegisteredExtension) ? (
                  <div
                    className="library-family-registered-format-results"
                    id={listboxId}
                    ref={registeredFormatListboxRef}
                    role="listbox"
                  >
                    {registeredFormatSuggestions.map((format, index) => (
                      <button
                        aria-selected={index === activeRegisteredFormatIndex}
                        className={index === activeRegisteredFormatIndex ? "active" : ""}
                        id={`${listboxId}-${format.format_id}`}
                        key={format.format_id}
                        onClick={() => addFormat(format)}
                        role="option"
                        tabIndex={-1}
                        type="button"
                      >
                        <strong>{format.display_name}</strong>
                        <span>{format.extensions.join(" · ")}</span>
                      </button>
                    ))}
                    {unknownRegisteredExtension ? (
                      <button
                        aria-selected={activeRegisteredFormatIndex === registeredFormatSuggestions.length}
                        className={activeRegisteredFormatIndex === registeredFormatSuggestions.length
                          ? "library-family-registered-format-custom-option active"
                          : "library-family-registered-format-custom-option"}
                        id={`${listboxId}-register-custom`}
                        onClick={() => beginCustomFormat(unknownRegisteredExtension)}
                        role="option"
                        tabIndex={-1}
                        type="button"
                      >
                        <strong>
                          {t("policy.libraryUpload.families.findRegisteredCustomOption", {
                            defaultValue: "Register '{{extension}}' as a new format…",
                            extension: unknownRegisteredExtension,
                          })}
                        </strong>
                      </button>
                    ) : null}
                  </div>
                ) : null}
              </div>
            )}
            {extensionError ? (
              <p className="field-error" role="alert">{extensionError}</p>
            ) : null}
          </div>

          {customFormatListOpen && !customFormatEditorOpen ? (
            <div
              aria-label={t("policy.libraryUpload.families.customFormat.integratedListLabel", {
                defaultValue: "Registered custom formats",
              })}
              className="library-family-add-formats-catalog"
              id={customFormatListId}
              role="list"
            >
              {draftCustomFormats.map((format) => {
                const selected = draft.format_ids.includes(format.format_id);
                const ownerFamilies = projectedFamilies.filter((family) =>
                  family.format_ids.includes(format.format_id)
                );
                const conflict = familyConflict(format.format_id, families, draft.family_id);
                const hasIndividualLimit = formatOverrideIds.includes(format.format_id);
                const removalBlocked = ownerFamilies.length > 0 || hasIndividualLimit;
                return (
                  <article className="library-custom-format-row" key={format.format_id} role="listitem">
                    <div className="library-custom-format-primary">
                      <strong>{format.display_name}</strong>
                      <small>{format.extensions.join(" · ")}</small>
                      {ownerFamilies.length > 0 ? (
                        <small>
                          {t("policy.libraryUpload.families.customFormat.usedBy", {
                            defaultValue: "Used by {{families}}",
                            families: ownerFamilies.map((family) => family.name).join(", "),
                          })}
                        </small>
                      ) : hasIndividualLimit ? (
                        <small>
                          {t("policy.libraryUpload.families.customFormat.hasIndividualLimit", {
                            defaultValue: "Remove its individual format limit before deleting it.",
                          })}
                        </small>
                      ) : null}
                    </div>
                    <div className="library-custom-format-row-actions">
                      {selected ? (
                        <span className="library-family-custom-format-selected">
                          {t("policy.libraryUpload.families.customFormat.selected", {
                            defaultValue: "In this family",
                          })}
                        </span>
                      ) : (
                        <button
                          aria-label={t("policy.libraryUpload.families.customFormat.addToFamilyLabel", {
                            defaultValue: "Add {{format}} to this family",
                            format: format.display_name,
                          })}
                          className="icon-button text-button"
                          disabled={busy || customFormatEditorOpen || Boolean(conflict)}
                          onClick={() => addFormat(customFormatOption(format))}
                          type="button"
                        >
                          {t("policy.libraryUpload.families.customFormat.addToFamily", {
                            defaultValue: "Add to family",
                          })}
                        </button>
                      )}
                      <button
                        aria-label={t("policy.libraryUpload.families.customFormat.editLabel", {
                          defaultValue: "Edit {{format}}",
                          format: format.display_name,
                        })}
                        className="icon-button text-button"
                        disabled={busy || customFormatEditorOpen}
                        onClick={() => beginCustomFormat("", format.format_id)}
                        type="button"
                      >
                        {t("actions.edit", { defaultValue: "Edit" })}
                      </button>
                      <button
                        aria-label={t("policy.libraryUpload.families.customFormat.deleteLabel", {
                          defaultValue: "Delete {{format}}",
                          format: format.display_name,
                        })}
                        className="icon-button danger"
                        disabled={busy || customFormatEditorOpen || removalBlocked}
                        onClick={() => removeDraftCustomFormat(format.format_id)}
                        type="button"
                      >
                        {t("policy.libraryUpload.families.customFormat.delete", {
                          defaultValue: "Delete format",
                        })}
                      </button>
                    </div>
                  </article>
                );
              })}

              {hasNewCustomFormats || (customFormatsDirty && (
                familyDraftDirty || builtinFormatOverridesDirty
              )) ? (
                <p className="meta-line" role="status">
                  {hasNewCustomFormats
                    ? t("policy.libraryUpload.families.customFormat.newRequiresFamily", {
                        defaultValue: "New custom formats must be saved with this family. Add/update the family to keep them.",
                      })
                    : t("policy.libraryUpload.families.customFormat.familyChangesRequireSave", {
                        defaultValue: "Family changes are also pending. Add/update the family to apply both changes together.",
                      })}
                </p>
              ) : null}
              <div className="library-family-custom-format-apply">
                <p>{t("policy.libraryUpload.families.customFormat.applyHint", {
                  defaultValue: "Apply edits or deletions to pending Settings changes without adding or updating this family.",
                })}</p>
                <button
                  className="icon-button text-button"
                  disabled={
                    busy || customFormatEditorOpen || !customFormatsDirty ||
                    blockedAliasGroupIds.size > 0 ||
                    Boolean(presetAliasConflict) ||
                    hasNewCustomFormats || familyDraftDirty || builtinFormatOverridesDirty
                  }
                  onClick={() => onApplyCustomFormats(draftCustomFormats)}
                  type="button"
                >
                  {t("policy.libraryUpload.families.customFormat.apply", {
                    defaultValue: "Apply format changes",
                  })}
                </button>
              </div>
            </div>
          ) : null}

          {!customFormatEditorOpen || presetAliasConflict ? (
            <p
              aria-live="polite"
              className={presetAliasConflict ? "field-error" : "meta-line"}
              id={errorId}
              role={presetAliasConflict ? "alert" : undefined}
            >
              {presetAliasConflict?.message ?? t("policy.libraryUpload.families.aliasHint", {
                defaultValue: "Registered aliases add existing formats; unknown aliases register Workspace custom formats.",
              })}
            </p>
          ) : null}
          <div
            className="library-family-alias-list-heading"
            data-family-editor-section="format-alias-groups"
          >
            <strong id={aliasGroupsTitleId}>
              {t("policy.libraryUpload.families.aliasGroupsLabel", {
                defaultValue: "Format alias groups",
              })}
            </strong>
            <small>
              {t("policy.libraryUpload.families.aliasGroupsHint", {
                defaultValue: "Every format added to this Family appears here. Preset members are marked; custom names and all extension aliases are editable. Alias changes apply Workspace-wide.",
              })}
            </small>
          </div>
          <FormatAliasGroupList
            baselineFormats={baselineFormatOptions}
            busy={busy}
            customFormats={draftCustomFormats}
            formats={availableFormats}
            key={aliasEditorResetVersion}
            onInteractionStateChange={handleAliasInteractionStateChange}
            onRemoveFormat={(formatId) => {
              setDraft((current) => ({
                ...customizeFamily(current),
                format_ids: current.format_ids.filter((candidate) => candidate !== formatId),
              }));
              if (!initialCustomFormatIds.has(formatId)) {
                setDraftCustomFormats((current) => current.filter(
                  (format) => format.format_id !== formatId,
                ));
              }
            }}
            onUpdateBuiltinFormat={updateBuiltinFormat}
            onUpdateCustomFormat={(format) => {
              setDraftCustomFormats((current) => current.map((candidate) =>
                candidate.format_id === format.format_id ? format : candidate
              ));
            }}
            presetFormatIds={startKind === "preset"
              ? presetBaseline?.format_ids ?? []
              : []}
            selectedFormats={selectedFormats}
            stableCustomOwners={stableCustomOwners}
          />
        </div>

        {formatConflicts.length > 0 ? (
          <div className="library-family-conflicts" role="alert">
            <strong>{t("policy.libraryUpload.families.errors.conflictTitle", {
              defaultValue: "Resolve duplicate formats before saving",
            })}</strong>
            {formatConflicts.map((conflict) => (
              <button
                className="text-button"
                key={`${conflict.familyId}-${conflict.formatId}`}
                onClick={() => onRevealConflict(conflict.familyId)}
                type="button"
              >
                {t("policy.libraryUpload.families.errors.conflictFamily", {
                  defaultValue: "Already used by {{family}}",
                  family: conflict.familyName,
                })}
              </button>
            ))}
          </div>
        ) : null}

        {atFamilyLimit ? (
          <p className="field-error" role="alert">
            {t("policy.libraryUpload.families.errors.familyLimit", {
              defaultValue: "This MVP supports up to {{count}} format families.",
              count: MAX_LIBRARY_FORMAT_FAMILIES,
            })}
          </p>
        ) : null}

      </form>
    </Dialog>
  );
}

function cloneFamily(family: LibraryPolicyFormatFamily): LibraryPolicyFormatFamily {
  return {
    ...family,
    preset_baseline: family.preset_baseline ? {
      ...family.preset_baseline,
      format_ids: [...family.preset_baseline.format_ids],
      format_extensions: cloneFormatExtensions(family.preset_baseline.format_extensions),
    } : null,
    format_ids: [...family.format_ids],
  };
}

function localizedPresetFamily(
  preset: LibraryFormatFamilyPreset,
  t: TFunction,
) {
  return {
    ...familyFromPreset(preset),
    name: t(`policy.libraryUpload.familyPresets.names.${preset.preset_id}`, {
      defaultValue: preset.label,
    }),
  };
}

type DialogPresetBaseline = NonNullable<LibraryPolicyFormatFamily["preset_baseline"]>;

function presetBaselineFromFamily(
  family: LibraryPolicyFormatFamily | null,
): DialogPresetBaseline | null {
  if (!family) return null;
  const persisted = family.preset_baseline;
  if (persisted) {
    return {
      preset_id: persisted.preset_id,
      preset_revision: persisted.preset_revision,
      format_ids: [...persisted.format_ids],
      format_extensions: cloneFormatExtensions(persisted.format_extensions),
    };
  }
  if (family.origin !== "preset" || !family.preset_id || !family.preset_revision) return null;
  return {
    preset_id: family.preset_id,
    preset_revision: family.preset_revision,
    format_ids: [...family.format_ids],
    format_extensions: {},
  };
}

function presetBaselineFromPreset(preset: LibraryFormatFamilyPreset): DialogPresetBaseline {
  return {
    preset_id: preset.preset_id,
    preset_revision: preset.revision,
    format_ids: [...preset.format_ids],
    format_extensions: cloneFormatExtensions(preset.format_extensions),
  };
}

function withPresetBaseline(
  family: LibraryPolicyFormatFamily,
  baseline: DialogPresetBaseline | null,
): LibraryPolicyFormatFamily {
  return {
    ...family,
    preset_baseline: baseline ? {
      ...baseline,
      format_ids: [...baseline.format_ids],
      format_extensions: cloneFormatExtensions(baseline.format_extensions),
    } : null,
  };
}

function applyPresetAliases(
  source: LibraryPolicyBuiltinFormatOverrides,
  baseline: DialogPresetBaseline,
  formats: LibraryFormatCapability[],
) {
  const next = cloneBuiltinFormatOverrides(source);
  for (const formatId of baseline.format_ids) {
    if (formatId.startsWith("custom-")) continue;
    const format = formats.find((candidate) => candidate.format_id === formatId);
    const baselineExtensions = baseline.format_extensions[formatId];
    if (!format || !baselineExtensions?.length) continue;
    const defaults = format.default_extensions ?? format.extensions;
    if (sameStringSet(baselineExtensions, defaults)) {
      delete next[formatId];
    } else {
      next[formatId] = { extensions: [...baselineExtensions] };
    }
  }
  return next;
}

function projectEffectiveFormatOptions(
  formats: LibraryFormatCapability[],
  overrides: LibraryPolicyBuiltinFormatOverrides,
  customFormats: LibraryPolicyCustomFormat[],
) {
  const byId = new Map<string, LibraryFamilyFormatOption>(
    formats
      .filter((format) => !format.format_id.startsWith("custom-"))
      .map((format) => {
        const override = overrides[format.format_id];
        return [format.format_id, {
          ...format,
          extensions: [...(
            override?.extensions ?? format.default_extensions ?? format.extensions
          )],
          extensions_overridden: Boolean(override),
        }] as const;
      }),
  );
  for (const format of customFormats) {
    byId.set(format.format_id, customFormatOption(format));
  }
  return [...byId.values()];
}

function findStablePresetAliasConflict(
  formatExtensions: Record<string, string[]>,
  stableFormats: LibraryFamilyFormatOption[],
  packageFormats: LibraryFormatCapability[],
) {
  for (const [targetFormatId, extensions] of Object.entries(formatExtensions)) {
    if (targetFormatId.startsWith("custom-")) continue;
    for (const rawExtension of extensions) {
      const extension = normalizeExtensionInput(rawExtension);
      if (!extension) continue;
      const targetAlreadyOwnedExtension = stableFormats.some((candidate) =>
        candidate.format_id === targetFormatId &&
        candidate.extensions.some(
          (candidateExtension) => normalizeExtensionInput(candidateExtension) === extension,
        )
      );
      const owner = stableFormats.find((candidate) =>
        candidate.format_id !== targetFormatId &&
        candidate.extensions.some(
          (candidateExtension) => normalizeExtensionInput(candidateExtension) === extension,
        ) &&
        !(
          targetAlreadyOwnedExtension &&
          isPackageOwnedAmbiguity(
            extension,
            targetFormatId,
            candidate.format_id,
            packageFormats,
          )
        )
      );
      if (owner) return { extension, owner };
    }
  }
  return null;
}

function findEffectiveAliasConflict(
  formats: LibraryFamilyFormatOption[],
  packageFormats: LibraryFormatCapability[],
) {
  const owners = new Map<string, LibraryFamilyFormatOption[]>();
  for (const format of formats) {
    for (const rawExtension of format.extensions) {
      const extension = normalizeExtensionInput(rawExtension);
      if (!extension) continue;
      const current = owners.get(extension) ?? [];
      if (!current.some((candidate) => candidate.format_id === format.format_id)) {
        current.push(format);
        owners.set(extension, current);
      }
    }
  }
  for (const [extension, extensionOwners] of owners) {
    for (let index = 0; index < extensionOwners.length; index += 1) {
      for (let otherIndex = index + 1; otherIndex < extensionOwners.length; otherIndex += 1) {
        const owner = extensionOwners[index];
        const otherOwner = extensionOwners[otherIndex];
        if (!isPackageOwnedAmbiguity(
          extension,
          owner.format_id,
          otherOwner.format_id,
          packageFormats,
        )) {
          return { extension, owner: otherOwner };
        }
      }
    }
  }
  return null;
}

function isPackageOwnedAmbiguity(
  extension: string,
  leftFormatId: string,
  rightFormatId: string,
  formats: LibraryFormatCapability[],
) {
  const ownsPackageAlias = (formatId: string) => {
    if (formatId.startsWith("custom-")) return false;
    const format = formats.find((candidate) => candidate.format_id === formatId);
    return Boolean(format && (format.default_extensions ?? format.extensions).some(
      (candidate) => normalizeExtensionInput(candidate) === extension,
    ));
  };
  return ownsPackageAlias(leftFormatId) && ownsPackageAlias(rightFormatId);
}

function sameStringSet(left: string[], right: string[]) {
  if (left.length !== right.length) return false;
  const rightSet = new Set(right);
  return left.every((value) => rightSet.has(value));
}

function cloneBuiltinFormatOverrides(
  overrides: LibraryPolicyBuiltinFormatOverrides,
): LibraryPolicyBuiltinFormatOverrides {
  return Object.fromEntries(
    Object.entries(overrides).map(([formatId, override]) => [
      formatId,
      { extensions: [...override.extensions] },
    ]),
  );
}

function cloneFormatExtensions(value: Record<string, string[]>) {
  return Object.fromEntries(
    Object.entries(value).map(([formatId, extensions]) => [formatId, [...extensions]]),
  );
}

function formatsForExtension(
  value: string,
  formats: LibraryFamilyFormatOption[],
) {
  const extension = normalizeExtensionInput(value);
  return extension
    ? formats.filter((format) => format.extensions.some(
        (candidate) => normalizeExtensionInput(candidate) === extension,
      ))
    : [];
}

function aliasGroupInputErrorMessage(error: AliasGroupInputError, t: TFunction) {
  const key = `policy.libraryUpload.families.inputErrors.${error.code}`;
  const defaults: Record<AliasGroupInputError["code"], string> = {
    duplicate_extension: "{{value}} appears more than once. Keep each extension in one alias group.",
    duplicate_label: "{{value}} is used by more than one alias group.",
    empty_input: "Enter at least one extension.",
    invalid_extension: "{{value}} is not a valid extension. Use letters, numbers, _, +, or -.",
    invalid_label: "Enter a valid alias group name.",
    invalid_syntax: "Use comma-separated extensions or [name](.ext1,.ext2).",
    too_many_extensions: "One alias group can contain up to {{count}} extensions.",
  };
  return t(key, {
    defaultValue: defaults[error.code],
    count: MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS,
    value: error.value ?? "",
  });
}

function formatByteLimit(bytes: number) {
  if (bytes >= 1_073_741_824) return `${Number((bytes / 1_073_741_824).toFixed(2))} GiB`;
  if (bytes >= 1_000_000) return `${Number((bytes / 1_000_000).toFixed(2))} MB`;
  if (bytes >= 1_000) return `${Number((bytes / 1_000).toFixed(2))} KB`;
  return `${bytes} B`;
}
