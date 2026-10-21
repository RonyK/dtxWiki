import { readFileSync } from "node:fs";
import { join } from "node:path";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { NewItemDialog } from "../NewItemDialog";

describe("NewItemDialog", () => {
  it("uses the content title budget for pages without cutting Unicode input", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<NewItemDialog parentLabel="Context" onClose={() => undefined} onSubmit={onSubmit} />);
    const input = screen.getByRole("textbox", { name: /title/i });
    const submit = screen.getByRole("button", { name: "Open editor" });
    fireEvent.change(input, { target: { value: "😀".repeat(257) } });
    expect(input).toHaveValue("😀".repeat(257));
    expect(submit).toBeDisabled();
    fireEvent.change(input, { target: { value: "😀".repeat(256) } });
    await userEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ title: "😀".repeat(256) }));
  });

  it.each([390, 1280])(
    "contains a deeply nested location at a %ipx viewport",
    (viewportWidth) => {
      vi.stubGlobal("innerWidth", viewportWidth);
      const parentNodes = deepFolderNodes(16);
      const selected = parentNodes[parentNodes.length - 1];

      render(
        <NewItemDialog
          initialParentId={selected.id}
          parentLabel={selected.breadcrumb
            .map((item) => item.title)
            .concat(selected.title)
            .join(" / ")}
          parentNodes={parentNodes as never[]}
          onClose={() => undefined}
          onSubmit={() => undefined}
        />,
      );

      const panel = screen.getByRole("dialog");
      installDialogLayoutProbe(panel, viewportWidth);

      expect(panel.scrollWidth).toBeLessThanOrEqual(panel.clientWidth);
    },
  );

  it("uses a fixed type heading and keeps the complete deep path accessible", () => {
    const parentNodes = deepFolderNodes(16);
    const selected = parentNodes[parentNodes.length - 1];
    const fullPath = selected.breadcrumb
      .map((item) => item.title)
      .concat(selected.title)
      .join(" / ");

    render(
      <NewItemDialog
        initialParentId={selected.id}
        parentLabel={fullPath}
        parentNodes={parentNodes as never[]}
        onClose={() => undefined}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByRole("heading", { name: "New page" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Level 16/ })).toBeNull();

    const location = screen.getByRole("button", { name: "Location" });
    expect(location).toHaveTextContent("… / Level 15 / Level 16");
    expect(location).toHaveAttribute("title", fullPath);
    const descriptionId = location.getAttribute("aria-describedby");
    expect(descriptionId).toBeTruthy();
    expect(document.getElementById(descriptionId ?? "")).toHaveTextContent(fullPath);
  });

  it("defines a bounded inline-size chain for dialog and tree picker content", () => {
    const editorStyles = readFileSync(
      join(process.cwd(), "src", "styles", "03-editor.css"),
      "utf-8",
    );
    const dialogStyles = readFileSync(
      join(process.cwd(), "src", "styles", "07-settings-policy.css"),
      "utf-8",
    );

    for (const selector of [
      ".tree-picker",
      ".tree-picker-trigger",
      ".tree-picker-trigger-label",
      ".tree-picker-row",
      ".tree-picker-label",
    ]) {
      expect(cssRule(editorStyles, selector)).toMatch(/min-inline-size:\s*0/);
      expect(cssRule(editorStyles, selector)).toMatch(/max-inline-size:\s*100%/);
    }
    expect(cssRule(editorStyles, ".tree-picker-menu")).toMatch(/min-inline-size:\s*0/);
    expect(cssRule(editorStyles, ".tree-picker-menu")).toMatch(
      /max-inline-size:\s*calc\(100vw - 24px\)/,
    );
    expect(cssRule(editorStyles, ".tree-picker-row")).toMatch(/clamp\(/);

    for (const selector of [
      ".dialog-panel",
      ".dialog-form",
      ".dialog-form > *",
      ".dialog-header > :first-child",
      ".form-field",
    ]) {
      expect(cssRule(dialogStyles, selector)).toMatch(/min-inline-size:\s*0/);
      expect(cssRule(dialogStyles, selector)).toMatch(/max-inline-size:\s*100%/);
    }
    expect(cssRule(dialogStyles, ".dialog-panel")).toMatch(/overflow:\s*visible/);
    expect(cssRule(dialogStyles, ".dialog-scroll-region")).toMatch(/overflow:\s*auto/);
  });

  it("submits page creation input", async () => {
    const onSubmit = vi.fn();

    render(
      <NewItemDialog
        parentLabel="Research"
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByRole("textbox", { name: "Page title" }), "Launch Plan");
    await userEvent.click(screen.getByRole("button", { name: "Open editor" }));

    expect(onSubmit).toHaveBeenCalledWith({
      itemType: "page",
      parentNodeId: null,
      slug: undefined,
      title: "Launch Plan",
    });
  });

  it("uses the localized create fallback instead of rendering an arbitrary error", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("raw req-secret"));

    render(
      <NewItemDialog
        parentLabel="Research"
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(
      screen.getByRole("textbox", { name: "Page title" }),
      "Launch Plan",
    );
    await userEvent.click(screen.getByRole("button", { name: "Open editor" }));

    expect(await screen.findByText("Failed to create Page")).toBeInTheDocument();
    expect(screen.queryByText("raw req-secret")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Close new item dialog" }),
    ).toHaveAttribute("title", "Close new item dialog");
  });

  it("uses the localized upload fallback instead of rendering an arbitrary error", async () => {
    const onPickFiles = vi.fn().mockRejectedValue(new Error("raw upload-secret"));

    render(
      <NewItemDialog
        canUpload
        initialItemType="upload"
        parentLabel="Library"
        onClose={() => undefined}
        onPickFiles={onPickFiles}
        onSubmit={() => undefined}
      />,
    );

    await userEvent.upload(
      screen.getByLabelText("Upload file"),
      new File(["content"], "source.md", { type: "text/markdown" }),
    );

    expect(await screen.findByText("Failed to upload file")).toBeInTheDocument();
    expect(screen.queryByText("raw upload-secret")).toBeNull();
  });

  it("keeps the result surface mounted while creation is pending", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    let resolveRequest: (() => void) | undefined;
    const request = new Promise<void>((resolve) => {
      resolveRequest = resolve;
    });
    render(
      <NewItemDialog
        parentLabel="Research"
        onClose={onClose}
        onSubmit={() => request}
      />,
    );
    await user.type(screen.getByRole("textbox", { name: "Page title" }), "Launch Plan");
    await user.click(screen.getByRole("button", { name: "Open editor" }));
    expect(screen.getByRole("button", { name: "Creating" })).toBeDisabled();

    const closeButton = screen.getByRole("button", {
      name: "Close new item dialog",
    });
    const cancelButton = screen.getByRole("button", { name: "Cancel" });
    expect(closeButton).toBeDisabled();
    expect(cancelButton).toBeDisabled();

    // Defense in depth: even if a caller or future wrapper invokes the button
    // handler despite its disabled UI state, the component-level close guard
    // must keep the pending mutation surface mounted.
    closeButton.removeAttribute("disabled");
    cancelButton.removeAttribute("disabled");
    fireEvent.click(closeButton);
    fireEvent.click(cancelButton);

    await user.keyboard("{Escape}");
    fireEvent.mouseDown(document.querySelector(".dialog-backdrop") as Element);

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    resolveRequest?.();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("submits folder creation input with an advanced slug", async () => {
    const onSubmit = vi.fn();

    render(
      <NewItemDialog
        initialItemType="folder"
        parentLabel="Context"
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByRole("heading", { name: "New folder" })).toBeInTheDocument();

    await userEvent.type(screen.getByRole("textbox", { name: "Folder name" }), "R&D Notes");
    await userEvent.click(screen.getByRole("button", { name: "Advanced slug" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Slug" }), "research-notes");
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));

    expect(onSubmit).toHaveBeenCalledWith({
      itemType: "folder",
      parentNodeId: null,
      slug: "research-notes",
      title: "R&D Notes",
    });
  });

  it("omits the slug when Advanced is open but left blank", async () => {
    // A blank Advanced field must defer to server-side derivation (the
    // placeholder promises the derived slug); sending "" would be rejected by
    // the backend slug pattern.
    const onSubmit = vi.fn();

    render(
      <NewItemDialog
        initialItemType="folder"
        parentLabel="Context"
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByRole("textbox", { name: "Folder name" }), "R&D Notes");
    await userEvent.click(screen.getByRole("button", { name: "Advanced slug" }));
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));

    expect(onSubmit).toHaveBeenCalledWith({
      itemType: "folder",
      parentNodeId: null,
      slug: undefined,
      title: "R&D Notes",
    });
  });

  it("submits the chosen location (F-5)", async () => {
    // When parent folders are supplied, the dialog shows a location picker; the
    // pre-selected parent flows through onSubmit so the rail "+" no longer pins
    // creation to the root.
    const onSubmit = vi.fn();
    const folderNode = {
      id: "folder-1",
      parent_id: null,
      title: "Research",
      slug: "research",
      can_have_children: true,
      breadcrumb: [],
    };

    render(
      <NewItemDialog
        initialItemType="folder"
        initialParentId="folder-1"
        parentLabel="Context"
        parentNodes={[folderNode as never]}
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByLabelText("Location")).toBeInTheDocument();
    await userEvent.type(
      screen.getByRole("textbox", { name: "Folder name" }),
      "Sub",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));

    expect(onSubmit).toHaveBeenCalledWith({
      itemType: "folder",
      parentNodeId: "folder-1",
      slug: undefined,
      title: "Sub",
    });
  });

  it("keeps the creation dialog open while expanding a location branch", async () => {
    const user = userEvent.setup();
    const parent = {
      id: "folder-parent",
      parent_id: null,
      title: "Research",
      slug: "research",
      can_have_children: true,
      breadcrumb: [],
    };
    const child = {
      ...parent,
      id: "folder-child",
      parent_id: parent.id,
      title: "Field notes",
      slug: "field-notes",
      breadcrumb: [
        {
          id: parent.id,
          node_kind: "folder",
          section: "context",
          slug: parent.slug,
          title: parent.title,
        },
      ],
    };
    const onClose = vi.fn();

    render(
      <NewItemDialog
        initialItemType="folder"
        parentLabel="Context"
        parentNodes={[parent, child] as never[]}
        onClose={onClose}
        onSubmit={() => undefined}
      />,
    );

    const dialog = screen.getByRole("dialog");
    await user.click(screen.getByRole("button", { name: "Location" }));
    const tree = screen.getByRole("tree", { name: "Location tree" });
    const parentRow = within(tree).getByRole("treeitem", { name: /Research/ });
    const expand = within(parentRow).getByRole("button", { name: "Expand Research" });

    await user.click(expand);

    expect(dialog).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(within(tree).getByRole("treeitem", { name: /Field notes/ })).toBeInTheDocument();
    expect(expand).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the section root once when a rootLabel is supplied", async () => {
    // The picker renders its own root row from `rootLabel`; the section-root
    // node coming back in the tree payload made a second, identically labelled
    // row nested under it ("Context > Context").
    const sectionRoot = {
      id: "node-context-root",
      section: "context",
      parent_id: null,
      title: "Context",
      slug: "context",
      is_system_node: true,
      target_id: null,
      can_have_children: true,
      breadcrumb: [],
    };
    const inbox = {
      ...sectionRoot,
      id: "node-inbox",
      parent_id: "node-context-root",
      title: "Inbox",
      slug: "inbox",
      breadcrumb: [],
    };

    render(
      <NewItemDialog
        initialItemType="folder"
        parentLabel="Context"
        parentNodes={[sectionRoot as never, inbox as never]}
        rootLabel="Context"
        onClose={() => undefined}
        onSubmit={() => undefined}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Location" }));
    const listbox = screen.getByRole("tree", { name: "Location tree" });
    const labels = Array.from(
      listbox.querySelectorAll(".tree-picker-label"),
    ).map((element) => element.textContent);

    expect(labels.filter((label) => label === "Context")).toHaveLength(1);
    expect(labels).toContain("Inbox");
  });

  it("keeps the location picker when hiding the root empties the list", async () => {
    // A workspace whose only folder IS the section root: the synthetic root row
    // is still a real destination, so the field must not disappear.
    const sectionRoot = {
      id: "node-context-root",
      section: "context",
      parent_id: null,
      title: "Context",
      slug: "context",
      is_system_node: true,
      target_id: null,
      can_have_children: true,
      breadcrumb: [],
    };

    render(
      <NewItemDialog
        initialItemType="folder"
        parentLabel="Context"
        parentNodes={[sectionRoot as never]}
        rootLabel="Context"
        onClose={() => undefined}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByLabelText("Location")).toBeInTheDocument();
  });

  it("normalizes an initial parent that points at the hidden section root", async () => {
    // The rail computes the initial parent from the selected node's parent_id,
    // which can be the section root itself. That id no longer has a row, so it
    // must resolve to the synthetic root rather than submitting a stale id.
    const onSubmit = vi.fn();
    const sectionRoot = {
      id: "node-context-root",
      section: "context",
      parent_id: null,
      title: "Context",
      slug: "context",
      is_system_node: true,
      target_id: null,
      can_have_children: true,
      breadcrumb: [],
    };

    render(
      <NewItemDialog
        initialItemType="folder"
        initialParentId="node-context-root"
        parentLabel="Context"
        parentNodes={[sectionRoot as never]}
        rootLabel="Context"
        onClose={() => undefined}
        onSubmit={onSubmit}
      />,
    );

    await userEvent.type(screen.getByRole("textbox", { name: "Folder name" }), "Sub");
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));

    expect(onSubmit).toHaveBeenCalledWith({
      itemType: "folder",
      parentNodeId: null,
      slug: undefined,
      title: "Sub",
    });
  });

  it("falls back to folder mode when page creation is disabled", () => {
    render(
      <NewItemDialog
        canCreatePage={false}
        initialItemType="page"
        parentLabel="Context"
        onClose={() => undefined}
        onSubmit={() => undefined}
      />,
    );

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("textbox", { name: "Folder name" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open editor" })).toBeNull();
  });

  it("can order Upload before Folder and select Upload by default", () => {
    render(
      <NewItemDialog
        canCreatePage={false}
        canUpload
        initialItemType="upload"
        parentLabel="Library"
        typeOrder={["upload", "folder"]}
        onClose={() => undefined}
        onSubmit={() => undefined}
      />,
    );

    const typeGroup = screen.getByRole("radiogroup", { name: "Item type" });
    const options = within(typeGroup).getAllByRole("radio");
    expect(options.map((option) => option.textContent)).toEqual([
      "Upload",
      "Folder",
    ]);
    expect(screen.getByRole("radio", { name: "Upload" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByLabelText("Upload file")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Upload files" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create folder" })).toBeNull();
  });
});

function deepFolderNodes(depth: number) {
  return Array.from({ length: depth }, (_, index) => ({
    id: `level-${index + 1}`,
    parent_id: index === 0 ? null : `level-${index}`,
    title: `Level ${index + 1}`,
    slug: `level-${index + 1}`,
    can_have_children: true,
    breadcrumb: Array.from({ length: index }, (__, ancestorIndex) => ({
      id: `level-${ancestorIndex + 1}`,
      section: "context",
      title: `Level ${ancestorIndex + 1}`,
      slug: `level-${ancestorIndex + 1}`,
      node_kind: "folder",
    })),
  }));
}

function installDialogLayoutProbe(panel: HTMLElement, viewportWidth: number) {
  const clientWidth = Math.min(438, viewportWidth - 40);
  // JSDOM does not perform layout. Model the intrinsic width of the picker's
  // non-wrapping trigger label, which is the element that forced the real
  // dialog to 10,055px before the path was compacted and the chain could shrink.
  const triggerLabel = panel.querySelector(".tree-picker-trigger span");
  const visibleTextWidth = (triggerLabel?.textContent?.trim().length ?? 0) * 7 + 80;

  Object.defineProperties(panel, {
    clientWidth: { configurable: true, value: clientWidth },
    scrollWidth: {
      configurable: true,
      value: Math.max(clientWidth, visibleTextWidth),
    },
  });
}

function cssRule(styles: string, selector: string) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return (
    styles.match(
      new RegExp(`(?:^|})\\s*[^{}]*${escapedSelector}[^{}]*\\{([^}]*)\\}`, "m"),
    )?.[1] ?? ""
  );
}
