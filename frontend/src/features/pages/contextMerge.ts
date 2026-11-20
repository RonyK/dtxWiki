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
 * How many places of a conflict the person decides, one count for every
 * screen that states it: each overlapping region of the body, each conflicted
 * property, and the tree location, which the Markdown does not hold. Title and
 * type are frontmatter lines, so an overlap there is a property's own line and
 * is counted with the properties, never twice; the frontmatter counts more
 * only when it has more overlaps than conflicted properties (tags, aliases).
 */
export function mergeOverlapCount(preview: Pick<ContextMergePreview, "keep_both" | "metadata_conflicts">): number {
  const fields = conflictedMetadataFields(preview);
  const outsideText = fields.filter((field) => field === "placement_parent_node_id").length;
  let frontmatterRegions = 0;
  let bodyRegions = 0;
  if (preview.keep_both) {
    const text = mergeSourceText(preview.keep_both);
    const frontmatterEnd = frontmatterExtent(text).length;
    for (const conflict of preview.keep_both.conflicts) {
      if (codePointOffsetToIndex(text, conflict.keep_both_start) < frontmatterEnd) frontmatterRegions += 1;
      else bodyRegions += 1;
    }
  }
  const properties = Math.max(frontmatterRegions, fields.length - outsideText) + outsideText;
  // A virtual-base conflict can need a choice without a region of its own.
  return Math.max(1, bodyRegions + properties);
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

/** 1-based inclusive line ranges of the regions the merge combined, for one choice. */
export function mergedLineRanges(candidate: ContextMergeCandidate, choice: ContextMergeChoice): Array<[number, number]> {
  const text = mergeSourceText(candidate);
  const lineOf = (index: number) => {
    let line = 1;
    for (let position = 0; position < index && position < text.length; position += 1) {
      if (text.charCodeAt(position) === 10) line += 1;
    }
    return line;
  };
  return candidate.conflicts.map((conflict) => {
    const start = codePointOffsetToIndex(text, choice === "keep_both" ? conflict.keep_both_start : conflict.latest_start);
    const end = codePointOffsetToIndex(text, choice === "keep_both" ? conflict.keep_both_end : conflict.latest_end);
    // An end offset sits after the region's last character; a region that ends
    // with its newline belongs to the line before it.
    const last = end > start && text.charCodeAt(end - 1) === 10 ? end - 1 : end;
    const startLine = lineOf(start);
    return [startLine, Math.max(startLine, lineOf(Math.max(start, last)))];
  });
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
