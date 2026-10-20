export type PageType =
  | "overview"
  | "source"
  | "entity"
  | "concept"
  | "synthesis"
  | "comparison"
  | "decision"
  | "contradiction"
  | "query"
  | "guide";

export type PageIcon = "lucide:file-text" | "lucide:book-open" | "lucide:lightbulb" | "lucide:rocket";

export type PageMetadata = {
  icon: PageIcon;
  type: PageType;
  tags: string[];
  aliases: string[];
  extraLines: string[];
};

export type ParsedPageDocument = {
  body: string;
  metadata: PageMetadata;
};

type KnownFrontmatterValues = {
  aliases: string[];
  aliasesPresent: boolean;
  hasKnownKey: boolean;
  icon: string | null;
  knownLineIndexes: Set<number>;
  tags: string[];
  tagsPresent: boolean;
  type: string | null;
};

export const PAGE_TYPE_OPTIONS: Array<{ value: PageType; label: string }> = [
  { value: "overview", label: "Overview" },
  { value: "source", label: "Source" },
  { value: "entity", label: "Entity" },
  { value: "concept", label: "Concept" },
  { value: "synthesis", label: "Synthesis" },
  { value: "comparison", label: "Comparison" },
  { value: "decision", label: "Decision" },
  { value: "contradiction", label: "Contradiction" },
  { value: "query", label: "Query" },
  { value: "guide", label: "Guide" },
];

export const PAGE_ICON_OPTIONS: Array<{ value: PageIcon; label: string }> = [
  { value: "lucide:file-text", label: "Document" },
  { value: "lucide:book-open", label: "Guide" },
  { value: "lucide:lightbulb", label: "Idea" },
  { value: "lucide:rocket", label: "Launch" },
];

export const DEFAULT_PAGE_METADATA: PageMetadata = {
  icon: "lucide:file-text",
  type: "overview",
  tags: [],
  aliases: [],
  extraLines: [],
};

const FRONTMATTER_DELIMITERS = new Set(["---", "..."]);
const KNOWN_FRONTMATTER_KEYS = new Set([
  "title",
  "slug",
  "icon",
  "type",
  "tags",
  "aliases",
]);

export function parsePageDocument(
  markdown: string,
  fallback: Partial<PageMetadata> = {},
): ParsedPageDocument {
  const defaults = metadataWithFallback(fallback);
  const normalized = markdown.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const lines = normalized.split("\n");
  if (lines.length < 3 || lines[0].trim() !== "---") {
    return { body: markdown, metadata: defaults };
  }
  const closingIndex = lines.findIndex((line, index) =>
    index > 0 && FRONTMATTER_DELIMITERS.has(line.trim()),
  );
  if (closingIndex === -1) {
    return { body: markdown, metadata: defaults };
  }
  const frontmatterLines = lines.slice(1, closingIndex);
  const values = parseKnownFrontmatter(frontmatterLines);
  if (!values.hasKnownKey) {
    return { body: markdown, metadata: defaults };
  }
  return {
    body: lines.slice(closingIndex + 1).join("\n").replace(/^\n/, ""),
    metadata: {
      icon: toPageIcon(values.icon) ?? defaults.icon,
      type: toPageType(values.type) ?? defaults.type,
      tags: values.tagsPresent ? values.tags : defaults.tags,
      aliases: values.aliasesPresent ? values.aliases : defaults.aliases,
      extraLines: frontmatterLines.filter((_, index) => !values.knownLineIndexes.has(index)),
    },
  };
}

export function serializePageDocument({
  body,
  metadata,
  slug,
  title,
}: {
  body: string;
  metadata: PageMetadata;
  slug: string;
  title: string;
}) {
  const lines = [
    "---",
    `title: ${quoteYamlString(title.trim() || "Untitled")}`,
    `slug: ${slug}`,
    `icon: ${quoteYamlString(metadata.icon)}`,
    `type: ${metadata.type}`,
    `tags: [${metadata.tags.map(quoteYamlString).join(", ")}]`,
    `aliases: [${metadata.aliases.map(quoteYamlString).join(", ")}]`,
    ...metadata.extraLines.filter((line) => !isKnownFrontmatterLine(line)),
    "---",
    "",
  ];
  return `${lines.join("\n")}${body.replace(/^\n+/, "")}`;
}

export function normalizeTagsInput(input: string) {
  return normalizeTags(input.split(","));
}

export function normalizeAliasesInput(input: string) {
  return normalizeAliases(input.split(","));
}

export function pageTypeLabel(value: PageType) {
  return PAGE_TYPE_OPTIONS.find((option) => option.value === value)?.label ?? "Overview";
}

export function pageIconLabel(value: PageIcon) {
  return PAGE_ICON_OPTIONS.find((option) => option.value === value)?.label ?? "Document";
}

function metadataWithFallback(fallback: Partial<PageMetadata>): PageMetadata {
  return {
    icon: toPageIcon(fallback.icon) ?? DEFAULT_PAGE_METADATA.icon,
    type: toPageType(fallback.type) ?? DEFAULT_PAGE_METADATA.type,
    tags: normalizeTags(fallback.tags ?? []),
    aliases: normalizeAliases(fallback.aliases ?? []),
    extraLines: fallback.extraLines ?? [],
  };
}

function parseKnownFrontmatter(lines: string[]): KnownFrontmatterValues {
  const values = {
    aliases: [] as string[],
    aliasesPresent: false,
    hasKnownKey: false,
    icon: null as string | null,
    knownLineIndexes: new Set<number>(),
    tags: [] as string[],
    tagsPresent: false,
    type: null as string | null,
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const match = /^([A-Za-z_][\w-]*)\s*:\s*(.*)$/.exec(line);
    if (!match) {
      continue;
    }
    const key = match[1];
    if (!KNOWN_FRONTMATTER_KEYS.has(key)) {
      continue;
    }
    values.hasKnownKey = true;
    values.knownLineIndexes.add(index);
    const rawValue = match[2];
    const value = parseScalar(rawValue);
    const block = collectIndentedBlock(lines, index + 1);
    block.indexes.forEach((lineIndex) => values.knownLineIndexes.add(lineIndex));
    if (key === "icon") values.icon = value;
    if (key === "type") values.type = value;
    if (key === "aliases") {
      values.aliasesPresent = true;
      values.aliases = rawValue.trim() ? parseAliases(rawValue) : parseBlockAliases(block.lines);
    }
    if (key === "tags") {
      values.tagsPresent = true;
      values.tags = rawValue.trim() ? parseTags(rawValue) : parseBlockTags(block.lines);
    }
  }
  return values;
}

function parseScalar(value: string) {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "null") {
    return "";
  }
  if (trimmed.startsWith("\"") && trimmed.endsWith("\"")) {
    // We serialize quoted scalar values with JSON.stringify(). Parse the same
    // JSON subset here so persisted Unicode and escaped quote/backslash values
    // round-trip instead of becoming literal `\\u....` text in the editor.
    try {
      const parsed = JSON.parse(trimmed);
      if (typeof parsed === "string") {
        return parsed;
      }
    } catch {
      // Keep the parser forgiving for hand-authored frontmatter. This matches
      // the previous behavior for malformed quoted scalar text.
    }
    return trimmed.slice(1, -1);
  }
  if (trimmed.startsWith("'") && trimmed.endsWith("'")) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function parseTags(value: string) {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "[]") {
    return [];
  }
  return normalizeTags(parseInlineMetadataValues(trimmed));
}

function parseBlockTags(lines: string[]) {
  return normalizeTags(
    lines
      .map((line) => /^\s*-\s*(.*)$/.exec(line)?.[1] ?? "")
      .filter(Boolean)
      .map(parseScalar),
  );
}

function parseAliases(value: string) {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "[]") {
    return [];
  }
  return normalizeAliases(parseInlineMetadataValues(trimmed));
}

function parseInlineMetadataValues(value: string) {
  const raw = value.startsWith("[") && value.endsWith("]") ? value.slice(1, -1) : value;
  const values: string[] = [];
  let token = "";
  let quote = "";
  let escaped = false;
  for (const character of raw) {
    if (quote) {
      token += character;
      if (escaped) escaped = false;
      else if (character === "\\" && quote === '"') escaped = true;
      else if (character === quote) quote = "";
    } else if (character === ",") {
      values.push(parseScalar(token));
      token = "";
    } else {
      if ((character === '"' || character === "'") && !token.trim()) quote = character;
      token += character;
    }
  }
  values.push(parseScalar(token));
  return values;
}

function parseBlockAliases(lines: string[]) {
  return normalizeAliases(
    lines
      .map((line) => /^\s*-\s*(.*)$/.exec(line)?.[1] ?? "")
      .filter(Boolean)
      .map(parseScalar),
  );
}

function collectIndentedBlock(lines: string[], startIndex: number) {
  const block = {
    indexes: [] as number[],
    lines: [] as string[],
  };
  for (let index = startIndex; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim()) {
      block.indexes.push(index);
      block.lines.push(line);
      continue;
    }
    if (/^\s+/.test(line) || /^\s*-\s+/.test(line)) {
      block.indexes.push(index);
      block.lines.push(line);
      continue;
    }
    break;
  }
  return block;
}

function normalizeTags(tags: string[]) {
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const tag of tags) {
    const value = normalizeTag(tag);
    const key = value.toLocaleLowerCase();
    if (!value || seen.has(key)) {
      continue;
    }
    seen.add(key);
    normalized.push(value);
  }
  return normalized;
}

function normalizeAliases(aliases: string[]) {
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const alias of aliases) {
    const value = normalizeAlias(alias);
    const key = value.toLocaleLowerCase();
    if (!value || seen.has(key)) {
      continue;
    }
    seen.add(key);
    normalized.push(value);
  }
  return normalized;
}

function normalizeTag(tag: string) {
  return tag
    .trim()
    .replace(/^#+/, "")
    .replace(/\s+/g, " ");
}

function normalizeAlias(alias: string) {
  return alias.trim().replace(/\s+/g, " ");
}

function toPageType(value?: string | null): PageType | null {
  return PAGE_TYPE_OPTIONS.some((option) => option.value === value) ? (value as PageType) : null;
}

function toPageIcon(value?: string | null): PageIcon | null {
  return PAGE_ICON_OPTIONS.some((option) => option.value === value) ? (value as PageIcon) : null;
}

function isKnownFrontmatterLine(line: string) {
  const match = /^([A-Za-z_][\w-]*)\s*:/.exec(line);
  return Boolean(match && KNOWN_FRONTMATTER_KEYS.has(match[1]));
}

function quoteYamlString(value: string) {
  return JSON.stringify(value);
}
