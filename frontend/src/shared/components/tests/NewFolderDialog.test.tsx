import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { NewFolderDialog } from "../NewFolderDialog";

describe("NewFolderDialog", () => {
  it("submits a trimmed name with an optional custom slug", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <NewFolderDialog
        parentLabel="Context"
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByRole("textbox", { name: /Folder name/i }), " Research Notes ");
    await userEvent.click(screen.getByRole("button", { name: "Advanced slug" }));
    await userEvent.type(screen.getAllByRole("textbox", { name: /Slug/ }).at(-1)!, "Research 2026!");
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));

    expect(onSubmit).toHaveBeenCalledWith({
      slug: "research-2026",
      title: "Research Notes",
    });
  });

  it("shows duplicate slug errors in user-facing copy", async () => {
    const onSubmit = vi
      .fn()
      .mockRejectedValue(new Error("duplicate_slug_under_parent"));
    render(
      <NewFolderDialog
        parentLabel="Context"
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByRole("textbox", { name: /Folder name/i }), "Research");
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));

    expect(await screen.findByText("Slug already in use under this folder"))
      .toBeInTheDocument();
  });

  it("uses a localized fallback instead of exposing an unexpected error message", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("raw req-secret"));
    render(
      <NewFolderDialog
        parentLabel="Context"
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByRole("textbox", { name: /Folder name/ }), "Research");
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));

    expect(await screen.findByText("Failed to create Folder")).toBeInTheDocument();
    expect(screen.queryByText("raw req-secret")).toBeNull();
    expect(screen.getByRole("button", { name: "Close new item dialog" }))
      .toHaveAttribute("title", "Close new item dialog");
  });

  it("closes on Escape and restores focus to the opener", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);

    const opener = screen.getByRole("button", { name: "Open dialog" });
    opener.focus();
    await user.click(opener);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });
});

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open dialog
      </button>
      {open ? (
        <NewFolderDialog
          parentLabel="Context"
          onClose={() => setOpen(false)}
          onSubmit={vi.fn().mockResolvedValue(undefined)}
        />
      ) : null}
    </>
  );
}
