import { describe, expect, it } from "vitest";

import {
  EDITOR_COMMANDS,
  EDITOR_COMMAND_SURFACE_REQUIREMENTS,
  applyMarkdownMutationPlan,
  editorShortcutKey,
  getMarkdownCommandState,
  planEditorCommand,
  planMarkdownMutation,
  validateEditorCommandRegistry,
  type EditorCommand,
  type MarkdownSelection,
  type TextMutationCommandId,
} from "../editorCommands";

function mutate(
  body: string,
  commandId: TextMutationCommandId,
  selection: MarkdownSelection,
) {
  const plan = planMarkdownMutation({ body, commandId, selection });
  return { body: applyMarkdownMutationPlan(body, plan), plan };
}

describe("editor command registry", () => {
  it("has one valid discriminated command contract for every required surface", () => {
    expect(validateEditorCommandRegistry()).toEqual([]);
    expect(new Set(EDITOR_COMMANDS.map((command) => command.kind))).toEqual(
      new Set([
        "text-mutation",
        "modal-mutation",
        "async-attachment",
        "external-effect",
      ]),
    );

    for (const [surface, ids] of Object.entries(
      EDITOR_COMMAND_SURFACE_REQUIREMENTS,
    )) {
      for (const id of ids) {
        expect(
          EDITOR_COMMANDS.find((command) => command.id === id)?.surfaces,
          `${id} should be registered on ${surface}`,
        ).toContain(surface);
      }
    }
  });

  it("keeps ids and normalized shortcuts collision-free", () => {
    const commands: readonly EditorCommand[] = EDITOR_COMMANDS;
    const ids = commands.map((command) => command.id);
    const shortcuts = commands.flatMap((command) =>
      command.shortcut ? [editorShortcutKey(command.shortcut)] : [],
    );
    expect(new Set(ids).size).toBe(ids.length);
    expect(new Set(shortcuts).size).toBe(shortcuts.length);

    const duplicated = [
      ...EDITOR_COMMANDS,
      EDITOR_COMMANDS[0],
    ] as readonly EditorCommand[];
    expect(validateEditorCommandRegistry(duplicated)).toContain(
      "Duplicate command id: undo",
    );
    expect(validateEditorCommandRegistry(duplicated)).toContain(
      "Shortcut collision: undo and undo (Mod+z)",
    );
  });

  it("keeps Attach visible at top level on desktop and mobile", () => {
    const attach = EDITOR_COMMANDS.find((command) => command.id === "attach");
    expect(attach).toMatchObject({
      kind: "async-attachment",
      actionBar: { desktop: "primary", mobile: "primary" },
    });
  });
});

describe("inline Markdown mutation planning", () => {
  it("removes a slash query and applies the command in one original-document change", () => {
    const body = "Before\n/bo\nAfter";
    const plan = planEditorCommand({
      body,
      commandId: "bold",
      replaceRange: { from: 7, to: 10 },
      selection: { anchor: 10, head: 10 },
    });

    expect(plan.change).toEqual({
      from: 7,
      to: 10,
      insert: "**bold text**",
    });
    expect(applyMarkdownMutationPlan(body, plan)).toBe(
      "Before\n**bold text**\nAfter",
    );
  });

  it("wraps a selection and removes either an outside or selected wrapper", () => {
    const applied = mutate("alpha", "bold", { anchor: 0, head: 5 });
    expect(applied.body).toBe("**alpha**");
    expect(applied.plan).toMatchObject({
      change: { from: 0, to: 5, insert: "**alpha**" },
      operation: "apply",
      selection: { anchor: 2, head: 7 },
    });

    const outsideRemoved = mutate("**alpha**", "bold", {
      anchor: 2,
      head: 7,
    });
    expect(outsideRemoved.body).toBe("alpha");
    expect(outsideRemoved.plan.operation).toBe("remove");
    expect(outsideRemoved.plan.change).toEqual({
      from: 0,
      to: 9,
      insert: "alpha",
    });

    const selectedRemoved = mutate("**alpha**", "bold", {
      anchor: 0,
      head: 9,
    });
    expect(selectedRemoved.body).toBe("alpha");
    expect(selectedRemoved.plan.selection).toEqual({ anchor: 0, head: 5 });
  });

  it("inserts selected placeholders at a collapsed caret and removes a containing wrapper", () => {
    const inserted = mutate("Hello ", "bold", { anchor: 6, head: 6 });
    expect(inserted.body).toBe("Hello **bold text**");
    expect(inserted.body.slice(
      inserted.plan.selection.anchor,
      inserted.plan.selection.head,
    )).toBe("bold text");

    const removed = mutate("before **alpha** after", "bold", {
      anchor: 11,
      head: 11,
    });
    expect(removed.body).toBe("before alpha after");
    expect(removed.plan.selection).toEqual({ anchor: 9, head: 9 });
  });

  it("normalizes a partially mixed selection instead of nesting duplicate markers", () => {
    const body = "**alpha** beta";
    expect(
      getMarkdownCommandState({
        body,
        commandId: "bold",
        selection: { anchor: 0, head: body.length },
      }),
    ).toBe("mixed");
    expect(
      mutate(body, "bold", { anchor: 0, head: body.length }).body,
    ).toBe("**alpha beta**");
  });

  it("supports multiline wrappers and preserves backwards selection direction", () => {
    const applied = mutate("alpha\nbeta", "strikethrough", {
      anchor: 10,
      head: 0,
    });
    expect(applied.body).toBe("~~alpha\nbeta~~");
    expect(applied.plan.selection.anchor).toBeGreaterThan(
      applied.plan.selection.head,
    );
    expect(
      applied.body.slice(
        applied.plan.selection.head,
        applied.plan.selection.anchor,
      ),
    ).toBe("alpha\nbeta");
  });

  it("does not confuse bold delimiters for italic state", () => {
    expect(
      getMarkdownCommandState({
        body: "**alpha**",
        commandId: "italic",
        selection: { anchor: 2, head: 7 },
      }),
    ).toBe("inactive");
  });

  it.each([
    ["italic", "*alpha*"],
    ["strikethrough", "~~alpha~~"],
    ["wikilink", "[[alpha]]"],
    ["citation", "@cite{alpha}"],
  ] as const)("toggles %s", (commandId, wrapped) => {
    const applied = mutate("alpha", commandId, { anchor: 0, head: 5 });
    expect(applied.body).toBe(wrapped);
    expect(
      mutate(wrapped, commandId, {
        anchor: 0,
        head: wrapped.length,
      }).body,
    ).toBe("alpha");
  });

  it("chooses a safe inline-code delimiter and removes it on the next toggle", () => {
    const applied = mutate("a`b", "inline-code", { anchor: 0, head: 3 });
    expect(applied.body).toBe("``a`b``");
    expect(
      mutate(applied.body, "inline-code", {
        anchor: 2,
        head: 5,
      }).body,
    ).toBe("a`b");
  });
});

describe("line and fenced Markdown mutation planning", () => {
  it("toggles headings for every selected line without consuming the next line", () => {
    const body = "alpha\nbeta\ngamma";
    const applied = mutate(body, "heading", { anchor: 0, head: 11 });
    expect(applied.body).toBe("## alpha\n## beta\ngamma");
    expect(
      mutate(applied.body, "heading", { anchor: 0, head: 16 }).body,
    ).toBe(body);
  });

  it("reports mixed list state, applies it uniformly, then removes it", () => {
    const body = "- alpha\nbeta";
    const selection = { anchor: 0, head: body.length };
    expect(
      getMarkdownCommandState({
        body,
        commandId: "bulleted-list",
        selection,
      }),
    ).toBe("mixed");
    const applied = mutate(body, "bulleted-list", selection);
    expect(applied.body).toBe("- alpha\n- beta");
    expect(
      mutate(applied.body, "bulleted-list", {
        anchor: 0,
        head: applied.body.length,
      }).body,
    ).toBe("alpha\nbeta");
  });

  it("normalizes list kinds and ordered numbering across multiple lines", () => {
    expect(
      mutate("- alpha\n- beta", "ordered-list", {
        anchor: 0,
        head: 14,
      }).body,
    ).toBe("1. alpha\n2. beta");
    expect(
      mutate("1. alpha\n2. beta", "task-list", {
        anchor: 0,
        head: 16,
      }).body,
    ).toBe("- [ ] alpha\n- [ ] beta");
  });

  it("creates a useful line placeholder for a collapsed empty selection", () => {
    const applied = mutate("", "task-list", { anchor: 0, head: 0 });
    expect(applied.body).toBe("- [ ] Task");
    expect(
      applied.body.slice(
        applied.plan.selection.anchor,
        applied.plan.selection.head,
      ),
    ).toBe("Task");
  });

  it("wraps a multiline code block in one change and toggles from inside the fence", () => {
    const applied = mutate("alpha\nbeta", "code-block", {
      anchor: 0,
      head: 10,
    });
    expect(applied.body).toBe("```\nalpha\nbeta\n```");
    expect(applied.plan.change).toEqual({
      from: 0,
      to: 10,
      insert: "```\nalpha\nbeta\n```",
    });
    expect(
      getMarkdownCommandState({
        body: applied.body,
        commandId: "code-block",
        selection: { anchor: 6, head: 6 },
      }),
    ).toBe("active");
    expect(
      mutate(applied.body, "code-block", { anchor: 6, head: 6 }).body,
    ).toBe("alpha\nbeta");
  });

  it("uses a longer fence when selected source already contains a fence", () => {
    const applied = mutate("```\ninner\n```", "code-block", {
      anchor: 0,
      head: 13,
    });
    expect(applied.body.startsWith("````\n")).toBe(true);
    expect(applied.body.endsWith("\n````")).toBe(true);
  });
});
