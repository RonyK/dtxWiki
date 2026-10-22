import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TreeNodePayload } from "../../../shared/api/workspace";
import { i18n } from "../../../shared/i18n";
import type { ContextHistoryStatus } from "../../../shared/lib/contextHistory";
import { ContextChildrenIndex, ContextScreen } from "../ContextScreen";

// These tests exercise write affordances (create/rename/move/delete/restore),
// gated on an editor+ role (Codex #25). Default to an owner-role workspace so
// the affordances render; individual tests can flip the role to "viewer".
const { ctxWorkspace } = vi.hoisted(() => ({
  ctxWorkspace: {
    current: { selectedWorkspace: { role: "owner" } } as Record<
      string,
      unknown
    >,
  },
}));
const { contextMermaid } = vi.hoisted(() => ({
  contextMermaid: {
    initialize: vi.fn(),
    parse: vi.fn().mockResolvedValue({ diagramType: "flowchart" }),
    render: vi.fn().mockResolvedValue({
      svg: '<svg xmlns="http://www.w3.org/2000/svg"><text>Decision Log</text></svg>',
    }),
  },
}));
vi.mock(
  "../../../shared/workspace/WorkspaceContext",
  async (importOriginal) => ({
    ...(await importOriginal<object>()),
    useOptionalWorkspaceContext: () => ctxWorkspace.current,
  }),
);
vi.mock("mermaid", () => ({ default: contextMermaid }));

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";
const PAGE_ID = "context-1";
const SECOND_PAGE_ID = "context-2";

describe("ContextScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.localStorage.setItem(
      "dtxwiki:workspace-layout:v2:context:left",
      "true",
    );
    ctxWorkspace.current = {
      selectedWorkspace: { role: "owner", visibility: "public" },
      workspaceSlug: "demo",
    };
  });

  afterEach(() => {
    Reflect.deleteProperty(window, "matchMedia");
  });

  function installClipboardStub() {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    return writeText;
  }

  function mockDecisionLogRoutes(
    options: {
      bodyMarkdown?: string;
      nestedSecondPage?: boolean;
      secondActivityFails?: boolean;
      activityNextCursor?: string;
      decisionLogChildCount?: number;
      reconciliationAvailable?: boolean;
    } = {},
  ) {
    const decisionLogChildCount = options.decisionLogChildCount ?? 0;
    const decisionLogChildren = Array.from(
      { length: decisionLogChildCount },
      (_, index) => ({
        ...treeNode(
          `decision-child-${String(index + 1).padStart(2, "0")}`,
          `decision-child-page-${index + 1}`,
          `Child ${String(index + 1).padStart(2, "0")}`,
        ),
        parent_id: "node-1",
        sort_key: String(index + 1).padStart(4, "0"),
        target_meta: {
          page_type: "overview",
          lifecycle_status: "published",
        },
      }),
    );
    const routes: Record<string, unknown> = {
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [
          contextPage(PAGE_ID, "Decision Log", "decision-log"),
          {
            ...contextPage(
              SECOND_PAGE_ID,
              "Architecture Notes",
              "architecture-notes",
            ),
            current_version_id: "version-2",
            latest_draft_version_id: "version-2",
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          {
            ...treeNode("node-1", PAGE_ID, "Decision Log"),
            can_have_children: decisionLogChildCount > 0,
            children_count: decisionLogChildCount,
          },
          folderNode("ancestor-0", "Inbox", { children_count: 1 }),
          {
            ...treeNode("node-2", SECOND_PAGE_ID, "Architecture Notes", [
              "Inbox",
            ]),
            parent_id: options.nestedSecondPage ? "ancestor-0" : null,
          },
          ...decisionLogChildren,
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]: {
        data: {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          body_md:
            options.bodyMarkdown ?? "# Decision Log\n\nNeedle body",
          workspace_id: WORKSPACE_ID,
          version_id: "version-1",
          version_status: "published",
          history_status: "published_snapshot",
          graph_status: "linear",
          is_visible_head: true,
          is_published_head: true,
          is_draft_head: false,
          is_alternative: false,
          is_discarded: false,
          parent_version_id: null,
          content_hash: "sha256:context",
          created_at: "2026-05-09T00:00:00Z",
          tree_path: [],
          parent_node_id: null,
          children_count: decisionLogChildCount,
          backlink_count: 12,
          warnings: [],
          reconciliation_pending_count: 0,
          reconciliation_pending_count_truncated: false,
          next_reconciliation_version_id: null,
          reconciliation_available: false,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/publish`]: {
        data: {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          is_public: true,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}`]:
        {
          data: {
            ...contextPage(
              SECOND_PAGE_ID,
              "Architecture Notes",
              "architecture-notes",
            ),
            current_version_id: "version-2",
            visible_version_id: "version-2",
            latest_draft_version_id: "version-2",
            body_md: "# Architecture Notes\n\nSelected second page",
            workspace_id: WORKSPACE_ID,
            version_id: "version-2",
            version_status: "draft",
            history_status: "current_draft",
            graph_status: "linear",
            is_visible_head: true,
            is_published_head: false,
            is_draft_head: true,
            is_alternative: false,
            is_discarded: false,
            parent_version_id: null,
            content_hash: "sha256:context-second",
            created_at: "2026-05-09T00:00:00Z",
            tree_path: [],
            parent_node_id: null,
            children_count: 0,
            warnings: [],
            reconciliation_pending_count: options.reconciliationAvailable ? 1 : 0,
            reconciliation_pending_count_truncated: false,
            next_reconciliation_version_id: options.reconciliationAvailable
              ? "version-alt"
              : null,
            reconciliation_available: options.reconciliationAvailable === true,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/activity?period=all&limit=50`]:
        {
          data: {
            items: [
              {
                id: "move-event-1",
                lane: "history",
                event_key: "tree.node.move",
                event_args: {
                  previous_parent_id: "folder-old",
                  new_parent_id: "folder-new",
                },
                title: "tree.node.move",
                body: "tree.node.move",
                target_title: null,
                actor: {
                  actor_kind: "human",
                  actor_id: null,
                  display_name: "Test Editor",
                  is_current_user: false,
                },
                target: {
                  target_kind: "workspace_tree_node",
                  target_id: "node-1",
                },
                created_at: "2026-05-10T00:00:00Z",
                review_item_id: null,
                affordances: ["open"],
              },
            ],
            unread_count: 0,
            next_cursor: options.activityNextCursor ?? null,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors`]:
        {
          data: {
            backlinks: [
              {
                kind: "context_page",
                id: SECOND_PAGE_ID,
                title: "Architecture Notes",
                path: "/context/architecture-notes.md",
                edge_kind: "wikilink",
                direction: "in",
              },
            ],
            outbound: [],
            citations: [
              {
                kind: "library_artifact",
                id: "artifact-1",
                title: "Deployment Runbook",
                path: "/library/library-1.md",
                edge_kind: "citation_source",
                direction: "out",
              },
            ],
            co_access: [
              {
                kind: "context_page",
                id: SECOND_PAGE_ID,
                title: "Architecture Notes",
                path: "/context/architecture-notes.md",
                edge_kind: "co_access",
                direction: "out",
              },
            ],
            mermaid: null,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors?format=mermaid`]:
        {
          data: {
            backlinks: [
              {
                kind: "context_page",
                id: SECOND_PAGE_ID,
                title: "Architecture Notes",
                path: "/context/architecture-notes.md",
                edge_kind: "wikilink",
                direction: "in",
              },
            ],
            outbound: [],
            citations: [
              {
                kind: "library_artifact",
                id: "artifact-1",
                title: "Deployment Runbook",
                path: "/library/library-1.md",
                edge_kind: "citation_source",
                direction: "out",
              },
            ],
            co_access: [
              {
                kind: "context_page",
                id: SECOND_PAGE_ID,
                title: "Architecture Notes",
                path: "/context/architecture-notes.md",
                edge_kind: "co_access",
                direction: "out",
              },
            ],
            mermaid: 'graph LR\n  seed["Decision Log"]',
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}/neighbors`]:
        {
          data: { backlinks: [], outbound: [], citations: [] },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}/neighbors?format=mermaid`]:
        {
          data: {
            backlinks: [],
            outbound: [],
            citations: [],
            co_access: [],
            mermaid: null,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`]:
        {
          data: {
            versions: [
              historyVersion("version-1", 2, "published_snapshot", {
                actorType: "user",
                isPublishedHead: true,
                isVisibleHead: true,
              }),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${SECOND_PAGE_ID}`]:
        {
          data: {
            versions: [
              historyVersion("version-2", 3, "current_draft", {
                actorType: "agent",
                isDraftHead: true,
                isVisibleHead: true,
              }),
              historyVersion("version-1", 2, "published_snapshot", {
                isPublishedHead: true,
              }),
              historyVersion("version-alt", 1, "alternative_draft", {
                isAlternative: true,
                reconciliationAvailable: options.reconciliationAvailable,
              }),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}/versions/version-alt/discard`]: {
        data: {
          version_id: "version-alt",
          version_no: 1,
          history_status: "discarded_draft",
          graph_status: "discarded",
          is_visible_head: false,
          is_published_head: false,
          is_draft_head: false,
          is_alternative: false,
          is_discarded: true,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}/versions/version-alt/reconciliation`]: {
        data: {
          page: {
            id: SECOND_PAGE_ID,
            slug: "architecture-notes",
            title: "Architecture Notes",
          },
          branch: {
            version_id: "version-alt",
            version_no: 1,
            content_hash: "branch-hash",
            created_at: "2026-05-09T00:00:00Z",
            actor_kind: "agent",
            actor_display_name: "Assistant",
            base_version_id: "version-base",
          },
          base: {
            version_id: "version-base",
            version_no: 1,
            content_hash: "base-hash",
            created_at: "2026-05-08T00:00:00Z",
          },
          original_head: {
            version_id: "version-1",
            version_no: 2,
            content_hash: "head-old-hash",
            created_at: "2026-05-09T00:00:00Z",
          },
          head: {
            version_id: "version-2",
            version_no: 3,
            content_hash: "head-hash",
            created_at: "2026-05-09T00:00:00Z",
          },
          head_advance_count: 1,
          blocks: [],
          conflicts: [],
          clean_changes: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}/activity?period=all&limit=50`]:
        {
          ...(options.secondActivityFails
            ? { error: new Error("activity unavailable") }
            : { data: { items: [], unread_count: 0, next_cursor: null } }),
        },
    };
    if (options.activityNextCursor) {
      routes[
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/activity?period=all&limit=50&cursor=${options.activityNextCursor}`
      ] = {
        data: {
          items: [
            {
              id: "older-create-event",
              lane: "history",
              event_key: "page.create",
              event_args: {},
              title: "page.create",
              body: "page.create",
              target_title: "Decision Log",
              actor: {
                actor_kind: "human",
                actor_id: null,
                display_name: "Earlier Editor",
                is_current_user: false,
              },
              target: { target_kind: "context_page", target_id: PAGE_ID },
              created_at: "2026-05-01T00:00:00Z",
              review_item_id: null,
              affordances: ["open"],
            },
          ],
          unread_count: 0,
          next_cursor: null,
        },
      };
    }
    return mockFetch(routes);
  }

  async function renderDecisionLog(
    options: {
      bodyMarkdown?: string;
      secondActivityFails?: boolean;
      reconciliationAvailable?: boolean;
    } = {},
  ) {
    const fetchMock = mockDecisionLogRoutes(options);
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("heading", { name: "Context" }),
    ).toBeInTheDocument();
    return fetchMock;
  }

  // The former single mega-test drove every surface in one 5s budget and timed
  // out under full-suite load (review R-9 / Codex F-8). Each surface now keeps
  // its own budget over the same fixture.
  it("renders the tree and hides the Details rail before any selection", async () => {
    await renderDecisionLog();
    expect(document.title).toBe("dtxWiki");
    expect(screen.getByText("2 items")).toHaveTextContent(/^2\s*items$/);
    expect(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Needle body")).not.toBeInTheDocument();
    // #4: no Details rail when nothing is selected (empty/default state).
    expect(screen.queryByRole("button", { name: "Show Details" })).toBeNull();
    // The list header carries the primary create action, the slot Library
    // (Upload) and Agent (+ New) already use.
    expect(
      screen.getByRole("link", { name: "New Context page" }),
    ).toHaveAttribute("href", "/context/new");
    expect(screen.queryByRole("link", { name: "Context home" })).toBeNull();
    expect(screen.getByText("Unpublished draft")).toBeInTheDocument();
  });

  it("shows a publish arrival notice once and clears navigation state", async () => {
    mockDecisionLogRoutes();
    render(
      <MemoryRouter
        initialEntries={[
          {
            pathname: "/context",
            search: `?target=${PAGE_ID}`,
            state: {
              contextPublishNotice: { kind: "published_with_findings" },
            },
          },
        ]}
      >
        <ContextScreen />
        <LocationStateProbe />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("Published, but a formatting issue was found."),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("location-state")).toHaveTextContent("null");
    });
  });

  it("says a displayed draft is unpublished and names the reader-visible head", async () => {
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );

    expect(await screen.findByText("Selected second page")).toBeInTheDocument();
    expect(screen.getByText("Editing draft")).toBeInTheDocument();
    const readerVersion = screen.getByRole("button", {
      name: /Readers see v2; open this version in History/,
    });
    expect(readerVersion).toBeInTheDocument();
    await userEvent.click(readerVersion);
    const historyPanel = await screen.findByRole("tabpanel", {
      name: /History/,
    });
    const draftRow = within(historyPanel).getByText("v3").closest("li");
    const publishedRow = within(historyPanel).getByText("v2").closest("li");
    expect(draftRow).toHaveAttribute("aria-current", "page");
    expect(publishedRow).not.toHaveAttribute("aria-current");
    expect(publishedRow).toHaveTextContent("Readers see this version");
    expect(historyPanel.querySelectorAll('[aria-current="page"]')).toHaveLength(
      1,
    );
    await waitFor(() => expect(publishedRow).toHaveFocus());
  });

  it("presents the reader-visible version as one described action with a decorative arrow", async () => {
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );

    const action = await screen.findByRole("button", {
      name: /Readers see v2; open this version in History/,
    });
    const descriptionId = action.getAttribute("aria-describedby");
    const tooltip = descriptionId
      ? document.getElementById(descriptionId)
      : null;

    expect(tooltip).toHaveAttribute("role", "tooltip");
    expect(action).not.toHaveTextContent("TAG TIP");
    expect(
      action.querySelector(".context-version-tag-tip-label"),
    ).toHaveAttribute("aria-hidden", "true");
    expect(
      action.querySelector(".context-version-tag-tip-label svg"),
    ).toBeInTheDocument();
  });

  it("keeps a conditional Children deep link URL-safe and clears its page when leaving", async () => {
    const searches: string[] = [];
    mockDecisionLogRoutes({ decisionLogChildCount: 6 });
    render(
      <MemoryRouter
        initialEntries={[
          `/context?target=${PAGE_ID}&tab=children&page=2`,
        ]}
      >
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextScreen />
      </MemoryRouter>,
    );

    const childrenPanel = await screen.findByRole("tabpanel", {
      name: /Children/,
    });
    expect(
      within(childrenPanel).getByRole("button", { name: /Child 06/ }),
    ).toBeInTheDocument();
    expect(
      within(childrenPanel).queryByRole("button", { name: /Child 05/ }),
    ).toBeNull();
    expect(searches.at(-1)).toBe(
      `?target=${PAGE_ID}&tab=children&page=2`,
    );

    await userEvent.click(screen.getByRole("tab", { name: "View" }));
    await waitFor(() => expect(searches.at(-1)).toBe(`?target=${PAGE_ID}`));
    expect(
      await screen.findByRole("tabpanel", { name: "View" }),
    ).toBeInTheDocument();
  });

  it("restores the Children page through Back and returns to View through Forward", async () => {
    const searches: string[] = [];
    mockDecisionLogRoutes({ decisionLogChildCount: 6 });
    render(
      <MemoryRouter
        initialEntries={[
          `/context?target=${PAGE_ID}&tab=children&page=2`,
        ]}
      >
        <HistoryButtons />
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("tabpanel", { name: /Children/ }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "View" }));
    await waitFor(() => expect(searches.at(-1)).toBe(`?target=${PAGE_ID}`));

    await userEvent.click(screen.getByRole("button", { name: "Go back" }));
    expect(
      await screen.findByRole("tabpanel", { name: /Children/ }),
    ).toBeInTheDocument();
    expect(searches.at(-1)).toBe(
      `?target=${PAGE_ID}&tab=children&page=2`,
    );
    expect(
      screen.getByRole("button", { name: "Child 06" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Go forward" }));
    expect(
      await screen.findByRole("tabpanel", { name: "View" }),
    ).toBeInTheDocument();
    expect(searches.at(-1)).toBe(`?target=${PAGE_ID}`);
  });

  it("falls a stale Children deep link back to View when the page has no direct children", async () => {
    const searches: string[] = [];
    mockDecisionLogRoutes({ decisionLogChildCount: 0 });
    render(
      <MemoryRouter
        initialEntries={[
          `/context?target=${PAGE_ID}&tab=children&page=3`,
        ]}
      >
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("tabpanel", { name: "View" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /Children/ })).toBeNull();
    await waitFor(() => expect(searches.at(-1)).toBe(`?target=${PAGE_ID}`));
  });

  it("synchronizes History with Back and resets to View when the page changes", async () => {
    mockDecisionLogRoutes({ nestedSecondPage: true });
    render(
      <MemoryRouter
        initialEntries={[
          `/context?target=${SECOND_PAGE_ID}`,
          `/context?target=${SECOND_PAGE_ID}&tab=history`,
        ]}
        initialIndex={1}
      >
        <BackButton />
        <ContextScreen />
      </MemoryRouter>,
    );

    const backButton = screen.getByRole("button", { name: "Go back" });
    backButton.focus();
    expect(
      await screen.findByRole("tabpanel", { name: /History/ }),
    ).toBeInTheDocument();
    expect(document.activeElement).toBe(backButton);

    await userEvent.click(backButton);
    expect(
      await screen.findByRole("tabpanel", { name: "View" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "View" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    await userEvent.click(screen.getByRole("tab", { name: /History/ }));
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "View" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("loads the reader-visible published version from older History pages", async () => {
    const detail = {
      ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
      current_version_id: "version-5",
      visible_version_id: "version-5",
      current_published_version_id: "version-2",
      latest_draft_version_id: "version-5",
      published_version_info: {
        version_id: "version-2",
        version_no: 2,
        published_at: "2026-05-08T00:00:00Z",
      },
      body_md: "# Decision Log\n\nDraft body",
      workspace_id: WORKSPACE_ID,
      version_id: "version-5",
      version_status: "draft",
      history_status: "current_draft",
      graph_status: "linear",
      is_visible_head: true,
      is_published_head: false,
      is_draft_head: true,
      is_alternative: false,
      is_discarded: false,
      parent_version_id: "version-4",
      content_hash: "sha256:draft-five",
      created_at: "2026-05-09T00:00:00Z",
      tree_path: [],
      parent_node_id: null,
      children_count: 0,
      warnings: [],
    };
    const olderHistoryUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}&cursor=cursor-1`;
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]: {
        data: detail,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`]:
        {
          data: {
            versions: [
              historyVersion("version-5", 5, "current_draft", {
                isDraftHead: true,
                isVisibleHead: true,
              }),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
            next_cursor: "cursor-1",
          },
        },
      [olderHistoryUrl]: {
        data: {
          versions: [
            historyVersion("version-2", 2, "published_snapshot", {
              isPublishedHead: true,
            }),
          ],
          changesets: [],
          review_decisions: [],
          diffs: null,
          next_cursor: null,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/activity?period=all&limit=50`]:
        {
          data: { items: [], unread_count: 0, next_cursor: null },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors?format=mermaid`]:
        {
          data: {
            backlinks: [],
            outbound: [],
            citations: [],
            co_access: [],
            mermaid: null,
          },
        },
    });

    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    const publishedTip = await screen.findByRole("button", {
      name: /Readers see v2; open this version in History/,
    });
    expect(fetchMock).not.toHaveBeenCalledWith(
      olderHistoryUrl,
      expect.anything(),
    );
    await userEvent.click(publishedTip);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        olderHistoryUrl,
        expect.anything(),
      ),
    );
    const panel = await screen.findByRole("tabpanel", { name: /History/ });
    const publishedRow = within(panel).getByText("v2").closest("li");
    expect(publishedRow).toHaveTextContent("Readers see this version");
    expect(panel.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
    await waitFor(() => expect(publishedRow).toHaveFocus());
  });

  it("renders an actor-hidden or invalid target as the generic Not Found surface", async () => {
    const fetchMock = mockDecisionLogRoutes();
    render(
      <MemoryRouter initialEntries={["/context?target=hidden-page"]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", {
        name: "We couldn't find that page",
      }),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) =>
        String(call[0]).includes("/context/pages/hidden-page"),
      ),
    ).toBe(false);
  });

  it("renders selected markdown with page actions and copy link", async () => {
    const writeText = installClipboardStub();
    await renderDecisionLog();

    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );

    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("Decision Log - dtxWiki"));
    const editAction = screen.getByRole("link", { name: /^Edit$/ });
    const moreAction = screen.getByRole("button", {
      name: "More page actions",
    });
    expect(editAction).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/edit`,
    );
    expect(editAction).toHaveClass("context-reader-header-action");
    expect(moreAction).toHaveClass("context-reader-header-action");
    await userEvent.click(moreAction);
    expect(
      screen.getByRole("menuitem", { name: "Rename" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Move" })).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: "Copy link" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: "Delete" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: "Copy link" }));
    expect(writeText).toHaveBeenCalledWith(
      expect.stringMatching(
        new RegExp(`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}$`),
      ),
    );
    expect(screen.getByText("Link copied")).toBeInTheDocument();
    const readerTabs = within(
      screen.getByRole("tablist", { name: "Context reader views" }),
    );
    expect(
      readerTabs.getAllByRole("tab").map((tab) => tab.textContent),
    ).toEqual(["View", "History1", "GraphPreview", "Activity1"]);
    expect(readerTabs.queryByRole("tab", { name: "Bundle" })).toBeNull();
  });

  it("marks the Graph tab as a localized Preview surface still in development", async () => {
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    const readerTabs = within(
      screen.getByRole("tablist", { name: "Context reader views" }),
    );
    const englishGraphTab = readerTabs.getByRole("tab", {
      name: "Graph, Preview",
    });
    expect(readerTabs.getByText("Preview")).toHaveClass("nav-preview-badge");
    expect(englishGraphTab).toHaveAttribute(
      "title",
      "Preview: this area is still being refined.",
    );

    try {
      await act(async () => {
        await i18n.changeLanguage("ko");
      });
      const koreanGraphTab = readerTabs.getByRole("tab", {
        name: "그래프, 미리보기",
      });
      expect(readerTabs.getByText("미리보기")).toHaveClass(
        "nav-preview-badge",
      );
      expect(koreanGraphTab).toHaveAttribute(
        "title",
        "미리보기: 아직 기능이 완성 단계가 아닙니다.",
      );
    } finally {
      await act(async () => {
        await i18n.changeLanguage("en");
      });
    }
  });

  it("resolves Context media references against the visible page version", async () => {
    const assetId = "6bd44e28-c2ca-4c51-a0f3-09a40efac8c2";
    mockDecisionLogRoutes({
      bodyMarkdown: `# Decision Log\n\n![Architecture](dtx-media://asset/${assetId})`,
    });

    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("img", { name: "Architecture" }),
    ).toHaveAttribute(
      "src",
      `/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/versions/version-1/media/${assetId}/content`,
    );
  });

  it("separates page audit events from version History into Activity", async () => {
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    const readerTabs = within(
      screen.getByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(readerTabs.getByRole("tab", { name: /History/ }));
    const historyPanel = screen.getByRole("tabpanel", { name: /History/ });
    expect(within(historyPanel).getByText("v2")).toBeInTheDocument();
    expect(within(historyPanel).queryByText(/Moved item/)).toBeNull();
    expect(
      within(historyPanel).queryByRole("button", { name: /Select version/ }),
    ).toBeNull();

    await userEvent.click(readerTabs.getByRole("tab", { name: /Activity/ }));
    const activityPanel = screen.getByRole("tabpanel", { name: /Activity/ });
    expect(
      await within(activityPanel).findByText(/Moved item/),
    ).toBeInTheDocument();
    expect(
      within(activityPanel).getByRole("list", { name: "Page activity" }),
    ).toBeInTheDocument();
    expect(within(activityPanel).getByText("Test Editor")).toBeInTheDocument();
    expect(within(activityPanel).queryByText("editor-1")).toBeNull();
    expect(within(activityPanel).queryByText("tree.node.move")).toBeNull();
    expect(within(activityPanel).queryByText("v2")).toBeNull();
  });

  it("appends older Activity cards from the server cursor", async () => {
    const cursor = "activity-cursor-1";
    const fetchMock = mockDecisionLogRoutes({ activityNextCursor: cursor });
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await userEvent.click(screen.getByRole("tab", { name: /^Activity$/ }));
    const activityPanel = screen.getByRole("tabpanel", { name: /Activity/ });
    expect(
      await within(activityPanel).findByText("Test Editor"),
    ).toBeInTheDocument();

    await userEvent.click(
      within(activityPanel).getByRole("button", {
        name: "Load older activity",
      }),
    );

    expect(
      await within(activityPanel).findByText("Earlier Editor"),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/activity?period=all&limit=50&cursor=${cursor}`,
      expect.anything(),
    );
    expect(
      within(activityPanel).queryByRole("button", {
        name: "Load older activity",
      }),
    ).toBeNull();
  });

  it("presents all lifecycle states and keeps the current draft distinct from the published head", async () => {
    const statusVersions = [
      historyVersion("version-8", 8, "current_draft", {
        isDraftHead: true,
        isVisibleHead: true,
      }),
      historyVersion("version-7", 7, "published_snapshot", {
        isPublishedHead: true,
      }),
      historyVersion("version-6", 6, "alternative_draft", {
        isAlternative: true,
      }),
      historyVersion("version-5", 5, "discarded_draft", {
        isDiscarded: true,
      }),
      historyVersion("version-4", 4, "superseded_snapshot"),
      historyVersion("version-3", 3, "superseded_draft"),
      historyVersion("version-2", 2, "state_inconsistent"),
    ];
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [
          {
            ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
            visible_version_id: "version-8",
            current_version_id: "version-8",
            current_published_version_id: "version-7",
            latest_draft_version_id: "version-8",
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]: {
        data: {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          visible_version_id: "version-8",
          current_version_id: "version-8",
          current_published_version_id: "version-7",
          latest_draft_version_id: "version-8",
          body_md: "# Decision Log\n\nCurrent draft body",
          workspace_id: WORKSPACE_ID,
          version_id: "version-8",
          version_status: "draft",
          history_status: "current_draft",
          graph_status: "linear",
          is_visible_head: true,
          is_published_head: false,
          is_draft_head: true,
          is_alternative: false,
          is_discarded: false,
          parent_version_id: "version-7",
          content_hash: "sha256:draft",
          created_at: "2026-05-10T00:00:00Z",
          tree_path: [],
          parent_node_id: null,
          children_count: 0,
          backlink_count: 0,
          warnings: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`]:
        {
          data: {
            versions: statusVersions,
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await userEvent.click(screen.getByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });

    const expectedStates = [
      [
        "v8",
        "Current draft",
        "The current edit is not published yet. Readers still see the published version.",
        "warn",
      ],
      ["v7", "Current published snapshot", "The version readers see.", "ok"],
      [
        "v6",
        "Alternative draft",
        "Preserved outside the current editing path.",
        "info",
      ],
      [
        "v5",
        "Discarded draft",
        "Removed from the active alternatives.",
        "muted",
      ],
      [
        "v4",
        "Superseded snapshot",
        "Replaced by a newer published version.",
        "neutral",
      ],
      ["v3", "Superseded draft", "Replaced by a newer version.", "neutral"],
      ["v2", "State review required", "Operator repair is required", "danger"],
    ] as const;
    for (const [version, label, description, tone] of expectedStates) {
      const card = within(panel)
        .getByText(version)
        .closest(".history-card") as HTMLElement;
      expect(card).toHaveAttribute("data-tone", tone);
      expect(within(card).getByText(label)).toBeInTheDocument();
      expect(
        within(card).getByText(
          new RegExp(description.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
        ),
      ).toBeInTheDocument();
    }

    const draftHead = within(panel)
      .getByText("v8")
      .closest(".history-card") as HTMLElement;
    const publishedHead = within(panel)
      .getByText("v7")
      .closest(".history-card") as HTMLElement;
    expect(draftHead).toHaveAttribute("aria-current", "page");
    expect(
      within(draftHead).getByRole("button", { name: "Show changes in v8 since v7" }),
    ).toBeInTheDocument();
    expect(
      within(draftHead).queryByRole("button", { name: "Restore this version" }),
    ).toBeNull();
    expect(publishedHead).not.toHaveAttribute("aria-current");
    expect(
      within(publishedHead).getByRole("button", { name: "Show changes in v7 since v6" }),
    ).toBeInTheDocument();
    expect(
      within(publishedHead).getByRole("button", {
        name: "Restore this version",
      }),
    ).toBeInTheDocument();
    expect(
      within(panel).getAllByRole("button", { name: "Discard" }),
    ).toHaveLength(1);
  });

  it("discards a preserved alternative with an explicit reason", async () => {
    const fetchMock = await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );
    const readerTabs = within(
      screen.getByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(readerTabs.getByRole("tab", { name: /History/ }));
    const historyPanel = screen.getByRole("tabpanel", { name: /History/ });
    const alternative = within(historyPanel)
      .getByText("v1")
      .closest(".history-card") as HTMLElement;

    await userEvent.click(
      within(alternative).getByRole("button", { name: "Discard" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Discard alternative v1? This cannot be undone.",
    });
    const confirm = within(dialog).getByRole("button", {
      name: "Discard branch permanently",
    });
    expect(confirm).toBeDisabled();
    await userEvent.type(
      within(dialog).getByLabelText("Discard reason"),
      "Superseded experiment",
    );
    await userEvent.click(confirm);

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}/versions/version-alt/discard`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ reason: "Superseded experiment" }),
      }),
    );
    expect(
      await screen.findByText("Alternative v1 was discarded."),
    ).toBeInTheDocument();
  });

  it("opens reconciliation only when the version capability is explicitly true", async () => {
    const fetchMock = await renderDecisionLog({ reconciliationAvailable: true });
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );

    expect(
      await screen.findByText(/1 branch requires reconciliation/i),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("tab", { name: /History/ }),
    );
    const alternative = within(screen.getByRole("tabpanel", { name: /History/ }))
      .getByText("v1")
      .closest(".history-card") as HTMLElement;
    expect(
      within(alternative).queryByRole("button", { name: "Restore this version" }),
    ).toBeNull();
    await userEvent.click(
      within(alternative).getByRole("button", { name: "Reconcile" }),
    );

    expect(
      await screen.findByRole("dialog", { name: "Architecture Notes" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}/versions/version-alt/reconciliation`,
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("keeps reconciliation hidden when the capability is absent", async () => {
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );

    expect(screen.queryByRole("button", { name: "Reconcile" })).toBeNull();
    expect(screen.queryByText(/requires reconciliation/i)).toBeNull();
  });

  it("renders Activity failure separately from its empty state", async () => {
    await renderDecisionLog({ secondActivityFails: true });
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );
    const readerTabs = within(
      screen.getByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(readerTabs.getByRole("tab", { name: /^Activity$/ }));

    expect(
      await screen.findByText("Could not load this page's activity."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Nothing has happened to this page yet."),
    ).toBeNull();
  });

  it("keeps the inspector to one row of Summary, Properties, and Backlinks", async () => {
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    // The Details inspector is now a TabbedInspector (item 25): Summary is the
    // default tab and still shows the citation-health summary. Scope queries to
    // the inspector region so they don't collide with the reader's tabs/chips.
    const inspector = within(
      screen.getByRole("region", { name: "Context detail" }),
    );
    expect(inspector.getByRole("tab", { name: /Summary/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(inspector.getByText("CITATION HEALTH")).toBeInTheDocument();
    expect(inspector.getByText("No warnings")).toBeInTheDocument();
    expect(inspector.getByText("0 warnings")).toBeInTheDocument();
    // Edit draft action removed from the Details inspector (item 26); the
    // central Edit button covers it.
    expect(screen.queryByRole("link", { name: "Edit draft" })).toBeNull();
    // Properties stays focused on page metadata; graph projection state belongs
    // to the body-width Graph view, not the default inspector.
    await userEvent.click(inspector.getByRole("tab", { name: /Properties/ }));
    expect(inspector.getByText("Decision log")).toBeInTheDocument();
    expect(inspector.getByText("decision-log")).toBeInTheDocument();
    expect(
      inspector.getByText("Current published snapshot"),
    ).toBeInTheDocument();
    expect(inspector.queryByText(/^Graph$/)).toBeNull();
    // Sprint-5 G2: the graph projection supplies navigable backlink and
    // citation lists while preserving the shared inspector panel.
    await userEvent.click(inspector.getByRole("tab", { name: /Backlinks/ }));
    expect(inspector.getByRole("tab", { name: /Backlinks/ })).toHaveTextContent(
      "12",
    );
    expect(
      inspector.getAllByRole("link", { name: /Architecture Notes/ })[0],
    ).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}`,
    );
    expect(
      inspector.getByRole("link", { name: /Deployment Runbook/ }),
    ).toHaveAttribute("href", "/library?target=library-1");
    expect(inspector.getByText("CITATIONS")).toBeInTheDocument();
    expect(inspector.getByText("OFTEN READ TOGETHER")).toBeInTheDocument();
    const citations = inspector.getByLabelText("CITATIONS");
    const coAccess = inspector.getByLabelText("OFTEN READ TOGETHER");
    expect(
      citations.querySelector(".page-backlinks-heading > span"),
    ).toBeNull();
    expect(coAccess.querySelector(".page-backlinks-heading > span")).toBeNull();
    expect(inspector.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(
      ["Summary", "Properties", "Backlinks12"],
    );
    expect(inspector.queryByRole("tab", { name: /^Graph$/ })).toBeNull();
    expect(inspector.queryByRole("tab", { name: /History/ })).toBeNull();
  });

  it("renders the body-width graph from one cached neighbor response", async () => {
    // Split out of the inspector-tab walk above: the reader tablist is a
    // separate control, and combining both walks in one test pushed its many
    // sequential userEvent interactions past the default timeout under parallel
    // suite load (masking, not fixing, the slowness). Each half now stays well
    // under the default budget.
    const fetchMock = await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    const readerTabs = within(
      screen.getByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    const graph = await screen.findByTestId("context-local-graph");
    expect(
      within(graph).getByRole("heading", { name: "Graph relationships" }),
    ).toBeInTheDocument();
    const architectureLinks = within(graph).getAllByRole("link", {
      name: "Architecture Notes",
    });
    expect(architectureLinks).toHaveLength(2);
    for (const link of architectureLinks) {
      expect(link).toHaveAttribute(
        "href",
        `/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}`,
      );
    }
    expect(
      within(graph).getByRole("link", { name: "Deployment Runbook" }),
    ).toHaveAttribute("href", "/library?target=library-1");
    await userEvent.click(readerTabs.getByRole("tab", { name: /View/ }));
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        String(input).includes(
          `/context/pages/${PAGE_ID}/neighbors?format=mermaid`,
        ),
      ),
    ).toHaveLength(1);
  });

  it("separates the member-only workspace link from readonly web publishing", async () => {
    const writeText = installClipboardStub();
    const fetchMock = await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    await screen.findByText("Needle body");

    await userEvent.click(screen.getByRole("button", { name: "Share" }));
    const dialog = screen.getByRole("dialog", { name: "Share Decision Log" });
    expect(
      within(dialog).getByRole("heading", { name: "Workspace link" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Only signed-in workspace members/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("heading", { name: "Publish to web" }),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("Public and discoverable")).toBeInTheDocument();
    expect(within(dialog).getByText("Ready")).toBeInTheDocument();

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Copy workspace link" }),
    );
    expect(writeText).toHaveBeenCalledWith(
      expect.stringMatching(
        new RegExp(`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}$`),
      ),
    );

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Publish to web" }),
    );
    expect(
      await within(dialog).findByRole("button", { name: "Unpublish from web" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText("/w/demo/pages/decision-log"),
    ).toBeInTheDocument();
    const publicPublishCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/publish`,
    );
    expect(publicPublishCall?.[1]).toEqual(
      expect.objectContaining({ method: "POST" }),
    );
    expect(JSON.parse(String(publicPublishCall?.[1]?.body))).toEqual({
      acknowledge_public_exposure: false,
      expected_published_version_id: "version-1",
      is_public: true,
    });
  });

  it("requires an Owner acknowledgement for an unlisted share from a private Workspace", async () => {
    ctxWorkspace.current = {
      selectedWorkspace: { role: "owner", visibility: "private" },
      workspaceSlug: "demo",
    };
    const fetchMock = await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    await screen.findByText("Needle body");
    await userEvent.click(screen.getByRole("button", { name: "Share" }));

    const dialog = screen.getByRole("dialog", { name: "Share Decision Log" });
    expect(within(dialog).getByText("Unlisted direct link")).toBeInTheDocument();
    const publish = within(dialog).getByRole("button", {
      name: "Publish to web",
    });
    expect(publish).toBeDisabled();

    await userEvent.click(
      within(dialog).getByRole("checkbox", {
        name: /Anyone with the link can read this page/i,
      }),
    );
    expect(publish).toBeEnabled();
    await userEvent.click(publish);

    const privatePublishCall = fetchMock.mock.calls.find(
      ([input]) => String(input) === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/publish`,
    );
    expect(privatePublishCall?.[1]).toEqual(
      expect.objectContaining({ method: "POST" }),
    );
    expect(JSON.parse(String(privatePublishCall?.[1]?.body))).toEqual({
      acknowledge_public_exposure: true,
      expected_published_version_id: "version-1",
      is_public: true,
    });
  });

  it("keeps Share mounted and disables explicit close while web publishing is pending", async () => {
    const publishResponse = deferredResponse();
    const fetchMock = await renderDecisionLog();
    const regularFetch = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input, init) => {
      if (input.toString() === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/publish`) {
        return publishResponse.promise;
      }
      if (!regularFetch) {
        throw new Error(`Unexpected request: ${input.toString()}`);
      }
      return regularFetch(input, init);
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    await screen.findByText("Needle body");
    await user.click(screen.getByRole("button", { name: "Share" }));
    const dialog = screen.getByRole("dialog", { name: "Share Decision Log" });
    await user.click(
      within(dialog).getByRole("button", { name: "Publish to web" }),
    );

    expect(
      within(dialog).getByRole("button", { name: "Saving…" }),
    ).toBeDisabled();
    const closeButton = within(dialog).getByRole("button", { name: "Close dialog" });
    expect(closeButton).toBeDisabled();
    closeButton.removeAttribute("disabled");
    fireEvent.click(closeButton);
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.mouseDown(document.querySelector(".dialog-backdrop") as Element);
    expect(
      screen.getByRole("dialog", { name: "Share Decision Log" }),
    ).toBeInTheDocument();

    publishResponse.resolve(
      jsonResponse({
        data: {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          is_public: true,
        },
      }),
    );
    expect(
      await within(dialog).findByRole("button", { name: "Unpublish from web" }),
    ).toBeInTheDocument();
    expect(closeButton).toBeEnabled();
  });

  it("does not claim an internal link was copied when clipboard access fails", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    const moreAction = await screen.findByRole("button", {
      name: "More page actions",
    });
    await userEvent.click(moreAction);
    await userEvent.click(screen.getByRole("menuitem", { name: "Copy link" }));

    expect(
      await screen.findByText(/Browser permission blocked copying/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Link copied")).not.toBeInTheDocument();
  });

  it("announces a reasoned TreeRail copy failure and keeps it visible for five seconds", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    await renderDecisionLog();
    fireEvent.contextMenu(
      screen.getByRole("treeitem", { name: "Decision Log" }),
      { clientX: 40, clientY: 40 },
    );
    const copyAction = screen.getByRole("menuitem", { name: "Copy link" });

    vi.useFakeTimers();
    try {
      await act(async () => {
        fireEvent.click(copyAction);
        await Promise.resolve();
        await Promise.resolve();
      });
      const feedback = screen.getByText(/Browser permission blocked copying/);
      expect(feedback.closest('[role="alert"]')).toBeInTheDocument();

      act(() => vi.advanceTimersByTime(4_999));
      expect(
        screen.getByText(/Browser permission blocked copying/),
      ).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(1));
      expect(
        screen.queryByText(/Browser permission blocked copying/),
      ).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not expose a generic loader exception to the Context UI", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        error: new Error("SQLSTATE 42P01 internal detail"),
      },
    });
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Failed to load Context")).toBeInTheDocument();
    expect(screen.queryByText(/SQLSTATE/)).toBeNull();
  });

  it("uses the shared Mermaid renderer for Context View and Graph diagrams", async () => {
    contextMermaid.initialize.mockClear();
    contextMermaid.render.mockClear();
    await renderDecisionLog({
      bodyMarkdown: [
        "# Decision Log",
        "",
        "Needle body",
        "",
        "```mermaid",
        "flowchart LR",
        "  viewA[View diagram] --> viewB[Shared renderer]",
        "```",
      ].join("\n"),
    });
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );

    await waitFor(() =>
      expect(contextMermaid.render).toHaveBeenCalledWith(
        expect.stringMatching(/^dtxwiki-mermaid-/),
        expect.stringContaining("viewA[View diagram]"),
      ),
    );

    const readerTabs = within(
      screen.getByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    await waitFor(() =>
      expect(contextMermaid.render).toHaveBeenCalledWith(
        expect.stringMatching(/^dtxwiki-mermaid-/),
        expect.stringContaining('seed["Decision Log"]'),
      ),
    );

    expect(contextMermaid.initialize).toHaveBeenCalledTimes(2);
    for (const [config] of contextMermaid.initialize.mock.calls) {
      expect(config).toEqual(
        expect.objectContaining({
          flowchart: expect.objectContaining({ useMaxWidth: false }),
          themeVariables: expect.objectContaining({ fontSize: "13px" }),
        }),
      );
    }
  });

  it("switches pages, shows empty graph states, and navigates the breadcrumb", async () => {
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const inspector = within(
      screen.getByRole("region", { name: "Context detail" }),
    );

    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );

    expect(await screen.findByText("Selected second page")).toBeInTheDocument();
    expect(screen.getAllByText("CITATION HEALTH").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    ).toHaveAttribute("aria-selected", "true");
    const readerTabs = within(
      screen.getByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    expect(
      await screen.findByText("No graph relationships to show."),
    ).toBeInTheDocument();
    await userEvent.click(readerTabs.getByRole("tab", { name: /^Activity$/ }));
    expect(
      await screen.findByText("Nothing has happened to this page yet."),
    ).toBeInTheDocument();
    await userEvent.click(inspector.getByRole("tab", { name: /Backlinks/ }));
    expect(
      inspector.getByText("No visible pages link here yet."),
    ).toBeInTheDocument();
    expect(
      inspector.getByText("No visible citations yet."),
    ).toBeInTheDocument();
    const breadcrumb = within(screen.getByLabelText("Breadcrumb"));
    expect(breadcrumb.getByRole("link", { name: "Context" })).toHaveAttribute(
      "href",
      "/context",
    );
    expect(breadcrumb.getByRole("link", { name: "Inbox" })).toHaveAttribute(
      "href",
      "/context?target=ancestor-0",
    );

    await userEvent.click(breadcrumb.getByRole("link", { name: "Inbox" }));

    expect(
      await screen.findByRole("heading", { name: "Inbox" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Selected second page")).not.toBeInTheDocument();
    // #4: a folder selection (no page detail) must NOT resurrect the Details
    // rail — it was previously gated on `state.detail || selectedNode`.
    expect(screen.queryByRole("button", { name: "Show Details" })).toBeNull();
    expect(screen.getByRole("treeitem", { name: "Inbox" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("resolves detail by tree target_id for a page past the flat-list cap", async () => {
    // The flat context/pages list only returns the first page (cap), but the
    // tree carries a node for the second page. Clicking it must fetch the
    // detail by id instead of a silent no-op.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
          treeNode("node-2", SECOND_PAGE_ID, "Capped Page"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}`]:
        {
          data: {
            ...contextPage(SECOND_PAGE_ID, "Capped Page", "capped-page"),
            body_md: "# Capped Page\n\nResolved past the cap",
            workspace_id: WORKSPACE_ID,
            version_id: "version-2",
            version_status: "published",
            history_status: "published_snapshot",
            graph_status: "linear",
            is_visible_head: true,
            is_published_head: true,
            is_draft_head: false,
            is_alternative: false,
            is_discarded: false,
            parent_version_id: null,
            content_hash: "sha256:capped",
            created_at: "2026-05-09T00:00:00Z",
            tree_path: [],
            parent_node_id: null,
            children_count: 0,
            warnings: [],
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${SECOND_PAGE_ID}`]:
        {
          data: {
            versions: [],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Capped Page" }),
    );

    expect(
      await screen.findByText("Resolved past the cap"),
    ).toBeInTheDocument();
  });

  it("keeps backlink totals and shows an error when neighbors fail", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]: {
        data: {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          body_md: "# Decision Log\n\nBody",
          workspace_id: WORKSPACE_ID,
          version_id: "version-1",
          version_status: "published",
          history_status: "published_snapshot",
          graph_status: "linear",
          is_visible_head: true,
          is_published_head: true,
          is_draft_head: false,
          is_alternative: false,
          is_discarded: false,
          parent_version_id: null,
          content_hash: "sha256:context",
          created_at: "2026-05-09T00:00:00Z",
          tree_path: [],
          parent_node_id: null,
          children_count: 0,
          backlink_count: 7,
          warnings: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`]:
        {
          data: {
            versions: [],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await screen.findByText("Body");
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const inspector = within(
      screen.getByRole("region", { name: "Context detail" }),
    );
    const backlinksTab = inspector.getByRole("tab", { name: /Backlinks/ });
    expect(backlinksTab).toHaveTextContent("7");
    await userEvent.click(backlinksTab);

    expect(inspector.getByRole("alert")).toHaveTextContent(
      "Neighbor details could not be loaded",
    );
    expect(
      inspector.getAllByText("Neighbor list unavailable.").length,
    ).toBeGreaterThan(0);
    expect(
      inspector.queryByText("No visible pages link here yet."),
    ).not.toBeInTheDocument();
  });

  it("renders the recency overview with a Load more control and page-size selector", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=10`]: {
        data: [
          contextPage(PAGE_ID, "Decision Log", "decision-log"),
          contextPage(
            SECOND_PAGE_ID,
            "Architecture Notes",
            "architecture-notes",
          ),
        ],
        next_cursor: "cursor-2",
        total: 7,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=10&cursor=cursor-2`]:
        {
          data: [contextPage("context-3", "Migration Plan", "migration-plan")],
          next_cursor: null,
          total: 7,
          has_more: false,
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
        ]),
      },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    // The badge uses the exact server total, not a misleading "7+", even
    // though more rows remain to load (Load more stays available below).
    expect(await screen.findByText("7 items")).toBeInTheDocument();
    expect(screen.queryByText("7+")).not.toBeInTheDocument();
    // The Load more button is present while the cursor points at older rows.
    expect(
      await screen.findByRole("button", { name: "Load more" }),
    ).toBeInTheDocument();

    // Load more appends the next keyset page and exhausts the cursor.
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("Migration Plan")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Load more" }),
      ).not.toBeInTheDocument();
    });
  });

  it("refreshes the recency overview after deleting a Context page", async () => {
    const user = userEvent.setup();
    let deleted = false;
    let listReads = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes/node-1` &&
        init?.method === "DELETE"
      ) {
        deleted = true;
        return jsonResponse({
          data: treeNode("node-1", PAGE_ID, "Decision Log"),
        });
      }
      if (
        url.startsWith(
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?`,
        )
      ) {
        listReads += 1;
        return jsonResponse({
          data: deleted
            ? []
            : [contextPage(PAGE_ID, "Decision Log", "decision-log")],
          next_cursor: null,
          total: deleted ? 0 : 1,
          has_more: false,
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
      ) {
        return jsonResponse({
          data: treeResponse(
            "context",
            deleted ? [] : [treeNode("node-1", PAGE_ID, "Decision Log")],
          ),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`
      ) {
        return jsonResponse({
          data: {
            ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
            body_md: "# Decision Log",
            workspace_id: WORKSPACE_ID,
            version_id: "version-1",
            version_status: "published",
            history_status: "published_snapshot",
            graph_status: "linear",
            is_visible_head: true,
            is_published_head: true,
            is_draft_head: false,
            is_alternative: false,
            is_discarded: false,
            parent_version_id: null,
            content_hash: "sha256:context",
            created_at: "2026-05-09T00:00:00Z",
            tree_path: [],
            parent_node_id: null,
            children_count: 0,
            warnings: [],
            reconciliation_pending_count: 0,
            reconciliation_pending_count_truncated: false,
            next_reconciliation_version_id: null,
            reconciliation_available: false,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`
      ) {
        return jsonResponse({
          data: {
            versions: [
              historyVersion("version-1", 1, "published_snapshot", {
                isPublishedHead: true,
                isVisibleHead: true,
              }),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors?format=mermaid`
      ) {
        return jsonResponse({
          data: {
            backlinks: [],
            outbound: [],
            citations: [],
            co_access: [],
            mermaid: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    const overview = (await screen.findByText("Recent updates")).closest(
      "section",
    );
    expect(overview).not.toBeNull();
    await user.click(
      within(overview as HTMLElement).getByRole("button", {
        name: /Decision Log/,
      }),
    );
    await screen.findByRole("heading", { name: "Decision Log", level: 2 });
    await user.click(
      screen.getByRole("button", { name: "More page actions" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Delete Decision Log",
    });
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(screen.queryByText("Decision Log")).not.toBeInTheDocument();
    });
    expect(
      screen.queryByRole("heading", { name: "We couldn't find that page" }),
    ).not.toBeInTheDocument();
    expect(listReads).toBeGreaterThanOrEqual(2);
  });

  it("removes a recently deleted Context page when detail returns 404", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.startsWith(
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?`,
        )
      ) {
        return jsonResponse({
          data: [contextPage(PAGE_ID, "Deleted elsewhere", "deleted-elsewhere")],
          next_cursor: null,
          total: 1,
          has_more: false,
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
      ) {
        return jsonResponse({
          data: treeResponse("context", [
            treeNode("node-1", PAGE_ID, "Deleted elsewhere"),
          ]),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`
      ) {
        return jsonResponse(
          {
            error_code: "not_found",
            message: "context page not found",
            request_id: "request-context-deleted-elsewhere",
            warnings: [],
          },
          404,
        );
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`
      ) {
        return jsonResponse({
          data: {
            versions: [],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors?format=mermaid`
      ) {
        return jsonResponse({
          data: {
            backlinks: [],
            outbound: [],
            citations: [],
            co_access: [],
            mermaid: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ContextScreen />
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
        "That Context page is no longer available. It was removed from the list.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Deleted elsewhere")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "We couldn't find that page" }),
    ).not.toBeInTheDocument();
  });

  it("filters the live Context overview by unpublished draft state", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=10`]: {
        data: [
          contextPage(PAGE_ID, "Published Only", "published-only"),
          {
            ...contextPage(
              SECOND_PAGE_ID,
              "Unpublished Work",
              "unpublished-work",
            ),
            current_version_id: "version-2",
            latest_draft_version_id: "version-2",
          },
        ],
        next_cursor: null,
        total: 2,
        has_more: false,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?has_draft=true&limit=10`]:
        {
          data: [
            {
              ...contextPage(
                SECOND_PAGE_ID,
                "Unpublished Work",
                "unpublished-work",
              ),
              current_version_id: "version-2",
              latest_draft_version_id: "version-2",
            },
          ],
          next_cursor: null,
          total: 1,
          has_more: false,
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?has_draft=false&limit=10`]:
        {
          data: [contextPage(PAGE_ID, "Published Only", "published-only")],
          next_cursor: null,
          total: 1,
          has_more: false,
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", []),
      },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Published Only")).toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Draft state" }),
      "with",
    );

    await waitFor(() => {
      expect(screen.queryByText("Published Only")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Unpublished Work")).toBeInTheDocument();
    expect(screen.getByText("1 item")).toBeInTheDocument();

    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Draft state" }),
      "without",
    );
    await waitFor(() => {
      expect(screen.queryByText("Unpublished Work")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Published Only")).toBeInTheDocument();
  });

  it("hides the History restore affordance from viewers (#7 parity)", async () => {
    ctxWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]: {
        data: {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          body_md: "# Decision Log",
          workspace_id: WORKSPACE_ID,
          version_id: "version-1",
          version_status: "published",
          history_status: "published_snapshot",
          graph_status: "linear",
          is_visible_head: true,
          is_published_head: true,
          is_draft_head: false,
          is_alternative: false,
          is_discarded: false,
          parent_version_id: null,
          content_hash: "sha256:context",
          created_at: "2026-05-09T00:00:00Z",
          tree_path: [],
          parent_node_id: null,
          children_count: 0,
          warnings: [],
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`]:
        {
          data: {
            versions: [
              historyVersion("version-1", 2, "published_snapshot", {
                actorType: "user",
                isPublishedHead: true,
                isVisibleHead: true,
              }),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    // The Edit link routes into a RequireEditor-gated editor; viewers must not
    // see it, and the node menu must keep only the read-only Copy link action.
    expect(screen.queryByRole("link", { name: /^Edit$/ })).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "More page actions" }),
    );
    expect(
      screen.getByRole("menuitem", { name: "Copy link" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Rename" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Move" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Delete" })).toBeNull();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(readerTabs.getByRole("tab", { name: /History/ }));
    const viewerHistory = screen.getByRole("tabpanel", { name: /History/ });
    expect(
      within(viewerHistory).getByText("Current published snapshot"),
    ).toBeInTheDocument();
    expect(within(viewerHistory).queryByText("Current draft")).toBeNull();
    expect(within(viewerHistory).queryByText("Alternative draft")).toBeNull();
    // restore is a write; viewers must not see it (backend re-enforces anyway).
    expect(
      screen.queryByRole("button", { name: "Restore this version" }),
    ).toBeNull();
  });

  it("hides the mobile New FAB from viewers", async () => {
    setMobileViewport(true);
    ctxWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", []),
      },
    });

    const viewerView = render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "No Context pages yet" }),
    ).toBeInTheDocument();
    // A viewer must not get the "New" FAB that opens the create dialog.
    expect(screen.queryByLabelText("Mobile workspace actions")).toBeNull();
    // ...nor the header's create link.
    expect(screen.queryByRole("link", { name: "New Context page" })).toBeNull();
    viewerView.unmount();

    ctxWorkspace.current = { selectedWorkspace: { role: "owner" } };
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "No Context pages yet" }),
    ).toBeInTheDocument();
    const mobileActions = within(
      screen.getByLabelText("Mobile workspace actions"),
    );
    expect(
      mobileActions.getByRole("button", { name: "Create Context item" }),
    ).toBeInTheDocument();
  });

  it("shows the setup sequence and writing CTA when Context is empty", async () => {
    const folderId = "context-folder-1";
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", []),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes`]: {
        data: {
          id: folderId,
          section: "context",
          title: "Research",
          slug: "research",
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
        },
      },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "No Context pages yet" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Workspace setup sequence")).toHaveTextContent(
      "Write Context page",
    );
    expect(
      screen.getByRole("link", { name: "Write first Context page" }),
    ).toHaveAttribute("href", "/context/new");

    await userEvent.click(
      screen.getByRole("button", { name: "Create Context item" }),
    );
    await userEvent.click(await screen.findByRole("radio", { name: "Folder" }));
    await userEvent.type(
      await screen.findByRole("textbox", { name: "Folder name" }),
      "Research",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Create folder" }),
    );

    expect(
      await screen.findByRole("treeitem", { name: "Research" }),
    ).toBeInTheDocument();
  });

  it("marks manual tree refresh busy and announces completion", async () => {
    const treeRefresh = deferredResponse();
    let treeCallCount = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.split("?")[0] ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`
      ) {
        return jsonResponse({
          data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
      ) {
        treeCallCount += 1;
        if (treeCallCount === 1) {
          return jsonResponse({
            data: treeResponse("context", [
              treeNode("node-1", PAGE_ID, "Decision Log"),
            ]),
          });
        }
        return treeRefresh.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Context" });
    // Refresh is a direct rail-header button now (no "⋯" overflow menu).
    await userEvent.click(
      screen.getByRole("button", { name: "Refresh Context tree" }),
    );

    const refreshingButton = screen.getByRole("button", {
      name: "Refreshing Context tree…",
    });
    expect(refreshingButton).toHaveAttribute("aria-busy", "true");
    expect(refreshingButton).toBeDisabled();

    treeRefresh.resolve(
      jsonResponse({
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
        ]),
      }),
    );

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Context tree refreshed",
      );
    });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Refresh Context tree" }),
      ).toBeEnabled();
    });
  });

  it("preserves a manually collapsed active path across tree refresh", async () => {
    mockDecisionLogRoutes({ nestedSecondPage: true });
    render(
      <MemoryRouter initialEntries={[`/context?target=${SECOND_PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Selected second page")).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Collapse Inbox" }),
    );
    expect(
      screen.queryByRole("treeitem", { name: "Architecture Notes" }),
    ).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Refresh Context tree" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Expand Inbox" }),
      ).toBeEnabled(),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("treeitem", { name: "Architecture Notes" }),
      ).not.toBeInTheDocument();
    });
  });

  it("compares and restores Context history versions in a diff modal", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/workspaces`) {
          return jsonResponse({
            data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
          });
        }
        if (
          url.split("?")[0] ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`
        ) {
          return jsonResponse({
            data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
          });
        }
        if (
          url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
        ) {
          return jsonResponse({
            data: treeResponse("context", [
              treeNode("node-1", PAGE_ID, "Decision Log"),
            ]),
          });
        }
        if (
          url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`
        ) {
          return jsonResponse({
            data: {
              ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
              current_version_id: "version-2",
              current_published_version_id: "version-2",
              body_md: "# Decision Log\n\nCurrent body",
              workspace_id: WORKSPACE_ID,
              version_id: "version-2",
              version_status: "published",
              history_status: "published_snapshot",
              graph_status: "linear",
              is_visible_head: true,
              is_published_head: true,
              is_draft_head: false,
              is_alternative: false,
              is_discarded: false,
              parent_version_id: "version-1",
              content_hash: "sha256:context",
              created_at: "2026-05-09T00:00:00Z",
              tree_path: [],
              parent_node_id: null,
              children_count: 0,
              warnings: [],
            },
          });
        }
        if (
          url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`
        ) {
          return jsonResponse({
            data: {
              versions: [
                historyVersion("version-2", 2, "published_snapshot", {
                  isPublishedHead: true,
                  isVisibleHead: true,
                }),
                historyVersion("version-1", 1, "superseded_snapshot"),
              ],
              changesets: [],
              review_decisions: [],
              diffs: null,
            },
          });
        }
        if (
          url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-1&to_version_id=version-2`
        ) {
          return jsonResponse({
            data: {
              from_version_id: "version-1",
              to_version_id: "version-2",
              from_version_no: 1,
              to_version_no: 2,
              lines: [
                "--- v1",
                "+++ v2",
                "@@ -1 +1 @@",
                "-Old body",
                "+Current body",
              ],
              additions: 1,
              deletions: 1,
            },
          });
        }
        if (
          url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/restore` &&
          init?.method === "POST"
        ) {
          expect(JSON.parse(String(init.body))).toEqual({
            version_id: "version-1",
          });
          return jsonResponse({
            data: {
              ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
              body_md: "# Decision Log\n\nOld body",
              version_id: "restored-version-3",
              parent_version_id: "version-2",
              created_at: "2026-05-09T01:00:00Z",
              content_hash: "sha256:restored",
            },
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await userEvent.click(screen.getByRole("tab", { name: /History/ }));

    const historyPanel = screen.getByRole("tabpanel", { name: /History/ });
    const versionOne = within(historyPanel)
      .getByText("v1")
      .closest(".history-card") as HTMLElement;
    expect(versionOne.tagName).toBe("LI");

    // The visible head offers its own Changes but never Restore; the explicit
    // two-version toolbar opens the newest version's Changes first.
    const versionTwo = within(historyPanel)
      .getByText("v2")
      .closest(".history-card") as HTMLElement;
    expect(versionTwo).toHaveAttribute("aria-current", "page");
    expect(
      within(versionTwo).getByRole("button", {
        name: "Show changes in v2 since v1",
      }),
    ).toHaveTextContent("Changes");
    expect(
      within(versionTwo).queryByRole("button", {
        name: "Restore this version",
      }),
    ).toBeNull();
    expect(
      within(versionOne).getByRole("button", { name: "Restore this version" }),
    ).toBeInTheDocument();

    await userEvent.click(
      within(historyPanel).getByRole("button", {
        name: "Compare two versions",
      }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v2 since v1",
    });
    const split = within(dialog).getByLabelText("Side-by-side diff");
    expect(
      within(split).getByRole("columnheader", {
        name: /Base version \(earlier\)/,
      }),
    ).not.toHaveAttribute("data-current-published");
    expect(
      within(split).getByRole("columnheader", {
        name: /Compared version \(later\)/,
      }),
    ).toHaveAttribute("data-current-published", "true");
    expect(within(split).getByText("Current body")).toHaveClass(
      "diff-viewer-cell-add",
    );
    expect(
      within(dialog).getByText("Current published v2 · visible to readers"),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "Restore this version" }),
    ).toBeNull();

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Close diff" }),
    );
    await userEvent.click(
      within(versionOne).getByRole("button", { name: "Restore this version" }),
    );

    expect(confirmSpy).toHaveBeenCalledWith(
      "Restore v1 as a new draft? The published version stays unchanged.",
    );
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/restore`,
      expect.objectContaining({ method: "POST" }),
    );

    await waitFor(() => {
      const restoreCallIndex = fetchMock.mock.calls.findIndex(
        (call) => String(call[0]) === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/restore`,
      );
      const subsequent = fetchMock.mock.calls
        .slice(restoreCallIndex + 1)
        .map((call) => String(call[0]));
      expect(subsequent).toEqual(
        expect.arrayContaining([
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?limit=10`,
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`,
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`,
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`,
        ]),
      );
    });
  });

  it("aborts and discards a stale Context diff after another pair is selected", async () => {
    const firstDiff = deferredResponse();
    const secondDiff = deferredResponse();
    const firstRequest: { signal: AbortSignal | null } = { signal: null };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.split("?")[0] ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`
      ) {
        return jsonResponse({
          data: [
            {
              ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
              current_version_id: "version-3",
              current_published_version_id: "version-3",
            },
          ],
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
      ) {
        return jsonResponse({
          data: treeResponse("context", [
            treeNode("node-1", PAGE_ID, "Decision Log"),
          ]),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`
      ) {
        return jsonResponse({
          data: {
            ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
            current_version_id: "version-3",
            current_published_version_id: "version-3",
            body_md: "# Decision Log\n\nCurrent body",
            workspace_id: WORKSPACE_ID,
            version_id: "version-3",
            version_status: "published",
            history_status: "published_snapshot",
            graph_status: "linear",
            is_visible_head: true,
            is_published_head: true,
            is_draft_head: false,
            is_alternative: false,
            is_discarded: false,
            parent_version_id: "version-2",
            content_hash: "sha256:context",
            created_at: "2026-05-09T00:00:00Z",
            tree_path: [],
            parent_node_id: null,
            children_count: 0,
            warnings: [],
          },
        });
      }
      if (
        url.split("?")[0] ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors`
      ) {
        return jsonResponse({
          data: {
            backlinks: [],
            outbound: [],
            citations: [],
            co_access: [],
            mermaid: null,
          },
        });
      }
      if (
        url.endsWith(
          `/changesets?target_type=context_page&target_id=${PAGE_ID}`,
        )
      ) {
        return jsonResponse({
          data: {
            versions: [
              historyVersion("version-3", 3, "published_snapshot", {
                isPublishedHead: true,
                isVisibleHead: true,
              }),
              historyVersion("version-2", 2, "superseded_snapshot"),
              historyVersion("version-1", 1, "superseded_snapshot"),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      if (url.includes("from_version_id=version-2")) {
        firstRequest.signal = init?.signal ?? null;
        return firstDiff.promise;
      }
      if (url.includes("from_version_id=version-1")) {
        return secondDiff.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const v2 = within(panel)
      .getByText("v2")
      .closest(".history-card") as HTMLElement;
    await userEvent.click(
      within(panel).getByRole("button", { name: "Compare two versions" }),
    );
    await userEvent.click(
      within(v2).getByRole("button", { name: "Show changes in v2 since v1" }),
    );
    if (!firstRequest.signal)
      throw new Error("First diff request did not carry a signal");
    expect(firstRequest.signal.aborted).toBe(true);

    secondDiff.resolve(
      jsonResponse({
        data: {
          from_version_id: "version-1",
          to_version_id: "version-2",
          from_version_no: 1,
          to_version_no: 2,
          lines: [
            "--- v1",
            "+++ v2",
            "@@ -1 +1 @@",
            "-Second selection",
            "+Current",
          ],
          additions: 1,
          deletions: 1,
        },
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v2 since v1",
    });
    expect(dialog).toHaveTextContent("Second selection");

    firstDiff.resolve(
      jsonResponse({
        data: contextVersionDiff("version-2", 2, "Stale first response"),
      }),
    );
    await waitFor(() => {
      expect(dialog).not.toHaveTextContent("Stale first response");
    });
  });

  it("re-enables Compare when the diff dialog is closed before the diff lands", async () => {
    // Closing invalidates the in-flight request, which makes its `finally` skip
    // the loading reset -- that card's Compare used to stay disabled and stuck
    // on "Loading diff" for the rest of the session.
    const pendingDiff = deferredResponse();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.split("?")[0] ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`
      ) {
        return jsonResponse({
          data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
      ) {
        return jsonResponse({
          data: treeResponse("context", [
            treeNode("node-1", PAGE_ID, "Decision Log"),
          ]),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`
      ) {
        return jsonResponse({
          data: {
            ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
            body_md: "# Decision Log\n\nCurrent body",
            workspace_id: WORKSPACE_ID,
            version_id: "version-2",
            version_status: "published",
            history_status: "published_snapshot",
            graph_status: "linear",
            is_visible_head: true,
            is_published_head: true,
            is_draft_head: false,
            is_alternative: false,
            is_discarded: false,
            parent_version_id: "version-1",
            content_hash: "sha256:context",
            created_at: "2026-05-09T00:00:00Z",
            tree_path: [],
            parent_node_id: null,
            children_count: 0,
            warnings: [],
          },
        });
      }
      if (
        url.split("?")[0] ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors`
      ) {
        return jsonResponse({
          data: {
            backlinks: [],
            outbound: [],
            citations: [],
            co_access: [],
            mermaid: null,
          },
        });
      }
      if (
        url.endsWith(
          `/changesets?target_type=context_page&target_id=${PAGE_ID}`,
        )
      ) {
        return jsonResponse({
          data: {
            versions: [
              historyVersion("version-2", 2, "published_snapshot", {
                isPublishedHead: true,
                isVisibleHead: true,
              }),
              historyVersion("version-1", 1, "superseded_snapshot"),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      if (url.includes("from_version_id=version-1")) {
        return pendingDiff.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const compareTwo = within(panel).getByRole("button", {
      name: "Compare two versions",
    });

    await userEvent.click(compareTwo);
    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v2 since v1",
    });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Close diff" }),
    );

    pendingDiff.resolve(
      jsonResponse({
        data: contextVersionDiff("version-1", 1, "Late body"),
      }),
    );

    await waitFor(() => {
      expect(compareTwo).toBeEnabled();
    });
  });

  it("renames a context tree node from the action menu", async () => {
    let folderTitle = "Research";
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/workspaces`) {
          return jsonResponse({
            data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
          });
        }
        if (
          url.split("?")[0] ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`
        ) {
          return jsonResponse({ data: [] });
        }
        if (
          url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
        ) {
          return jsonResponse({
            data: treeResponse("context", [
              folderNode("context-root", "Context", {
                children_count: 1,
                is_system_node: true,
                slug: "context",
              }),
              folderNode("folder-1", folderTitle, {
                breadcrumb: [
                  {
                    id: "context-root",
                    section: "context",
                    title: "Context",
                    slug: "context",
                    node_kind: "folder",
                  },
                ],
                parent_id: "context-root",
              }),
            ]),
          });
        }
        if (
          url ===
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes/folder-1` &&
          init?.method === "PATCH"
        ) {
          folderTitle = JSON.parse(String(init.body)).display_name;
          return jsonResponse({ data: folderNode("folder-1", folderTitle) });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    const folderRow = await screen.findByRole("treeitem", { name: "Research" });
    expect(
      screen.getByRole("heading", { name: /System-managed Context/ }),
    ).toBeInTheDocument();
    fireEvent.contextMenu(folderRow, { clientX: 40, clientY: 40 });
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const displayName = await screen.findByRole("textbox", {
      name: "Display name",
    });
    await userEvent.clear(displayName);
    await userEvent.type(displayName, "Evidence");
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes/folder-1`,
      expect.objectContaining({ method: "PATCH" }),
    );
    expect(
      await screen.findByRole("treeitem", { name: "Evidence" }),
    ).toBeInTheDocument();
  });

  // C8: two root-level sibling pages plus the routes an anchored reorder
  // touches. The reorder POST swaps the served sort keys so the refetched
  // tree proves the screen rendered the server's new order, not a local guess.
  function anchoredReorderFetch() {
    let reordered = false;
    const reorderBodies: unknown[] = [];
    const nodes = () => [
      {
        ...treeNode("node-1", PAGE_ID, "Decision Log"),
        sort_key: reordered ? "0002" : "0001",
      },
      {
        ...treeNode("node-2", SECOND_PAGE_ID, "Architecture Notes"),
        sort_key: reordered ? "0001" : "0002",
      },
    ];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.split("?")[0] ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`
      ) {
        return jsonResponse({
          data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
      ) {
        return jsonResponse({ data: treeResponse("context", nodes()) });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/reorder` &&
        init?.method === "POST"
      ) {
        reorderBodies.push(JSON.parse(String(init.body)));
        reordered = true;
        return jsonResponse({ data: nodes()[0] });
      }
      const pageDetail = [
        { id: PAGE_ID, title: "Decision Log", slug: "decision-log" },
        {
          id: SECOND_PAGE_ID,
          title: "Architecture Notes",
          slug: "architecture-notes",
        },
      ].find(
        (candidate) =>
          url ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${candidate.id}`,
      );
      if (pageDetail) {
        return jsonResponse({
          data: {
            ...contextPage(pageDetail.id, pageDetail.title, pageDetail.slug),
            body_md: `# ${pageDetail.title}\n\nBody`,
            workspace_id: WORKSPACE_ID,
            version_id: "version-1",
            version_status: "published",
            graph_status: "linear",
            parent_version_id: null,
            content_hash: "sha256:context",
            created_at: "2026-05-09T00:00:00Z",
            tree_path: [],
            parent_node_id: null,
            children_count: 0,
            warnings: [],
          },
        });
      }
      if (url.includes("/changesets?target_type=context_page&target_id=")) {
        return jsonResponse({
          data: {
            versions: [],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      if (url.endsWith("/neighbors")) {
        return jsonResponse({
          data: { backlinks: [], outbound: [], citations: [], co_access: [] },
        });
      }
      if (url.includes("/activity")) {
        return jsonResponse({
          data: { items: [], unread_count: 0, next_cursor: null },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    return { reorderBodies };
  }

  it("reorders a tree node by dropping it between siblings (C8)", async () => {
    const { reorderBodies } = anchoredReorderFetch();

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    const movingRow = await screen.findByRole("treeitem", {
      name: "Decision Log",
    });
    const anchorRow = screen.getByRole("treeitem", {
      name: "Architecture Notes",
    });
    vi.spyOn(anchorRow, "getBoundingClientRect").mockReturnValue(
      rowBounds({ top: 100, height: 40 }),
    );

    const dataTransfer = createDataTransfer();
    fireEvent.dragStart(movingRow, { dataTransfer });
    fireTreeDragEvent(anchorRow, "dragover", { clientY: 130, dataTransfer });
    expect(anchorRow).toHaveAttribute("data-drop-placement", "after");
    fireTreeDragEvent(anchorRow, "drop", { clientY: 130, dataTransfer });

    await waitFor(() =>
      expect(reorderBodies).toEqual([
        { node_id: "node-1", anchor_node_id: "node-2", placement: "after" },
      ]),
    );
    // The screen refetched the tree and rendered the server's new order.
    await waitFor(() => {
      const nodeIds = screen
        .getAllByRole("treeitem")
        .map((row) => row.getAttribute("data-node-id"));
      expect(nodeIds).toEqual(["node-2", "node-1"]);
    });
  });

  it("reorders a tree node from the row menu's anchored Move up (C8)", async () => {
    const { reorderBodies } = anchoredReorderFetch();

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    const row = await screen.findByRole("treeitem", {
      name: "Architecture Notes",
    });
    fireEvent.contextMenu(row, { clientX: 40, clientY: 40 });
    await userEvent.click(screen.getByRole("menuitem", { name: "Move up" }));

    await waitFor(() =>
      expect(reorderBodies).toEqual([
        { node_id: "node-2", anchor_node_id: "node-1", placement: "before" },
      ]),
    );
    await waitFor(() => {
      const nodeIds = screen
        .getAllByRole("treeitem")
        .map((el) => el.getAttribute("data-node-id"));
      expect(nodeIds).toEqual(["node-2", "node-1"]);
    });
  });

  it("surfaces a failed reorder in the Context error banner", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url.split("?")[0] ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`
      ) {
        return jsonResponse({
          data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
      ) {
        return jsonResponse({
          data: treeResponse("context", [
            treeNode("node-1", PAGE_ID, "Decision Log"),
            {
              ...treeNode("node-2", SECOND_PAGE_ID, "Architecture Notes"),
              sort_key: "0002",
            },
          ]),
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/reorder` &&
        init?.method === "POST"
      ) {
        throw new Error("Sibling order changed underneath this move");
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    const row = await screen.findByRole("treeitem", {
      name: "Architecture Notes",
    });
    fireEvent.contextMenu(row, { clientX: 40, clientY: 40 });
    await userEvent.click(screen.getByRole("menuitem", { name: "Move up" }));

    // The failure must reach the visible banner, not only TreeRail's
    // screen-reader live region, without exposing generic exception internals.
    expect(
      await screen.findByText(
        "Could not reorder the Context item. Try again.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Sibling order changed underneath this move"),
    ).toBeNull();
  });

  it("splits the folder card count into pages and folders (M-C)", async () => {
    // The folder "Workspace" holds two page children and one sub-folder; the
    // card must read "2 pages · 1 folder" rather than the ambiguous
    // "3 children", and the page_type must render humanized (not raw snake_case).
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          folderNode("folder-root", "Workspace", { children_count: 3 }),
          {
            ...treeNode("node-1", PAGE_ID, "Decision Log"),
            parent_id: "folder-root",
          },
          {
            ...treeNode("node-2", SECOND_PAGE_ID, "Architecture Notes"),
            parent_id: "folder-root",
            target_meta: {
              page_type: "overview",
              lifecycle_status: "published",
            },
          },
          folderNode("folder-child", "Archive", { parent_id: "folder-root" }),
        ]),
      },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Workspace" }),
    );

    expect(await screen.findByText("2 pages · 1 folder")).toBeInTheDocument();
    expect(screen.queryByText("3 children")).toBeNull();
    expect(document.querySelector(".folder-summary-footer")).toBeNull();
    // The child page row humanizes page_type ("decision_log" -> "Decision log").
    expect(screen.getByText(/Decision log · published/)).toBeInTheDocument();
    // This page is outside the paginated context/pages response; tree target
    // metadata must still keep its folder-card row complete.
    expect(screen.getByText(/Overview · published/)).toBeInTheDocument();
  });

  it("keeps folder pagination inside the detail section while preserving total counts", async () => {
    const pageChildren = Array.from({ length: 6 }, (_, index) => ({
      ...treeNode(
        `folder-page-${index + 1}`,
        `folder-target-${index + 1}`,
        `Folder page ${index + 1}`,
      ),
      parent_id: "folder-root",
      sort_key: String(index + 1).padStart(4, "0"),
    }));
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          folderNode("folder-root", "Workspace", { children_count: 7 }),
          ...pageChildren,
          folderNode("folder-child", "Archive", {
            parent_id: "folder-root",
            sort_key: "0007",
          }),
        ]),
      },
    });
    const searches: string[] = [];
    render(
      <MemoryRouter
        initialEntries={["/context?target=folder-root&childrenPage=2"]}
      >
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextScreen />
      </MemoryRouter>,
    );

    const heading = await screen.findByRole("heading", { name: "Workspace" });
    const section = heading.closest("section.detail-section") as HTMLElement;
    const pagination = within(section).getByRole("navigation", {
      name: "Children pagination",
    });
    expect(pagination.closest(".folder-summary-footer")).toBeInTheDocument();
    expect(pagination.closest(".detail-section")).toBe(section);
    expect(within(section).getByText(/6 pages.*1 folder/)).toBeInTheDocument();
    expect(
      within(section).getByRole("button", { name: /Folder page 6/ }),
    ).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: /Archive/ })).toBeInTheDocument();
    expect(searches.at(-1)).toBe("?target=folder-root&childrenPage=2");
  });

  it("pluralizes each count independently: one page, two folders (M-C)", async () => {
    // A folder with a single page and two sub-folders must read
    // "1 page · 2 folders" — each count pluralized on its own. The previous
    // single-template string produced the wrong "1 pages · 2 folder".
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          folderNode("folder-root", "Workspace", { children_count: 3 }),
          {
            ...treeNode("node-1", PAGE_ID, "Decision Log"),
            parent_id: "folder-root",
          },
          folderNode("folder-a", "Archive", { parent_id: "folder-root" }),
          folderNode("folder-b", "Backups", { parent_id: "folder-root" }),
        ]),
      },
    });

    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Workspace" }),
    );

    expect(await screen.findByText("1 page · 2 folders")).toBeInTheDocument();
    expect(screen.queryByText("1 pages · 2 folder")).toBeNull();
  });

  function pendingReviewFetch(
    supersededReviewCount = 0,
    reviewStatus = "pending",
    reconciliationPendingCount = 0,
  ) {
    return {
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]: {
        data: {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          current_version_id: "version-1",
          current_published_version_id: "version-1",
          latest_draft_version_id: "version-2",
          body_md: "# Decision Log\n\nBody",
          workspace_id: WORKSPACE_ID,
          version_id: "version-2",
          version_status: "draft",
          graph_status: "linear",
          parent_version_id: "version-1",
          content_hash: "sha256:draft",
          created_at: "2026-05-09T01:00:00Z",
          tree_path: [],
          parent_node_id: null,
          children_count: 0,
          warnings: [],
          pending_draft_version_id: "version-2",
          pending_draft_warnings: [
            "Heading level jumps from H1 to H3 at line 5.",
          ],
          reconciliation_pending_count: reconciliationPendingCount,
          reconciliation_pending_count_truncated: false,
          next_reconciliation_version_id:
            reconciliationPendingCount > 0 ? "version-alt" : null,
          reconciliation_available: reconciliationPendingCount > 0,
          pending_review: {
            review_item_id: "rev-1",
            status: reviewStatus,
            risk_level: "medium",
          },
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`]:
        {
          data: {
            versions: [
              // The pending draft is what readers of this page currently see;
              // the published version keeps the current pointer but is no
              // longer the visible head.
              historyVersion("version-2", 2, "draft", undefined, {
                is_latest_draft: true,
                is_visible_head: true,
              }),
              historyVersion("version-1", 1, "published", undefined, {
                is_current: true,
                is_current_published: true,
              }),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-1&to_version_id=version-2`]:
        {
          data: {
            from_version_id: "version-1",
            to_version_id: "version-2",
            from_version_no: 1,
            to_version_no: 2,
            lines: [
              "--- v1",
              "+++ v2",
              "@@ -1 +1 @@",
              "-Published body",
              "+Newest draft body",
            ],
            additions: 1,
            deletions: 1,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/rev-1`]: {
        data: {
          id: "rev-1",
          status: reviewStatus,
          superseded_review_count: supersededReviewCount,
        },
      },
    };
  }

  function comparisonHistoryFetch(
    crossBranch = false,
  ): Record<string, unknown> {
    const responses: Record<string, unknown> = pendingReviewFetch();
    responses[`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`] = {
      data: [
        {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          current_version_id: "version-3",
          current_published_version_id: "version-3",
        },
      ],
    };
    responses[
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`
    ] = {
      data: {
        ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
        current_version_id: "version-3",
        current_published_version_id: "version-3",
        body_md: "# Decision Log\n\nThird body",
        workspace_id: WORKSPACE_ID,
        version_id: "version-3",
        version_status: "published",
        graph_status: crossBranch ? "alternative" : "linear",
        parent_version_id: crossBranch ? "version-1" : "version-2",
        content_hash: "sha256:v3",
        created_at: "2026-05-09T02:00:00Z",
        tree_path: [],
        parent_node_id: null,
        children_count: 0,
        backlink_count: 0,
        warnings: [],
        pending_draft_version_id: null,
        pending_draft_warnings: [],
        pending_review: null,
      },
    };
    responses[
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`
    ] = {
      data: {
        versions: [
          historyVersion(
            "version-3",
            3,
            "published",
            crossBranch ? "version-1" : "version-2",
            {
              is_current: true,
              is_current_published: true,
              is_visible_head: true,
            },
          ),
          historyVersion("version-2", 2, "superseded", "version-1"),
          historyVersion("version-1", 1, "superseded", null),
        ],
        changesets: [],
        review_decisions: [],
        diffs: null,
      },
    };
    responses[
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-1&to_version_id=version-2`
    ] = {
      data: {
        from_version_id: "version-1",
        to_version_id: "version-2",
        from_version_no: 1,
        to_version_no: 2,
        lines: [
          "--- v1",
          "+++ v2",
          "@@ -1 +1 @@",
          "-First body",
          "+Second body",
        ],
        additions: 1,
        deletions: 1,
      },
    };
    responses[
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-2&to_version_id=version-3`
    ] = {
      data: {
        from_version_id: "version-2",
        to_version_id: "version-3",
        from_version_no: 2,
        to_version_no: 3,
        lines: [
          "--- v2",
          "+++ v3",
          "@@ -1 +1 @@",
          "-Second body",
          "+Third body",
        ],
        additions: 1,
        deletions: 1,
      },
    };
    responses[
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-1&to_version_id=version-3`
    ] = {
      data: {
        from_version_id: "version-1",
        to_version_id: "version-3",
        from_version_no: 1,
        to_version_no: 3,
        lines: [
          "--- v1",
          "+++ v3",
          "@@ -1 +1 @@",
          "-First body",
          "+Third body",
        ],
        additions: 1,
        deletions: 1,
      },
    };
    return responses;
  }

  it("opens the pending draft comparison with the newer draft on the right in one click", async () => {
    mockFetch(pendingReviewFetch());
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "View changes" }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v2 since v1",
    });
    expect(screen.getByRole("tab", { name: /History/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const split = within(dialog).getByRole("table", {
      name: "Side-by-side diff",
    });
    expect(within(split).getByText("Published body")).toHaveClass(
      "diff-viewer-cell-del",
    );
    expect(within(split).getByText("Newest draft body")).toHaveClass(
      "diff-viewer-cell-add",
    );
    expect(
      within(split).getByRole("columnheader", {
        name: /Base version \(earlier\)/,
      }),
    ).toHaveAttribute("data-current-published", "true");
    expect(
      within(split).getByRole("columnheader", {
        name: /Compared version \(later\)/,
      }),
    ).not.toHaveAttribute("data-current-published");
  });

  it("offers Restore on neither the pending draft nor the current pointer", async () => {
    // Published v1 keeps the current pointer while pending draft v2 is the
    // visible head. Restoring v2 would fork a duplicate version and leave its
    // pending review targeting a non-head version, so the gate must read the
    // publication metadata, not just `is_current`.
    mockFetch(pendingReviewFetch());
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const draftCard = within(panel)
      .getByText("v2")
      .closest(".history-card") as HTMLElement;
    const publishedCard = within(panel)
      .getByText("v1")
      .closest(".history-card") as HTMLElement;

    expect(
      within(draftCard).getByRole("button", { name: "Show changes in v2 since v1" }),
    ).toBeInTheDocument();
    expect(
      within(draftCard).queryByRole("button", { name: "Restore this version" }),
    ).toBeNull();
    expect(
      within(publishedCard).queryByRole("button", {
        name: "Restore this version",
      }),
    ).toBeNull();
  });

  function emptyBaselineHistoryFetch(): Record<string, unknown> {
    return {
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`]: {
        data: [contextPage(PAGE_ID, "Decision Log", "decision-log")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`]: {
        data: treeResponse("context", [
          treeNode("node-1", PAGE_ID, "Decision Log"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]: {
        data: {
          ...contextPage(PAGE_ID, "Decision Log", "decision-log"),
          body_md: "# Decision Log\n\nFirst body",
          workspace_id: WORKSPACE_ID,
          version_id: "version-1",
          version_status: "published",
          graph_status: "linear",
          parent_version_id: null,
          content_hash: "sha256:first",
          created_at: "2026-05-09T00:00:00Z",
          tree_path: [],
          parent_node_id: null,
          children_count: 0,
          backlink_count: 0,
          warnings: [],
          pending_draft_version_id: null,
          pending_draft_warnings: [],
          pending_review: null,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`]:
        {
          data: {
            versions: [
              historyVersion("version-1", 1, "published", null, {
                is_current: true,
                is_current_published: true,
                is_visible_head: true,
              }),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/activity?period=all&limit=50`]:
        {
          data: { items: [], unread_count: 0, next_cursor: null },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors`]:
        {
          data: { backlinks: [], outbound: [], citations: [], co_access: [] },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?baseline=empty&to_version_id=version-1`]:
        {
          data: {
            baseline: "empty",
            from_version_id: null,
            to_version_id: "version-1",
            from_version_no: 0,
            to_version_no: 1,
            lines: [
              "--- empty",
              "+++ v1",
              "@@ -0,0 +1,3 @@",
              "+# Decision Log",
              "+",
              "+First body",
            ],
            additions: 3,
            deletions: 0,
            metadata_changes: [],
          },
        },
    };
  }

  it("removes Compare from v1 because it has no previous version", async () => {
    const fetchMock = mockFetch(emptyBaselineHistoryFetch());
    const searches: string[] = [];
    render(
      <MemoryRouter>
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await userEvent.click(screen.getByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const firstVersion = within(panel)
      .getByText("v1")
      .closest(".history-card") as HTMLElement;
    expect(
      within(firstVersion).queryByRole("button", { name: /Show changes/ }),
    ).toBeNull();
    expect(within(firstVersion).getByText("First version")).toBeInTheDocument();
    expect(
      within(panel).getByRole("button", { name: "Compare two versions" }),
    ).toBeDisabled();
    expect(searches.at(-1)).not.toContain("compareFrom");
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/diff?")),
    ).toBe(false);
  });

  it("restores an empty-baseline comparison from a deep link", async () => {
    mockFetch(emptyBaselineHistoryFetch());
    render(
      <MemoryRouter
        initialEntries={[
          `/context?target=${PAGE_ID}&tab=history&compareFrom=empty&compareTo=version-1`,
        ]}
      >
        <ContextScreen />
      </MemoryRouter>,
    );

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("v0 -> v1")).toBeInTheDocument();
    expect(within(dialog).getByText("First body")).toHaveClass(
      "diff-viewer-cell-add",
    );
    expect(
      within(dialog).getByRole("option", { name: "Empty baseline" }),
    ).toBeEnabled();
  });

  it("normalizes a reversed comparison deep link with one history replace", async () => {
    mockFetch(pendingReviewFetch());
    const searches: string[] = [];
    const reversed = `?target=${PAGE_ID}&tab=history&compareFrom=version-2&compareTo=version-1`;
    const normalized = `?target=${PAGE_ID}&tab=history&compareFrom=version-1&compareTo=version-2`;
    render(
      <MemoryRouter initialEntries={[`/context${reversed}`]}>
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextScreen />
      </MemoryRouter>,
    );

    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v2 since v1",
    });
    expect(await within(dialog).findByText("Newest draft body")).toHaveClass(
      "diff-viewer-cell-add",
    );
    await waitFor(() => expect(searches.at(-1)).toBe(normalized));
    expect(searches).toEqual([reversed, normalized]);
  });

  it("closes a comparison when Back removes only its URL parameters", async () => {
    mockFetch(comparisonHistoryFetch());
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}&tab=history`]}>
        <BackButton />
        <ContextScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("tabpanel", { name: /History/ });
    await userEvent.click(
      screen.getByRole("button", { name: "Compare two versions" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Changes in v3 since v2" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Go back" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("tab", { name: /History/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("uses native selectors that reject identical endpoints and normalize either choice order", async () => {
    const fetchMock = mockFetch(comparisonHistoryFetch());
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    await userEvent.click(
      screen.getByRole("button", { name: "Compare two versions" }),
    );

    let dialog = await screen.findByRole("dialog", {
      name: "Changes in v3 since v2",
    });
    const earlier = within(dialog).getByRole("combobox", {
      name: "Earlier version",
    });
    const later = within(dialog).getByRole("combobox", {
      name: "Later version",
    });
    expect(earlier.tagName).toBe("SELECT");
    expect(later.tagName).toBe("SELECT");
    expect(earlier).toHaveValue("version-2");
    expect(later).toHaveValue("version-3");
    expect(
      within(earlier).getByRole("option", {
        name: "v3 — unavailable: already selected as the later version",
      }),
    ).toBeDisabled();
    expect(
      within(later).getByRole("option", {
        name: "v2 — unavailable: already selected as the earlier version",
      }),
    ).toBeDisabled();
    expect(dialog).toHaveTextContent(
      "A version cannot be compared with itself. The pair is ordered oldest-first automatically.",
    );

    // Choosing an older value in the "later" control still becomes the left
    // endpoint. The request, title, selectors, and rendered body all agree.
    await userEvent.selectOptions(later, "version-1");
    dialog = await screen.findByRole("dialog", { name: "Changes in v2 since v1" });
    expect(
      within(dialog).getByRole("combobox", { name: "Earlier version" }),
    ).toHaveValue("version-1");
    expect(
      within(dialog).getByRole("combobox", { name: "Later version" }),
    ).toHaveValue("version-2");
    expect(await within(dialog).findByText("Second body")).toHaveClass(
      "diff-viewer-cell-add",
    );

    // Choosing a newer value in the "earlier" control likewise becomes the
    // right endpoint after normalization.
    await userEvent.selectOptions(
      within(dialog).getByRole("combobox", { name: "Earlier version" }),
      "version-3",
    );
    dialog = await screen.findByRole("dialog", { name: "Changes in v3 since v2" });
    expect(
      within(dialog).getByRole("combobox", { name: "Earlier version" }),
    ).toHaveValue("version-2");
    expect(
      within(dialog).getByRole("combobox", { name: "Later version" }),
    ).toHaveValue("version-3");
    expect(await within(dialog).findByText("Third body")).toHaveClass(
      "diff-viewer-cell-add",
    );

    const diffRequests = fetchMock.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.includes(`/context/pages/${PAGE_ID}/diff?`));
    expect(diffRequests).toEqual([
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-2&to_version_id=version-3`,
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-1&to_version_id=version-2`,
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-2&to_version_id=version-3`,
    ]);
  });

  it("keeps a row comparison and the selector on the same pair, then restores focus", async () => {
    mockFetch(comparisonHistoryFetch());
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const versionTwo = within(panel)
      .getByText("v2")
      .closest(".history-card") as HTMLElement;
    const opener = within(versionTwo).getByRole("button", {
      name: "Show changes in v2 since v1",
    });
    await userEvent.click(opener);

    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v2 since v1",
    });
    expect(
      within(dialog).getByRole("combobox", { name: "Earlier version" }),
    ).toHaveValue("version-1");
    expect(
      within(dialog).getByRole("combobox", { name: "Later version" }),
    ).toHaveValue("version-2");

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Close diff" }),
    );
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("loads the exact authorized parent through the History cursor", async () => {
    const responses = comparisonHistoryFetch();
    const historyUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`;
    responses[historyUrl] = {
      data: {
        versions: [
          historyVersion("version-3", 3, "published", "version-2", {
            is_current: true,
            is_current_published: true,
            is_visible_head: true,
          }),
        ],
        changesets: [],
        review_decisions: [],
        diffs: null,
        next_cursor: "cursor-older",
      },
    };
    const olderHistoryUrl = `${historyUrl}&cursor=cursor-older`;
    responses[olderHistoryUrl] = {
      data: {
        versions: [
          historyVersion("version-2", 2, "superseded", "version-1"),
          historyVersion("version-1", 1, "superseded", null),
        ],
        changesets: [],
        review_decisions: [],
        diffs: null,
        next_cursor: null,
      },
    };
    const fetchMock = mockFetch(responses);
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const latest = within(panel)
      .getByText("v3")
      .closest(".history-card") as HTMLElement;
    expect(
      within(latest).getByText("Continues from an older version"),
    ).toBeInTheDocument();
    await userEvent.click(
      within(latest).getByRole("button", {
        name: "Show changes in v3 since the version it was based on",
      }),
    );

    expect(
      await screen.findByRole("dialog", { name: "Changes in v3 since v2" }),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(olderHistoryUrl, expect.anything());
  });

  it("does not expose or request a hidden History parent", async () => {
    const responses = comparisonHistoryFetch();
    const historyUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`;
    responses[historyUrl] = {
      data: {
        versions: [
          {
            ...historyVersion("version-3", 3, "published", null, {
              is_current: true,
              is_current_published: true,
              is_visible_head: true,
            }),
            prior_relation: "hidden",
          },
        ],
        changesets: [],
        review_decisions: [],
        diffs: null,
        next_cursor: "cursor-containing-hidden-parent",
      },
    };
    const fetchMock = mockFetch(responses);
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const latest = within(panel)
      .getByText("v3")
      .closest(".history-card") as HTMLElement;
    expect(
      within(latest).getByText("Previous version is not visible"),
    ).toBeInTheDocument();
    expect(
      within(latest).getByText("Changes unavailable"),
    ).toBeInTheDocument();
    expect(
      within(latest).queryByRole("button", { name: /Show changes in v3/ }),
    ).toBeNull();
    expect(
      fetchMock.mock.calls.some((call) =>
        String(call[0]).includes("cursor-containing-hidden-parent"),
      ),
    ).toBe(false);
  });

  it("warns when the older endpoint is not an ancestor of the newer endpoint", async () => {
    mockFetch(comparisonHistoryFetch(true));
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    await userEvent.click(
      screen.getByRole("button", { name: "Compare two versions" }),
    );

    let dialog = await screen.findByRole("dialog", {
      name: "Changes in v3 since v1",
    });
    await userEvent.selectOptions(
      within(dialog).getByRole("combobox", { name: "Earlier version" }),
      "version-2",
    );
    // v2 is not v3's parent here, so this is a free comparison, not Changes.
    dialog = await screen.findByRole("dialog", { name: "Compare v2 and v3" });
    expect(dialog).toHaveTextContent(
      "These versions are on different lineages. This diff compares snapshots across a branch, not a single progression.",
    );
  });

  // These two check the DOM mapping of the layout (shared/lib/historyGraph.ts,
  // whose own tests cover the routing rules): which lane each painted segment
  // sits in. Asserting only that a path or a pass line exists is what let a
  // same-base fork render as one straight line ever since the graph shipped.
  const segmentLanes = (row: HTMLElement, selector: string) =>
    Array.from(row.querySelectorAll<HTMLElement>(selector)).map((element) =>
      element.style.getPropertyValue("--history-segment-lane"),
    );

  it("draws a fork beside its sibling and joins it only at their shared parent", async () => {
    // v3 (published head) and v2 both fork from v1.
    mockFetch(comparisonHistoryFetch(true));
    render(
      <MemoryRouter
        initialEntries={[`/context?target=${PAGE_ID}&tab=history`]}
      >
        <ContextScreen />
      </MemoryRouter>,
    );

    const panel = await screen.findByRole("tabpanel", { name: /History/ });
    const rowOf = (label: string) =>
      within(panel).getByText(label).closest(".history-graph-row") as HTMLElement;
    const head = rowOf("v3");
    const fork = rowOf("v2");

    expect(head).toHaveAttribute("data-history-lane", "0");
    expect(head.querySelector(".history-graph-bend")).toBeNull();
    expect(fork).toHaveAttribute("data-history-lane", "1");
    // v3 -> v1 passes v2 on the trunk lane, beside v2's dot, never through it.
    expect(segmentLanes(fork, ".history-graph-pass-line")).toEqual(["0"]);
    // v2's own edge leaves in its lane and bends into v1's at the row bottom.
    expect(
      segmentLanes(fork, ".history-graph-line.history-graph-before-bend"),
    ).toEqual(["1"]);
    expect(fork.querySelector(".history-graph-bend")).not.toBeNull();
  });

  it("draws a merge departing into the branch lane and the branch joining at its fork point", async () => {
    // v4 (published head, parent v2) merges v3, which forked from v1.
    const responses = comparisonHistoryFetch();
    const detailUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`;
    const detailResponse = responses[detailUrl] as {
      data: Record<string, unknown>;
    };
    responses[detailUrl] = {
      data: {
        ...detailResponse.data,
        current_version_id: "version-4",
        current_published_version_id: "version-4",
        visible_version_id: "version-4",
      },
    };
    const historyUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=context_page&target_id=${PAGE_ID}`;
    responses[historyUrl] = {
      data: {
        versions: [
          historyVersion("version-4", 4, "published", "version-2", {
            is_current: true,
            is_current_published: true,
            is_visible_head: true,
          }),
          {
            ...historyVersion("version-3", 3, "alternative_draft", "version-1"),
            merged_into_version_id: "version-4",
          },
          historyVersion("version-2", 2, "superseded", "version-1"),
          historyVersion("version-1", 1, "published", null),
        ],
        changesets: [],
        review_decisions: [],
        diffs: null,
        next_cursor: null,
      },
    };
    mockFetch(responses);
    render(
      <MemoryRouter
        initialEntries={[`/context?target=${PAGE_ID}&tab=history`]}
      >
        <ContextScreen />
      </MemoryRouter>,
    );

    const panel = await screen.findByRole("tabpanel", { name: /History/ });
    const rowOf = (label: string) =>
      within(panel).getByText(label).closest(".history-graph-row") as HTMLElement;
    const merge = rowOf("v4");
    const branch = rowOf("v3");
    const trunkBelow = rowOf("v2");

    expect(merge.querySelector(".history-graph-departure")).not.toBeNull();
    expect(
      segmentLanes(merge, ".history-graph-line.history-graph-after-departure"),
    ).toEqual(["1"]);
    expect(branch).toHaveAttribute("data-history-lane", "1");
    expect(branch.querySelector(".history-graph-incoming-line")).not.toBeNull();
    expect(segmentLanes(branch, ".history-graph-pass-line")).toEqual(["0"]);
    // v3 -> v1 keeps lane 1 past v2 and bends into v1's lane at v2's bottom;
    // nothing passes through v2's own dot on lane 0.
    expect(segmentLanes(trunkBelow, ".history-graph-pass-line")).toEqual(["1"]);
    expect(trunkBelow.querySelector(".history-graph-bend")).not.toBeNull();
  });

  it("localizes a generic History diff exception instead of exposing internals", async () => {
    const responses = comparisonHistoryFetch();
    responses[
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/diff?from_version_id=version-1&to_version_id=version-2`
    ] = { error: new Error("SQLSTATE 23505 internal detail") };
    mockFetch(responses);
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const versionTwo = within(panel)
      .getByText("v2")
      .closest(".history-card") as HTMLElement;
    await userEvent.click(
      within(versionTwo).getByRole("button", { name: "Show changes in v2 since v1" }),
    );

    expect(await screen.findByText("Failed to load changes")).toBeInTheDocument();
    expect(screen.queryByText(/SQLSTATE/)).toBeNull();
  });

  it("opens a full-screen unified diff at 390px with visible add/remove markers", async () => {
    setMobileViewport(true, 390);
    mockFetch(comparisonHistoryFetch());
    render(
      <MemoryRouter initialEntries={[`/context?target=${PAGE_ID}`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    await userEvent.click(
      screen.getByRole("button", { name: "Compare two versions" }),
    );

    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v3 since v2",
    });
    expect(dialog).toHaveClass("is-mobile-fullscreen");
    expect(
      within(dialog).getByRole("button", { name: "Unified" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      within(dialog).queryByRole("table", { name: "Side-by-side diff" }),
    ).toBeNull();
    expect(await within(dialog).findByText("-Second body")).toHaveClass(
      "diff-line-del",
    );
    expect(within(dialog).getByText("+Third body")).toHaveClass(
      "diff-line-add",
    );
  });

  it("surfaces an inline pending-draft review with reviewer actions (F1)", async () => {
    ctxWorkspace.current = {
      selectedWorkspace: { role: "owner", id: WORKSPACE_ID },
    };
    mockFetch(pendingReviewFetch());
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    expect(
      await screen.findByText(/1 pending review for this page/),
    ).toBeInTheDocument();
    // Sweep-2: the draft's lint warnings are surfaced at the approval point.
    expect(screen.getByText(/1 lint warning/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Request changes" }),
    ).toBeInTheDocument();
  });

  it("warns about pending reconciliation before publish without blocking approval", async () => {
    ctxWorkspace.current = {
      selectedWorkspace: { role: "owner", id: WORKSPACE_ID },
    };
    const responses: Record<string, unknown> = pendingReviewFetch(0, "pending", 2);
    responses[
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/rev-1/decision`
    ] = {
      data: {
        id: "rev-1",
        status: "approved",
        published_version_id: "version-2",
        superseded_review_count: 0,
      },
    };
    const fetchMock = mockFetch(responses);
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    const approve = await screen.findByRole("button", { name: "Approve" });
    await waitFor(() => expect(approve).toBeEnabled());

    await userEvent.click(approve);

    expect(confirm).toHaveBeenCalledWith(
      "2 branches are still waiting for reconciliation. Publish this version anyway?",
    );
    expect(
      fetchMock.mock.calls.some(([input]) =>
        String(input).endsWith(`/reviews/rev-1/decision`),
      ),
    ).toBe(false);

    confirm.mockReturnValueOnce(true);
    await userEvent.click(approve);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).endsWith(`/reviews/rev-1/decision`),
        ),
      ).toBe(true),
    );
  });

  it("hides pending-draft reviewer actions for viewers (F1 RBAC)", async () => {
    ctxWorkspace.current = {
      selectedWorkspace: { role: "viewer", id: WORKSPACE_ID },
    };
    mockFetch(pendingReviewFetch());
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    expect(
      await screen.findByText(/1 pending review for this page/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it.each([
    {
      earlierCount: 1,
      totalCopy: "2 pending reviews for this page",
      impactCopy:
        "Approving will publish this version. 1 earlier pending review will be settled with it.",
    },
    {
      earlierCount: 2,
      totalCopy: "3 pending reviews for this page",
      impactCopy:
        "Approving will publish this version. 2 earlier pending reviews will be settled with it.",
    },
  ])(
    "shows the full pending count and $earlierCount-review approval impact before approval",
    async ({ earlierCount, totalCopy, impactCopy }) => {
      ctxWorkspace.current = {
        selectedWorkspace: { role: "owner", id: WORKSPACE_ID },
      };
      mockFetch(pendingReviewFetch(earlierCount));
      render(
        <MemoryRouter>
          <ContextScreen />
        </MemoryRouter>,
      );

      await userEvent.click(
        await screen.findByRole("treeitem", { name: "Decision Log" }),
      );

      expect(
        await screen.findByText(new RegExp(totalCopy)),
      ).toBeInTheDocument();
      expect(screen.getByText(impactCopy)).toBeInTheDocument();
      expect(
        screen.getByRole("link", {
          name: `View all ${earlierCount + 1} pending reviews in the queue`,
        }),
      ).toHaveAttribute("href", "/review?review=rev-1");
      expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    },
  );

  it("shows no extra settlement copy when no earlier review will be settled", async () => {
    ctxWorkspace.current = {
      selectedWorkspace: { role: "owner", id: WORKSPACE_ID },
    };
    mockFetch(pendingReviewFetch(0));
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    });

    expect(
      screen.getByText(/1 pending review for this page/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/earlier pending reviews?/i)).toBeNull();
    expect(
      screen.queryByRole("link", { name: /pending reviews in the queue/i }),
    ).toBeNull();
  });

  it("does not promise publication or settlement for a changes-requested draft", async () => {
    ctxWorkspace.current = {
      selectedWorkspace: { role: "owner", id: WORKSPACE_ID },
    };
    mockFetch(pendingReviewFetch(2, "changes_requested"));
    render(
      <MemoryRouter>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );

    expect(
      await screen.findByText(/Draft has changes requested/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Approving will publish/i)).toBeNull();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(
      screen.getByRole("link", { name: "Open Review queue" }),
    ).toHaveAttribute("href", "/review");
  });

  describe("P10.1 canonical route", () => {
    function renderAtCanonicalRoute(path: string) {
      return render(
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/context" element={<div>Context overview route</div>} />
            <Route
              path="/workspaces/:workspaceId/context/pages/:pageId"
              element={<ContextScreen />}
            />
          </Routes>
        </MemoryRouter>,
      );
    }

    it("resolves the initial target from the canonical route's :pageId", async () => {
      mockDecisionLogRoutes();
      renderAtCanonicalRoute(`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`);

      expect(await screen.findByText("Needle body")).toBeInTheDocument();
    });

    it("prefers the ?target= query param over the route's :pageId when both are present", async () => {
      mockDecisionLogRoutes();
      renderAtCanonicalRoute(
        `/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}?target=${PAGE_ID}`,
      );

      expect(await screen.findByText("Needle body")).toBeInTheDocument();
      expect(screen.queryByText("Selected second page")).not.toBeInTheDocument();
    });

    it("leaves a deleted canonical page route instead of retaining its stale :pageId", async () => {
      const user = userEvent.setup();
      const fetchMock = mockDecisionLogRoutes();
      const baseFetch = fetchMock.getMockImplementation();
      let deleted = false;
      fetchMock.mockImplementation(async (input, init) => {
        const url = input.toString();
        if (
          url ===
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes/node-1` &&
          init?.method === "DELETE"
        ) {
          deleted = true;
          return jsonResponse({ data: null });
        }
        if (
          deleted &&
          url.startsWith(
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages?`,
          )
        ) {
          return jsonResponse({
            data: [],
            next_cursor: null,
            total: 0,
            has_more: false,
          });
        }
        if (
          deleted &&
          url ===
            `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`
        ) {
          return jsonResponse({ data: treeResponse("context", []) });
        }
        if (!baseFetch) {
          throw new Error("Missing base fetch implementation");
        }
        return baseFetch(input, init);
      });
      renderAtCanonicalRoute(
        `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`,
      );

      await screen.findByText("Needle body");
      await user.click(
        screen.getByRole("button", { name: "More page actions" }),
      );
      await user.click(screen.getByRole("menuitem", { name: "Delete" }));
      const dialog = await screen.findByRole("dialog", {
        name: "Delete Decision Log",
      });
      await user.click(within(dialog).getByRole("button", { name: "Delete" }));

      expect(await screen.findByText("Context overview route")).toBeInTheDocument();
    });

    it("silently resyncs the ambient workspace when the routed id is already a member", async () => {
      const selectWorkspaceId = vi.fn();
      ctxWorkspace.current = {
        selectedWorkspace: { role: "owner", visibility: "public" },
        workspaceSlug: "demo",
        selectedId: "ambient-workspace",
        workspaces: [{ id: WORKSPACE_ID }, { id: "ambient-workspace" }],
        selectWorkspaceId,
      };
      mockDecisionLogRoutes();
      renderAtCanonicalRoute(`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`);

      await screen.findByText("Needle body");
      expect(selectWorkspaceId).toHaveBeenCalledWith(WORKSPACE_ID);
    });

    it("does not touch ambient workspace state when the routed id is not a known member", async () => {
      const selectWorkspaceId = vi.fn();
      ctxWorkspace.current = {
        selectedWorkspace: { role: "owner", visibility: "public" },
        workspaceSlug: "demo",
        selectedId: "ambient-workspace",
        workspaces: [{ id: "ambient-workspace" }],
        selectWorkspaceId,
      };
      mockDecisionLogRoutes();
      renderAtCanonicalRoute(
        `/workspaces/unknown-workspace/context/pages/${PAGE_ID}`,
      );

      // The fixture's fetch mocks only answer for WORKSPACE_ID, so the load
      // still resolves via the untouched ambient workspace — the point under
      // test is that the unknown routed id never reaches selectWorkspaceId.
      await screen.findByText("Needle body");
      expect(selectWorkspaceId).not.toHaveBeenCalled();
    });
  });
});

describe("ContextChildrenIndex", () => {
  it("exposes direct page and folder children in a labelled landmark", async () => {
    const parent = {
      ...treeNode("parent", PAGE_ID, "Parent"),
      can_have_children: true,
      children_count: 2,
    };
    const pageChild = {
      ...treeNode("page-child", SECOND_PAGE_ID, "Child page"),
      parent_id: parent.id,
    };
    const folderChild = folderNode("folder-child", "Child folder", {
      parent_id: parent.id,
    });
    const onSelectNode = vi.fn();
    render(
      <MemoryRouter>
        <ContextChildrenIndex
          node={parent}
          nodes={[parent, pageChild, folderChild]}
          onSelectNode={onSelectNode}
        />
      </MemoryRouter>,
    );
    const landmark = screen.getByRole("region", { name: "Children" });
    expect(
      within(landmark).getByRole("button", { name: "Child page" }),
    ).toBeInTheDocument();
    await userEvent.click(
      within(landmark).getByRole("button", { name: "Child folder" }),
    );
    expect(onSelectNode).toHaveBeenCalledWith(folderChild);
  });

  it("uses five-item numeric URL pagination and clamps an invalid page", async () => {
    const parent = {
      ...treeNode("parent", PAGE_ID, "Parent"),
      can_have_children: true,
      children_count: 12,
    };
    const children = Array.from({ length: 12 }, (_, index) => ({
      ...treeNode(
        `child-${String(index + 1).padStart(2, "0")}`,
        `page-${index + 1}`,
        `Child ${String(index + 1).padStart(2, "0")}`,
      ),
      parent_id: parent.id,
      sort_key: String(index + 1).padStart(2, "0"),
    }));
    const searches: string[] = [];
    render(
      <MemoryRouter initialEntries={["/context?page=99"]}>
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextChildrenIndex
          node={parent}
          nodes={[parent, ...children]}
          onSelectNode={vi.fn()}
        />
      </MemoryRouter>,
    );

    const landmark = screen.getByRole("region", { name: "Children" });
    expect(
      within(landmark).getByText("Showing 11–12 of 12"),
    ).toBeInTheDocument();
    expect(
      within(landmark).getByRole("button", { name: "Child 11" }),
    ).toBeInTheDocument();
    expect(
      within(landmark).queryByRole("button", { name: "Child 10" }),
    ).toBeNull();
    expect(
      within(landmark).getByRole("button", { name: "Page 3" }),
    ).toHaveAttribute("aria-current", "page");
    await waitFor(() => expect(searches.at(-1)).toBe("?page=3"));

    await userEvent.click(
      within(landmark).getByRole("button", { name: "Page 2" }),
    );
    expect(
      within(landmark).getByText("Showing 6–10 of 12"),
    ).toBeInTheDocument();
    expect(
      within(landmark).getByRole("button", { name: "Child 06" }),
    ).toBeInTheDocument();
    expect(searches.at(-1)).toBe("?page=2");
  });

  it("removes a malformed Children page parameter instead of preserving it", async () => {
    const parent = {
      ...treeNode("parent", PAGE_ID, "Parent"),
      can_have_children: true,
      children_count: 1,
    };
    const child = {
      ...treeNode("child", SECOND_PAGE_ID, "Child"),
      parent_id: parent.id,
    };
    const searches: string[] = [];
    render(
      <MemoryRouter initialEntries={["/context?page=not-a-page"]}>
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextChildrenIndex
          node={parent}
          nodes={[parent, child]}
          onSelectNode={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("button", { name: "Child" })).toBeInTheDocument();
    await waitFor(() => expect(searches.at(-1)).toBe(""));
  });
});

function setMobileViewport(matches: boolean, width?: number) {
  if (width !== undefined) {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: width,
    });
  }
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

function SearchProbe({ onSearch }: { onSearch: (search: string) => void }) {
  const location = useLocation();
  useEffect(() => {
    onSearch(location.search);
  }, [location.search, onSearch]);
  return null;
}

function LocationStateProbe() {
  const location = useLocation();
  return (
    <output data-testid="location-state">
      {JSON.stringify(location.state)}
    </output>
  );
}

function BackButton() {
  const navigate = useNavigate();
  return (
    <button onClick={() => navigate(-1)} type="button">
      Go back
    </button>
  );
}

function HistoryButtons() {
  const navigate = useNavigate();
  return (
    <>
      <button onClick={() => navigate(-1)} type="button">
        Go back
      </button>
      <button onClick={() => navigate(1)} type="button">
        Go forward
      </button>
    </>
  );
}

function mockFetch(responses: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    // The context/pages list endpoint now carries pagination query params
    // (?limit=…&cursor=…); fall back to the query-stripped path so existing
    // mocks keyed on the bare endpoint still match.
    const body = responses[url] ?? responses[url.split("?")[0]];
    if (
      body &&
      typeof body === "object" &&
      "error" in body &&
      body.error instanceof Error
    ) {
      throw body.error;
    }
    if (!body) {
      throw new Error(`Unexpected request: ${url}`);
    }
    return jsonResponse(body);
  });
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function contextPage(id: string, title: string, slug: string) {
  return {
    id,
    slug,
    title,
    page_type: "decision_log",
    lifecycle_status: "published",
    visible_version_id: "version-1",
    current_version_id: "version-1",
    current_published_version_id: "version-1",
    latest_draft_version_id: null,
    is_public: false,
    updated_at: "2026-05-09T00:00:00Z",
  };
}

// Publication metadata is NOT derivable from version_status on the backend:
// a published version stops being the visible head the moment a draft is
// pending, and only one version can hold each flag. Deriving these booleans
// from the synthetic status here would hide exactly that class of regression,
// so every call site states the combination it means (all false by default).
function historyVersion(
  id: string,
  versionNo: number,
  requestedStatus: ContextHistoryStatus | "published" | "superseded" | "draft",
  optionsOrPrior:
    | {
        actorType?: string;
        isAlternative?: boolean;
        isDiscarded?: boolean;
        isDraftHead?: boolean;
        isPublishedHead?: boolean;
        reconciliationAvailable?: boolean;
        isVisibleHead?: boolean;
        priorRelation?: "root" | "visible" | "hidden" | "missing";
      }
    | string
    | null = {},
  publication: Partial<{
    is_current: boolean;
    is_current_published: boolean;
    is_latest_draft: boolean;
    is_visible_head: boolean;
  }> = {},
) {
  const historyStatus: ContextHistoryStatus =
    requestedStatus === "published"
      ? "published_snapshot"
      : requestedStatus === "superseded"
        ? "superseded_snapshot"
        : requestedStatus === "draft"
          ? "current_draft"
          : requestedStatus;
  const options =
    optionsOrPrior && typeof optionsOrPrior === "object" ? optionsOrPrior : {};
  const priorVersionId =
    typeof optionsOrPrior === "string" || optionsOrPrior === null
      ? optionsOrPrior
      : versionNo > 1
        ? `version-${versionNo - 1}`
        : null;
  const isVisibleHead =
    options.isVisibleHead ?? publication.is_visible_head ?? false;
  const isPublishedHead =
    options.isPublishedHead ?? publication.is_current_published ?? false;
  const isDraftHead =
    options.isDraftHead ?? publication.is_latest_draft ?? false;
  return {
    version_id: id,
    version_no: versionNo,
    history_status: historyStatus,
    graph_status:
      historyStatus === "alternative_draft"
        ? "branched"
        : historyStatus === "discarded_draft"
          ? "discarded"
          : "linear",
    actor_type: options.actorType ?? "human",
    actor_display_name: "Test User",
    created_at: "2026-05-09T00:00:00Z",
    prior_version_id: priorVersionId,
    prior_relation:
      options.priorRelation ?? (priorVersionId ? "visible" : "root"),
    actor_id: null,
    lines_added: versionNo,
    lines_removed: 0,
    review_disposition:
      historyStatus === "current_draft" ? "pending" : "approved",
    is_current: publication.is_current ?? isVisibleHead,
    is_current_published: isPublishedHead,
    is_latest_draft: isDraftHead,
    review_status: historyStatus === "current_draft" ? "pending" : "approved",
    review_item_id: `review-${id}`,
    is_visible_head: isVisibleHead,
    is_published_head: isPublishedHead,
    is_draft_head: isDraftHead,
    is_alternative: options.isAlternative ?? false,
    is_discarded: options.isDiscarded ?? false,
    reconciliation_available: options.reconciliationAvailable,
  };
}

function contextVersionDiff(
  versionId: string,
  versionNo: number,
  body: string,
) {
  return {
    from_version_id: versionId,
    to_version_id: "version-3",
    from_version_no: versionNo,
    to_version_no: 3,
    lines: [
      `--- v${versionNo}`,
      "+++ v3",
      "@@ -1 +1 @@",
      `-${body}`,
      "+Current",
    ],
    additions: 1,
    deletions: 1,
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
    section: "context",
    title,
    slug: null,
    node_kind: "page_ref",
    parent_id: null,
    sort_key: "0001",
    target_kind: "context_page",
    target_id: targetId,
    is_system_node: false,
    can_have_children: false,
    primary_placement: true,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: breadcrumb.map((ancestor, index) => ({
      id: `ancestor-${index}`,
      section: "context",
      title: ancestor,
      slug: ancestor.toLowerCase(),
      node_kind: "folder",
    })),
  };
}

function folderNode(
  id: string,
  title: string,
  overrides: Partial<TreeNodePayload> = {},
): TreeNodePayload {
  return {
    id,
    section: "context",
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
    ...overrides,
  };
}

// HTML5 drag-and-drop is absent from jsdom, so the drop path is driven with
// hand-built events carrying a minimal DataTransfer and a mocked row rect —
// the same technique TreeRail's own tests use.
function createDataTransfer() {
  const values = new Map<string, string>();
  return {
    dropEffect: "none",
    effectAllowed: "all",
    getData: vi.fn((type: string) => values.get(type) ?? ""),
    setData: vi.fn((type: string, value: string) => {
      values.set(type, value);
    }),
  };
}

function rowBounds({ top, height }: { top: number; height: number }): DOMRect {
  return {
    bottom: top + height,
    height,
    left: 0,
    right: 240,
    top,
    width: 240,
    x: 0,
    y: top,
    toJSON: () => ({}),
  };
}

function fireTreeDragEvent(
  element: HTMLElement,
  type: "dragover" | "drop",
  {
    clientY,
    dataTransfer,
  }: {
    clientY: number;
    dataTransfer: ReturnType<typeof createDataTransfer>;
  },
) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperties(event, {
    clientY: { value: clientY },
    dataTransfer: { value: dataTransfer },
  });
  fireEvent(element, event);
}

function treeResponse(section: "context", nodes: TreeNodePayload[]) {
  return {
    nodes,
    policies: {
      [section]: {
        allow_user_folder_create: true,
        allow_page_as_parent: true,
      },
    },
  };
}
