import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

import type { ContextMergePreview } from "../../../shared/api/pages";
import { ContextMergeDialog } from "../ContextMergeDialog";

vi.mock("../../../shared/api/workspace", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../../shared/api/workspace")>(),
  listTree: vi.fn(async () => []),
  readTreeNode: vi.fn(async () => { throw new Error("not loaded"); }),
}));

const metadata = (title: string) => ({
  title, page_type: "guide", tags: [], aliases: [], placement_parent_node_id: null,
});

// Editor-authored bodies start with front matter, which the rendered result
// omits: regions are numbered in the whole source. The heading holds an
// emoji: one Unicode code point on the server, two UTF-16 units in the
// browser. A region that starts on the next line must mark only that line's
// block; reading the code-point offset as a string index would land on the
// heading's newline and mark the heading too. The title is a front matter
// line, so its conflict is also a region there, as the server reports it: one
// place for the person, counted once, and not marked in the rendered result.
const titleLine = 'title: "Draft title"\n';
const frontMatter = `---\n${titleLine}type: guide\n---\n`;
const keepBothBody = `${frontMatter}## 😀\nours\ntheirs\n\nend\n`;
const latestBody = `${frontMatter}## 😀\nours\n\nend\n`;

const at = (offset: number) => [...frontMatter].length + offset;

function conflictPreview(): ContextMergePreview {
  const candidate = (body: string, start: number, end: number, title: string) => ({
    body_md: body,
    page_metadata: metadata(title),
    block_map: [],
    result_hash: (title === "Draft title" ? "a" : "b").repeat(64),
    conflicts: [
      { ours: titleLine, theirs: 'title: "Published title"\n', keep_both_start: 4, keep_both_end: 4 + titleLine.length, latest_start: 4, latest_end: 4 + titleLine.length },
      { ours: "ours\n", theirs: "theirs\n", keep_both_start: start, keep_both_end: end, latest_start: start, latest_end: end },
    ],
    offset_unit: "unicode_code_points" as const,
  });
  return {
    page_id: "page-1",
    version_id: "draft-1",
    content_hash: "c".repeat(64),
    references_hash: "r".repeat(64),
    editing_revision: 3,
    published_version_id: "pub-1",
    target_version_id: "pub-1",
    parent_version_ids: ["base-1"],
    kind: "conflict",
    result: null,
    // Code points: the front matter, then "## 😀\n" is 5, "ours\ntheirs\n" is
    // 12, "ours\n" is 5.
    keep_both: candidate(keepBothBody, at(5), at(17), "Draft title"),
    latest: candidate(latestBody, at(5), at(10), "Draft title"),
    metadata_conflicts: ["title"],
    base_conflicted: false,
    latest_side: "ours",
    offset_unit: "unicode_code_points",
    sides: {
      ours: {
        version_id: "draft-1",
        saved_by: { actor_kind: "human", actor_id: "u1", display_name: "Hong" },
        last_saved_at: "2026-11-18T10:00:00Z",
        metadata: { title: "Draft title", page_type: "guide", placement_parent_node_id: null },
      },
      theirs: {
        version_id: "pub-1",
        saved_by: { actor_kind: "human", actor_id: "u2", display_name: "Kim" },
        last_saved_at: "2026-11-18T09:00:00Z",
        metadata: { title: "Published title", page_type: "guide", placement_parent_node_id: null },
      },
    },
  } as ContextMergePreview;
}

it("offers the two choices, marks combined blocks by code point, and returns the person's answer", async () => {
  const user = userEvent.setup();
  const onConfirm = vi.fn();
  render(
    <MemoryRouter>
      <ContextMergeDialog kind="publish" onClose={vi.fn()} onConfirm={onConfirm} pageId="page-1"
        preview={conflictPreview()} publishedVersionNumber={12} workspaceId="ws-1" />
    </MemoryRouter>,
  );
  const dialog = await screen.findByRole("dialog", { name: "Merge changes" });
  expect(within(dialog).getByText(/overlaps the published version \(v12\) in 2 places/)).toBeInTheDocument();
  const confirm = within(dialog).getByRole("button", { name: "Publish this result" });
  // Nothing is chosen for the person: the result appears after a choice.
  expect(confirm).toBeDisabled();
  expect(within(dialog).getByText("Choose one of the two options above")).toBeInTheDocument();

  await user.click(within(dialog).getByRole("radio", { name: /Keep both changes/ }));
  const marked = () => Array.from(dialog.querySelectorAll("[data-merged-region='true']")).map((node) => node.textContent);
  expect(within(dialog).getByText(/theirs/)).toBeInTheDocument();
  expect(marked()).toEqual(["ours\ntheirs"]);
  expect(within(dialog).getByText("1 merged section is highlighted")).toBeInTheDocument();

  await user.click(within(dialog).getByRole("radio", { name: /Prefer the latest change/ }));
  expect(within(dialog).queryByText(/theirs/)).toBeNull();
  expect(marked()).toEqual(["ours"]);

  // The conflicted title starts at the later save's value; one click takes the
  // other side's, and "Enter a value" allows any title.
  const titles = within(dialog).getByRole("radiogroup", { name: "Title" });
  expect(within(titles).getByRole("radio", { name: /Draft title/ })).toHaveAttribute("aria-checked", "true");
  await user.click(within(titles).getByRole("radio", { name: /Published title/ }));
  await user.click(within(titles).getByRole("radio", { name: "Enter a value" }));
  const titleInput = within(dialog).getByRole("textbox", { name: "Title to enter" });
  await user.clear(titleInput);
  expect(confirm).toBeDisabled();
  await user.type(titleInput, "Agreed title");

  await user.click(confirm);
  expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({
    choice: "latest",
    bodyMd: null,
    pageMetadata: { title: "Agreed title" },
  }));
});
