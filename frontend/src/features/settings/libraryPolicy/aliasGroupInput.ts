import {
  invalidCustomFormatExtensions,
  MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS,
  MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH,
  normalizeCustomFormatName,
  normalizeExtensionInput,
  validCustomFormatName,
} from "./model";

export type AliasGroupInputGroup = {
  label: string;
  extensions: string[];
};

export type AliasGroupInputErrorCode =
  | "empty_input"
  | "invalid_syntax"
  | "invalid_label"
  | "invalid_extension"
  | "duplicate_label"
  | "duplicate_extension"
  | "too_many_extensions";

export type AliasGroupInputError = {
  code: AliasGroupInputErrorCode;
  value?: string;
  groupIndex?: number;
  extensionIndex?: number;
  position?: number;
};

export type AliasGroupInputResult =
  | {
    ok: true;
    groups: AliasGroupInputGroup[];
    explicit: boolean;
  }
  | {
    ok: false;
    error: AliasGroupInputError;
  };

type ParseFailure = Extract<AliasGroupInputResult, { ok: false }>;

/**
 * Parses either a plain comma-separated extension list or the explicit
 * `[label](.one,.two), [other](.three)` interchange syntax.
 *
 * Plain input always becomes one group whose label is derived from its first
 * normalized extension. The parser never returns a partially parsed result.
 */
export function parseAliasGroupInput(rawValue: string): AliasGroupInputResult {
  const value = rawValue.trim();
  if (!value) return failure("empty_input");

  if (value.startsWith("[")) return parseExplicitGroups(value);
  if (containsExplicitSyntax(value)) {
    return failure("invalid_syntax", { value, position: firstSyntaxPosition(value) });
  }

  const extensions = parseExtensions(value.split(","), 0);
  if (!extensions.ok) return extensions;

  return {
    ok: true,
    explicit: false,
    groups: [{
      label: extensions.extensions[0].slice(1),
      extensions: extensions.extensions,
    }],
  };
}

/** Serializes groups to the canonical copy-and-paste representation. */
export function serializeAliasGroups(groups: readonly AliasGroupInputGroup[]) {
  return groups.map((group) => {
    const label = escapeLabel(normalizeCustomFormatName(group.label));
    const extensions = group.extensions.map(normalizeExtensionInput);
    return `[${label}](${extensions.join(",")})`;
  }).join(", ");
}

function parseExplicitGroups(value: string): AliasGroupInputResult {
  const groups: AliasGroupInputGroup[] = [];
  const seenLabels = new Set<string>();
  const seenExtensions = new Set<string>();
  let cursor = 0;

  while (cursor < value.length) {
    cursor = skipWhitespace(value, cursor);
    const groupIndex = groups.length;
    if (value[cursor] !== "[") {
      return failure("invalid_syntax", { position: cursor, groupIndex });
    }

    const labelResult = readLabel(value, cursor + 1);
    if (!labelResult.ok) {
      return failure("invalid_syntax", { position: labelResult.position, groupIndex });
    }
    const label = normalizeCustomFormatName(labelResult.value);
    if (
      !label ||
      label.length > MAX_LIBRARY_CUSTOM_FORMAT_NAME_LENGTH ||
      !validCustomFormatName(label)
    ) {
      return failure("invalid_label", { value: labelResult.value, groupIndex });
    }
    const labelKey = label.toLocaleLowerCase();
    if (seenLabels.has(labelKey)) {
      return failure("duplicate_label", { value: label, groupIndex });
    }

    cursor = skipWhitespace(value, labelResult.next);
    if (value[cursor] !== "(") {
      return failure("invalid_syntax", { position: cursor, groupIndex });
    }

    const extensionsEnd = findClosingParenthesis(value, cursor + 1);
    if (!extensionsEnd.ok) {
      return failure("invalid_syntax", { position: extensionsEnd.position, groupIndex });
    }
    const extensionSource = value.slice(cursor + 1, extensionsEnd.position);
    const extensionsResult = parseExtensions(extensionSource.split(","), groupIndex);
    if (!extensionsResult.ok) return extensionsResult;

    for (let extensionIndex = 0; extensionIndex < extensionsResult.extensions.length; extensionIndex += 1) {
      const extension = extensionsResult.extensions[extensionIndex];
      if (seenExtensions.has(extension)) {
        return failure("duplicate_extension", {
          value: extension,
          groupIndex,
          extensionIndex,
        });
      }
      seenExtensions.add(extension);
    }

    groups.push({ label, extensions: extensionsResult.extensions });
    seenLabels.add(labelKey);
    cursor = skipWhitespace(value, extensionsEnd.position + 1);
    if (cursor === value.length) break;
    if (value[cursor] !== ",") {
      return failure("invalid_syntax", { position: cursor, groupIndex });
    }
    cursor = skipWhitespace(value, cursor + 1);
    if (cursor === value.length || value[cursor] !== "[") {
      return failure("invalid_syntax", { position: cursor, groupIndex: groupIndex + 1 });
    }
  }

  return { ok: true, groups, explicit: true };
}

function parseExtensions(
  rawExtensions: string[],
  groupIndex: number,
): ParseFailure | { ok: true; extensions: string[] } {
  if (rawExtensions.length > MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS) {
    return failure("too_many_extensions", { groupIndex });
  }

  const extensions: string[] = [];
  const seen = new Set<string>();
  for (let extensionIndex = 0; extensionIndex < rawExtensions.length; extensionIndex += 1) {
    const rawExtension = rawExtensions[extensionIndex];
    const extension = normalizeExtensionInput(rawExtension);
    if (!extension || invalidCustomFormatExtensions([extension]).length > 0) {
      return failure("invalid_extension", {
        value: rawExtension.trim(),
        groupIndex,
        extensionIndex,
      });
    }
    if (seen.has(extension)) {
      return failure("duplicate_extension", { value: extension, groupIndex, extensionIndex });
    }
    seen.add(extension);
    extensions.push(extension);
  }
  return { ok: true, extensions };
}

function readLabel(
  value: string,
  start: number,
): { ok: true; value: string; next: number } | { ok: false; position: number } {
  let label = "";
  let cursor = start;
  while (cursor < value.length) {
    const character = value[cursor];
    if (character === "]") {
      return { ok: true, value: label, next: cursor + 1 };
    }
    if (character === "\\") {
      if (cursor + 1 >= value.length) return { ok: false, position: cursor };
      label += value[cursor + 1];
      cursor += 2;
      continue;
    }
    label += character;
    cursor += 1;
  }
  return { ok: false, position: value.length };
}

function findClosingParenthesis(
  value: string,
  start: number,
): { ok: true; position: number } | { ok: false; position: number } {
  for (let cursor = start; cursor < value.length; cursor += 1) {
    if (value[cursor] === "(") return { ok: false, position: cursor };
    if (value[cursor] === ")") return { ok: true, position: cursor };
  }
  return { ok: false, position: value.length };
}

function skipWhitespace(value: string, start: number) {
  let cursor = start;
  while (cursor < value.length && /\s/.test(value[cursor])) cursor += 1;
  return cursor;
}

function failure(
  code: AliasGroupInputErrorCode,
  details: Omit<AliasGroupInputError, "code"> = {},
): ParseFailure {
  return { ok: false, error: { code, ...details } };
}

function escapeLabel(value: string) {
  return value.replace(/\\/g, "\\\\").replace(/]/g, "\\]");
}

function firstSyntaxPosition(value: string) {
  for (let index = 0; index < value.length; index += 1) {
    if ("[]()".includes(value[index])) return index;
  }
  return 0;
}

function containsExplicitSyntax(value: string) {
  return firstSyntaxPosition(value) > 0 || "[]()".includes(value[0]);
}
