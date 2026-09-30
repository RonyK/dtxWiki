import type { EditorView as EditorViewType } from "@codemirror/view";

import { AI_INLINE_REWRITE_ENABLED } from "./editorFeatureFlags";
import type { EditorCommandId } from "./editorCommands";

export type BlockMenuGroup = "Text" | "Lists" | "Media" | "dtxWiki" | "AI";

// English display labels for each group heading, used as the i18next
// `defaultValue` fallback for `pages:blocks.groups.<group>` (see PageEditor's
// BlockMenu). "dtxWiki" is the product name, not a category word, so its
// label is the product name unchanged rather than a translated category.
export const BLOCK_MENU_GROUP_LABELS: Record<BlockMenuGroup, string> = {
  Text: "Text",
  Lists: "Lists",
  Media: "Media",
  dtxWiki: "dtxWiki",
  AI: "AI",
};

export type BlockMenuEntry = {
  id: string;
  commandId?: EditorCommandId;
  group: BlockMenuGroup;
  // `label` stays the i18next `defaultValue` fallback (and is what
  // filterBlockEntries's keyword search matches against); `labelKey` is the
  // `pages:blocks.entries.<id>` key the menu resolves through `t()`.
  label: string;
  labelKey: string;
  keywords: string[];
  template: string;
};

const CURSOR_MARKER = "{{cursor}}";
const DECISION_ID_MARKER = "{{decisionId}}";
const ACTION_ID_MARKER = "{{actionId}}";

function randomShortId(): string {
  const bytes = new Uint8Array(4);
  const cryptoApi: Crypto | undefined =
    typeof globalThis !== "undefined"
      ? (globalThis.crypto as Crypto | undefined)
      : undefined;
  if (cryptoApi?.getRandomValues) {
    cryptoApi.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  let value = "";
  for (let i = 0; i < bytes.length; i += 1) {
    value += bytes[i].toString(36).padStart(2, "0");
  }
  return value.slice(0, 6);
}

const ALL_BLOCK_MENU_GROUPS: BlockMenuGroup[] = ["Text", "Lists", "Media", "dtxWiki", "AI"];

export const BLOCK_MENU_GROUPS: BlockMenuGroup[] = AI_INLINE_REWRITE_ENABLED
  ? ALL_BLOCK_MENU_GROUPS
  : ALL_BLOCK_MENU_GROUPS.filter((group) => group !== "AI");

const ALL_BLOCK_MENU_ENTRIES: BlockMenuEntry[] = [
  {
    id: "h1",
    group: "Text",
    label: "Heading 1",
    labelKey: "blocks.entries.h1",
    keywords: ["h1", "title"],
    template: "# {{cursor}}Heading",
  },
  {
    id: "h2",
    commandId: "heading",
    group: "Text",
    label: "Heading 2",
    labelKey: "blocks.entries.h2",
    keywords: ["h2", "section"],
    template: "## {{cursor}}Heading",
  },
  {
    id: "h3",
    group: "Text",
    label: "Heading 3",
    labelKey: "blocks.entries.h3",
    keywords: ["h3", "subsection"],
    template: "### {{cursor}}Heading",
  },
  {
    id: "paragraph",
    group: "Text",
    label: "Paragraph",
    labelKey: "blocks.entries.paragraph",
    keywords: ["text", "body"],
    template: "{{cursor}}Paragraph",
  },
  {
    id: "bold",
    commandId: "bold",
    group: "Text",
    label: "Bold",
    labelKey: "blocks.entries.bold",
    keywords: ["strong", "format"],
    template: "**{{cursor}}bold text**",
  },
  {
    id: "italic",
    commandId: "italic",
    group: "Text",
    label: "Italic",
    labelKey: "blocks.entries.italic",
    keywords: ["emphasis", "format"],
    template: "*{{cursor}}italic text*",
  },
  {
    id: "strikethrough",
    commandId: "strikethrough",
    group: "Text",
    label: "Strikethrough",
    labelKey: "blocks.entries.strikethrough",
    keywords: ["strike", "format"],
    template: "~~{{cursor}}strikethrough text~~",
  },
  {
    id: "inline-code",
    commandId: "inline-code",
    group: "Text",
    label: "Inline code",
    labelKey: "blocks.entries.inline-code",
    keywords: ["code", "monospace"],
    template: "`{{cursor}}code`",
  },
  {
    id: "link",
    commandId: "link",
    group: "Text",
    label: "Link",
    labelKey: "blocks.entries.link",
    keywords: ["url", "hyperlink"],
    template: "[{{cursor}}link text](https://example.com)",
  },
  {
    id: "quote",
    commandId: "quote",
    group: "Text",
    label: "Quote",
    labelKey: "blocks.entries.quote",
    keywords: ["blockquote"],
    template: "> {{cursor}}Quote",
  },
  {
    id: "code-block",
    commandId: "code-block",
    group: "Text",
    label: "Code block",
    labelKey: "blocks.entries.code-block",
    keywords: ["pre", "fence"],
    template: "```text\n{{cursor}}\n```",
  },
  {
    id: "inline-math",
    commandId: "math",
    group: "Text",
    label: "Inline math",
    labelKey: "blocks.entries.inline-math",
    keywords: ["math", "latex", "formula", "katex"],
    template: "$ {{cursor}}x^2 $",
  },
  {
    id: "display-math",
    commandId: "math",
    group: "Text",
    label: "Display math",
    labelKey: "blocks.entries.display-math",
    keywords: ["math", "latex", "equation", "katex"],
    template: "$$\n{{cursor}}x^2\n$$",
  },
  {
    id: "emoji",
    commandId: "emoji",
    group: "Text",
    label: "Emoji",
    labelKey: "blocks.entries.emoji",
    keywords: ["emoji", "symbol", "reaction", "icon"],
    template: "{{cursor}}",
  },
  {
    id: "divider",
    group: "Text",
    label: "Divider",
    labelKey: "blocks.entries.divider",
    keywords: ["rule", "hr"],
    template: "---",
  },
  {
    id: "bulleted-list",
    commandId: "bulleted-list",
    group: "Lists",
    label: "Bulleted list",
    labelKey: "blocks.entries.bulleted-list",
    keywords: ["ul", "bullet"],
    template: "- {{cursor}}List item",
  },
  {
    id: "numbered-list",
    commandId: "ordered-list",
    group: "Lists",
    label: "Numbered list",
    labelKey: "blocks.entries.numbered-list",
    keywords: ["ol", "ordered"],
    template: "1. {{cursor}}List item",
  },
  {
    id: "task-list",
    commandId: "task-list",
    group: "Lists",
    label: "Task list",
    labelKey: "blocks.entries.task-list",
    keywords: ["todo", "checkbox"],
    template: "- [ ] {{cursor}}Task",
  },
  {
    id: "image",
    commandId: "image",
    group: "Media",
    label: "Image",
    labelKey: "blocks.entries.image",
    keywords: ["picture", "asset"],
    template: "{{cursor}}",
  },
  {
    id: "attach",
    commandId: "attach",
    group: "Media",
    label: "Attach files",
    labelKey: "blocks.entries.attach",
    keywords: ["upload", "file", "image"],
    template: "{{cursor}}",
  },
  {
    id: "embed-link",
    group: "Media",
    label: "Embed link card",
    labelKey: "blocks.entries.embed-link",
    keywords: ["url", "preview", "bookmark"],
    template: "[{{cursor}}Link title](https://example.com)",
  },
  {
    id: "table",
    commandId: "table",
    group: "Media",
    label: "Table",
    labelKey: "blocks.entries.table",
    keywords: ["grid", "gfm"],
    template: "| {{cursor}}Column | Column |\n|---|---|\n| Value | Value |",
  },
  {
    id: "wikilink",
    commandId: "wikilink",
    group: "dtxWiki",
    label: "Wikilink",
    labelKey: "blocks.entries.wikilink",
    keywords: ["page", "internal", "link"],
    template: "[[{{cursor}}page-slug|Page title]]",
  },
  {
    id: "citation",
    commandId: "citation",
    group: "dtxWiki",
    label: "Citation",
    labelKey: "blocks.entries.citation",
    keywords: ["cite", "source", "reference"],
    template: "@cite{{{cursor}}source-id}",
  },
  {
    id: "decision",
    group: "dtxWiki",
    label: "Decision block",
    labelKey: "blocks.entries.decision",
    keywords: ["adr", "record"],
    template:
      "<!-- decision id={{decisionId}} status=proposed owner=@owner -->\n{{cursor}}Decision summary\n<!-- /decision -->",
  },
  {
    id: "action",
    group: "dtxWiki",
    label: "Action block",
    labelKey: "blocks.entries.action",
    keywords: ["task", "owner", "due"],
    template:
      "<!-- action id={{actionId}} owner=@owner due=YYYY-MM-DD -->\n- {{cursor}}Action item\n<!-- /action -->",
  },
  {
    id: "footnote",
    group: "dtxWiki",
    label: "Footnote",
    labelKey: "blocks.entries.footnote",
    keywords: ["note", "reference"],
    template: "Footnote reference.[^note]\n\n[^note]: {{cursor}}Footnote text",
  },
  {
    id: "callout-note",
    group: "dtxWiki",
    label: "Callout note",
    labelKey: "blocks.entries.callout-note",
    keywords: ["alert", "note"],
    template: "> [!NOTE]\n> {{cursor}}Note",
  },
  {
    id: "callout-tip",
    group: "dtxWiki",
    label: "Callout tip",
    labelKey: "blocks.entries.callout-tip",
    keywords: ["alert", "tip"],
    template: "> [!TIP]\n> {{cursor}}Tip",
  },
  {
    id: "callout-warning",
    group: "dtxWiki",
    label: "Callout warning",
    labelKey: "blocks.entries.callout-warning",
    keywords: ["alert", "warning"],
    template: "> [!WARNING]\n> {{cursor}}Warning",
  },
  {
    id: "callout-important",
    group: "dtxWiki",
    label: "Callout important",
    labelKey: "blocks.entries.callout-important",
    keywords: ["alert", "important"],
    template: "> [!IMPORTANT]\n> {{cursor}}Important",
  },
  {
    id: "callout-caution",
    group: "dtxWiki",
    label: "Callout caution",
    labelKey: "blocks.entries.callout-caution",
    keywords: ["alert", "caution"],
    template: "> [!CAUTION]\n> {{cursor}}Caution",
  },
  {
    id: "ai-continue",
    group: "AI",
    label: "Continue writing",
    labelKey: "blocks.entries.ai-continue",
    keywords: ["draft", "extend", "continue"],
    template: "{{cursor}}",
  },
  {
    id: "ai-summarize",
    group: "AI",
    label: "Summarize selection",
    labelKey: "blocks.entries.ai-summarize",
    keywords: ["summary", "compress"],
    template: "{{cursor}}",
  },
  {
    id: "ai-claims",
    group: "AI",
    label: "Extract claims with citations",
    labelKey: "blocks.entries.ai-claims",
    keywords: ["claims", "citation", "source"],
    template: "{{cursor}}",
  },
  {
    id: "ai-decision",
    group: "AI",
    label: "Rewrite as Decision Record",
    labelKey: "blocks.entries.ai-decision",
    keywords: ["decision", "adr"],
    template: "{{cursor}}",
  },
  {
    id: "ai-translate",
    group: "AI",
    label: "Translate",
    labelKey: "blocks.entries.ai-translate",
    keywords: ["language", "localize"],
    template: "{{cursor}}",
  },
  {
    id: "ai-tighten",
    group: "AI",
    label: "Tighten prose",
    labelKey: "blocks.entries.ai-tighten",
    keywords: ["rewrite", "clean", "style"],
    template: "{{cursor}}",
  },
];

export const BLOCK_MENU_ENTRIES: BlockMenuEntry[] = AI_INLINE_REWRITE_ENABLED
  ? ALL_BLOCK_MENU_ENTRIES
  : ALL_BLOCK_MENU_ENTRIES.filter((entry) => entry.group !== "AI");

export function filterBlockEntries(query: string) {
  const normalized = query.trim().toLowerCase();
  if (!normalized) {
    return BLOCK_MENU_ENTRIES;
  }
  return BLOCK_MENU_ENTRIES.filter((entry) =>
    [entry.label, entry.group, ...entry.keywords]
      .join(" ")
      .toLowerCase()
      .includes(normalized),
  );
}

export function blockSnippet(template: string) {
  let text = template;
  while (text.includes(DECISION_ID_MARKER)) {
    text = text.replace(DECISION_ID_MARKER, `dec-${randomShortId()}`);
  }
  while (text.includes(ACTION_ID_MARKER)) {
    text = text.replace(ACTION_ID_MARKER, `act-${randomShortId()}`);
  }
  const markerIndex = text.indexOf(CURSOR_MARKER);
  if (markerIndex === -1) {
    return { text, cursorOffset: text.length };
  }
  return {
    text: text.replace(CURSOR_MARKER, ""),
    cursorOffset: markerIndex,
  };
}

export function blockInsertRangeForSelection(view: EditorViewType) {
  const { head } = view.state.selection.main;
  const line = view.state.doc.lineAt(head);
  const lineText = view.state.doc.sliceString(line.from, line.to);
  if (lineText.trim().length === 0) {
    return { from: line.from, to: line.to };
  }
  return { from: line.to, to: line.to };
}

export function blockInsertPrefix(
  view: EditorViewType,
  range: { from: number; to: number },
) {
  const line = view.state.doc.lineAt(range.from);
  const lineText = view.state.doc.sliceString(line.from, line.to);
  if (lineText.trim().length === 0) {
    return "";
  }
  return "\n\n";
}
