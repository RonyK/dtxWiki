import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError } from "../../../shared/api/client";
import type { ContextMergePreview, PreparedMergeChoice } from "../../../shared/api/pages";
import { ContextMergeDialog } from "../ContextMergeDialog";
import { useContextMerge } from "../useContextMerge";

afterEach(() => vi.restoreAllMocks());

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
// heading's newline and mark the heading too.
const frontMatter = "---\ntype: guide\n---\n";
const keepBothBody = `${frontMatter}## 😀\nours\ntheirs\n\nend\n`;
const latestBody = `${frontMatter}## 😀\nours\n\nend\n`;

const at = (offset: number) => [...frontMatter].length + offset;

function conflictPreview(): ContextMergePreview {
  const candidate = (body: string, start: number, end: number, title: string) => ({
    body_md: body,
    page_metadata: metadata(title),
    block_map: [],
    result_hash: (title === "Draft title" ? "a" : "b").repeat(64),
    conflicts: [{ ours: "ours\n", theirs: "theirs\n", keep_both_start: start, keep_both_end: end, latest_start: start, latest_end: end }],
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
  const dialog = await screen.findByRole("dialog", { name: "Combine changes" });
  expect(within(dialog).getByText(/overlaps the published version \(v12\) in 2 places/)).toBeInTheDocument();
  const confirm = within(dialog).getByRole("button", { name: "Combine with this result" });
  // Nothing is chosen for the person: the result appears after a choice.
  expect(confirm).toBeDisabled();
  expect(within(dialog).getByText("Choose one of the two options above")).toBeInTheDocument();

  await user.click(within(dialog).getByRole("radio", { name: /Keep both changes/ }));
  const marked = () => Array.from(dialog.querySelectorAll("[data-merged-region='true']")).map((node) => node.textContent);
  expect(within(dialog).getByText(/theirs/)).toBeInTheDocument();
  expect(marked()).toEqual(["ours\ntheirs"]);

  await user.click(within(dialog).getByRole("radio", { name: /Prefer the latest change/ }));
  expect(within(dialog).queryByText(/theirs/)).toBeNull();
  expect(marked()).toEqual(["ours"]);

  // The conflicted title starts at the later save's value; one click takes the
  // other side's, and "Edit myself" allows any title.
  const titles = within(dialog).getByRole("radiogroup", { name: "Title" });
  expect(within(titles).getByRole("radio", { name: /Draft title/ })).toHaveAttribute("aria-checked", "true");
  await user.click(within(titles).getByRole("radio", { name: /Published title/ }));
  await user.click(within(titles).getByRole("radio", { name: "Edit myself" }));
  const titleInput = within(dialog).getByRole("textbox", { name: "Title" });
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

it("asks again when the preview changed after the answer, and ends only on an unknown write", async () => {
  // Git's rules for a merge write: a failure before the write keeps the
  // person's answer; a result that changed after they answered (here the
  // overlap is gone) is shown and confirmed again, never written as is; only
  // a write whose outcome is unknown ends the merge.
  const user = userEvent.setup();
  const conflict = conflictPreview();
  const clean = {
    ...conflict, kind: "automatic_merge", keep_both: null, latest: null, metadata_conflicts: [],
    result: { ...conflict.latest!, conflicts: [], result_hash: "f".repeat(64) },
  } as ContextMergePreview;
  const edited = { ...conflict, result: { ...conflict.keep_both!, result_hash: "e".repeat(64) } };
  vi.spyOn(globalThis, "fetch")
    .mockRejectedValueOnce(new TypeError("Failed to fetch"))
    .mockResolvedValueOnce(new Response(JSON.stringify({ data: edited }), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  const loadPreview = vi.fn().mockResolvedValueOnce(conflict).mockResolvedValueOnce(clean);
  const unknown = new TypeError("Failed to fetch");
  const write = vi.fn<(choice: PreparedMergeChoice) => Promise<string>>()
    .mockRejectedValueOnce(new ApiError(409, { error_code: "stale_base_version", message: "stale", request_id: "r1", warnings: [] }))
    .mockRejectedValueOnce(unknown);
  let outcome: Promise<unknown> = Promise.resolve();
  function Publish() {
    const merge = useContextMerge();
    return (
      <>
        <button type="button" onClick={() => {
          outcome = merge.runMergeWrite({
            kind: "publish", workspaceId: "ws-1", pageId: "page-1", errorMessage: () => "Could not combine.",
            loadPreview, write,
          }).catch((error: unknown) => error);
        }}>Publish</button>
        {merge.dialog}
      </>
    );
  }
  render(<MemoryRouter><Publish /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: "Publish" }));
  let dialog = await screen.findByRole("dialog", { name: "Combine changes" });
  await user.click(within(dialog).getByRole("radio", { name: /Keep both changes/ }));
  await user.click(within(within(dialog).getByRole("radiogroup", { name: "Title" })).getByRole("radio", { name: "Edit myself" }));
  const title = within(dialog).getByRole("textbox", { name: "Title" });
  await user.clear(title);
  await user.type(title, "Agreed title");
  const confirm = () => within(dialog).getByRole("button", { name: "Combine with this result" });

  // Preparing the edited result failed before any write: the answer stays.
  await user.click(confirm());
  expect(await within(dialog).findByText("Could not combine.")).toBeInTheDocument();
  expect(within(dialog).getByRole("textbox", { name: "Title" })).toHaveValue("Agreed title");
  expect(write).not.toHaveBeenCalled();

  // The answer was out of date and the newer preview no longer overlaps: it
  // is shown to confirm again, not written.
  await user.click(confirm());
  dialog = await screen.findByRole("dialog", { name: "Combine changes" });
  expect(await within(dialog).findByText(/Nothing overlaps any more/)).toBeInTheDocument();
  expect(within(dialog).queryByRole("radio", { name: /Keep both changes/ })).toBeNull();
  expect(write).toHaveBeenCalledTimes(1);
  expect(write.mock.calls[0][0]).toMatchObject({
    source: "chosen", on_conflict: "keep_both", result_hash: "e".repeat(64),
    result_page_metadata: { title: "Agreed title" },
  });

  // The clean result, confirmed, is written; its outcome is unknown, so the
  // merge ends and the caller recovers.
  await user.click(confirm());
  expect(await outcome).toBe(unknown);
  expect(screen.queryByRole("dialog", { name: "Combine changes" })).toBeNull();
  expect(write).toHaveBeenCalledTimes(2);
  expect(write.mock.calls[1][0]).toMatchObject({ source: "chosen", result_hash: "f".repeat(64) });
  expect(write.mock.calls[1][0]).not.toHaveProperty("on_conflict");
});
