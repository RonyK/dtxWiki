import { describe, expect, it } from "vitest";

import type { McpChatTimelineItem } from "../../../shared/api/operations";
import { i18n } from "../../../shared/i18n";
import { chatHasEditPreviews, editSummaryLine } from "../ChatEditCard";

type EditSummary = NonNullable<McpChatTimelineItem["edit_summary"]>;

function summary(overrides: Partial<EditSummary> = {}): EditSummary {
  return {
    source: "receipt",
    edit_mode: "patch",
    operation_counts: {},
    operations: [],
    changed_sections: [],
    chars_added: null,
    chars_removed: null,
    preview_budget_exhausted: 0,
    previews_purged: false,
    previews_withheld: false,
    ...overrides,
  };
}

function item(editSummary: EditSummary | null): McpChatTimelineItem {
  return { edit_summary: editSummary } as McpChatTimelineItem;
}

describe("chatHasEditPreviews", () => {
  it("offers deletion only while some call still holds preview text", () => {
    expect(chatHasEditPreviews([item(null)])).toBe(false);
    // Sizes and op kinds alone are not previews.
    expect(chatHasEditPreviews([
      item(summary({ operations: [{ index: 0, op: "replace_text", previews: [] }] })),
    ])).toBe(false);
    expect(chatHasEditPreviews([
      item(summary({
        operations: [{
          index: 0,
          op: "replace_text",
          previews: [{ field: "new_text", text: "x", truncated: false }],
        }],
      })),
    ])).toBe(true);
    expect(chatHasEditPreviews([
      item(summary({ first_change: { line: 3, after_preview: "x" } })),
    ])).toBe(true);
    expect(chatHasEditPreviews([
      item(summary({ body_preview: { field: "body_md", text: "x", truncated: false } })),
    ])).toBe(true);
  });

  it("counts withheld text, which exists but is not shown to this reader, and not purged calls", () => {
    expect(chatHasEditPreviews([item(summary({ previews_withheld: true }))])).toBe(true);
    expect(chatHasEditPreviews([
      item(summary({
        previews_purged: true,
        body_preview: { field: "body_md", text: null, truncated: false },
      })),
    ])).toBe(false);
  });
});

describe("editSummaryLine", () => {
  const t = i18n.getFixedT("en", "chats");

  it("caps the sections it names and says how many more changed", () => {
    expect(editSummaryLine(summary({
      operation_counts: { replace_text: 2 },
      changed_sections: [["A"], ["B"], ["C"], ["D"], []],
      chars_added: 120,
      chars_removed: 3,
    }), t)).toBe("replace_text ×2 · § A, § B, § C and 2 more · +120/−3 chars");
  });

  it("names the top of the document and reports an added-only size", () => {
    expect(editSummaryLine(summary({
      changed_sections: [[]],
      chars_added: 10,
    }), t)).toBe("Top of document · +10 chars");
  });

  it("returns nothing when there is nothing to say", () => {
    expect(editSummaryLine(summary(), t)).toBeNull();
  });
});
