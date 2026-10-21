import { EditorView } from "@codemirror/view";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TreeNodePayload } from "../../../shared/api/workspace";

// Upload/delete affordances are gated on an editor+ role (Codex #25). Default
// to an owner-role workspace so they render; tests can flip the role to
// "viewer".
const { libWorkspace } = vi.hoisted(() => ({
  libWorkspace: {
    current: { selectedWorkspace: { role: "owner" } } as Record<string, unknown>,
  },
}));
vi.mock("../../../shared/workspace/WorkspaceContext", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useOptionalWorkspaceContext: () => libWorkspace.current,
}));

vi.mock("../PdfLibraryPreview", () => ({
  PdfLibraryPreview: ({
    extractedText,
    fileUrl,
    title,
  }: {
    extractedText: string | null;
    fileUrl: string;
    title: string;
  }) => (
    <div data-testid="pdf-item-preview">
      <span>{title}</span>
      <span>{fileUrl}</span>
      {extractedText ? <pre>{extractedText}</pre> : null}
    </div>
  ),
}));

import { LibraryScreen } from "../LibraryScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";
const LIBRARY_ITEM_ID = "item-1";
const SECOND_LIBRARY_ITEM_ID = "item-2";

describe("LibraryScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.localStorage.setItem("dtxwiki:workspace-layout:v2:library:left", "true");
    libWorkspace.current = { selectedWorkspace: { role: "owner" } };
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    Reflect.deleteProperty(window, "matchMedia");
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1024,
    });
  });

  it.each([
    { uri: "https://example.com/untitled", expected: "https://example.com/untitled" },
    { uri: "upload://untitled.md", expected: "Markdown" },
  ])("uses the reader display title for an untitled Library item ($uri)", async ({ uri, expected }) => {
    const previousTitle = document.title;
    const detail = { ...libraryItemDetail(LIBRARY_ITEM_ID, "", uri), title: null };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ ...libraryItemSummary(LIBRARY_ITEM_ID, "", uri), title: null }],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [treeNode("node-1", LIBRARY_ITEM_ID, expected)]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
    });
    const { unmount } = render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: expected, level: 2 })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe(`${expected} - dtxWiki`));
    unmount();
    expect(document.title).toBe(previousTitle);
  });

  it("renders library items, current version, and extracted view", async () => {
    const apiDetail = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "API Item",
      "https://example.com/api-source",
    );
    (apiDetail.versions[0] as { extraction_summary: string | null }).extraction_summary =
      String.raw`C:\private\converter.exe failed with secret arguments`;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
          libraryItemSummary(
            SECOND_LIBRARY_ITEM_ID,
            "Design Item",
            "https://example.com/design-source",
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
          folderNode("ancestor-0", "References"),
          treeNode("node-2", SECOND_LIBRARY_ITEM_ID, "Design Item", ["References"]),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: apiDetail,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${SECOND_LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          SECOND_LIBRARY_ITEM_ID,
          "Design Item",
          "https://example.com/design-source",
          "sha256:source-second",
          "# Design Item\n\nSecond artifact",
        ),
      },
    });

    // C2: no ?target no longer auto-opens items[0]; deep-link the item so its
    // detail is selected.
    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "API Item", level: 2 }),
    ).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("API Item - dtxWiki"));
    // Item 5 (M1): the markdown item body now renders instead of raw <pre>,
    // so its leading "# API Item" appears as a level-1 heading in the preview.
    expect(
      await screen.findByRole(
        "heading",
        { name: "API Item", level: 1 },
        { timeout: 3_000 },
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "View" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /History/ })).toBeInTheDocument();
    expect(screen.getByText(/Needle artifact/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /History/ }));
    expect(screen.getByText("Document conversion completed."))
      .toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("converter.exe");
    await userEvent.click(screen.getByRole("tab", { name: "View" }));

    await userEvent.click(screen.getByRole("treeitem", { name: "Design Item" }));

    expect(
      await screen.findByRole("heading", { name: "Design Item", level: 2 }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/Second artifact/)).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Design Item - dtxWiki"));
    expect(
      screen.getByRole("treeitem", { name: "Design Item" }),
    ).toHaveAttribute("aria-selected", "true");
    const breadcrumb = within(screen.getByLabelText("Breadcrumb"));
    expect(breadcrumb.getByRole("link", { name: "Library" }))
      .toHaveAttribute("href", "/library");
    expect(breadcrumb.getByRole("link", { name: "References" }))
      .toHaveAttribute("href", "/library?target=ancestor-0");

    await userEvent.click(breadcrumb.getByRole("link", { name: "References" }));

    expect(await screen.findByRole("heading", { name: "Library" }))
      .toBeInTheDocument();
    expect(screen.queryByText(/Second artifact/)).not.toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("dtxWiki"));
    expect(screen.getByRole("treeitem", { name: "References" }))
      .toHaveAttribute("aria-selected", "true");
  });

  it("resolves detail by tree target_id for an item past the flat-list cap", async () => {
    // The flat library list only returns the first item (cap), but the tree
    // carries a node for the second item. Clicking it must fetch the detail by
    // id rather than being a silent no-op.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
          treeNode("node-2", SECOND_LIBRARY_ITEM_ID, "Capped Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${SECOND_LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          SECOND_LIBRARY_ITEM_ID,
          "Capped Item",
          "https://example.com/capped-source",
          "sha256:capped",
          "# Capped Item\n\nResolved past the cap",
        ),
      },
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Capped Item" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Capped Item", level: 2 }),
    ).toBeInTheDocument();
  });

  it("renders the recency overview with a Load more control and page-size selector", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items?limit=10`]: {
        data: [
          {
            ...libraryItemSummary(
              LIBRARY_ITEM_ID,
              "API Item",
              "https://example.com/api-source",
            ),
            processing_status: "queued",
          },
          libraryItemSummary(
            SECOND_LIBRARY_ITEM_ID,
            "Design Item",
            "https://example.com/design-source",
          ),
        ],
        next_cursor: "cursor-2",
        total: 9,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items?limit=10&cursor=cursor-2`]: {
        data: [
          libraryItemSummary("item-3", "Runbook", "https://example.com/runbook"),
        ],
        next_cursor: null,
        total: 9,
        has_more: false,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          folderNode("references", "References"),
        ]),
      },
    });

    // Land on a folder node so no item is selected and the overview renders
    // (entry without a target still auto-selects items[0] until C2).
    render(
      <MemoryRouter initialEntries={["/library?target=references"]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    // Exact server total reads "9 items" (F-3), not "9+", with Load more still available below.
    expect(await screen.findByText("9 items")).toBeInTheDocument();
    expect(screen.getByText("Queued", { selector: ".status-pill" }))
      .toBeInTheDocument();
    expect(screen.queryByText("9+")).not.toBeInTheDocument();

    // Load more appends the next keyset page and exhausts the cursor.
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("Runbook")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Load more" }),
      ).not.toBeInTheDocument();
    });
  });

  it("removes a deleted item from the Load-more recency cache", async () => {
    const user = userEvent.setup();
    const appendedId = SECOND_LIBRARY_ITEM_ID;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items?limit=10`]: {
        data: [
          libraryItemSummary(
            LIBRARY_ITEM_ID,
            "Base item",
            "https://example.com/base",
          ),
        ],
        next_cursor: "cursor-2",
        total: 2,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items?limit=10&cursor=cursor-2`]: {
        data: [
          libraryItemSummary(
            appendedId,
            "Deleted appendix",
            "https://example.com/deleted-appendix",
          ),
        ],
        next_cursor: null,
        total: 2,
        has_more: false,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          folderNode("references", "References"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${appendedId}`]: (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) =>
        init?.method === "DELETE"
          ? { data: { id: appendedId, deleted: true } }
          : {
              data: libraryItemDetail(
                appendedId,
                "Deleted appendix",
                "https://example.com/deleted-appendix",
              ),
            },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${appendedId}/references`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/library?target=references"]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Load more" }));
    await user.click(
      await screen.findByRole("button", { name: /Deleted appendix/ }),
    );
    expect(
      await screen.findByRole("heading", {
        name: "Deleted appendix",
        level: 2,
      }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Delete Deleted appendix",
    });
    await user.click(
      await within(dialog).findByRole("button", {
        name: "Delete permanently",
      }),
    );

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await screen.findByText("Recent updates");
    expect(screen.queryByText("Deleted appendix")).not.toBeInTheDocument();
    expect(await screen.findByText("Base item")).toBeInTheDocument();
    expect(screen.getByText("1 item")).toBeInTheDocument();
  });

  it("removes a recently deleted item when its detail returns 404", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items?limit=10`]: {
        data: [
          libraryItemSummary(
            LIBRARY_ITEM_ID,
            "Deleted elsewhere",
            "https://example.com/deleted-elsewhere",
          ),
        ],
        next_cursor: null,
        total: 1,
        has_more: false,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          folderNode("references", "References"),
          treeNode("node-1", LIBRARY_ITEM_ID, "Deleted elsewhere"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: jsonResponse(
        {
          error_code: "not_found",
          message: "library item not found",
          request_id: "request-deleted-elsewhere",
          warnings: [],
        },
        404,
      ),
    });

    render(
      <MemoryRouter initialEntries={["/library?target=references"]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    const overview = (await screen.findByText("Recent updates")).closest(
      "section",
    );
    expect(overview).not.toBeNull();
    await user.click(
      within(overview as HTMLElement).getByRole("button", {
        name: /Deleted elsewhere/,
      }),
    );

    expect(
      await screen.findByText(
        "That Library item is no longer available. It was removed from the list.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Deleted elsewhere")).not.toBeInTheDocument();
  });

  it("resets the reader to the View tab when switching items", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
          libraryItemSummary(
            SECOND_LIBRARY_ITEM_ID,
            "Design Item",
            "https://example.com/design-source",
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
          treeNode("node-2", SECOND_LIBRARY_ITEM_ID, "Design Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${SECOND_LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          SECOND_LIBRARY_ITEM_ID,
          "Design Item",
          "https://example.com/design-source",
          "sha256:source-second",
          "# Design Item\n\nSecond artifact",
        ),
      },
    });

    // C2: deep-link the first item so its detail is selected (no auto-open).
    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "API Item", level: 2 }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /History/ }));

    expect(screen.getByRole("tab", { name: /History/ }))
      .toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText(/Needle artifact/)).not.toBeInTheDocument();
    // §3-D: a History row is metadata only now, not a "preview this version"
    // control -- it renders as a plain container, not a button.
    const currentVersionRow = screen.getByText(/Document conversion completed/)
      .closest(".history-card");
    expect(currentVersionRow).not.toBeNull();
    expect(currentVersionRow).toHaveAttribute("aria-current", "page");
    expect(currentVersionRow?.tagName).not.toBe("BUTTON");

    await userEvent.click(screen.getByRole("treeitem", { name: "Design Item" }));

    expect(await screen.findByText(/Second artifact/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "View" }))
      .toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /History/ }))
      .toHaveAttribute("aria-selected", "false");
  });

  it("preserves a manual collapse of the selected path across tree refresh", async () => {
    const nestedTree: TreeNodePayload[] = [
      {
        ...folderNode("folder-references", "References"),
        children_count: 1,
      },
      {
        ...treeNode("node-2", SECOND_LIBRARY_ITEM_ID, "Design Item", [
          "References",
        ]),
        parent_id: "folder-references",
        breadcrumb: [
          {
            id: "folder-references",
            section: "library",
            title: "References",
            slug: "references",
            node_kind: "folder",
          },
        ],
      },
    ];
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items?limit=10`]: {
        data: [
          libraryItemSummary(
            SECOND_LIBRARY_ITEM_ID,
            "Design Item",
            "https://example.com/design-source",
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", nestedTree),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${SECOND_LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          SECOND_LIBRARY_ITEM_ID,
          "Design Item",
          "https://example.com/design-source",
          "sha256:source-second",
          "# Design Item\n\nSecond artifact",
        ),
      },
    });

    render(
      <MemoryRouter
        initialEntries={[`/library?target=${SECOND_LIBRARY_ITEM_ID}`]}
      >
        <LibraryScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Design Item", level: 2 });
    expect(
      screen.getByRole("treeitem", { name: "Design Item" }),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Collapse References" }),
    );
    expect(
      screen.queryByRole("treeitem", { name: "Design Item" }),
    ).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Refresh Library tree" }),
    );

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Expand References" }),
      ).toBeEnabled();
    });
    expect(
      screen.queryByRole("treeitem", { name: "Design Item" }),
    ).not.toBeInTheDocument();
  });

  it("hides extracted artifact nodes from the Library explorer", async () => {
    const libraryItemNode = {
      ...treeNode("node-1", LIBRARY_ITEM_ID, "Config JSON"),
      can_have_children: true,
      children_count: 1,
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "Config JSON", "https://example.com/config.json", "json"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          libraryItemNode,
          libraryArtifactTreeNode(
            "node-artifact-1",
            libraryItemNode.id,
            `${LIBRARY_ITEM_ID}-artifact-1`,
            "Extracted Text",
          ),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          LIBRARY_ITEM_ID,
          "Config JSON",
          "https://example.com/config.json",
          "sha256:json",
          '{ "service": "dtxWiki" }',
          "json",
        ),
      },
    });

    // C2: deep-link the item so its detail is selected (no auto-open).
    const view = render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Config JSON" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: "Extracted Text" }))
      .not.toBeInTheDocument();
    const libraryItemTreeNode = screen.getByRole("treeitem", { name: "Config JSON" });
    expect(libraryItemTreeNode).toHaveAttribute("aria-selected", "true");
    expect(libraryItemTreeNode).not.toHaveAttribute("aria-expanded");
    expect(view.container.querySelector(".library-json-viewer"))
      .toHaveTextContent("dtxWiki");
  });

  it("shows the setup sequence and task CTA when Library is empty", async () => {
    setMobileViewport(true);
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", []),
      },
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    expect(screen.getByLabelText("Workspace setup sequence"))
      .toHaveTextContent("Add source material");
    expect(screen.getByRole("button", { name: "Add first Library item" }))
      .toBeInTheDocument();
    // The empty state owns the single upload CTA. Duplicating Upload in the
    // header made two controls trigger the same hidden file input.
    expect(screen.queryByRole("button", { name: "Upload" })).toBeNull();
    // A fixed mobile Upload FAB used to cover the adjacent Open Context CTA,
    // so the overview must not create the mobile-action dock either.
    expect(screen.queryByLabelText("Mobile workspace actions")).toBeNull();
    // empty state: the Details rail is gated on a selected item, so it is absent.
    expect(screen.queryByRole("button", { name: "Show Details" })).toBeNull();
  });

  it("uploads a Library file and selects the uploaded item", async () => {
    let itemListCalls = 0;
    let treeCalls = 0;
    let operationId = "";
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.set(Array.from({ length: 16 }, (_, index) => index));
      return bytes;
    });
    // Plain-HTTP LAN browsers expose getRandomValues without randomUUID.
    vi.stubGlobal("crypto", { getRandomValues });
    const uploaded = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Uploaded Notes",
      `upload://${WORKSPACE_ID}/notes.md`,
      "sha256:uploaded",
      "# Uploaded Notes\n\nUploaded artifact",
    );
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) => {
        if ((init?.method ?? "GET") === "GET") {
          return {
            data: itemListCalls++ === 0
              ? []
              : [libraryItemSummary(LIBRARY_ITEM_ID, "Uploaded Notes", uploaded.uri)],
          };
        }
        operationId = new Headers(init?.headers).get(
          "X-DTX-Library-Upload-Operation-Id",
        ) ?? "";
        return { data: uploaded };
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: () => ({
        data: treeResponse(
          "library",
          treeCalls++ === 0
            ? []
            : [treeNode("node-1", LIBRARY_ITEM_ID, "Uploaded Notes")],
        ),
      }),
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    const hiddenFileInput = screen.getByLabelText("Upload Library file");
    expect(hiddenFileInput).toHaveAttribute(
      "accept",
      ".md,.markdown,text/markdown,.txt,text/plain,.csv,text/csv,.json,application/json,.yaml,.yml,application/yaml,text/yaml",
    );
    expect(hiddenFileInput).toHaveAttribute("multiple");
    // A55: the programmatic file input is absent from layout, accessibility,
    // and Tab order; the visible Upload button is the sole user-facing control.
    expect(hiddenFileInput).toHaveAttribute("hidden");
    expect(hiddenFileInput).toHaveAttribute("tabindex", "-1");

    await userEvent.upload(
      screen.getByLabelText("Upload Library file"),
      new File(["# Uploaded Notes"], "notes.md", { type: "text/markdown" }),
    );

    expect(await screen.findByText(/Uploaded artifact/)).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: "Uploaded Notes" }))
      .toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "Upload queue" }))
      .toHaveTextContent("notes.md Succeeded");
    expect(getRandomValues).toHaveBeenCalledOnce();
    expect(operationId).toBe("00010203-0405-4607-8809-0a0b0c0d0e0f");
  });

  it("fails visibly without getRandomValues and never falls back to randomUUID", async () => {
    const randomUUID = vi.fn(() => "00000000-0000-4000-8000-000000000001");
    vi.stubGlobal("crypto", { randomUUID });
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", []),
      },
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    await userEvent.upload(
      screen.getByLabelText("Upload Library file"),
      new File(["# Notes"], "notes.md", { type: "text/markdown" }),
    );

    expect(
      await screen.findByText("The Library file could not be uploaded. Try again."),
    ).toBeInTheDocument();
    expect(randomUUID).not.toHaveBeenCalled();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          input.toString().endsWith("/library/items") && init?.method === "POST",
      ),
    ).toBe(false);
  });

  it("uploads a Library file from drag and drop", async () => {
    let itemListCalls = 0;
    let treeCalls = 0;
    const uploaded = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Dropped Notes",
      `upload://${WORKSPACE_ID}/dropped.md`,
      "sha256:dropped",
      "# Dropped Notes\n\nDropped artifact",
    );
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) => (init?.method ?? "GET") === "GET"
        ? {
            data: itemListCalls++ === 0
              ? []
              : [libraryItemSummary(LIBRARY_ITEM_ID, "Dropped Notes", uploaded.uri)],
          }
        : { data: uploaded },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: () => ({
        data: treeResponse(
          "library",
          treeCalls++ === 0
            ? []
            : [treeNode("node-1", LIBRARY_ITEM_ID, "Dropped Notes")],
        ),
      }),
    });

    const view = render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    const main = view.container.querySelector(".feature-main") as HTMLElement;
    const droppedFile = new File(["# Dropped Notes"], "dropped.md", {
      type: "text/markdown",
    });

    fireEvent.dragOver(main, {
      dataTransfer: { dropEffect: "none", files: [], types: ["Files"] },
    });
    expect(main).toHaveClass("feature-main--drag-over");
    expect(screen.getByText("Drop files to upload")).toBeInTheDocument();

    fireEvent.drop(main, {
      dataTransfer: { files: [droppedFile], types: ["Files"] },
    });

    expect(await screen.findByText(/Dropped artifact/)).toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: "Dropped Notes" }))
      .toHaveAttribute("aria-selected", "true");
  });

  it("derives picker and drop validation from workspace capabilities", async () => {
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", []),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`]: {
        data: {
          policy_revision: "policy-1",
          max_files_per_batch: 2,
          formats: [
            {
              format_id: "pdf",
              category: "document",
              extensions: [".pdf"],
              media_types: ["application/pdf"],
              upload: true,
              conversion: "stored_unconverted",
              preview_kind: "pdf",
              max_bytes: 4,
            },
            {
              format_id: "exe",
              category: "document",
              extensions: [".exe"],
              media_types: ["application/x-msdownload"],
              upload: false,
              conversion: "denied",
              preview_kind: "metadata",
              max_bytes: 1,
            },
          ],
        },
      },
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    const input = await screen.findByLabelText("Upload Library file");
    expect(input).toHaveAttribute("accept", ".pdf,application/pdf");
    expect(input).toHaveAttribute("multiple");

    fireEvent.change(input, {
      target: {
        files: [
          new File(["unsafe"], "unsafe.exe", {
            type: "application/x-msdownload",
          }),
        ],
      },
    });

    expect(await screen.findByText("Failed", { selector: ".status-pill" }))
      .toBeInTheDocument();
    expect(
      screen.getByText("This file format is not enabled for this workspace."),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          input.toString().endsWith("/library/items") && init?.method === "POST",
      ),
    ).toBe(false);
  });

  it("uploads every dropped file and reports per-file failures", async () => {
    let itemListCalls = 0;
    let treeCalls = 0;
    const uploadedFileNames: string[] = [];
    const firstUploaded = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "First Notes",
      `upload://${WORKSPACE_ID}/first.md`,
      "sha256:first",
      "# First Notes\n\nFirst artifact",
    );
    const secondUploaded = libraryItemDetail(
      SECOND_LIBRARY_ITEM_ID,
      "Second Notes",
      `upload://${WORKSPACE_ID}/second.md`,
      "sha256:second",
      "# Second Notes\n\nSecond dropped artifact",
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.split("?")[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items` &&
        (init?.method ?? "GET") === "GET"
      ) {
        return jsonResponse({
          data:
            itemListCalls++ === 0
              ? []
              : [
                  libraryItemSummary(LIBRARY_ITEM_ID, "First Notes", firstUploaded.uri),
                  libraryItemSummary(
                    SECOND_LIBRARY_ITEM_ID,
                    "Second Notes",
                    secondUploaded.uri,
                  ),
                ],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`) {
        return jsonResponse({
          data: treeResponse(
            "library",
            treeCalls++ === 0
              ? []
              : [
                  treeNode("node-1", LIBRARY_ITEM_ID, "First Notes"),
                  treeNode("node-2", SECOND_LIBRARY_ITEM_ID, "Second Notes"),
                ],
          ),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`) {
        const file = (init?.body as FormData).get("file") as File;
        uploadedFileNames.push(file.name);
        if (file.name === "broken.md") {
          return jsonResponse(
            {
              error_code: "library_upload_too_large",
              message: "Library uploads must be 1 MB or smaller.",
              request_id: "request-2",
              warnings: [],
              limit_bytes: 1000000,
              settings_link: "/settings?section=library",
            },
            422,
          );
        }
        return jsonResponse({
          data: file.name === "first.md" ? firstUploaded : secondUploaded,
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`) {
        return jsonResponse({ data: defaultLibraryCapabilities() });
      }
      if (/\/library\/versions\/[^/]+\/preview$/.test(url)) {
        return jsonResponse(
          {
            error_code: "not_found",
            message: "Preview manifest endpoint is unavailable",
            request_id: "test-request",
            warnings: [],
          },
          404,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    const main = view.container.querySelector(".feature-main") as HTMLElement;

    fireEvent.drop(main, {
      dataTransfer: {
        files: [
          new File(["# First"], "first.md", { type: "text/markdown" }),
          new File(["# Broken"], "broken.md", { type: "text/markdown" }),
          new File(["# Second"], "second.md", { type: "text/markdown" }),
        ],
        types: ["Files"],
      },
    });

    expect(await screen.findByText(/Second dropped artifact/)).toBeInTheDocument();
    expect(uploadedFileNames).toEqual(["first.md", "broken.md", "second.md"]);
    expect(
      await screen.findByText(
        "Failed to upload 1 of 3 files. broken.md: The Library file exceeds the configured upload limit.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open Library policy settings" }),
    ).toHaveAttribute("href", "/settings?section=library");
    expect(screen.getByRole("treeitem", { name: "Second Notes" }))
      .toHaveAttribute("aria-selected", "true");
  });

  it("retries a failed upload with the retained file", async () => {
    let itemListCalls = 0;
    let treeCalls = 0;
    let uploadCalls = 0;
    const operationIds: string[] = [];
    const uploaded = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Retried Notes",
      `upload://${WORKSPACE_ID}/retry.md`,
      "sha256:retried",
      "# Retried Notes\n\nUploaded after retry",
    );
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) => {
        if ((init?.method ?? "GET") === "GET") {
          return {
            data:
              itemListCalls++ === 0
                ? []
                : [libraryItemSummary(LIBRARY_ITEM_ID, "Retried Notes", uploaded.uri)],
          };
        }
        uploadCalls += 1;
        operationIds.push(
          new Headers(init?.headers).get(
            "X-DTX-Library-Upload-Operation-Id",
          ) ?? "",
        );
        return uploadCalls === 1
          ? new Response(JSON.stringify({
              error_code: "library_storage_temporarily_unavailable",
              message: "Temporary upload failure.",
              request_id: "request-retry",
              warnings: [],
            }), {
              status: 503,
              headers: {
                "Content-Type": "application/json",
                "Retry-After": "2",
              },
            })
          : { data: uploaded };
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: () => ({
        data: treeResponse(
          "library",
          treeCalls++ === 0
            ? []
            : [treeNode("node-1", LIBRARY_ITEM_ID, "Retried Notes")],
        ),
      }),
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    await userEvent.upload(
      screen.getByLabelText("Upload Library file"),
      new File(["# Retried Notes"], "retry.md", { type: "text/markdown" }),
    );

    expect(await screen.findByText("Failed", { selector: ".status-pill" }))
      .toBeInTheDocument();
    const retry = screen.getByRole("button", {
      name: "Retry upload for retry.md",
    });
    expect(retry).toBeDisabled();
    expect(screen.getByText(/Try again in [12] seconds\./)).toBeInTheDocument();
    await waitFor(() => expect(retry).toBeEnabled(), { timeout: 3_000 });

    await userEvent.click(retry);

    expect(await screen.findByText(/Uploaded after retry/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Upload queue" }))
      .toHaveTextContent("retry.md Succeeded");
    expect(
      screen.queryByRole("button", { name: "Retry upload for retry.md" }),
    ).toBeNull();
    expect(uploadCalls).toBe(2);
    expect(operationIds[0]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(operationIds[1]).toBe(operationIds[0]);
  });

  it("edits canonical semantic Markdown with the shared editor and saves immediately", async () => {
    const detail = editableMarkdownLibraryItem();
    const semanticPath =
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/` +
      `${LIBRARY_ITEM_ID}-version-1/semantic-markdown`;
    const requests: Array<{ method: string; body: unknown }> = [];
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [detail] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Editable note"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
      [semanticPath]: (_input: RequestInfo | URL, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        if (method === "PUT") {
          const body = JSON.parse(String(init?.body));
          requests.push({ method, body });
          return {
            data: semanticMarkdownPayload({
              body_markdown: body.body_markdown,
              content_sha256: "c".repeat(64),
              overlay_id: "overlay-1",
              revision: 1,
              source: "overlay",
            }),
          };
        }
        requests.push({ method, body: null });
        return { data: semanticMarkdownPayload() };
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Edit Markdown" }),
    );
    const editor = await screen.findByRole("textbox", { name: "Markdown body" });
    expect(screen.getByRole("tab", { name: "Source" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Reading" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Split" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /review/i })).toBeNull();

    const editorView = EditorView.findFromDOM(editor);
    if (!editorView) throw new Error("Shared Markdown editor was not mounted");
    expect(editorView.state.doc.toString()).toBe(semanticMarkdownPayload().body_markdown);
    const editedMarkdown = "# Edited note\n\nSaved directly";
    // This test covers persistence, so deliver its fixture edit in one real
    // CodeMirror transaction instead of rerendering the Library per character.
    act(() => {
      editorView.dispatch({
        changes: { from: 0, to: editorView.state.doc.length, insert: editedMarkdown },
      });
    });
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(requests[0]).toEqual({ method: "GET", body: null });
    expect(requests[1]).toMatchObject({
      method: "PUT",
      body: {
        expected_revision: 0,
        expected_base_artifact_id: `${LIBRARY_ITEM_ID}-artifact-1`,
        expected_base_content_sha256: "a".repeat(64),
      },
    });
    expect((requests[1].body as { body_markdown: string }).body_markdown)
      .toBe(editedMarkdown);
    expect(screen.queryByText(/submit.*review/i)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox", { name: "Markdown body" })).toBeNull();
  });

  it("blocks a stale semantic Markdown save until the latest body is reloaded", async () => {
    const detail = editableMarkdownLibraryItem();
    const semanticPath =
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/` +
      `${LIBRARY_ITEM_ID}-version-1/semantic-markdown`;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [detail] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Editable note"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
      [semanticPath]: (_input: RequestInfo | URL, init?: RequestInit) =>
        init?.method === "PUT"
          ? jsonResponse(
              {
                error_code: "stale_base_version",
                message: "stale details",
                request_id: "request-stale",
                warnings: [],
              },
              409,
            )
          : { data: semanticMarkdownPayload() },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Edit Markdown" }),
    );
    const editor = await screen.findByRole("textbox", { name: "Markdown body" });
    await userEvent.click(editor);
    await userEvent.keyboard("{End} changed");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This Markdown changed after you opened it",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Reload latest" }),
    ).toBeInTheDocument();
  });

  it("keeps the semantic editor single-pane and fully labelled at a 390px viewport", async () => {
    setMobileViewport(true);
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 390,
    });
    const detail = editableMarkdownLibraryItem();
    const semanticPath =
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/` +
      `${LIBRARY_ITEM_ID}-version-1/semantic-markdown`;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [detail] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Editable note"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
      [semanticPath]: {
        data: semanticMarkdownPayload({
          body_markdown: [
            "# Editable note",
            "",
            '<img alt="Raw beacon" src="https://beacon.example/raw.png">',
            '<video controls src="https://beacon.example/raw.mp4"></video>',
            "",
            "![Remote image](https://beacon.example/markdown.png)",
          ].join("\n"),
        }),
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Edit Markdown" }),
    );

    expect(await screen.findByRole("textbox", { name: "Markdown body" }))
      .toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Source" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Reading" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Split" })).toBeNull();
    expect(screen.getByRole("toolbar", { name: "Editor actions" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    await userEvent.click(screen.getByRole("tab", { name: "Reading" }));
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("video")).toBeNull();
    const readingPanel = screen.getByRole("tabpanel", { name: "Reading" });
    expect(within(readingPanel).getByText("Remote image")).toBeInTheDocument();
  });

  it("keeps a successful upload succeeded when the post-upload refresh fails", async () => {
    let itemListCalls = 0;
    const uploaded = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Stored Notes",
      `upload://${WORKSPACE_ID}/stored.md`,
      "sha256:stored",
      "# Stored Notes\n\nUpload persisted before refresh",
    );
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) => {
        if ((init?.method ?? "GET") !== "GET") {
          return { data: uploaded };
        }
        itemListCalls += 1;
        return itemListCalls === 1
          ? { data: [] }
          : jsonResponse(
              {
                error_code: "library_refresh_failed",
                message: "Temporary list refresh failure.",
                request_id: "request-refresh",
                warnings: [],
              },
              503,
            );
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", []),
      },
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    await userEvent.upload(
      screen.getByLabelText("Upload Library file"),
      new File(["# Stored Notes"], "stored.md", { type: "text/markdown" }),
    );

    const uploadQueue = await screen.findByRole("region", { name: "Upload queue" });
    expect(uploadQueue).toHaveTextContent("stored.md Succeeded");
    expect(within(uploadQueue).queryByText("Failed")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Retry upload for stored.md" }),
    ).toBeNull();
    expect(
      await screen.findByText(
        "File upload succeeded, but Library could not be refreshed. Reload to see the latest item.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("Upload persisted before refresh"))
      .toBeInTheDocument();
  });

  it("retries a cancelled active upload with the retained file", async () => {
    let itemListCalls = 0;
    let treeCalls = 0;
    let uploadCalls = 0;
    const uploaded = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Cancelled Then Retried",
      `upload://${WORKSPACE_ID}/cancelled.md`,
      "sha256:cancelled-retried",
      "# Cancelled Then Retried\n\nUpload completed",
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.split("?")[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items` &&
        (init?.method ?? "GET") === "GET"
      ) {
        return jsonResponse({
          data:
            itemListCalls++ === 0
              ? []
              : [
                  libraryItemSummary(
                    LIBRARY_ITEM_ID,
                    "Cancelled Then Retried",
                    uploaded.uri,
                  ),
                ],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`) {
        return jsonResponse({
          data: treeResponse(
            "library",
            treeCalls++ === 0
              ? []
              : [treeNode("node-1", LIBRARY_ITEM_ID, "Cancelled Then Retried")],
          ),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`) {
        uploadCalls += 1;
        if (uploadCalls === 1) {
          return await new Promise<Response>((_resolve, reject) => {
            const abort = () => reject(new DOMException("Aborted", "AbortError"));
            if (init?.signal?.aborted) {
              abort();
            } else {
              init?.signal?.addEventListener("abort", abort, { once: true });
            }
          });
        }
        return jsonResponse({ data: uploaded });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`) {
        return jsonResponse({ data: defaultLibraryCapabilities() });
      }
      if (/\/library\/versions\/[^/]+\/preview$/.test(url)) {
        return jsonResponse(
          {
            error_code: "not_found",
            message: "Preview manifest endpoint is unavailable",
            request_id: "test-request",
            warnings: [],
          },
          404,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    await userEvent.upload(
      screen.getByLabelText("Upload Library file"),
      new File(["# Cancelled"], "cancelled.md", { type: "text/markdown" }),
    );
    expect(await screen.findByText("Uploading", { selector: ".status-pill" }))
      .toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(await screen.findByText(
      "Result unknown — retry to reconcile",
      { selector: ".status-pill" },
    ))
      .toBeInTheDocument();
    const retry = screen.getByRole("button", {
      name: "Retry upload for cancelled.md",
    });
    await waitFor(() => expect(retry).toBeEnabled());
    await userEvent.click(retry);

    expect(await screen.findByText(/Upload completed/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Upload queue" }))
      .toHaveTextContent("cancelled.md Succeeded");
    expect(uploadCalls).toBe(2);
  });

  it("links to Library policy settings when upload exceeds the configured limit", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.split("?")[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items` &&
        (init?.method ?? "GET") === "GET"
      ) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`) {
        return jsonResponse({ data: treeResponse("library", []) });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`) {
        return jsonResponse(
          {
            error_code: "library_upload_too_large",
            message: "Library uploads must be 1 MB or smaller.",
            request_id: "request-1",
            warnings: [],
            limit_bytes: 1000000,
            settings_link: "/settings?section=library",
          },
          422,
        );
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`) {
        return jsonResponse({ data: defaultLibraryCapabilities() });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();

    await userEvent.upload(
      screen.getByLabelText("Upload Library file"),
      new File(["too large"], "large.md", { type: "text/markdown" }),
    );

    expect(
      await screen.findByText(
        "The Library file exceeds the configured upload limit.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open Library policy settings" }),
    ).toHaveAttribute("href", "/settings?section=library");
    expect(
      screen.queryByRole("button", { name: "Retry upload for large.md" }),
    ).toBeNull();
  });

  it("renders JSON Library files with a structured viewer and falls back for invalid JSON", async () => {
    const invalidId = "item-invalid-json";
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "Config JSON", "https://example.com/config.json", "json"),
          libraryItemSummary(invalidId, "Broken JSON", "https://example.com/broken.json", "json"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Config JSON"),
          treeNode("node-2", invalidId, "Broken JSON"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          LIBRARY_ITEM_ID,
          "Config JSON",
          "https://example.com/config.json",
          "sha256:json",
          '{ "service": "dtxWiki", "limits": { "upload": true } }',
          "json",
        ),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${invalidId}`]: {
        data: libraryItemDetail(
          invalidId,
          "Broken JSON",
          "https://example.com/broken.json",
          "sha256:bad-json",
          "{ invalid",
          "json",
        ),
      },
    });

    // C2: deep-link the JSON item so its detail is selected (no auto-open).
    const view = render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Config JSON" }))
      .toBeInTheDocument();
    expect(view.container.querySelector(".library-json-viewer")).toBeInTheDocument();
    expect(view.container.querySelector(".library-json-viewer"))
      .toHaveTextContent("dtxWiki");

    await userEvent.click(screen.getByRole("treeitem", { name: "Broken JSON" }));

    expect(await screen.findByRole("heading", { name: "Broken JSON" }))
      .toBeInTheDocument();
    expect(view.container.querySelector(".library-json-viewer")).not.toBeInTheDocument();
    expect(screen.getByText("{ invalid")).toHaveClass("markdown-snippet");
  });

  it("renders PDF Library files with the PDF viewer when a document URL is available", async () => {
    const detail = {
      ...libraryItemDetail(
        LIBRARY_ITEM_ID,
        "Policy PDF",
        "https://example.com/policy.pdf",
        "sha256:pdf",
        "Extracted PDF text",
        "pdf",
      ),
      preview_url: `/api/v1/workspaces/${WORKSPACE_ID}/library/${LIBRARY_ITEM_ID}/preview`,
      original_url:
        `/api/v1/workspaces/${WORKSPACE_ID}/library/versions/` +
        `${LIBRARY_ITEM_ID}-version-1/content`,
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "Policy PDF", "https://example.com/policy.pdf", "pdf"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Policy PDF"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
    });

    // C2: deep-link the PDF item so its detail is selected (no auto-open).
    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Policy PDF" }))
      .toBeInTheDocument();
    expect(await screen.findByTestId("pdf-item-preview")).toHaveTextContent(
      `/api/v1/workspaces/${WORKSPACE_ID}/library/${LIBRARY_ITEM_ID}/preview`,
    );
    expect(screen.getByTestId("pdf-item-preview"))
      .not.toHaveTextContent("https://example.com/policy.pdf");
    expect(screen.getByTestId("pdf-item-preview")).toHaveTextContent(
      "Extracted PDF text",
    );
  });

  // §3-D: the old `current_ready_version_id` bridge that quietly substituted
  // a previous READY version's content in the Preview pane when the latest
  // version's conversion failed is gone. The pane now always shows the
  // latest version's own state, even when that means "nothing to preview" --
  // the side Inspector's own "ready version" artifact summary is a separate,
  // still-valid concept and is unaffected.
  it("previews only the current version's own state when a newer version failed, but still inspects the last ready version", async () => {
    const ready = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Versioned Notes",
      `upload://${WORKSPACE_ID}/versioned.md`,
      "sha256:ready",
      "# Ready copy\n\nLast successful artifact",
    );
    const latestVersion = {
      ...ready.current_version,
      id: `${LIBRARY_ITEM_ID}-version-2`,
      version_no: 2,
      content_hash: "sha256:failed",
      processing_status: "failed",
    };
    const detail = {
      ...ready,
      current_version_id: latestVersion.id,
      current_ready_version_id: ready.current_version.id,
      current_version: latestVersion,
      versions: [latestVersion, ready.current_version],
      artifacts: [
        {
          ...ready.artifacts[0],
          id: `${LIBRARY_ITEM_ID}-artifact-failed`,
          library_item_version_id: latestVersion.id,
          index_text: "# Failed copy\n\nMust not be previewed",
        },
        ready.artifacts[0],
      ],
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          {
            ...libraryItemSummary(
              LIBRARY_ITEM_ID,
              "Versioned Notes",
              detail.uri,
            ),
            current_version_id: latestVersion.id,
            current_ready_version_id: ready.current_version.id,
            processing_status: "failed",
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Versioned Notes"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
    });

    const view = render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Must not be previewed"))
      .toBeInTheDocument();
    expect(screen.queryByText("Last successful artifact")).toBeNull();
    expect(screen.getByText("Failed", { selector: ".status-pill" }))
      .toBeInTheDocument();
    expect(screen.queryByText(/upload:\/\//)).toBeNull();
    expect(screen.queryByText("memory://artifact")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    expect(view.container.querySelectorAll(".artifact-row")).toHaveLength(1);
    const artifactHeader = screen.getByText("ARTIFACTS").closest("header");
    expect(artifactHeader).not.toBeNull();
    expect(within(artifactHeader as HTMLElement).getByText("1"))
      .toBeInTheDocument();
  });

  it("retries a retryable conversion as an item action without exposing Job details", async () => {
    const failed = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Failed conversion",
      `upload://${WORKSPACE_ID}/failed.docx`,
      "sha256:failed-conversion",
      "",
      "docx",
      "manual_upload",
    );
    failed.processing_status = "failed";
    failed.current_version.processing_status = "failed";
    failed.versions[0].processing_status = "failed";
    Object.assign(failed.current_version, {
      conversion: {
        status: "failed",
        reason: "document_conversion_failed",
        retryable: true,
      },
    });
    Object.assign(failed.versions[0], {
      conversion: {
        status: "failed",
        reason: "document_conversion_failed",
        retryable: true,
      },
    });
    const queued = {
      ...failed,
      processing_status: "queued",
      current_version: {
        ...failed.current_version,
        processing_status: "queued",
        conversion: { status: "queued", reason: null, retryable: false },
      },
      versions: [
        {
          ...failed.versions[0],
          processing_status: "queued",
          conversion: { status: "queued", reason: null, retryable: false },
        },
      ],
    };
    let retryRequests = 0;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          {
            ...libraryItemSummary(
              LIBRARY_ITEM_ID,
              "Failed conversion",
              failed.uri,
              "docx",
              "manual_upload",
            ),
            processing_status: "failed",
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Failed conversion"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: failed,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${failed.current_version.id}/conversion/retry`]: (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) => {
        expect(init?.method).toBe("POST");
        retryRequests += 1;
        return { data: queued };
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    const retry = await screen.findByRole("button", {
      name: "Retry conversion",
    });
    expect(retry).toHaveAttribute("aria-busy", "false");
    expect(document.body).not.toHaveTextContent("library_conversion");
    expect(document.body).not.toHaveTextContent("retry_count");

    await userEvent.click(retry);

    await waitFor(() => expect(retryRequests).toBe(1));
    expect(
      screen.queryByRole("button", { name: "Retry conversion" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Queued", { selector: ".status-pill" }))
      .toBeInTheDocument();
    await userEvent.click(
      within(screen.getByLabelText("Breadcrumb")).getByRole("link", {
        name: "Library",
      }),
    );
    const overview = screen.getByText("Recent updates").closest("section");
    expect(overview).not.toBeNull();
    const overviewRow = within(overview as HTMLElement).getByRole("button", {
      name: /Failed conversion/,
    });
    expect(within(overviewRow).getByText("Queued", { selector: ".status-pill" }))
      .toBeInTheDocument();
  });

  it("does not show retry for a persisted input-limit denial", async () => {
    const denied = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Input limit exceeded",
      `upload://${WORKSPACE_ID}/oversized.docx`,
      "sha256:input-limit",
      "",
      "docx",
      "manual_upload",
    );
    denied.processing_status = "failed";
    denied.current_version.processing_status = "failed";
    denied.versions[0].processing_status = "failed";
    Object.assign(denied.current_version, {
      conversion: {
        status: "failed",
        reason: "document_conversion_failed",
        retryable: false,
      },
    });
    Object.assign(denied.versions[0], {
      conversion: {
        status: "failed",
        reason: "document_conversion_failed",
        retryable: false,
      },
    });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          {
            ...libraryItemSummary(
              LIBRARY_ITEM_ID,
              denied.title,
              denied.uri,
              "docx",
              "manual_upload",
            ),
            processing_status: "failed",
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-input-limit", LIBRARY_ITEM_ID, denied.title),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: denied,
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Input limit exceeded" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Failed", { selector: ".status-pill" }))
      .toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Retry conversion" }),
    ).not.toBeInTheDocument();
  });

  it.each(["failure", "success"] as const)(
    "does not let a late retry %s from the previous item corrupt the current retry state",
    async (lateOutcome) => {
      function failedDetail(id: string, title: string) {
        const detail = libraryItemDetail(
          id,
          title,
          `upload://${WORKSPACE_ID}/${id}.docx`,
          `sha256:${id}`,
          "",
          "docx",
          "manual_upload",
        );
        detail.processing_status = "failed";
        detail.current_version.processing_status = "failed";
        detail.versions[0].processing_status = "failed";
        Object.assign(detail.current_version, {
          conversion: {
            status: "failed",
            reason: "document_conversion_failed",
            retryable: true,
          },
        });
        Object.assign(detail.versions[0], {
          conversion: {
            status: "failed",
            reason: "document_conversion_failed",
            retryable: true,
          },
        });
        return detail;
      }

      function queuedDetail(detail: ReturnType<typeof failedDetail>) {
        return {
          ...detail,
          processing_status: "queued",
          current_version: {
            ...detail.current_version,
            processing_status: "queued",
            conversion: { status: "queued", reason: null, retryable: false },
          },
          versions: [
            {
              ...detail.versions[0],
              processing_status: "queued",
              conversion: { status: "queued", reason: null, retryable: false },
            },
          ],
        };
      }

      const firstFailed = failedDetail(LIBRARY_ITEM_ID, "First failed conversion");
      const secondFailed = failedDetail(
        SECOND_LIBRARY_ITEM_ID,
        "Second failed conversion",
      );
      const firstQueued = queuedDetail(firstFailed);
      const secondQueued = queuedDetail(secondFailed);
      let resolveFirstRetry!: (response: MockFetchResponse) => void;
      let resolveSecondRetry!: (response: MockFetchResponse) => void;
      const firstRetryResponse = new Promise<MockFetchResponse>((resolve) => {
        resolveFirstRetry = resolve;
      });
      const secondRetryResponse = new Promise<MockFetchResponse>((resolve) => {
        resolveSecondRetry = resolve;
      });
      let firstRetryRequests = 0;
      let secondRetryRequests = 0;
      mockFetch({
        [`${BASE}/api/v1/workspaces`]: {
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
          data: [
            {
              ...libraryItemSummary(
                LIBRARY_ITEM_ID,
                firstFailed.title,
                firstFailed.uri,
                "docx",
                "manual_upload",
              ),
              processing_status: "failed",
            },
            {
              ...libraryItemSummary(
                SECOND_LIBRARY_ITEM_ID,
                secondFailed.title,
                secondFailed.uri,
                "docx",
                "manual_upload",
              ),
              processing_status: "failed",
            },
          ],
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
          data: treeResponse("library", [
            treeNode("node-1", LIBRARY_ITEM_ID, firstFailed.title),
            treeNode("node-2", SECOND_LIBRARY_ITEM_ID, secondFailed.title),
          ]),
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
          data: firstFailed,
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${SECOND_LIBRARY_ITEM_ID}`]: {
          data: secondFailed,
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${firstFailed.current_version.id}/conversion/retry`]: () => {
          firstRetryRequests += 1;
          return firstRetryResponse;
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${secondFailed.current_version.id}/conversion/retry`]: () => {
          secondRetryRequests += 1;
          return secondRetryResponse;
        },
      });

      render(
        <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
          <LibraryScreen />
        </MemoryRouter>,
      );

      await userEvent.click(
        await screen.findByRole("button", { name: "Retry conversion" }),
      );
      await waitFor(() => expect(firstRetryRequests).toBe(1));

      await userEvent.click(
        screen.getByRole("treeitem", { name: secondFailed.title }),
      );
      expect(
        await screen.findByRole("heading", {
          name: secondFailed.title,
          level: 2,
        }),
      ).toBeInTheDocument();
      const secondRetry = screen.getByRole("button", {
        name: "Retry conversion",
      });
      await userEvent.click(secondRetry);
      await waitFor(() => expect(secondRetryRequests).toBe(1));
      expect(secondRetry).toHaveAttribute("aria-busy", "true");
      expect(secondRetry).toBeDisabled();

      await act(async () => {
        resolveFirstRetry(
          lateOutcome === "success"
            ? { data: firstQueued }
            : jsonResponse(
                {
                  error_code: "library_conversion_retry_not_available",
                  message: "The previous item can no longer be retried",
                  request_id: "late-first-retry",
                  warnings: [],
                },
                409,
              ),
        );
        await new Promise((resolve) => setTimeout(resolve, 0));
      });

      expect(
        screen.getByRole("heading", { name: secondFailed.title, level: 2 }),
      ).toBeInTheDocument();
      expect(secondRetry).toHaveAttribute("aria-busy", "true");
      expect(secondRetry).toBeDisabled();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      await userEvent.click(secondRetry);
      expect(secondRetryRequests).toBe(1);

      await act(async () => {
        resolveSecondRetry({ data: secondQueued });
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(
        screen.queryByRole("button", { name: "Retry conversion" }),
      ).not.toBeInTheDocument();
      expect(screen.getByText("Queued", { selector: ".status-pill" }))
        .toBeInTheDocument();
    },
  );

  it("does not let a late retry success replace a newer current version of the same item", async () => {
    const title = "Versioned failed conversion";
    const firstFailed = libraryItemDetail(
      LIBRARY_ITEM_ID,
      title,
      `upload://${WORKSPACE_ID}/versioned.docx`,
      "sha256:version-1",
      "",
      "docx",
      "manual_upload",
    );
    firstFailed.current_version_id = firstFailed.current_version.id;
    firstFailed.processing_status = "failed";
    firstFailed.current_version.processing_status = "failed";
    firstFailed.versions[0].processing_status = "failed";
    Object.assign(firstFailed.current_version, {
      conversion: {
        status: "failed",
        reason: "document_conversion_failed",
        retryable: true,
      },
    });
    Object.assign(firstFailed.versions[0], {
      conversion: {
        status: "failed",
        reason: "document_conversion_failed",
        retryable: true,
      },
    });
    const firstQueued = {
      ...firstFailed,
      processing_status: "queued",
      current_version: {
        ...firstFailed.current_version,
        processing_status: "queued",
        conversion: { status: "queued", reason: null, retryable: false },
      },
      versions: [
        {
          ...firstFailed.versions[0],
          processing_status: "queued",
          conversion: { status: "queued", reason: null, retryable: false },
        },
      ],
    };
    const newer = libraryItemDetail(
      LIBRARY_ITEM_ID,
      title,
      `upload://${WORKSPACE_ID}/versioned.docx`,
      "sha256:version-2",
      "# Version two",
      "docx",
      "manual_upload",
    );
    newer.current_version.id = `${LIBRARY_ITEM_ID}-version-2`;
    newer.current_version.version_no = 2;
    newer.current_version.processing_status = "ready";
    newer.current_version_id = newer.current_version.id;
    newer.processing_status = "ready";
    newer.versions = [{ ...newer.current_version }];
    const other = libraryItemDetail(
      SECOND_LIBRARY_ITEM_ID,
      "Other library item",
      "https://example.com/other",
    );
    let resolveFirstRetry!: (response: MockFetchResponse) => void;
    const firstRetryResponse = new Promise<MockFetchResponse>((resolve) => {
      resolveFirstRetry = resolve;
    });
    let firstItemReads = 0;
    let retryRequests = 0;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          {
            ...libraryItemSummary(
              LIBRARY_ITEM_ID,
              title,
              firstFailed.uri,
              "docx",
              "manual_upload",
            ),
            processing_status: "failed",
          },
          libraryItemSummary(
            SECOND_LIBRARY_ITEM_ID,
            other.title,
            other.uri,
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, title),
          treeNode("node-2", SECOND_LIBRARY_ITEM_ID, other.title),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: () => ({
        data: firstItemReads++ === 0 ? firstFailed : newer,
      }),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${SECOND_LIBRARY_ITEM_ID}`]: {
        data: other,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${firstFailed.current_version.id}/conversion/retry`]: () => {
        retryRequests += 1;
        return firstRetryResponse;
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Retry conversion" }),
    );
    await waitFor(() => expect(retryRequests).toBe(1));
    await userEvent.click(
      screen.getByRole("treeitem", { name: other.title }),
    );
    expect(
      await screen.findByRole("heading", { name: other.title, level: 2 }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("treeitem", { name: title }));
    expect(
      await screen.findByRole("heading", { name: title, level: 2 }),
    ).toBeInTheDocument();
    expect(await screen.findByText("v2")).toBeInTheDocument();

    await act(async () => {
      resolveFirstRetry({ data: firstQueued });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.queryByText("Queued", { selector: ".status-pill" }))
      .not.toBeInTheDocument();
  });

  it("polls converting details until they are ready", async () => {
    let detailCalls = 0;
    const queued = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Converting Notes",
      "https://example.com/converting.md",
    );
    queued.processing_status = "converting";
    queued.current_version.processing_status = "converting";
    queued.versions[0].processing_status = "converting";
    const ready = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Converting Notes",
      "https://example.com/converting.md",
      "sha256:ready",
      "# Converted\n\nPolling finished",
    );
    ready.processing_status = "ready";
    ready.current_version.processing_status = "ready";
    ready.versions[0].processing_status = "ready";
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          {
            ...libraryItemSummary(
              LIBRARY_ITEM_ID,
              "Converting Notes",
              "https://example.com/converting.md",
            ),
            processing_status: "converting",
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Converting Notes"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: () => ({
        data: detailCalls++ === 0 ? queued : ready,
      }),
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Converting", { selector: ".status-pill" }))
      .toBeInTheDocument();
    expect(await screen.findByText("Polling finished", {}, { timeout: 2_500 }))
      .toBeInTheDocument();
    expect(detailCalls).toBeGreaterThanOrEqual(2);
    await userEvent.click(
      within(screen.getByLabelText("Breadcrumb")).getByRole("link", {
        name: "Library",
      }),
    );
    const overview = screen.getByText("Recent updates").closest("section");
    expect(overview).not.toBeNull();
    const overviewRow = within(overview as HTMLElement).getByRole("button", {
      name: /Converting Notes/,
    });
    expect(within(overviewRow).getByText("Ready", { selector: ".status-pill" }))
      .toBeInTheDocument();
  });

  it("ignores an older poll after the same item advances to a new current version", async () => {
    const title = "Version-fenced polling";
    const versionOne = libraryItemDetail(
      LIBRARY_ITEM_ID,
      title,
      "https://example.com/version-fenced",
      "sha256:version-one",
    );
    versionOne.current_version_id = versionOne.current_version.id;
    versionOne.processing_status = "converting";
    versionOne.current_version.processing_status = "converting";
    versionOne.versions[0].processing_status = "converting";
    const versionTwo = libraryItemDetail(
      LIBRARY_ITEM_ID,
      title,
      "https://example.com/version-fenced",
      "sha256:version-two",
    );
    versionTwo.current_version.id = `${LIBRARY_ITEM_ID}-version-2`;
    versionTwo.current_version.version_no = 2;
    versionTwo.current_version.processing_status = "converting";
    versionTwo.current_version_id = versionTwo.current_version.id;
    versionTwo.processing_status = "converting";
    versionTwo.versions = [{ ...versionTwo.current_version }];
    const lateVersionOne = {
      ...versionOne,
      processing_status: "ready",
      current_version: {
        ...versionOne.current_version,
        processing_status: "ready",
      },
      versions: [
        {
          ...versionOne.versions[0],
          processing_status: "ready",
        },
      ],
    };
    let resolveOldPoll!: (response: MockFetchResponse) => void;
    const oldPollResponse = new Promise<MockFetchResponse>((resolve) => {
      resolveOldPoll = resolve;
    });
    let detailReads = 0;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          {
            ...libraryItemSummary(
              LIBRARY_ITEM_ID,
              title,
              versionOne.uri,
            ),
            current_version_id: versionOne.current_version_id,
            processing_status: "converting",
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, title),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: () => {
        detailReads += 1;
        if (detailReads === 1) {
          return { data: versionOne };
        }
        if (detailReads === 2) {
          return oldPollResponse;
        }
        return { data: versionTwo };
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("v1")).toBeInTheDocument();
    await waitFor(() => expect(detailReads).toBe(2), { timeout: 2_500 });
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(detailReads).toBeGreaterThanOrEqual(3));
    expect(await screen.findByText("v2")).toBeInTheDocument();

    await act(async () => {
      resolveOldPoll({ data: lateVersionOne });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.queryByText("v1")).not.toBeInTheDocument();
    expect(screen.getByText("Converting", { selector: ".status-pill" }))
      .toBeInTheDocument();
  });

  it("keeps an appended overview item in sync through retry and conversion polling", async () => {
    const title = "Appended failed conversion";
    const failed = libraryItemDetail(
      SECOND_LIBRARY_ITEM_ID,
      title,
      `upload://${WORKSPACE_ID}/appended.docx`,
      "sha256:appended-failed",
      "",
      "docx",
      "manual_upload",
    );
    failed.processing_status = "failed";
    failed.current_version.processing_status = "failed";
    failed.versions[0].processing_status = "failed";
    Object.assign(failed.current_version, {
      conversion: {
        status: "failed",
        reason: "document_conversion_failed",
        retryable: true,
      },
    });
    Object.assign(failed.versions[0], {
      conversion: {
        status: "failed",
        reason: "document_conversion_failed",
        retryable: true,
      },
    });
    const queued = {
      ...failed,
      processing_status: "queued",
      current_version: {
        ...failed.current_version,
        processing_status: "queued",
        conversion: { status: "queued", reason: null, retryable: false },
      },
      versions: [
        {
          ...failed.versions[0],
          processing_status: "queued",
          conversion: { status: "queued", reason: null, retryable: false },
        },
      ],
    };
    const ready = {
      ...queued,
      processing_status: "ready",
      current_version: {
        ...queued.current_version,
        processing_status: "ready",
        conversion: { status: "ready", reason: null, retryable: false },
      },
      versions: [
        {
          ...queued.versions[0],
          processing_status: "ready",
          conversion: { status: "ready", reason: null, retryable: false },
        },
      ],
      artifacts: [
        {
          ...failed.artifacts[0],
          index_text: "# Appended conversion ready",
        },
      ],
    };
    let detailReads = 0;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items?limit=10`]: {
        data: [
          libraryItemSummary(
            LIBRARY_ITEM_ID,
            "Base page item",
            "https://example.com/base",
          ),
        ],
        next_cursor: "cursor-2",
        total: 2,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items?limit=10&cursor=cursor-2`]: {
        data: [
          {
            ...libraryItemSummary(
              SECOND_LIBRARY_ITEM_ID,
              title,
              failed.uri,
              "docx",
              "manual_upload",
            ),
            processing_status: "failed",
          },
        ],
        next_cursor: null,
        total: 2,
        has_more: false,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [folderNode("references", "References")]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${SECOND_LIBRARY_ITEM_ID}`]: () => ({
        data: detailReads++ === 0 ? failed : detailReads === 2 ? queued : ready,
      }),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${failed.current_version.id}/conversion/retry`]: {
        data: queued,
      },
    });

    render(
      <MemoryRouter initialEntries={["/library?target=references"]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await userEvent.click(
      await screen.findByRole("button", { name: new RegExp(title) }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Retry conversion" }),
    );
    expect(await screen.findByText("Queued", { selector: ".status-pill" }))
      .toBeInTheDocument();

    await userEvent.click(
      within(screen.getByLabelText("Breadcrumb")).getByRole("link", {
        name: "Library",
      }),
    );
    let overview = screen.getByText("Recent updates").closest("section");
    expect(overview).not.toBeNull();
    let overviewRow = within(overview as HTMLElement).getByRole("button", {
      name: new RegExp(title),
    });
    expect(within(overviewRow).getByText("Queued", { selector: ".status-pill" }))
      .toBeInTheDocument();

    await userEvent.click(overviewRow);
    await waitFor(() => expect(detailReads).toBeGreaterThanOrEqual(3), {
      timeout: 4_000,
    });
    expect(
      await screen.findByText("Ready", { selector: ".status-pill" }, { timeout: 4_000 }),
    ).toBeInTheDocument();
    await userEvent.click(
      within(screen.getByLabelText("Breadcrumb")).getByRole("link", {
        name: "Library",
      }),
    );
    overview = screen.getByText("Recent updates").closest("section");
    expect(overview).not.toBeNull();
    overviewRow = within(overview as HTMLElement).getByRole("button", {
      name: new RegExp(title),
    });
    expect(within(overviewRow).getByText("Ready", { selector: ".status-pill" }))
      .toBeInTheDocument();
  }, 10_000);

  it("falls back to extracted text when an uploaded PDF has no browser URL", async () => {
    // A version can still lack a viewable original even when it came from an
    // upload -- e.g. the source blob was GC'd after conversion, or this is a
    // legacy row from before per-version original_url existed
    // (backend/services/domain_read.py only sets it when source_blob_id is
    // present). Override the fixture's default (blob-backed) original_url to
    // model that case explicitly.
    const detail = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "Uploaded PDF",
      `upload://${WORKSPACE_ID}/manual.pdf`,
      "sha256:uploaded-pdf",
      "Uploaded extracted text",
      "pdf",
    );
    Object.assign(detail.current_version, { original_url: null });
    Object.assign(detail.versions[0], { original_url: null });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(
            LIBRARY_ITEM_ID,
            "Uploaded PDF",
            `upload://${WORKSPACE_ID}/manual.pdf`,
            "pdf",
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Uploaded PDF"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
    });

    // C2: deep-link the PDF item so its detail is selected (no auto-open).
    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Uploaded PDF" }))
      .toBeInTheDocument();
    expect(screen.queryByTestId("pdf-item-preview")).toBeNull();
    expect(screen.getByText("PDF preview unavailable")).toBeInTheDocument();
    expect(screen.getByText("Uploaded extracted text")).toHaveClass("markdown-snippet");
  });

  it("gives an uploaded row a format subtitle instead of a bare one-line row", async () => {
    // The upload:// handle stays hidden (it is internal plumbing), but the row
    // still needs a second line or it renders ~14px shorter than the web-source
    // rows beside it. The format is the one fact the row does not already show.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(
            LIBRARY_ITEM_ID,
            "Uploaded Notes",
            `upload://${WORKSPACE_ID}/notes.md`,
            "markdown",
            "manual_upload",
          ),
          libraryItemSummary(
            SECOND_LIBRARY_ITEM_ID,
            "Web Source",
            "https://example.com/web-source",
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Uploaded Notes"),
          treeNode("node-2", SECOND_LIBRARY_ITEM_ID, "Web Source"),
        ]),
      },
    });

    render(
      <MemoryRouter initialEntries={["/library"]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    await screen.findAllByText("Uploaded Notes");
    const rows = Array.from(
      document.querySelectorAll<HTMLElement>(".entity-row"),
    );
    const uploadedRow = rows.find((row) =>
      row.textContent?.includes("Uploaded Notes"),
    );
    const webRow = rows.find((row) => row.textContent?.includes("Web Source"));
    expect(uploadedRow).toBeDefined();
    expect(webRow).toBeDefined();
    expect(within(uploadedRow!).getByText("Markdown")).toBeInTheDocument();
    expect(screen.queryByText(`upload://${WORKSPACE_ID}/notes.md`)).toBeNull();
    expect(
      within(webRow!).getByText("https://example.com/web-source"),
    ).toBeInTheDocument();
  });

  it("keeps detail upload out of desktop and mobile actions and restores focus after delete cancellation", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(
            LIBRARY_ITEM_ID,
            "API Item",
            "https://example.com/api-source",
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          LIBRARY_ITEM_ID,
          "API Item",
          "https://example.com/api-source",
        ),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}/references`]: {
        data: [],
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "API Item", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload" })).toBeNull();
    expect(screen.getByLabelText("Upload Library file")).toBeInTheDocument();
    expect(
      within(screen.getByLabelText("Mobile workspace actions"))
        .queryByRole("button", { name: /upload/i }),
    ).toBeNull();

    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    expect(deleteButtons).toHaveLength(1);
    const deleteButton = deleteButtons[0];
    expect(deleteButton).toHaveClass("danger", "library-reader-delete-action");
    const headingActions = deleteButton.closest(".detail-heading-actions");
    expect(headingActions).not.toBeNull();
    expect(
      within(headingActions as HTMLElement).getByRole("link", {
        name: "Download original file",
      }),
    ).toBeInTheDocument();

    await user.click(deleteButton);
    const dialog = await screen.findByRole("dialog", { name: "Delete API Item" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(deleteButton).toHaveFocus());
  });

  // P10.1: the delete-confirmation dialog's referencing-page list used to
  // link through the deleted /p/{pageId} permalink resolver.
  it("links a referencing Context page to its canonical permalink in the delete dialog", async () => {
    const user = userEvent.setup();
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(
            LIBRARY_ITEM_ID,
            "API Item",
            "https://example.com/api-source",
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          LIBRARY_ITEM_ID,
          "API Item",
          "https://example.com/api-source",
        ),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}/references`]: {
        data: [{ id: "page-1", title: "Runbook", slug: "runbook" }],
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "API Item", level: 2 }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete API Item" });
    expect(within(dialog).getByRole("link", { name: "Runbook" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/context/pages/page-1`,
    );
  });

  it("hides reader upload/delete affordances and drag-drop from viewers", async () => {
    libWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
      },
    });

    // C2: deep-link the item so its detail is selected (no auto-open), then
    // assert the viewer sees no write affordances on the reader.
    const view = render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "API Item", level: 2 }),
    ).toBeInTheDocument();
    // Upload/delete/edit are writes and stay hidden. Original download is a
    // read action, so viewers retain it in the item toolbar.
    expect(screen.queryByRole("button", { name: "Upload" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit Markdown" })).toBeNull();
    expect(
      screen.getByRole("link", { name: "Download original file" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Upload Library file")).toBeNull();

    const main = view.container.querySelector(".feature-main") as HTMLElement;
    fireEvent.dragOver(main, {
      dataTransfer: { dropEffect: "none", files: [], types: ["Files"] },
    });
    expect(main).not.toHaveClass("feature-main--drag-over");
    fireEvent.drop(main, {
      dataTransfer: {
        files: [new File(["# Notes"], "notes.md", { type: "text/markdown" })],
        types: ["Files"],
      },
    });
    expect(screen.queryByText("Uploading")).toBeNull();
  });

  it("hides the header Upload button and empty-state CTA from viewers", async () => {
    libWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", []),
      },
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "No Library items yet" }))
      .toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add first Library item" })).toBeNull();
    expect(screen.getByRole("link", { name: "Open Context" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload" })).toBeNull();
    expect(screen.queryByLabelText("Upload Library file")).toBeNull();
  });

  it("routes the reader connector/format chips through the shared domain label map", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(
            LIBRARY_ITEM_ID,
            "Config JSON",
            "https://example.com/config.json",
            "json",
            "manual_upload",
          ),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Config JSON"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(
          LIBRARY_ITEM_ID,
          "Config JSON",
          "https://example.com/config.json",
          "sha256:json",
          '{ "service": "dtxWiki" }',
          "json",
          "manual_upload",
        ),
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Config JSON", level: 2 }))
      .toBeInTheDocument();
    // connector_type "manual_upload" -> mapped label "Manual upload" (not raw
    // snake_case); format_hint "json" -> acronym-aware "JSON".
    expect(screen.getByText("Manual upload")).toBeInTheDocument();
    expect(screen.queryByText("manual_upload")).toBeNull();
    expect(screen.getByText("JSON")).toBeInTheDocument();
  });

  it("humanizes the artifact type in the inspector via the domain label map", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: libraryItemDetail(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "API Item", level: 2 }))
      .toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));

    // artifact_type "extracted_markdown" -> mapped "Extracted markdown", never
    // the raw snake_case the deleted local labelFor() would have left.
    expect(await screen.findByText("Extracted markdown")).toBeInTheDocument();
    expect(screen.queryByText("extracted_markdown")).toBeNull();
  });

  it("downloads the current stored-unconverted original through an object URL", async () => {
    const originalPath =
      `/api/v1/workspaces/${WORKSPACE_ID}/library/versions/` +
      `${LIBRARY_ITEM_ID}-version-1/content`;
    const detail = storedUnconvertedLibraryItem(originalPath);
    let resolveOriginal!: (response: Response) => void;
    const originalResponse = new Promise<Response>((resolve) => {
      resolveOriginal = resolve;
    });
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [detail] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Stored PDF"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
      [`${BASE}${originalPath}`]: () => originalResponse,
    });
    const createObjectUrl = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:library-original");
    const revokeObjectUrl = vi.spyOn(URL, "revokeObjectURL");
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Stored PDF", level: 2 }))
      .toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const downloadButton = await screen.findByRole("button", {
      name: "Download original file",
    });
    expect(document.body).not.toHaveTextContent("upload://");
    expect(document.body).not.toHaveTextContent("storage_ref://");

    await userEvent.click(downloadButton);

    const busyButton = screen.getByRole("button", {
      name: "Downloading original file…",
    });
    expect(busyButton).toBeDisabled();
    expect(busyButton).toHaveAttribute("aria-busy", "true");

    resolveOriginal(
      new Response("source bytes", {
        headers: {
          "Content-Disposition":
            "attachment; filename=report.pdf; filename*=UTF-8''%EB%B3%B4%EA%B3%A0%EC%84%9C.pdf",
          "Content-Type": "application/pdf",
        },
      }),
    );

    await waitFor(() => {
      expect(revokeObjectUrl).toHaveBeenCalledWith("blob:library-original");
    });
    expect(createObjectUrl).toHaveBeenCalledOnce();
    expect(anchorClick).toHaveBeenCalledOnce();
    const anchor = anchorClick.mock.instances[0] as HTMLAnchorElement;
    expect(anchor.download).toBe("\uBCF4\uACE0\uC11C.pdf");
    expect(document.body.contains(anchor)).toBe(false);
    expect(
      screen.getByRole("button", { name: "Download original file" }),
    ).toBeEnabled();
  });

  it.each(["html", "javascript", "docx", "pptx", "xlsx"])(
    "warns before a potentially active %s original leaves dtxWiki",
    async (formatHint) => {
      const originalPath =
        `/api/v1/workspaces/${WORKSPACE_ID}/library/versions/` +
        `${LIBRARY_ITEM_ID}-version-1/content`;
      const detail = {
        ...storedUnconvertedLibraryItem(originalPath),
        format_hint: formatHint,
        trust_level: ["html", "javascript"].includes(formatHint)
          ? "unknown"
          : "trusted",
      };
      let originalRequests = 0;
      mockFetch({
        [`${BASE}/api/v1/workspaces`]: {
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [detail] },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
          data: treeResponse("library", [
            treeNode("node-1", LIBRARY_ITEM_ID, "Stored PDF"),
          ]),
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
          data: detail,
        },
        [`${BASE}${originalPath}`]: () => {
          originalRequests += 1;
          return new Response("source bytes");
        },
      });
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

      render(
        <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
          <LibraryScreen />
        </MemoryRouter>,
      );

      expect(await screen.findByRole("heading", { name: "Stored PDF", level: 2 }))
        .toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
      await userEvent.click(await screen.findByRole("button", {
        name: "Download original file",
      }));

      expect(confirm).toHaveBeenCalledWith(
        "dtxWiki does not execute this file during preview, but the original may contain active content that another application can run after download. Download only if you trust the file.",
      );
      expect(originalRequests).toBe(0);
      expect(screen.getByRole("button", { name: "Download original file" }))
        .not.toBeDisabled();
    },
  );

  it("reports an original-download failure accessibly and allows retry", async () => {
    const originalPath =
      `/api/v1/workspaces/${WORKSPACE_ID}/library/versions/` +
      `${LIBRARY_ITEM_ID}-version-1/content`;
    const detail = storedUnconvertedLibraryItem(originalPath);
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [detail] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Stored PDF"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
      [`${BASE}${originalPath}`]: () => {
        throw new TypeError("network details must not reach the UI");
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Stored PDF", level: 2 }))
      .toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    await userEvent.click(
      await screen.findByRole("button", { name: "Download original file" }),
    );

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("The original file could not be downloaded. Please try again.");
    const retryButton = screen.getByRole("button", {
      name: "Download original file",
    });
    expect(retryButton).toBeEnabled();
    expect(retryButton).toHaveAttribute("aria-busy", "false");
  });

  it("renders a translated, numeric Items count badge in the overview header", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [
          libraryItemSummary(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source"),
          libraryItemSummary(
            SECOND_LIBRARY_ITEM_ID,
            "Design Item",
            "https://example.com/design-source",
          ),
        ],
        next_cursor: null,
        total: 2,
        has_more: false,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          folderNode("references", "References"),
        ]),
      },
    });

    // Land on a folder so the overview header (not an item detail) renders.
    render(
      <MemoryRouter initialEntries={["/library?target=references"]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Library" }))
      .toBeInTheDocument();
    // Translated eyebrow + a pluralized count whose visible badge reads
    // "{n} items" (F-3), not a bare number.
    expect(screen.getByText("Reference store")).toBeInTheDocument();
    expect(screen.getByText("2 items")).toHaveTextContent(/^2\s*items$/);
  });

  // §3-D: Library always shows the latest version -- there is no more
  // History-driven selection to bind Preview or the trailing download link
  // to (the retired P10.2 contract). History itself only ever offers a
  // read-only, per-row download of that row's own preserved original.
  it("always shows the current version and never lets History change what Preview or the trailing download link target", async () => {
    const detail = twoVersionLibraryItemDetail();
    const olderVersion = detail.versions[1];
    const currentVersion = detail.versions[0];
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [libraryItemSummary(LIBRARY_ITEM_ID, "Versioned Item", "https://example.com/versioned-source")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "Versioned Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Versioned Item", level: 2 }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/Second version body/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download original file" }))
      .toHaveAttribute("href", currentVersion.original_url);

    await userEvent.click(screen.getByRole("tab", { name: /History/ }));

    // Both rows are plain metadata containers now, not clickable buttons.
    const rows = screen.getAllByText(/^v[12]$/).map((el) => el.closest(".history-card"));
    expect(rows.every((row) => row?.tagName !== "BUTTON")).toBe(true);

    // v1's preserved original gets its own download action...
    expect(
      screen.getByRole("link", { name: "Download this version" }),
    ).toHaveAttribute("href", olderVersion.original_url);

    // ...and neither viewing History nor that per-row download changes what
    // the reader itself is displaying or what the trailing action targets.
    expect(screen.getByRole("link", { name: "Download original file" }))
      .toHaveAttribute("href", currentVersion.original_url);
    await userEvent.click(screen.getByRole("tab", { name: "View" }));
    expect(await screen.findByText(/Second version body/)).toBeInTheDocument();
    expect(screen.queryByText(/First version body/)).toBeNull();
  });

  it("uploads a new version onto an existing item and reflects it as current (P10.2)", async () => {
    const detail = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "API Item",
      "https://example.com/api-source",
    );
    const updatedVersion = {
      id: `${LIBRARY_ITEM_ID}-version-2`,
      library_item_id: LIBRARY_ITEM_ID,
      version_no: 2,
      content_hash: "sha256:v2",
      raw_storage_ref: "memory://source-v2",
      processing_status: "completed",
      extraction_summary: null,
      previous_version_id: `${LIBRARY_ITEM_ID}-version-1`,
      created_at: "2026-06-01T00:00:00Z",
      fetched_at: "2026-06-01T00:00:00Z",
      original_url: `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${LIBRARY_ITEM_ID}-version-2/content`,
    };
    const updatedDetail = {
      ...detail,
      current_version_id: updatedVersion.id,
      current_version: updatedVersion,
      versions: [updatedVersion, ...detail.versions],
      updated_at: "2026-06-01T00:00:00Z",
    };
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [libraryItemSummary(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}/versions`]: {
        data: updatedDetail,
      },
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "API Item", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /History/ })).toHaveTextContent("1");

    const versionInput = screen.getByLabelText("Upload new version");
    await userEvent.upload(
      versionInput,
      new File(["# API Item v2"], "api-item-v2.md", { type: "text/markdown" }),
    );

    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /History/ })).toHaveTextContent("2"),
    );
    expect(
      screen.getByRole("link", { name: "Download original file" }),
    ).toHaveAttribute("href", updatedVersion.original_url);

    const versionsCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith(`/library/items/${LIBRARY_ITEM_ID}/versions`),
    );
    expect(versionsCall).toBeDefined();
    const [, init] = versionsCall!;
    expect(init).toMatchObject({ method: "POST", credentials: "include" });
    const body = init?.body as FormData;
    expect((body.get("file") as File).name).toBe("api-item-v2.md");
    const headers = new Headers(init?.headers);
    expect(headers.get("X-DTX-Library-Upload-Operation-Id")).toBeTruthy();
  });

  it("reports an add-version failure without losing the item detail", async () => {
    const detail = libraryItemDetail(
      LIBRARY_ITEM_ID,
      "API Item",
      "https://example.com/api-source",
    );
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [libraryItemSummary(LIBRARY_ITEM_ID, "API Item", "https://example.com/api-source")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [
          treeNode("node-1", LIBRARY_ITEM_ID, "API Item"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}`]: {
        data: detail,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items/${LIBRARY_ITEM_ID}/versions`]: () =>
        new Response(JSON.stringify({
          error_code: "library_storage_temporarily_unavailable",
          message: "Please retry later.",
          request_id: "request-1",
          warnings: [],
        }), { status: 503, headers: { "Content-Type": "application/json" } }),
    });

    render(
      <MemoryRouter initialEntries={[`/library?target=${LIBRARY_ITEM_ID}`]}>
        <LibraryScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "API Item", level: 2 }),
    ).toBeInTheDocument();
    await userEvent.upload(
      screen.getByLabelText("Upload new version"),
      new File(["# API Item v2"], "api-item-v2.md", { type: "text/markdown" }),
    );

    expect(
      await screen.findByText(
        "Library storage is temporarily unavailable. Retry with the same file; a successful earlier attempt will not be duplicated.",
      ),
    ).toBeInTheDocument();
    // The item detail (and its single existing version) must still be there.
    expect(screen.getByRole("tab", { name: /History/ })).toHaveTextContent("1");
    expect(
      await screen.findByRole("button", { name: "Upload new version" }),
    ).toBeEnabled();
  });

  it("offers Upload or Folder from the rail + when folders are allowed", async () => {
    // The header "+" jumped straight to the file picker, and an empty Library
    // has no row to hang the per-row "+" or the context menu on -- so there
    // was no way to create a folder at all.
    const created: unknown[] = [];
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [], { allow_user_folder_create: true }),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes`]: (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) => {
        created.push(JSON.parse(String(init?.body)));
        return { data: folderNode("folder-new", "Research") };
      },
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Create Library item" }),
    );
    // Upload stays the default for the header; Folder is one click away.
    expect(await screen.findByRole("radio", { name: "Upload" })).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "Folder" }));
    await userEvent.type(
      await screen.findByRole("textbox", { name: "Folder name" }),
      "Research",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toMatchObject({
      section: "library",
      node_kind: "folder",
      title: "Research",
    });
  });

  it("keeps upload one click, and the row + working, when folders are off", async () => {
    // Two failures share this branch. The dialog would show no choice at all
    // with folder creation disabled, so routing through it would add a step to
    // a one-click upload -- and the per-row "+" emits "folder" regardless of
    // canUpload, which used to open a dialog with zero available types: it
    // rendered nothing, had no close button, and the click was simply lost.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: { data: [] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=library`]: {
        data: treeResponse("library", [folderNode("folder-1", "References")]),
      },
    });

    render(
      <MemoryRouter>
        <LibraryScreen />
      </MemoryRouter>,
    );

    const picked = vi.fn();
    const trigger = await screen.findByRole("button", {
      name: "Create Library item",
    });
    screen.getByLabelText("Upload Library file").addEventListener("click", picked);

    await userEvent.click(trigger);
    expect(picked).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();

    await userEvent.click(
      screen.getByRole("button", { name: "Create item under References" }),
    );
    expect(picked).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

type MockFetchResponse = unknown | Response;
type MockFetchFactory = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => MockFetchResponse | Promise<MockFetchResponse>;

function mockFetch(
  responses: Record<
    string,
    MockFetchResponse | MockFetchFactory
  >,
) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    // The Library list endpoint now carries pagination query params
    // (?limit=…&cursor=…); fall back to the query-stripped path so existing
    // mocks keyed on the bare endpoint still match.
    const response =
      responses[url] ??
      responses[url.split("?")[0]] ??
      (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`
        ? { data: defaultLibraryCapabilities() }
        : /\/library\/versions\/[^/]+\/preview$/.test(url)
          ? jsonResponse(
              {
                error_code: "not_found",
                message: "Preview manifest endpoint is unavailable",
                request_id: "test-request",
                warnings: [],
              },
              404,
            )
        : undefined);
    if (!response) {
      throw new Error(`Unexpected request: ${url}`);
    }
    const body = await (
      typeof response === "function" ? response(input, init) : response
    );
    if (body instanceof Response) return body;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

function defaultLibraryCapabilities() {
  const formats = [
    ["markdown", [".md", ".markdown"], ["text/markdown"]],
    ["text", [".txt"], ["text/plain"]],
    ["csv", [".csv"], ["text/csv"]],
    ["json", [".json"], ["application/json"]],
    ["yaml", [".yaml", ".yml"], ["application/yaml", "text/yaml"]],
  ] as const;
  return {
    policy_revision: "test-policy",
    capability_revision: "test-capabilities",
    max_files_per_batch: 20,
    formats: formats.map(([formatId, extensions, mediaTypes]) => ({
      format_id: formatId,
      category: "text",
      extensions,
      media_types: mediaTypes,
      upload: true,
      policy_enabled: true,
      deployment_available: true,
      converter_available: true,
      conversion: "available",
      preview_kind: formatId === "json" ? "metadata" : "markdown",
      max_bytes: 20_000_000,
      max_upload_bytes: 20_000_000,
      max_convertible_bytes: 20_000_000,
      upload_limit_sources: { workspace: 20_000_000 },
      upload_limiting_source: "workspace",
    })),
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function libraryItemSummary(
  id: string,
  title: string,
  uri: string,
  formatHint = "markdown",
  connectorType = "web",
) {
  return {
    id,
    uri,
    title,
    connector_type: connectorType,
    format_hint: formatHint,
    trust_level: "trusted",
    lifecycle_status: "active",
    current_version_id: "source-version-1",
    processing_status: "completed",
    updated_at: "2026-05-09T00:00:00Z",
  };
}

function libraryItemDetail(
  id: string,
  title: string,
  uri: string,
  contentHash = "sha256:source",
  artifactText = "# API Item\n\nNeedle artifact",
  formatHint = "markdown",
  connectorType = "web",
) {
  return {
    ...libraryItemSummary(id, title, uri, formatHint, connectorType),
    metadata: {},
    current_version: {
      id: `${id}-version-1`,
      library_item_id: id,
      version_no: 1,
      content_hash: contentHash,
      raw_storage_ref: "memory://source",
      processing_status: "completed",
      extraction_summary: null,
      previous_version_id: null,
      created_at: "2026-05-09T00:00:00Z",
      fetched_at: "2026-05-09T00:00:00Z",
      original_url: `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${id}-version-1/content`,
    },
    versions: [
      {
        id: `${id}-version-1`,
        library_item_id: id,
        version_no: 1,
        content_hash: contentHash,
        raw_storage_ref: "memory://source",
        processing_status: "completed",
        extraction_summary: null,
        previous_version_id: null,
        created_at: "2026-05-09T00:00:00Z",
        fetched_at: "2026-05-09T00:00:00Z",
        original_url: `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${id}-version-1/content`,
      },
    ],
    artifacts: [
      {
        id: `${id}-artifact-1`,
        library_item_version_id: `${id}-version-1`,
        artifact_type: "extracted_markdown",
        storage_ref: "memory://artifact",
        extraction_method: "plain_text",
        index_text: artifactText,
        search_doc: null,
        location_model: {},
        quality_score: null,
        created_at: "2026-05-09T00:00:00Z",
      },
    ],
  };
}

function twoVersionLibraryItemDetail() {
  const olderVersion = {
    id: `${LIBRARY_ITEM_ID}-version-1`,
    library_item_id: LIBRARY_ITEM_ID,
    version_no: 1,
    content_hash: "sha256:v1",
    raw_storage_ref: "memory://source-v1",
    processing_status: "completed",
    extraction_summary: null,
    previous_version_id: null,
    created_at: "2026-05-01T00:00:00Z",
    fetched_at: "2026-05-01T00:00:00Z",
    original_url: `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${LIBRARY_ITEM_ID}-version-1/content`,
    // Server-authoritative per §3-D: this older version's blob is still
    // retained, so its History row gets a download action.
    original_download_available: true,
  };
  const currentVersion = {
    id: `${LIBRARY_ITEM_ID}-version-2`,
    library_item_id: LIBRARY_ITEM_ID,
    version_no: 2,
    content_hash: "sha256:v2",
    raw_storage_ref: "memory://source-v2",
    processing_status: "completed",
    extraction_summary: null,
    previous_version_id: olderVersion.id,
    created_at: "2026-05-09T00:00:00Z",
    fetched_at: "2026-05-09T00:00:00Z",
    original_url: `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/versions/${LIBRARY_ITEM_ID}-version-2/content`,
  };
  return {
    ...libraryItemSummary(LIBRARY_ITEM_ID, "Versioned Item", "https://example.com/versioned-source"),
    current_version_id: currentVersion.id,
    metadata: {},
    current_version: currentVersion,
    versions: [currentVersion, olderVersion],
    artifacts: [
      {
        id: `${LIBRARY_ITEM_ID}-artifact-2`,
        library_item_version_id: currentVersion.id,
        artifact_type: "extracted_markdown",
        storage_ref: "memory://artifact-v2",
        extraction_method: "plain_text",
        index_text: "# Versioned Item\n\nSecond version body",
        search_doc: null,
        location_model: {},
        quality_score: null,
        created_at: "2026-05-09T00:00:00Z",
      },
      {
        id: `${LIBRARY_ITEM_ID}-artifact-1`,
        library_item_version_id: olderVersion.id,
        artifact_type: "extracted_markdown",
        storage_ref: "memory://artifact-v1",
        extraction_method: "plain_text",
        index_text: "# Versioned Item\n\nFirst version body",
        search_doc: null,
        location_model: {},
        quality_score: null,
        created_at: "2026-05-01T00:00:00Z",
      },
    ],
  };
}

function editableMarkdownLibraryItem() {
  const detail = libraryItemDetail(
    LIBRARY_ITEM_ID,
    "Editable note",
    `upload://${WORKSPACE_ID}/editable.md`,
  );
  Object.assign(detail.current_version, {
    selected_conversion_attempt_id: "attempt-1",
  });
  Object.assign(detail.versions[0], {
    selected_conversion_attempt_id: "attempt-1",
  });
  Object.assign(detail.artifacts[0], {
    role: "markdown",
    media_type: "text/markdown",
    byte_size: 23,
    ordinal: 0,
    is_complete: true,
  });
  return detail;
}

function semanticMarkdownPayload(
  overrides: Partial<{
    body_markdown: string;
    content_sha256: string;
    overlay_id: string | null;
    revision: number;
    source: "base" | "overlay";
  }> = {},
) {
  return {
    library_item_id: LIBRARY_ITEM_ID,
    version_id: `${LIBRARY_ITEM_ID}-version-1`,
    base_artifact_id: `${LIBRARY_ITEM_ID}-artifact-1`,
    base_content_sha256: "a".repeat(64),
    overlay_id: null,
    revision: 0,
    source: "base" as const,
    body_markdown: "# Editable note\n\nOriginal body",
    content_sha256: "b".repeat(64),
    byte_size: 30,
    updated_at: "2026-05-09T00:00:00Z",
    ...overrides,
  };
}

function storedUnconvertedLibraryItem(originalUrl: string) {
  const detail = libraryItemDetail(
    LIBRARY_ITEM_ID,
    "Stored PDF",
    `upload://${WORKSPACE_ID}/stored.pdf`,
    "sha256:stored-source",
    "",
    "pdf",
    "manual_upload",
  );
  const currentVersion = {
    ...detail.current_version,
    id: `${LIBRARY_ITEM_ID}-version-1`,
    library_item_id: LIBRARY_ITEM_ID,
    raw_storage_ref: "storage_ref://private/stored.pdf",
    original_url: originalUrl,
    processing_status: "stored_unconverted",
  };
  return {
    ...detail,
    current_version_id: currentVersion.id,
    current_ready_version_id: null,
    current_ready_version: null,
    lifecycle_status: "stored_unconverted",
    processing_status: "stored_unconverted",
    current_version: currentVersion,
    versions: [currentVersion],
    artifacts: [],
  };
}

function treeNode(
  id: string,
  targetId: string,
  title: string,
  breadcrumb: string[] = [],
): TreeNodePayload {
  return {
    id,
    section: "library",
    title,
    slug: null,
    node_kind: "page_ref",
    parent_id: null,
    sort_key: "0001",
    target_kind: "library_item",
    target_id: targetId,
    is_system_node: false,
    can_have_children: false,
    primary_placement: true,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: breadcrumb.map((ancestor, index) => ({
      id: `ancestor-${index}`,
      section: "library",
      title: ancestor,
      slug: ancestor.toLowerCase(),
      node_kind: "folder",
    })),
  };
}

function folderNode(id: string, title: string): TreeNodePayload {
  return {
    id,
    section: "library",
    title,
    slug: title.toLowerCase(),
    node_kind: "folder",
    parent_id: null,
    sort_key: "0001",
    target_kind: null,
    target_id: null,
    is_system_node: false,
    can_have_children: true,
    primary_placement: false,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: [],
  };
}

function libraryArtifactTreeNode(
  id: string,
  parentId: string,
  targetId: string,
  title: string,
): TreeNodePayload {
  return {
    ...treeNode(id, targetId, title),
    node_kind: "library_artifact_ref",
    parent_id: parentId,
    target_kind: "library_artifact",
    can_have_children: false,
    children_count: 0,
    breadcrumb: [
      {
        id: parentId,
        section: "library",
        title: "Config JSON",
        slug: null,
        node_kind: "page_ref",
      },
    ],
  };
}

function treeResponse(
  section: "library",
  nodes: TreeNodePayload[],
  // Folder creation is off in the default fixture, which is also the branch
  // that keeps uploading a one-click action. Tests needing the choice opt in.
  policy: { allow_user_folder_create: boolean } = { allow_user_folder_create: false },
) {
  return {
    nodes,
    policies: {
      [section]: {
        allow_user_folder_create: policy.allow_user_folder_create,
        allow_page_as_parent: true,
      },
    },
  };
}

function setMobileViewport(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}
