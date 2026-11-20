import type {
  ContextMergeCandidate,
  ContextMergeChoiceValue,
  ContextMergeMetadataInput,
  ContextMergePreview,
} from "../../shared/api/pages";
import type { components } from "../../shared/api/types.gen";

export type { ContextMergeCandidate, ContextMergePreview };
export type ContextMergeChoice = ContextMergeChoiceValue;
export type ContextMergeSide = components["schemas"]["ContextMergeSide"];
export type ContextMergeMetadataField = "title" | "page_type" | "placement_parent_node_id";
export type ContextMergeConflictRegion = ContextMergeCandidate["conflicts"][number];
/** The draft (or the person's own save) is "ours"; what it merges with is "theirs". */
export type ContextMergeSideKey = "ours" | "theirs";

/** Metadata the person changed in the merge dialog; omitted fields keep the merge result. */
export type ContextMergeMetadataEdit = Pick<ContextMergeMetadataInput, ContextMergeMetadataField>;

export type ContextMergeResolution = {
  choice: ContextMergeChoice;
  candidate: ContextMergeCandidate;
  /** Null when the person kept the merge module's text as it is. */
  bodyMd: string | null;
  /** Null when every conflicted field keeps the merge result. */
  pageMetadata: ContextMergeMetadataEdit | null;
};

const METADATA_FIELDS: readonly ContextMergeMetadataField[] = ["title", "page_type", "placement_parent_node_id"];

export function conflictedMetadataFields(preview: Pick<ContextMergePreview, "metadata_conflicts">): ContextMergeMetadataField[] {
  return METADATA_FIELDS.filter((field) => preview.metadata_conflicts.includes(field));
}

/** The leading frontmatter block: its line count and its length, both 0 when there is none. */
export function frontmatterExtent(text: string): { lines: number; length: number } {
  if (!text.startsWith("---\n")) return { lines: 0, length: 0 };
  const lines = text.split("\n");
  let length = lines[0].length + 1;
  for (let index = 1; index < lines.length; index += 1) {
    length += lines[index].length + 1;
    if (lines[index] === "---") return { lines: index + 1, length: Math.min(length, text.length) };
  }
  return { lines: 0, length: 0 };
}

/**
 * How many places of a conflict the person decides: one per merge item, the
 * same number the version bar, the editor, the Review screen and the dialog's
 * cards state. A virtual-base conflict can need a choice without a place of
 * its own, so a conflict is never zero places.
 */
export function mergeOverlapCount(preview: Pick<ContextMergePreview, "keep_both" | "metadata_conflicts">): number {
  return Math.max(1, mergeItems(preview).length);
}

/** The text the merge module produced; conflict offsets index into it. */
export function mergeSourceText(candidate: ContextMergeCandidate): string {
  return candidate.conflict_body_md ?? candidate.body_md;
}

/**
 * Converts a Unicode code point offset (the server's `offset_unit`) to a
 * JavaScript string index. Characters outside the BMP count once on the server
 * and twice in UTF-16, so every astral character before the offset shifts it.
 */
export function codePointOffsetToIndex(text: string, offset: number): number {
  if (offset <= 0) return 0;
  let index = 0;
  let codePoints = 0;
  while (index < text.length && codePoints < offset) {
    const code = text.charCodeAt(index);
    index += code >= 0xd800 && code <= 0xdbff && index + 1 < text.length ? 2 : 1;
    codePoints += 1;
  }
  return index;
}

export function metadataValue(
  metadata: { title?: string | null; page_type?: string | null; placement_parent_node_id?: string | null },
  field: ContextMergeMetadataField,
): string | null {
  const value = metadata[field];
  return typeof value === "string" ? value : null;
}

/** The full body and metadata the person chose, for writers that send the whole result. */
export function resolvedMergeResult(resolution: ContextMergeResolution) {
  const metadata = resolution.candidate.page_metadata;
  const edit = resolution.pageMetadata ?? {};
  return {
    body_md: resolution.bodyMd ?? resolution.candidate.body_md,
    page_metadata: {
      title: edit.title ?? metadata.title,
      page_type: edit.page_type ?? metadata.page_type,
      tags: [...metadata.tags],
      aliases: [...metadata.aliases],
      placement_parent_node_id: "placement_parent_node_id" in edit
        ? edit.placement_parent_node_id ?? null
        : metadata.placement_parent_node_id ?? null,
    },
  };
}

/**
 * One place the person decides. Conflicted properties come first, as the
 * frontmatter does; then each overlapping region of the body in order. A
 * region inside the frontmatter is not a place of its own: its title and type
 * lines are the property items, and tags and aliases merge as sets on the
 * server (`merge_metadata`), so they never conflict.
 */
export type ContextMergeItem =
  | { key: string; kind: "field"; field: ContextMergeMetadataField }
  | {
    key: string;
    kind: "region";
    conflict: ContextMergeConflictRegion;
    /** 1-based lines of the region in the keep-both text. */
    startLine: number;
    endLine: number;
    /** The headings above the region, outermost first. */
    headings: string[];
  };

export function mergeItems(preview: Pick<ContextMergePreview, "keep_both" | "metadata_conflicts">): ContextMergeItem[] {
  const items: ContextMergeItem[] = conflictedMetadataFields(preview).map((field) => ({ key: `field:${field}`, kind: "field", field }));
  const candidate = preview.keep_both;
  if (!candidate) return items;
  const text = mergeSourceText(candidate);
  const frontmatter = frontmatterExtent(text);
  const lines = text.split("\n");
  candidate.conflicts.forEach((conflict, index) => {
    const start = codePointOffsetToIndex(text, conflict.keep_both_start);
    if (start < frontmatter.length) return;
    const end = codePointOffsetToIndex(text, conflict.keep_both_end);
    const [startLine, endLine] = lineSpan(text, start, end);
    items.push({ key: `region:${index}`, kind: "region", conflict, startLine, endLine, headings: headingsAbove(lines, frontmatter.lines, startLine) });
  });
  return items;
}

/** 1-based inclusive lines of [start, end); an end after a newline belongs to the line before it. */
function lineSpan(text: string, start: number, end: number): [number, number] {
  const lineOf = (index: number) => {
    let line = 1;
    for (let position = 0; position < index && position < text.length; position += 1) {
      if (text.charCodeAt(position) === 10) line += 1;
    }
    return line;
  };
  const last = end > start && text.charCodeAt(end - 1) === 10 ? end - 1 : end;
  const startLine = lineOf(start);
  return [startLine, Math.max(startLine, lineOf(Math.max(start, last)))];
}

function headingsAbove(lines: string[], fromLine: number, beforeLine: number): string[] {
  const stack: Array<{ level: number; title: string }> = [];
  let fence = false;
  for (let index = fromLine; index < beforeLine - 1 && index < lines.length; index += 1) {
    const line = lines[index];
    if (/^\s*(```|~~~)/.test(line)) fence = !fence;
    if (fence) continue;
    const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (!heading) continue;
    const level = heading[1].length;
    while (stack.length > 0 && stack[stack.length - 1].level >= level) stack.pop();
    stack.push({ level, title: heading[2] });
  }
  return stack.map((entry) => entry.title);
}

/** The method the merge module applies to every place at once. */
export type ContextMergeMethod = ContextMergeChoice;

/** The person's own answer for one place; "method" follows the method that decided it. */
export type ContextMergeItemChoice =
  | { source: "method" }
  | { source: ContextMergeSideKey }
  | { source: "both" }
  | { source: "edit"; value: string | null };

/** What a place resolves to: one side, both (a body region only), or the person's text. */
export type ContextMergeOutcome = ContextMergeSideKey | "both" | "edit";

export function itemOutcome(
  preview: ContextMergePreview,
  item: ContextMergeItem,
  choice: ContextMergeItemChoice,
  method: ContextMergeMethod,
): ContextMergeOutcome {
  if (choice.source !== "method") return choice.source;
  if (item.kind === "region") return method === "keep_both" ? "both" : preview.latest_side;
  const value = metadataValue(preview[method]?.page_metadata ?? {}, item.field);
  const sides = preview.sides;
  if (sides && value === metadataValue(sides.ours.metadata, item.field)) return "ours";
  if (sides && value === metadataValue(sides.theirs.metadata, item.field)) return "theirs";
  return preview.latest_side;
}

function sliceOf(candidate: ContextMergeCandidate | null | undefined, start: number, end: number): string {
  if (!candidate) return "";
  const text = mergeSourceText(candidate);
  return text.slice(codePointOffsetToIndex(text, start), codePointOffsetToIndex(text, end));
}

/** The text a body region contributes to the result. */
export function regionResult(
  preview: ContextMergePreview,
  item: Extract<ContextMergeItem, { kind: "region" }>,
  choice: ContextMergeItemChoice,
  method: ContextMergeMethod,
): string {
  const { conflict } = item;
  const both = sliceOf(preview.keep_both, conflict.keep_both_start, conflict.keep_both_end);
  if (choice.source === "edit") {
    const value = choice.value ?? "";
    // The region owns its closing newline; an edit keeps the line structure.
    return value && both.endsWith("\n") && !value.endsWith("\n") ? `${value}\n` : value;
  }
  if (choice.source === "ours") return conflict.ours;
  if (choice.source === "theirs") return conflict.theirs;
  if (choice.source === "both" || method === "keep_both") return both;
  return sliceOf(preview.latest, conflict.latest_start, conflict.latest_end);
}

/** The value a conflicted property takes in the result. */
export function fieldResult(
  preview: ContextMergePreview,
  field: ContextMergeMetadataField,
  choice: ContextMergeItemChoice,
  method: ContextMergeMethod,
): string | null {
  if (choice.source === "edit") return choice.value;
  if ((choice.source === "ours" || choice.source === "theirs") && preview.sides) {
    return metadataValue(preview.sides[choice.source].metadata, field);
  }
  return metadataValue(preview[method]?.page_metadata ?? {}, field);
}

/**
 * The result of "Custom": the keep-both text with every body region replaced
 * by its answer, under the server's prepared frontmatter with the chosen title
 * and type written into it. Body preparation reads the type from the
 * frontmatter, so the chosen value goes there as well as into the metadata.
 */
export function customMergeResolution(
  preview: ContextMergePreview,
  items: ContextMergeItem[],
  choices: Record<string, ContextMergeItemChoice>,
  method: ContextMergeMethod,
): ContextMergeResolution | null {
  const candidate = preview.keep_both;
  if (!candidate) return null;
  const choiceOf = (item: ContextMergeItem) => choices[item.key] ?? { source: "method" as const };
  let body = mergeSourceText(candidate);
  const regions = items
    .filter((item): item is Extract<ContextMergeItem, { kind: "region" }> => item.kind === "region")
    .map((item) => ({
      start: codePointOffsetToIndex(body, item.conflict.keep_both_start),
      end: codePointOffsetToIndex(body, item.conflict.keep_both_end),
      text: regionResult(preview, item, choiceOf(item), method),
    }))
    .sort((left, right) => right.start - left.start);
  for (const region of regions) body = body.slice(0, region.start) + region.text + body.slice(region.end);
  const prepared = frontmatterExtent(candidate.body_md);
  let head = candidate.body_md.slice(0, prepared.length);
  const edit: ContextMergeMetadataEdit = {};
  for (const item of items) {
    if (item.kind !== "field") continue;
    const value = fieldResult(preview, item.field, choiceOf(item), method);
    if (item.field === "title" && value) head = withFrontmatterLine(head, "title", JSON.stringify(value));
    if (item.field === "page_type" && value) head = withFrontmatterLine(head, "type", value);
    if (value === metadataValue(candidate.page_metadata, item.field)) continue;
    if (item.field === "placement_parent_node_id") edit.placement_parent_node_id = value;
    else if (item.field === "page_type" && value) edit.page_type = value as ContextMergeMetadataEdit["page_type"];
    else if (item.field === "title" && value) edit.title = value;
  }
  if (head) body = head + body.slice(frontmatterExtent(body).length);
  return { choice: "keep_both", candidate, bodyMd: body, pageMetadata: Object.keys(edit).length > 0 ? edit : null };
}

function withFrontmatterLine(head: string, key: string, value: string): string {
  const lines = head.split("\n");
  const index = lines.findIndex((line, position) => position > 0 && line.startsWith(`${key}:`));
  if (index >= 0) lines[index] = `${key}: ${value}`;
  else lines.splice(1, 0, `${key}: ${value}`);
  return lines.join("\n");
}

/**
 * Line differences from `from` to `to` (Myers): which lines of `to` are new,
 * and how many lines of `from` were removed before each line of `to` (the last
 * entry counts removals at the end). Null when the texts differ too much to
 * mark usefully.
 */
export function lineChanges(from: string[], to: string[], maxEdits = 600): { added: boolean[]; removedBefore: number[] } | null {
  const n = from.length;
  const m = to.length;
  const offset = n + m + 1;
  const v = new Int32Array(2 * offset + 1);
  const trace: Int32Array[] = [];
  let found = -1;
  for (let d = 0; d <= Math.min(n + m, maxEdits); d += 1) {
    for (let k = -d; k <= d; k += 2) {
      let x = d === 0 ? 0 : k === -d || (k !== d && v[offset + k - 1] < v[offset + k + 1]) ? v[offset + k + 1] : v[offset + k - 1] + 1;
      let y = x - k;
      while (x < n && y < m && from[x] === to[y]) { x += 1; y += 1; }
      v[offset + k] = x;
    }
    trace.push(v.slice(offset - d, offset + d + 1));
    if (v[offset + n - m] >= n && Math.abs(n - m) <= d && (n - m - d) % 2 === 0) { found = d; break; }
  }
  if (found < 0) return null;
  const added = new Array<boolean>(m).fill(false);
  const removedBefore = new Array<number>(m + 1).fill(0);
  let x = n;
  let y = m;
  for (let d = found; d > 0; d -= 1) {
    const previous = trace[d - 1];
    const at = (k: number) => previous[k + d - 1];
    const k = x - y;
    const down = k === -d || (k !== d && at(k - 1) < at(k + 1));
    const previousK = down ? k + 1 : k - 1;
    const previousX = at(previousK);
    const previousY = previousX - previousK;
    while (x > previousX && y > previousY) { x -= 1; y -= 1; }
    if (down) added[previousY] = true;
    else removedBefore[y] += 1;
    x = previousX;
    y = previousY;
  }
  return { added, removedBefore };
}

/**
 * Word-level marks between two short texts: for each token of each side,
 * whether it is not shared with the other. Null when the texts are too long
 * to compare word by word.
 */
export function wordChanges(left: string, right: string, limit = 160_000): { left: Array<[string, boolean]>; right: Array<[string, boolean]> } | null {
  const a = left.match(/\s+|[^\s]+/g) ?? [];
  const b = right.match(/\s+|[^\s]+/g) ?? [];
  if (a.length * b.length > limit) return null;
  const table = Array.from({ length: a.length + 1 }, () => new Uint16Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const keptA = new Array<boolean>(a.length).fill(false);
  const keptB = new Array<boolean>(b.length).fill(false);
  for (let i = 0, j = 0; i < a.length && j < b.length;) {
    if (a[i] === b[j]) { keptA[i] = true; keptB[j] = true; i += 1; j += 1; }
    else if (table[i + 1][j] >= table[i][j + 1]) i += 1;
    else j += 1;
  }
  const mark = (tokens: string[], kept: boolean[]) => tokens.map((token, index): [string, boolean] => [token, !kept[index] && /\S/.test(token)]);
  return { left: mark(a, keptA), right: mark(b, keptB) };
}
