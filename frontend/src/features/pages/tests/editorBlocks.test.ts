import { describe, expect, it } from "vitest";

import {
  BLOCK_MENU_ENTRIES,
  BLOCK_MENU_GROUPS,
  blockSnippet,
  filterBlockEntries,
} from "../editorBlocks";

describe("editor block menu entries", () => {
  it("does not insert page frontmatter into the markdown body", () => {
    expect(BLOCK_MENU_ENTRIES.map((entry) => entry.id)).not.toContain("page-property");
    expect(filterBlockEntries("frontmatter")).toEqual([]);
    expect(BLOCK_MENU_ENTRIES.some((entry) => entry.template.trimStart().startsWith("---\n")))
      .toBe(false);
  });

  it("keeps image insertion on the upload flow instead of a placeholder URL", () => {
    const imageEntry = BLOCK_MENU_ENTRIES.find((entry) => entry.id === "image");

    expect(imageEntry?.template).not.toContain("image-url");
  });

  it("keeps slash templates aligned with GFM-supported markdown", () => {
    expect(BLOCK_MENU_ENTRIES.map((entry) => entry.id)).not.toContain("definition");

    const footnoteEntry = BLOCK_MENU_ENTRIES.find((entry) => entry.id === "footnote");
    expect(footnoteEntry?.template).toContain("[^note]");
    expect(footnoteEntry?.template).not.toContain("^[");
  });

  it("does not advertise raw video insertion without a server capability", () => {
    expect(BLOCK_MENU_ENTRIES.map((entry) => entry.id)).not.toContain("video");
    expect(filterBlockEntries("video")).toEqual([]);
    expect(filterBlockEntries("movie")).toEqual([]);
  });

  it("offers portable math slash templates for the KaTeX renderer", () => {
    const inlineMath = BLOCK_MENU_ENTRIES.find((entry) => entry.id === "inline-math");
    const displayMath = BLOCK_MENU_ENTRIES.find((entry) => entry.id === "display-math");

    expect(inlineMath?.template).toBe("$ {{cursor}}x^2 $");
    expect(displayMath?.template).toBe("$$\n{{cursor}}x^2\n$$");
    expect(filterBlockEntries("math").map((entry) => entry.id)).toEqual(
      expect.arrayContaining(["inline-math", "display-math"]),
    );
  });

  it("offers a portable emoji picker slash command without shortcode rewriting", () => {
    const emojiEntry = BLOCK_MENU_ENTRIES.find((entry) => entry.id === "emoji");

    expect(emojiEntry).toMatchObject({
      group: "Text",
      label: "Emoji",
      template: "{{cursor}}",
    });
    expect(filterBlockEntries("emoji").map((entry) => entry.id)).toContain("emoji");
  });

  it("hides AI slash menu commands while inline rewrite is disabled", () => {
    const aiEntries = BLOCK_MENU_ENTRIES.filter((entry) => entry.group === "AI");

    expect(BLOCK_MENU_GROUPS).not.toContain("AI");
    expect(aiEntries).toEqual([]);
    expect(filterBlockEntries("summarize")).toEqual([]);
  });

  it("generates a unique short id for each decision block insert", () => {
    const decisionEntry = BLOCK_MENU_ENTRIES.find((entry) => entry.id === "decision");
    expect(decisionEntry?.template).toContain("{{decisionId}}");

    const first = blockSnippet(decisionEntry!.template);
    const second = blockSnippet(decisionEntry!.template);

    expect(first.text).toMatch(/<!-- decision id=dec-[a-z0-9]{4,6} status=proposed/);
    expect(first.text).not.toContain("{{decisionId}}");
    expect(first.text).not.toContain("dec-001");
    expect(first.text).not.toEqual(second.text);
  });

  it("generates a unique short id for each action block insert and uses a literal date placeholder", () => {
    const actionEntry = BLOCK_MENU_ENTRIES.find((entry) => entry.id === "action");
    expect(actionEntry?.template).toContain("{{actionId}}");
    expect(actionEntry?.template).toContain("due=YYYY-MM-DD");
    expect(actionEntry?.template).not.toContain("2026-06-01");

    const first = blockSnippet(actionEntry!.template);
    const second = blockSnippet(actionEntry!.template);

    expect(first.text).toMatch(/<!-- action id=act-[a-z0-9]{4,6} owner=@owner due=YYYY-MM-DD/);
    expect(first.text).not.toContain("{{actionId}}");
    expect(first.text).not.toEqual(second.text);
  });
});
