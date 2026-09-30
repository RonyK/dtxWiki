export const EDITOR_COMMAND_KINDS = [
  "text-mutation",
  "modal-mutation",
  "async-attachment",
  "external-effect",
] as const;

export type EditorCommandKind = (typeof EDITOR_COMMAND_KINDS)[number];
export type EditorCommandSurface =
  | "action-bar"
  | "floating-toolbar"
  | "shortcut"
  | "slash";
export type EditorMode = "source" | "reading" | "split";
export type EditorCommandState = "inactive" | "active" | "mixed";
export type EditorCommandGroup = "history" | "format" | "insert" | "block" | "more";

export type TextMutationCommandId =
  | "heading"
  | "bold"
  | "italic"
  | "strikethrough"
  | "inline-code"
  | "wikilink"
  | "citation"
  | "code-block"
  | "ordered-list"
  | "bulleted-list"
  | "task-list"
  | "quote";
export type ModalMutationCommandId = "link" | "emoji" | "math" | "table";
export type AsyncAttachmentCommandId = "image" | "attach";
export type ExternalEffectCommandId = "undo" | "redo" | "comment";
export type EditorCommandId =
  | TextMutationCommandId
  | ModalMutationCommandId
  | AsyncAttachmentCommandId
  | ExternalEffectCommandId;

export type EditorShortcut = {
  key: string;
  modifiers: readonly ("Alt" | "Mod" | "Shift")[];
  display: string;
};

export type EditorActionBarPlacement = {
  desktop: "primary" | "overflow";
  mobile: "primary" | "overflow";
  group: EditorCommandGroup;
  order: number;
  shortLabel?: string;
};

type EditorCommandBase<TId extends EditorCommandId, TKind extends EditorCommandKind> = {
  id: TId;
  kind: TKind;
  labelKey: string;
  fallbackLabel: string;
  modes: readonly EditorMode[];
  surfaces: readonly EditorCommandSurface[];
  shortcut?: EditorShortcut;
  actionBar?: EditorActionBarPlacement;
};

export type TextMutationCommand = EditorCommandBase<
  TextMutationCommandId,
  "text-mutation"
> & {
  mutation: TextMutationCommandId;
};

export type ModalMutationCommand = EditorCommandBase<
  ModalMutationCommandId,
  "modal-mutation"
> & {
  modal: "emoji-picker" | "link-dialog" | "math-dialog" | "table-dialog";
};

export type AsyncAttachmentCommand = EditorCommandBase<
  AsyncAttachmentCommandId,
  "async-attachment"
> & {
  attachment: "files" | "image";
};

export type ExternalEffectCommand = EditorCommandBase<
  ExternalEffectCommandId,
  "external-effect"
> & {
  effect: "comment" | "redo" | "undo";
};

export type EditorCommand =
  | TextMutationCommand
  | ModalMutationCommand
  | AsyncAttachmentCommand
  | ExternalEffectCommand;

const EDITING_MODES = ["source", "split"] as const;

const shortcut = (
  display: string,
  key: string,
  ...modifiers: EditorShortcut["modifiers"]
): EditorShortcut => ({ display, key, modifiers });

const actionBar = (
  order: number,
  group: EditorCommandGroup,
  shortLabel: string,
  mobile: EditorActionBarPlacement["mobile"] = "overflow",
  desktop: EditorActionBarPlacement["desktop"] = "primary",
): EditorActionBarPlacement => ({ desktop, group, mobile, order, shortLabel });

export const EDITOR_COMMANDS = [
  {
    id: "undo",
    kind: "external-effect",
    effect: "undo",
    labelKey: "editor.commands.undo",
    fallbackLabel: "Undo",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "shortcut"],
    shortcut: shortcut("Ctrl/Cmd+Z", "z", "Mod"),
    actionBar: actionBar(10, "history", "Undo"),
  },
  {
    id: "redo",
    kind: "external-effect",
    effect: "redo",
    labelKey: "editor.commands.redo",
    fallbackLabel: "Redo",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "shortcut"],
    shortcut: shortcut("Ctrl/Cmd+Shift+Z", "z", "Mod", "Shift"),
    actionBar: actionBar(20, "history", "Redo"),
  },
  {
    id: "heading",
    kind: "text-mutation",
    mutation: "heading",
    labelKey: "editor.commands.heading",
    fallbackLabel: "Heading",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Alt+2", "2", "Mod", "Alt"),
    actionBar: actionBar(30, "format", "H", "primary"),
  },
  {
    id: "bold",
    kind: "text-mutation",
    mutation: "bold",
    labelKey: "editor.commands.bold",
    fallbackLabel: "Bold",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "floating-toolbar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+B", "b", "Mod"),
    actionBar: actionBar(40, "format", "B", "primary"),
  },
  {
    id: "italic",
    kind: "text-mutation",
    mutation: "italic",
    labelKey: "editor.commands.italic",
    fallbackLabel: "Italic",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "floating-toolbar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+I", "i", "Mod"),
    actionBar: actionBar(50, "format", "I"),
  },
  {
    id: "strikethrough",
    kind: "text-mutation",
    mutation: "strikethrough",
    labelKey: "editor.commands.strikethrough",
    fallbackLabel: "Strikethrough",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "floating-toolbar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Shift+X", "x", "Mod", "Shift"),
    actionBar: actionBar(60, "format", "S"),
  },
  {
    id: "inline-code",
    kind: "text-mutation",
    mutation: "inline-code",
    labelKey: "editor.commands.inlineCode",
    fallbackLabel: "Inline code",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "floating-toolbar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Shift+.", ".", "Mod", "Shift"),
    actionBar: actionBar(70, "format", "Code"),
  },
  {
    id: "link",
    kind: "modal-mutation",
    modal: "link-dialog",
    labelKey: "editor.commands.link",
    fallbackLabel: "Link",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "floating-toolbar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+K", "k", "Mod"),
    actionBar: actionBar(80, "insert", "Link", "primary"),
  },
  {
    id: "image",
    kind: "async-attachment",
    attachment: "image",
    labelKey: "editor.commands.image",
    fallbackLabel: "Image",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "slash"],
    actionBar: actionBar(90, "insert", "Image"),
  },
  {
    id: "attach",
    kind: "async-attachment",
    attachment: "files",
    labelKey: "editor.commands.attachFiles",
    fallbackLabel: "Attach files",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "slash"],
    actionBar: actionBar(100, "insert", "Attach", "primary"),
  },
  {
    id: "bulleted-list",
    kind: "text-mutation",
    mutation: "bulleted-list",
    labelKey: "editor.commands.bulletedList",
    fallbackLabel: "Bulleted list",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Shift+8", "8", "Mod", "Shift"),
    actionBar: actionBar(110, "block", "List"),
  },
  {
    id: "task-list",
    kind: "text-mutation",
    mutation: "task-list",
    labelKey: "editor.commands.taskList",
    fallbackLabel: "Task list",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Shift+9", "9", "Mod", "Shift"),
    actionBar: actionBar(120, "block", "Task"),
  },
  {
    id: "quote",
    kind: "text-mutation",
    mutation: "quote",
    labelKey: "editor.commands.quote",
    fallbackLabel: "Quote",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "slash"],
    actionBar: actionBar(130, "block", "Quote"),
  },
  {
    id: "table",
    kind: "modal-mutation",
    modal: "table-dialog",
    labelKey: "editor.commands.table",
    fallbackLabel: "Table",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "slash"],
    actionBar: actionBar(140, "block", "Table"),
  },
  {
    id: "ordered-list",
    kind: "text-mutation",
    mutation: "ordered-list",
    labelKey: "editor.commands.orderedList",
    fallbackLabel: "Numbered list",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Shift+7", "7", "Mod", "Shift"),
    actionBar: actionBar(150, "more", "Numbered", "overflow", "overflow"),
  },
  {
    id: "code-block",
    kind: "text-mutation",
    mutation: "code-block",
    labelKey: "editor.commands.codeBlock",
    fallbackLabel: "Code block",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Shift+K", "k", "Mod", "Shift"),
    actionBar: actionBar(160, "more", "Code block", "overflow", "overflow"),
  },
  {
    id: "wikilink",
    kind: "text-mutation",
    mutation: "wikilink",
    labelKey: "editor.commands.wikilink",
    fallbackLabel: "Wikilink",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "floating-toolbar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Shift+L", "l", "Mod", "Shift"),
    actionBar: actionBar(170, "more", "Wikilink", "overflow", "overflow"),
  },
  {
    id: "citation",
    kind: "text-mutation",
    mutation: "citation",
    labelKey: "editor.commands.citation",
    fallbackLabel: "Citation",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "floating-toolbar", "shortcut", "slash"],
    shortcut: shortcut("Ctrl/Cmd+Shift+C", "c", "Mod", "Shift"),
    actionBar: actionBar(180, "more", "Citation", "overflow", "overflow"),
  },
  {
    id: "emoji",
    kind: "modal-mutation",
    modal: "emoji-picker",
    labelKey: "editor.commands.emoji",
    fallbackLabel: "Emoji",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "slash"],
    actionBar: actionBar(190, "more", "Emoji", "overflow", "overflow"),
  },
  {
    id: "math",
    kind: "modal-mutation",
    modal: "math-dialog",
    labelKey: "editor.commands.math",
    fallbackLabel: "Math",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "slash"],
    actionBar: actionBar(200, "more", "Math", "overflow", "overflow"),
  },
  {
    id: "comment",
    kind: "external-effect",
    effect: "comment",
    labelKey: "editor.commands.comment",
    fallbackLabel: "Comment",
    modes: EDITING_MODES,
    surfaces: ["action-bar", "floating-toolbar", "shortcut"],
    shortcut: shortcut("Ctrl/Cmd+Shift+M", "m", "Mod", "Shift"),
    actionBar: actionBar(210, "more", "Comment", "overflow", "overflow"),
  },
] as const satisfies readonly EditorCommand[];

export const EDITOR_COMMAND_SURFACE_REQUIREMENTS = {
  "action-bar": [
    "heading",
    "bold",
    "italic",
    "strikethrough",
    "inline-code",
    "link",
    "image",
    "attach",
    "bulleted-list",
    "ordered-list",
    "task-list",
    "quote",
    "code-block",
    "table",
  ],
  "floating-toolbar": [
    "bold",
    "italic",
    "strikethrough",
    "inline-code",
    "link",
    "wikilink",
    "citation",
    "comment",
  ],
  shortcut: [
    "bold",
    "italic",
    "strikethrough",
    "inline-code",
    "code-block",
    "ordered-list",
    "bulleted-list",
    "task-list",
    "link",
    "wikilink",
    "citation",
    "comment",
  ],
  slash: [
    "heading",
    "bold",
    "italic",
    "strikethrough",
    "inline-code",
    "code-block",
    "ordered-list",
    "bulleted-list",
    "task-list",
    "link",
    "wikilink",
    "citation",
    "image",
    "attach",
    "table",
    "emoji",
    "math",
  ],
} as const satisfies Record<EditorCommandSurface, readonly EditorCommandId[]>;

export function editorCommandById(
  id: EditorCommandId,
  commands: readonly EditorCommand[] = EDITOR_COMMANDS,
) {
  return commands.find((command) => command.id === id);
}

export function isTextMutationCommand(
  command: EditorCommand,
): command is TextMutationCommand {
  return command.kind === "text-mutation";
}

export function editorShortcutKey(shortcutValue: EditorShortcut) {
  const modifierOrder: Record<EditorShortcut["modifiers"][number], number> = {
    Mod: 0,
    Alt: 1,
    Shift: 2,
  };
  const modifiers = [...shortcutValue.modifiers]
    .sort((left, right) => modifierOrder[left] - modifierOrder[right])
    .join("+");
  return `${modifiers}+${shortcutValue.key.toLocaleLowerCase()}`;
}

export function editorShortcutCodeMirrorKey(shortcutValue: EditorShortcut) {
  const modifierOrder: Record<EditorShortcut["modifiers"][number], number> = {
    Mod: 0,
    Alt: 1,
    Shift: 2,
  };
  return [
    ...[...shortcutValue.modifiers]
      .sort((left, right) => modifierOrder[left] - modifierOrder[right]),
    shortcutValue.key,
  ].join("-");
}

export function validateEditorCommandRegistry(
  commands: readonly EditorCommand[] = EDITOR_COMMANDS,
) {
  const errors: string[] = [];
  const ids = new Set<string>();
  const shortcuts = new Map<string, string>();

  for (const command of commands) {
    if (ids.has(command.id)) {
      errors.push(`Duplicate command id: ${command.id}`);
    }
    ids.add(command.id);

    if (new Set(command.surfaces).size !== command.surfaces.length) {
      errors.push(`Duplicate surface on command: ${command.id}`);
    }
    if (command.surfaces.includes("action-bar") !== Boolean(command.actionBar)) {
      errors.push(`Action-bar placement mismatch: ${command.id}`);
    }
    if (command.surfaces.includes("shortcut") !== Boolean(command.shortcut)) {
      errors.push(`Shortcut declaration mismatch: ${command.id}`);
    }
    if (command.actionBar && !command.actionBar.shortLabel?.trim()) {
      errors.push(`Action-bar label is empty: ${command.id}`);
    }
    if (command.shortcut) {
      const key = editorShortcutKey(command.shortcut);
      const existing = shortcuts.get(key);
      if (existing) {
        errors.push(`Shortcut collision: ${existing} and ${command.id} (${key})`);
      } else {
        shortcuts.set(key, command.id);
      }
    }
  }

  for (const [surface, requiredIds] of Object.entries(
    EDITOR_COMMAND_SURFACE_REQUIREMENTS,
  ) as [EditorCommandSurface, readonly EditorCommandId[]][]) {
    for (const id of requiredIds) {
      const command = commands.find((candidate) => candidate.id === id);
      if (!command) {
        errors.push(`Missing command for ${surface}: ${id}`);
      } else if (!command.surfaces.includes(surface)) {
        errors.push(`Missing ${surface} surface: ${id}`);
      }
    }
  }
  return errors;
}

export type MarkdownSelection = { anchor: number; head: number };
export type MarkdownChange = { from: number; to: number; insert: string };
export type MarkdownMutationPlan = {
  change: MarkdownChange;
  operation: "apply" | "remove";
  selection: MarkdownSelection;
};

export type PlanMarkdownMutationInput = {
  body: string;
  commandId: TextMutationCommandId;
  selection: MarkdownSelection;
};

export type PlanEditorCommandInput = PlanMarkdownMutationInput & {
  replaceRange?: { from: number; to: number } | null;
};

type NormalizedSelection = {
  backward: boolean;
  from: number;
  to: number;
};

type FixedInlineDefinition = {
  placeholder: string;
  prefix: string;
  suffix: string;
};

const FIXED_INLINE_DEFINITIONS: Partial<
  Record<TextMutationCommandId, FixedInlineDefinition>
> = {
  bold: { placeholder: "bold text", prefix: "**", suffix: "**" },
  italic: { placeholder: "italic text", prefix: "*", suffix: "*" },
  strikethrough: {
    placeholder: "strikethrough text",
    prefix: "~~",
    suffix: "~~",
  },
  wikilink: { placeholder: "page-slug", prefix: "[[", suffix: "]]" },
  citation: { placeholder: "source-id", prefix: "@cite{", suffix: "}" },
};

export function planMarkdownMutation({
  body,
  commandId,
  selection,
}: PlanMarkdownMutationInput): MarkdownMutationPlan {
  const normalized = normalizeSelection(body, selection);
  const inlineDefinition = FIXED_INLINE_DEFINITIONS[commandId];
  if (inlineDefinition) {
    return planFixedInline(body, normalized, inlineDefinition);
  }
  if (commandId === "inline-code") {
    return planInlineCode(body, normalized);
  }
  if (commandId === "code-block") {
    return planCodeBlock(body, normalized);
  }
  return planLineMutation(
    body,
    normalized,
    commandId as LineMutationCommandId,
  );
}

export function planEditorCommand({
  replaceRange,
  ...input
}: PlanEditorCommandInput): MarkdownMutationPlan {
  if (!replaceRange) return planMarkdownMutation(input);

  const replacementFrom = clamp(
    Math.min(replaceRange.from, replaceRange.to),
    0,
    input.body.length,
  );
  const replacementTo = clamp(
    Math.max(replaceRange.from, replaceRange.to),
    replacementFrom,
    input.body.length,
  );
  const removedLength = replacementTo - replacementFrom;
  const virtualBody = `${input.body.slice(0, replacementFrom)}${input.body.slice(replacementTo)}`;
  const virtualPlan = planMarkdownMutation({
    body: virtualBody,
    commandId: input.commandId,
    selection: { anchor: replacementFrom, head: replacementFrom },
  });
  const resultBody = applyMarkdownMutationPlan(virtualBody, virtualPlan);
  const mapVirtualPosition = (position: number) =>
    position <= replacementFrom ? position : position + removedLength;
  const finalFrom = Math.min(
    replacementFrom,
    mapVirtualPosition(virtualPlan.change.from),
  );
  const finalTo = Math.max(
    replacementTo,
    mapVirtualPosition(virtualPlan.change.to),
  );
  const unchangedSuffixLength = input.body.length - finalTo;
  const insertTo = resultBody.length - unchangedSuffixLength;
  return {
    ...virtualPlan,
    change: {
      from: finalFrom,
      to: finalTo,
      insert: resultBody.slice(finalFrom, insertTo),
    },
  };
}

export function applyMarkdownMutationPlan(
  body: string,
  plan: MarkdownMutationPlan,
) {
  return `${body.slice(0, plan.change.from)}${plan.change.insert}${body.slice(plan.change.to)}`;
}

export function getMarkdownCommandState({
  body,
  commandId,
  selection,
}: PlanMarkdownMutationInput): EditorCommandState {
  const normalized = normalizeSelection(body, selection);
  const definition = FIXED_INLINE_DEFINITIONS[commandId];
  if (definition) {
    return fixedInlineState(body, normalized, definition);
  }
  if (commandId === "inline-code") {
    return inlineCodeState(body, normalized);
  }
  if (commandId === "code-block") {
    const containing = findContainingFence(body, normalized.from, normalized.to);
    if (containing) return "active";
    return findIntersectingFence(body, normalized.from, normalized.to)
      ? "mixed"
      : "inactive";
  }
  return lineCommandState(
    body,
    normalized,
    commandId as LineMutationCommandId,
  );
}

function normalizeSelection(
  body: string,
  selection: MarkdownSelection,
): NormalizedSelection {
  const anchor = clamp(selection.anchor, 0, body.length);
  const head = clamp(selection.head, 0, body.length);
  return {
    backward: anchor > head,
    from: Math.min(anchor, head),
    to: Math.max(anchor, head),
  };
}

function planFixedInline(
  body: string,
  selection: NormalizedSelection,
  definition: FixedInlineDefinition,
): MarkdownMutationPlan {
  const { from, to } = selection;
  const selected = body.slice(from, to);
  const selectedWrapper = fullFixedWrapper(selected, definition);
  if (selectedWrapper) {
    return planWithSelection(
      { from, to, insert: selectedWrapper.inner },
      from,
      from + selectedWrapper.inner.length,
      selection.backward,
      "remove",
    );
  }

  if (hasFixedWrapperAround(body, from, to, definition)) {
    const changeFrom = from - definition.prefix.length;
    return planWithSelection(
      {
        from: changeFrom,
        to: to + definition.suffix.length,
        insert: selected,
      },
      changeFrom,
      changeFrom + selected.length,
      selection.backward,
      "remove",
    );
  }

  const containing = findContainingFixedWrapper(body, from, to, definition);
  if (containing && from === to) {
    const inner = body.slice(containing.innerFrom, containing.innerTo);
    const cursorOffset = from - containing.innerFrom;
    return planWithSelection(
      { from: containing.from, to: containing.to, insert: inner },
      containing.from + cursorOffset,
      containing.from + cursorOffset,
      false,
      "remove",
    );
  }

  const source = selected || definition.placeholder;
  const normalizedSource =
    selected && fixedInlineState(body, selection, definition) === "mixed"
      ? stripFixedPairs(selected, definition)
      : source;
  const insert = `${definition.prefix}${normalizedSource}${definition.suffix}`;
  const contentFrom = from + definition.prefix.length;
  return planWithSelection(
    { from, to, insert },
    contentFrom,
    contentFrom + normalizedSource.length,
    selection.backward,
    "apply",
  );
}

function fixedInlineState(
  body: string,
  selection: NormalizedSelection,
  definition: FixedInlineDefinition,
): EditorCommandState {
  const selected = body.slice(selection.from, selection.to);
  if (
    fullFixedWrapper(selected, definition) ||
    hasFixedWrapperAround(body, selection.from, selection.to, definition)
  ) {
    return "active";
  }
  const containing = findContainingFixedWrapper(
    body,
    selection.from,
    selection.to,
    definition,
  );
  if (selection.from === selection.to) {
    return containing ? "active" : "inactive";
  }
  if (containing) {
    return "active";
  }
  if (
    stripFixedPairs(selected, definition) !== selected ||
    findContainingFixedWrapper(body, selection.from, selection.from, definition) ||
    findContainingFixedWrapper(body, selection.to, selection.to, definition)
  ) {
    return "mixed";
  }
  return "inactive";
}

function fullFixedWrapper(
  value: string,
  definition: FixedInlineDefinition,
) {
  if (
    value.length < definition.prefix.length + definition.suffix.length ||
    !value.startsWith(definition.prefix) ||
    !value.endsWith(definition.suffix) ||
    !validFixedBoundary(value, 0, value.length, definition)
  ) {
    return null;
  }
  return {
    inner: value.slice(definition.prefix.length, value.length - definition.suffix.length),
  };
}

function hasFixedWrapperAround(
  body: string,
  from: number,
  to: number,
  definition: FixedInlineDefinition,
) {
  const wrapperFrom = from - definition.prefix.length;
  const wrapperTo = to + definition.suffix.length;
  return (
    wrapperFrom >= 0 &&
    body.slice(wrapperFrom, from) === definition.prefix &&
    body.slice(to, wrapperTo) === definition.suffix &&
    validFixedBoundary(body, wrapperFrom, wrapperTo, definition)
  );
}

function validFixedBoundary(
  body: string,
  from: number,
  to: number,
  definition: FixedInlineDefinition,
) {
  if (definition.prefix !== "*") return true;
  return body[from - 1] !== "*" && body[from + 1] !== "*" &&
    body[to - 2] !== "*" && body[to] !== "*";
}

function findContainingFixedWrapper(
  body: string,
  from: number,
  to: number,
  definition: FixedInlineDefinition,
) {
  let open = body.lastIndexOf(definition.prefix, from);
  while (open >= 0) {
    const innerFrom = open + definition.prefix.length;
    const close = body.indexOf(definition.suffix, innerFrom);
    if (close >= innerFrom && innerFrom <= from && close >= to) {
      const wrapperTo = close + definition.suffix.length;
      if (validFixedBoundary(body, open, wrapperTo, definition)) {
        return { from: open, innerFrom, innerTo: close, to: wrapperTo };
      }
    }
    if (open === 0) break;
    open = body.lastIndexOf(definition.prefix, open - 1);
  }
  return null;
}

function stripFixedPairs(value: string, definition: FixedInlineDefinition) {
  let cursor = 0;
  let output = "";
  while (cursor < value.length) {
    const open = value.indexOf(definition.prefix, cursor);
    if (open < 0) {
      output += value.slice(cursor);
      break;
    }
    const close = value.indexOf(
      definition.suffix,
      open + definition.prefix.length,
    );
    const wrapperTo = close + definition.suffix.length;
    if (
      close < 0 ||
      !validFixedBoundary(value, open, wrapperTo, definition)
    ) {
      output += value.slice(cursor, open + definition.prefix.length);
      cursor = open + definition.prefix.length;
      continue;
    }
    output += value.slice(cursor, open);
    output += value.slice(open + definition.prefix.length, close);
    cursor = wrapperTo;
  }
  return output;
}

function planInlineCode(
  body: string,
  selection: NormalizedSelection,
): MarkdownMutationPlan {
  const selected = body.slice(selection.from, selection.to);
  const selectedWrapper = fullCodeWrapper(selected);
  if (selectedWrapper) {
    return planWithSelection(
      { from: selection.from, to: selection.to, insert: selectedWrapper.inner },
      selection.from,
      selection.from + selectedWrapper.inner.length,
      selection.backward,
      "remove",
    );
  }
  const adjacent = codeWrapperAround(body, selection.from, selection.to);
  if (adjacent) {
    const changeFrom = selection.from - adjacent.marker.length;
    return planWithSelection(
      { from: changeFrom, to: selection.to + adjacent.marker.length, insert: selected },
      changeFrom,
      changeFrom + selected.length,
      selection.backward,
      "remove",
    );
  }
  const containing = findContainingCodeWrapper(body, selection.from, selection.to);
  if (containing && selection.from === selection.to) {
    const inner = body.slice(containing.innerFrom, containing.innerTo);
    const cursorOffset = selection.from - containing.innerFrom;
    return planWithSelection(
      { from: containing.from, to: containing.to, insert: inner },
      containing.from + cursorOffset,
      containing.from + cursorOffset,
      false,
      "remove",
    );
  }

  const source = selected || "code";
  const normalizedSource =
    selected && inlineCodeState(body, selection) === "mixed"
      ? stripCodePairs(selected)
      : source;
  const delimiter = "`".repeat(Math.max(1, longestBacktickRun(normalizedSource) + 1));
  return planWithSelection(
    {
      from: selection.from,
      to: selection.to,
      insert: `${delimiter}${normalizedSource}${delimiter}`,
    },
    selection.from + delimiter.length,
    selection.from + delimiter.length + normalizedSource.length,
    selection.backward,
    "apply",
  );
}

function inlineCodeState(
  body: string,
  selection: NormalizedSelection,
): EditorCommandState {
  const selected = body.slice(selection.from, selection.to);
  if (
    fullCodeWrapper(selected) ||
    codeWrapperAround(body, selection.from, selection.to)
  ) {
    return "active";
  }
  const containing = findContainingCodeWrapper(body, selection.from, selection.to);
  if (selection.from === selection.to) {
    return containing ? "active" : "inactive";
  }
  if (containing) return "active";
  if (
    stripCodePairs(selected) !== selected ||
    findContainingCodeWrapper(body, selection.from, selection.from) ||
    findContainingCodeWrapper(body, selection.to, selection.to)
  ) {
    return "mixed";
  }
  return "inactive";
}

function fullCodeWrapper(value: string) {
  const opening = /^`+/.exec(value)?.[0];
  const closing = /`+$/.exec(value)?.[0];
  if (!opening || opening !== closing || value.length < opening.length * 2) {
    return null;
  }
  return { inner: value.slice(opening.length, value.length - opening.length), marker: opening };
}

function codeWrapperAround(body: string, from: number, to: number) {
  const before = /`+$/.exec(body.slice(0, from))?.[0];
  const after = /^`+/.exec(body.slice(to))?.[0];
  return before && before === after ? { marker: before } : null;
}

function findContainingCodeWrapper(body: string, from: number, to: number) {
  const lineFrom = body.lastIndexOf("\n", from - 1) + 1;
  const nextNewline = body.indexOf("\n", to);
  const lineTo = nextNewline < 0 ? body.length : nextNewline;
  const runs: { from: number; to: number; marker: string }[] = [];
  const matcher = /`+/g;
  matcher.lastIndex = lineFrom;
  let match = matcher.exec(body);
  while (match && match.index < lineTo) {
    runs.push({
      from: match.index,
      marker: match[0],
      to: match.index + match[0].length,
    });
    match = matcher.exec(body);
  }
  for (let left = runs.length - 2; left >= 0; left -= 1) {
    const opening = runs[left];
    if (opening.to > from) continue;
    for (let right = left + 1; right < runs.length; right += 1) {
      const closing = runs[right];
      if (
        opening.marker === closing.marker &&
        closing.from >= to
      ) {
        return {
          from: opening.from,
          innerFrom: opening.to,
          innerTo: closing.from,
          to: closing.to,
        };
      }
    }
  }
  return null;
}

function stripCodePairs(value: string) {
  return value.replace(/(`+)([^\n]*?)\1/g, "$2");
}

function longestBacktickRun(value: string) {
  let longest = 0;
  for (const match of value.matchAll(/`+/g)) {
    longest = Math.max(longest, match[0].length);
  }
  return longest;
}

type FenceRange = {
  closeFrom: number;
  from: number;
  innerFrom: number;
  innerTo: number;
  to: number;
};

function planCodeBlock(
  body: string,
  selection: NormalizedSelection,
): MarkdownMutationPlan {
  const containing = findContainingFence(body, selection.from, selection.to);
  if (containing) {
    const inner = body.slice(containing.innerFrom, containing.innerTo);
    const anchor = clamp(selection.from - containing.innerFrom, 0, inner.length);
    const head = clamp(selection.to - containing.innerFrom, 0, inner.length);
    return planWithSelection(
      { from: containing.from, to: containing.to, insert: inner },
      containing.from + anchor,
      containing.from + head,
      selection.backward,
      "remove",
    );
  }

  const range = selectedLineRange(body, selection.from, selection.to);
  const selected = body.slice(range.from, range.to);
  const source = selected || "code";
  const fence = "`".repeat(Math.max(3, longestFenceRun(source) + 1));
  const prefix = `${fence}\n`;
  return planWithSelection(
    {
      from: range.from,
      to: range.to,
      insert: `${prefix}${source}\n${fence}`,
    },
    range.from + prefix.length,
    range.from + prefix.length + source.length,
    selection.backward,
    "apply",
  );
}

function longestFenceRun(value: string) {
  let longest = 0;
  for (const match of value.matchAll(/^\s*(`{3,})/gm)) {
    longest = Math.max(longest, match[1].length);
  }
  return longest;
}

function fenceRanges(body: string) {
  const ranges: FenceRange[] = [];
  const lines = lineRecords(body);
  let open: { char: string; from: number; innerFrom: number; length: number } | null = null;
  for (const line of lines) {
    const fence = /^\s*(`{3,}|~{3,})/.exec(line.text)?.[1];
    if (!fence) continue;
    if (!open) {
      open = {
        char: fence[0],
        from: line.from,
        innerFrom: line.to < body.length ? line.to + 1 : line.to,
        length: fence.length,
      };
      continue;
    }
    if (fence[0] === open.char && fence.length >= open.length) {
      ranges.push({
        closeFrom: line.from,
        from: open.from,
        innerFrom: open.innerFrom,
        innerTo: line.from > 0 && body[line.from - 1] === "\n" ? line.from - 1 : line.from,
        to: line.to,
      });
      open = null;
    }
  }
  return ranges;
}

function findContainingFence(body: string, from: number, to: number) {
  return fenceRanges(body).find(
    (range) => from >= range.innerFrom && to <= range.innerTo,
  );
}

function findIntersectingFence(body: string, from: number, to: number) {
  return fenceRanges(body).find((range) => {
    if (from === to) return from >= range.from && from <= range.to;
    return from < range.to && to > range.from;
  });
}

type LineMutationCommandId = Exclude<
  TextMutationCommandId,
  | "bold"
  | "citation"
  | "code-block"
  | "inline-code"
  | "italic"
  | "strikethrough"
  | "wikilink"
>;

type PrefixMatch = { content: string; indent: string; prefixLength: number };

function planLineMutation(
  body: string,
  selection: NormalizedSelection,
  commandId: LineMutationCommandId,
): MarkdownMutationPlan {
  const range = selectedLineRange(body, selection.from, selection.to);
  const original = body.slice(range.from, range.to);
  const originalLines = original.split("\n");
  const nonBlank = originalLines.filter((line) => line.trim().length > 0);
  const remove = nonBlank.length > 0 &&
    nonBlank.every((line) => targetPrefixMatch(line, commandId));
  let itemNumber = 0;
  const transformed = originalLines.map((line) => {
    if (!line.trim()) return line;
    const target = targetPrefixMatch(line, commandId);
    const general = generalPrefixMatch(line, commandId);
    if (remove && target) {
      return `${target.indent}${target.content}`;
    }
    itemNumber += 1;
    const source = general ?? { content: line.trimStart(), indent: leadingWhitespace(line), prefixLength: leadingWhitespace(line).length };
    return `${source.indent}${linePrefix(commandId, itemNumber)}${source.content}`;
  });

  if (
    selection.from === selection.to &&
    original.trim().length === 0
  ) {
    const placeholder = linePlaceholder(commandId);
    const prefix = linePrefix(commandId, 1);
    const indent = leadingWhitespace(original);
    const insert = `${indent}${prefix}${placeholder}`;
    return planWithSelection(
      { from: range.from, to: range.to, insert },
      range.from + indent.length + prefix.length,
      range.from + insert.length,
      false,
      "apply",
    );
  }

  const insert = transformed.join("\n");
  const mappedFrom = mapLinePosition(
    originalLines,
    transformed,
    selection.from - range.from,
  );
  const mappedTo = mapLinePosition(
    originalLines,
    transformed,
    selection.to - range.from,
  );
  return planWithSelection(
    { from: range.from, to: range.to, insert },
    range.from + mappedFrom,
    range.from + mappedTo,
    selection.backward,
    remove ? "remove" : "apply",
  );
}

function lineCommandState(
  body: string,
  selection: NormalizedSelection,
  commandId: LineMutationCommandId,
): EditorCommandState {
  const range = selectedLineRange(body, selection.from, selection.to);
  const lines = body
    .slice(range.from, range.to)
    .split("\n")
    .filter((line) => line.trim().length > 0);
  if (!lines.length) return "inactive";
  const activeCount = lines.filter((line) => targetPrefixMatch(line, commandId)).length;
  if (activeCount === lines.length) return "active";
  return activeCount > 0 ? "mixed" : "inactive";
}

function targetPrefixMatch(
  line: string,
  commandId: LineMutationCommandId,
): PrefixMatch | null {
  const expression = commandId === "heading"
    ? /^(\s*)#{1,6}\s+/
    : commandId === "bulleted-list"
      ? /^(\s*)[-*+]\s+(?!\[[ xX]\]\s+)/
      : commandId === "ordered-list"
        ? /^(\s*)\d+[.)]\s+/
        : commandId === "task-list"
          ? /^(\s*)[-*+]\s+\[[ xX]\]\s+/
          : /^(\s*)>\s?/;
  const match = expression.exec(line);
  if (!match) return null;
  return {
    content: line.slice(match[0].length),
    indent: match[1],
    prefixLength: match[0].length,
  };
}

function generalPrefixMatch(
  line: string,
  commandId: LineMutationCommandId,
): PrefixMatch | null {
  if (commandId === "heading" || commandId === "quote") {
    return targetPrefixMatch(line, commandId);
  }
  const match = /^(\s*)(?:[-*+]\s+\[[ xX]\]\s+|[-*+]\s+|\d+[.)]\s+)/.exec(line);
  if (!match) return null;
  return {
    content: line.slice(match[0].length),
    indent: match[1],
    prefixLength: match[0].length,
  };
}

function linePrefix(commandId: LineMutationCommandId, itemNumber: number) {
  if (commandId === "heading") return "## ";
  if (commandId === "bulleted-list") return "- ";
  if (commandId === "ordered-list") return `${itemNumber}. `;
  if (commandId === "task-list") return "- [ ] ";
  return "> ";
}

function linePlaceholder(commandId: LineMutationCommandId) {
  if (commandId === "heading") return "Heading";
  if (commandId === "task-list") return "Task";
  if (commandId === "quote") return "Quote";
  return "List item";
}

function mapLinePosition(
  originalLines: readonly string[],
  transformedLines: readonly string[],
  rawPosition: number,
) {
  const originalLength = originalLines.join("\n").length;
  const position = clamp(rawPosition, 0, originalLength);
  let originalStart = 0;
  let transformedStart = 0;
  for (let index = 0; index < originalLines.length; index += 1) {
    const original = originalLines[index];
    const transformed = transformedLines[index];
    const originalEnd = originalStart + original.length;
    if (position <= originalEnd || index === originalLines.length - 1) {
      const originalContent = contentStart(original);
      const transformedContent = contentStart(transformed);
      const offset = position - originalStart;
      const mappedOffset = offset <= originalContent
        ? Math.min(transformedContent, transformed.length)
        : Math.min(
            transformed.length,
            transformedContent + offset - originalContent,
          );
      return transformedStart + mappedOffset;
    }
    originalStart = originalEnd + 1;
    transformedStart += transformed.length + 1;
  }
  return transformedLines.join("\n").length;
}

function contentStart(line: string) {
  const match = /^(\s*)(?:#{1,6}\s+|[-*+]\s+\[[ xX]\]\s+|[-*+]\s+|\d+[.)]\s+|>\s?)/.exec(line);
  return match?.[0].length ?? leadingWhitespace(line).length;
}

function leadingWhitespace(line: string) {
  return /^\s*/.exec(line)?.[0] ?? "";
}

function selectedLineRange(body: string, from: number, to: number) {
  const start = body.lastIndexOf("\n", Math.max(0, from - 1)) + 1;
  const endProbe = to > from && body[to - 1] === "\n" ? to - 1 : to;
  const newline = body.indexOf("\n", endProbe);
  return { from: start, to: newline < 0 ? body.length : newline };
}

function lineRecords(body: string) {
  const records: { from: number; text: string; to: number }[] = [];
  let from = 0;
  for (const line of body.split("\n")) {
    const to = from + line.length;
    records.push({ from, text: line, to });
    from = to + 1;
  }
  return records;
}

function planWithSelection(
  change: MarkdownChange,
  anchor: number,
  head: number,
  backward: boolean,
  operation: MarkdownMutationPlan["operation"],
): MarkdownMutationPlan {
  return {
    change,
    operation,
    selection: backward ? { anchor: head, head: anchor } : { anchor, head },
  };
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), maximum);
}
