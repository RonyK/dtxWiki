import { expect, it } from "vitest";

import type { ContextMergePreview } from "../../../shared/api/pages";
import { customMergeResolution, mergeItems, regionResult } from "../contextMerge";

// Both sides changed the last alias and the first paragraph: the merge reports
// one region that begins in the frontmatter and runs into the body. Its body
// part stays a place to decide; the frontmatter follows the server's merge
// (aliases as a set).
const head = '---\ntitle: "T"\n';
const keepBoth = `${head}aliases: ["a"]\naliases: ["b"]\n---\n\npara ours\npara theirs\n\nend\n`;
const latest = `${head}aliases: ["a"]\n---\n\npara ours\n\nend\n`;
const prepared = `${head}aliases: ["a", "b"]\n---\n\npara ours\npara theirs\n\nend\n`;
const region = {
  ours: 'aliases: ["a"]\n---\n\npara ours\n',
  theirs: 'aliases: ["b"]\n---\n\npara theirs\n',
  keep_both_start: head.length,
  keep_both_end: keepBoth.indexOf("\n\nend"),
  latest_start: head.length,
  latest_end: latest.indexOf("\n\nend"),
};
const candidate = (body: string, conflictBody: string) => ({
  body_md: body, conflict_body_md: conflictBody, block_map: [], result_hash: "a".repeat(64),
  page_metadata: { title: "T", page_type: "guide", tags: [], aliases: ["a", "b"], placement_parent_node_id: null },
  conflicts: [{ ...region, keep_both_end: region.keep_both_end + 1, latest_end: region.latest_end + 1 }],
  offset_unit: "unicode_code_points" as const,
});
const preview = {
  kind: "conflict", metadata_conflicts: [], latest_side: "ours",
  keep_both: candidate(prepared, keepBoth),
  latest: candidate(prepared, latest),
} as unknown as ContextMergePreview;

it("keeps the body part of an overlap that begins in the frontmatter as a place", () => {
  const items = mergeItems(preview);
  expect(items).toHaveLength(1);
  expect(items[0]).toMatchObject({ kind: "region", ours: "\npara ours\n", theirs: "\npara theirs\n", startLine: 6 });

  // Choosing the published side replaces only the body part, under the
  // server's merged frontmatter.
  expect(customMergeResolution(preview, items, { [items[0].key]: { source: "theirs" } }, "keep_both")?.bodyMd)
    .toBe(`${head}aliases: ["a", "b"]\n---\n\npara theirs\n\nend\n`);
  // Following "latest" takes the latest text's body part.
  expect(customMergeResolution(preview, items, {}, "latest")?.bodyMd)
    .toBe(`${head}aliases: ["a", "b"]\n---\n\npara ours\n\nend\n`);
});

it("splits an overlap at the top of a page into a front matter place and a body place", () => {
  // The draft has front matter with an icon line the server keeps as written;
  // the published version is plain text. The overlap starts at the opening
  // delimiter, which is not the line closing the front matter.
  const draft = '---\ntitle: "T"\nicon: "lucide:star"\n---\n\npara new\n';
  const kept = `${draft}para legacy\n\nend\n`;
  const newest = `${draft}\nend\n`;
  const server = '---\ntitle: "T"\nslug: s\ntype: guide\ntags: []\naliases: []\n';
  const conflict = {
    ours: draft, theirs: "para legacy\n",
    keep_both_start: 0, keep_both_end: kept.indexOf("\n\nend") + 1,
    latest_start: 0, latest_end: newest.indexOf("\n\nend") + 1,
  };
  const side = (body: string, conflictBody: string) => ({
    body_md: body, conflict_body_md: conflictBody, block_map: [], result_hash: "a".repeat(64),
    page_metadata: { title: "T", page_type: "guide", tags: [], aliases: [], placement_parent_node_id: null },
    conflicts: [conflict], offset_unit: "unicode_code_points" as const,
  });
  const top = {
    kind: "conflict", metadata_conflicts: [], latest_side: "ours",
    keep_both: side(`${server}icon: "lucide:star"\n---\n\npara new\npara legacy\n\nend\n`, kept),
    latest: side(`${server}icon: "lucide:star"\n---\n\npara new\n\nend\n`, newest),
  } as unknown as ContextMergePreview;

  const [head, body] = mergeItems(top);
  expect(head).toMatchObject({ part: "frontmatter", ours: 'icon: "lucide:star"\n', theirs: "" });
  expect(body).toMatchObject({ part: "body", ours: "\npara new\n", theirs: "para legacy\n" });
  // The published side everywhere: no icon, and no front matter text in the body.
  expect(customMergeResolution(top, [head, body], { [head.key]: { source: "theirs" }, [body.key]: { source: "theirs" } }, "keep_both")?.bodyMd)
    .toBe(`${server}---\npara legacy\n\nend\n`);
  expect(customMergeResolution(top, [head, body], {}, "latest")?.bodyMd)
    .toBe(`${server}icon: "lucide:star"\n---\n\npara new\n\nend\n`);
  // An edit keeps the line ending of the result it started from.
  if (body.kind !== "region") throw new Error("body place expected");
  expect(regionResult(top, body, { source: "edit", value: "x", newline: false }, "latest")).toBe("x");
  expect(regionResult(top, body, { source: "edit", value: "x", newline: true }, "latest")).toBe("x\n");
  // A result ending in a blank line keeps it through an unchanged edit.
  expect(regionResult(top, body, { source: "edit", value: "x\n", newline: true }, "latest")).toBe("x\n\n");
});

it("never makes a place of front matter lines the server writes itself, block lists included", () => {
  // Only an indented alias entry differs: the server owns it (the aliases
  // block) and merges aliases as a set, so there is nothing to decide.
  const kept = '---\ntitle: "T"\naliases:\n  - a\n  - b\n  - c\n---\n\nbody\n';
  const conflict = {
    ours: "  - b\n", theirs: "  - c\n",
    keep_both_start: kept.indexOf("  - b"), keep_both_end: kept.indexOf("---\n\nbody"),
    latest_start: kept.indexOf("  - b"), latest_end: kept.indexOf("  - c"),
  };
  const side = (conflictBody: string) => ({
    body_md: conflictBody, conflict_body_md: conflictBody, block_map: [], result_hash: "a".repeat(64),
    page_metadata: { title: "T", page_type: "guide", tags: [], aliases: ["a", "b", "c"], placement_parent_node_id: null },
    conflicts: [conflict], offset_unit: "unicode_code_points" as const,
  });
  const blockList = {
    kind: "conflict", metadata_conflicts: [], latest_side: "ours",
    keep_both: side(kept), latest: side(kept.replace("  - c\n", "")),
  } as unknown as ContextMergePreview;
  expect(mergeItems(blockList)).toEqual([]);
});
