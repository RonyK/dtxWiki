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

// Editor-authored bodies start with front matter: regions are numbered in the
// whole source. The heading holds an emoji: one Unicode code point on the
// server, two UTF-16 units in the browser, so reading a code-point offset as a
// string index would cut the wrong text. The title is a front matter line, so
// its conflict is also a region there, as the server reports it: one place
// for the person (the title card), never a second body card.
const titleLine = 'title: "Draft title"\n';
const frontMatter = `---\n${titleLine}type: guide\n---\n`;
// "auto line" came from the published side without overlapping anything.
const keepBothBody = `${frontMatter}## 😀\nours\ntheirs\n\nend\nauto line\n`;
const latestBody = `${frontMatter}## 😀\nours\n\nend\nauto line\n`;
const draftBody = `${frontMatter}## 😀\nours\n\nend\n`;

const at = (offset: number) => [...frontMatter].length + offset;

function conflictPreview(): ContextMergePreview {
  const candidate = (body: string, title: string) => ({
    body_md: body,
    page_metadata: metadata(title),
    block_map: [],
    result_hash: (body === keepBothBody ? "a" : "b").repeat(64),
    conflicts: [
      { ours: titleLine, theirs: 'title: "Published title"\n', keep_both_start: 4, keep_both_end: 4 + titleLine.length, latest_start: 4, latest_end: 4 + titleLine.length },
      // Code points: "## 😀\n" is 5, "ours\ntheirs\n" is 12, "ours\n" is 5.
      { ours: "ours\n", theirs: "theirs\n", keep_both_start: at(5), keep_both_end: at(17), latest_start: at(5), latest_end: at(10) },
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
    keep_both: candidate(keepBothBody, "Draft title"),
    latest: candidate(latestBody, "Draft title"),
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

function renderDialog(onConfirm = vi.fn()) {
  render(
    <MemoryRouter>
      <ContextMergeDialog kind="publish" onClose={vi.fn()} onConfirm={onConfirm} oursBody={draftBody} pageId="page-1"
        preview={conflictPreview()} publishedVersionNumber={12} workspaceId="ws-1" />
    </MemoryRouter>,
  );
  return onConfirm;
}

it("shows each overlapping place once, with both sides, and writes the method's own result", async () => {
  const user = userEvent.setup();
  const onConfirm = renderDialog();
  const dialog = await screen.findByRole("dialog", { name: "Merge changes" });
  // Two places -- the title and one body region -- not three: the title's
  // front matter line is the title card, not a body card of its own.
  expect(within(dialog).getByText(/2 overlapping places are decided by the merge method below/)).toBeInTheDocument();
  expect(within(dialog).getAllByRole("region", { name: /^Overlapping place/ })).toHaveLength(2);
  // The line the published side added elsewhere is shown as merged automatically.
  expect(within(dialog).getByText(/1 change that does not overlap was merged automatically/)).toBeInTheDocument();
  expect(within(dialog).getByText("auto line")).toBeInTheDocument();

  // "Keep both changes" is chosen when the dialog opens: the body place keeps
  // both sides, cut by code point, and the title holds the later save's value.
  expect(within(dialog).getByRole("radio", { name: /Keep both changes/ })).toHaveAttribute("aria-checked", "true");
  const body = within(dialog).getByRole("region", { name: /Overlapping place 2/ });
  expect(within(body).getByText("Result · Keep both changes")).toBeInTheDocument();
  expect(body.querySelector(".context-merge-result-text")?.textContent).toBe("ours\ntheirs\n");
  const title = within(dialog).getByRole("region", { name: /Overlapping place 1/ });
  expect(within(title).getByRole("button", { name: /^Current draft/ })).toHaveAttribute("aria-pressed", "true");

  // A method writes the server's own result for it, unedited.
  await user.click(within(dialog).getByRole("radio", { name: /Prefer the latest change/ }));
  expect(body.querySelector(".context-merge-result-text")?.textContent).toBe("ours\n");
  await user.click(within(dialog).getByRole("button", { name: "Publish this result" }));
  expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ choice: "latest", bodyMd: null, pageMetadata: null }));
});

it("turns a choice at one place into Custom, keeps the other places, and asks before discarding an edit", async () => {
  const user = userEvent.setup();
  const onConfirm = renderDialog();
  const dialog = await screen.findByRole("dialog", { name: "Merge changes" });
  const body = within(dialog).getByRole("region", { name: /Overlapping place 2/ });
  const title = within(dialog).getByRole("region", { name: /Overlapping place 1/ });

  // Choosing the published side of the body place: the method becomes Custom
  // and the title keeps the previous method's result.
  await user.click(within(body).getByRole("button", { name: /^Published v12/ }));
  expect(within(dialog).getByRole("radio", { name: /Custom/ })).toHaveAttribute("aria-checked", "true");
  expect(within(body).getByText("Chosen · Published")).toBeInTheDocument();
  expect(within(title).getByText("Previous method's result")).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "Publish this result" }));
  expect(onConfirm).toHaveBeenLastCalledWith(expect.objectContaining({
    choice: "keep_both",
    bodyMd: `${frontMatter}## 😀\ntheirs\n\nend\nauto line\n`,
    pageMetadata: null,
  }));

  // Pressing the chosen side again lets go of it: both sides are kept. The
  // title is edited directly; it goes to the metadata and the front matter.
  await user.click(within(body).getByRole("button", { name: /^Published v12/ }));
  expect(within(body).getByText("Chosen · Both")).toBeInTheDocument();
  await user.click(within(title).getByRole("button", { name: /Edit directly/ }));
  const input = within(title).getByRole("textbox", { name: "Edit the result of Title" });
  await user.clear(input);
  await user.type(input, "Agreed title");
  await user.click(within(title).getByRole("button", { name: "Done" }));
  await user.click(within(dialog).getByRole("button", { name: "Publish this result" }));
  expect(onConfirm).toHaveBeenLastCalledWith(expect.objectContaining({
    choice: "keep_both",
    bodyMd: `---\ntitle: "Agreed title"\ntype: guide\n---\n## 😀\nours\ntheirs\n\nend\nauto line\n`,
    pageMetadata: { title: "Agreed title" },
  }));

  // Another method decides every place again; an edit is lost only after a
  // yes. Escape answers the question on top, not the merge dialog under it.
  await user.click(within(dialog).getByRole("radio", { name: /Prefer the latest change/ }));
  await screen.findByRole("dialog", { name: "Change the merge method?" });
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "Change the merge method?" })).toBeNull();
  expect(dialog).toBeInTheDocument();
  expect(within(dialog).getByRole("radio", { name: /Custom/ })).toHaveAttribute("aria-checked", "true");
  await user.click(within(dialog).getByRole("radio", { name: /Prefer the latest change/ }));
  const question = await screen.findByRole("dialog", { name: "Change the merge method?" });
  expect(within(question).getByText(/1 · Title/)).toBeInTheDocument();
  await user.click(within(question).getByRole("button", { name: "Change" }));
  expect(within(dialog).getByRole("radio", { name: /Prefer the latest change/ })).toHaveAttribute("aria-checked", "true");
  await user.click(within(dialog).getByRole("button", { name: "Publish this result" }));
  expect(onConfirm).toHaveBeenLastCalledWith(expect.objectContaining({ choice: "latest", bodyMd: null, pageMetadata: null }));
});
