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
  Link,
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setTreeFavoriteActor } from "../../../shared/api/treeFavorites";
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
const { ctxAuth } = vi.hoisted(() => ({
  ctxAuth: { current: null as { user: { user_id: string } } | null },
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
const { workspaceGraphEnabled } = vi.hoisted(() => ({
  workspaceGraphEnabled: { current: false },
}));
vi.mock(
  "../../../shared/workspace/WorkspaceContext",
  async (importOriginal) => ({
    ...(await importOriginal<object>()),
    useOptionalWorkspaceContext: () => ctxWorkspace.current,
  }),
);
vi.mock("../../../shared/auth/AuthContext", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useOptionalAuth: () => ctxAuth.current,
}));
vi.mock("mermaid", () => ({ default: contextMermaid }));
// The Knowledge Map link follows the server capability for this workspace.
vi.mock("../../graph/useWorkspaceGraphCapability", () => ({
  useWorkspaceGraphCapability: () => ({
    status: "ready",
    capability: null,
    enabled: workspaceGraphEnabled.current,
  }),
}));
vi.mock("../../graph/GraphCanvas", () => ({
  GraphCanvas: ({ scene, pinnedIds }: {
    scene: { nodes: unknown[]; edges: unknown[] };
    pinnedIds: string[];
  }) => (
    <div
      data-edges={scene.edges.length}
      data-nodes={scene.nodes.length}
      data-pinned={pinnedIds.join(",")}
      data-testid="context-graph-canvas"
    />
  ),
}));

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const SECOND_WORKSPACE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PAGE_ID = "22222222-2222-4222-8222-000000000001";
const SECOND_PAGE_ID = "33333333-3333-4333-8333-333333333333";
const fixtureUuid = (value: number) =>
  `00000000-0000-4000-8000-${String(value).padStart(12, "0")}`;

async function selectGraphNodeDetails(graph: HTMLElement, label: string) {
  const row = within(graph).getByText(label, {
    exact: true,
    selector: ".graph-relationship-list > details > ul > li > button strong",
  }).closest("button");
  expect(row).not.toBeNull();
  await userEvent.click(row!);
  const details = within(graph).getByRole("complementary", { name: "Details" });
  expect(within(details).getByRole("heading", { level: 5 }).textContent).toBe(label);
  return details;
}

describe("ContextScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.localStorage.setItem(
      "dtxwiki:workspace-layout:v2:context:left",
      "true",
    );
    ctxWorkspace.current = {
      selectedWorkspace: { id: WORKSPACE_ID, role: "owner", visibility: "public" },
      workspaceSlug: "demo",
      workspaces: [{ id: WORKSPACE_ID, role: "owner", visibility: "public" }],
    };
    ctxAuth.current = null;
    workspaceGraphEnabled.current = false;
    setTreeFavoriteActor(null);
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
      graphBounded?: boolean;
      neighborsBounded?: boolean;
      neighborsHandler?: (pageId: string) => unknown;
      graphQueryHandler?: (
        request: {
          seeds?: Array<{ kind: "context_page"; id: string }>;
          time?: { mode?: "current" | "appearance" | "action" | "as_of" };
          depth?: 1 | 2;
          budget?: { nodes: number; edges: number };
        },
        init?: RequestInit,
      ) => unknown;
      libraryGraphNodeKind?: "library_artifact" | "library_item_version";
      unpublishedSecondPage?: boolean;
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
          folderNode("55555555-5555-4555-8555-555555555555", "Inbox", { children_count: 1 }),
          {
            ...treeNode("node-2", SECOND_PAGE_ID, "Architecture Notes", [
              "Inbox",
            ]),
            breadcrumb: [{
              id: "55555555-5555-4555-8555-555555555555",
              section: "context",
              title: "Inbox",
              slug: "inbox",
              node_kind: "folder",
            }],
            parent_id: options.nestedSecondPage ? "55555555-5555-4555-8555-555555555555" : null,
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
            current_published_version_id: options.unpublishedSecondPage
              ? null
              : "version-1",
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/graph/query`]: (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) => {
        const request = JSON.parse(String(init?.body)) as {
          seeds?: Array<{ kind: "context_page"; id: string }>;
          time?: { mode?: "current" | "appearance" | "action" | "as_of" };
          depth?: 1 | 2;
          budget?: { nodes: number; edges: number };
        };
        if (options.graphQueryHandler) {
          return options.graphQueryHandler(request, init);
        }
        const payload = contextGraphPayload(
          request.seeds?.[0]?.id ?? PAGE_ID,
          request.time?.mode ?? "appearance",
          request.seeds,
          options.libraryGraphNodeKind,
          request.depth,
        );
        return {
          data: options.graphBounded
            ? {
                ...payload,
                truncated: true,
                warnings: ["graph_budget_applied", "private_internal_reason"],
              }
            : payload,
        };
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
    if (options.neighborsHandler) {
      for (const pageId of [PAGE_ID, SECOND_PAGE_ID]) {
        routes[`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${pageId}/neighbors`] =
          () => options.neighborsHandler!(pageId);
      }
    } else if (options.neighborsBounded) {
      const route = routes[`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors`] as {
        data: Record<string, unknown>;
      };
      route.data.truncated = true;
      route.data.warnings = ["graph_projection_source_bounded", "private_internal_reason"];
    }
    return mockFetch(routes);
  }

  async function renderDecisionLog(
    options: {
      bodyMarkdown?: string;
      secondActivityFails?: boolean;
      reconciliationAvailable?: boolean;
      graphBounded?: boolean;
      neighborsBounded?: boolean;
      neighborsHandler?: (pageId: string) => unknown;
      graphQueryHandler?: (
        request: {
          seeds?: Array<{ kind: "context_page"; id: string }>;
          time?: { mode?: "current" | "appearance" | "action" | "as_of" };
          depth?: 1 | 2;
          budget?: { nodes: number; edges: number };
        },
        init?: RequestInit,
      ) => unknown;
      libraryGraphNodeKind?: "library_artifact" | "library_item_version";
      unpublishedSecondPage?: boolean;
    } = {},
  ) {
    const fetchMock = mockDecisionLogRoutes(options);
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
  it("refreshes the overview and tree after leaving the reader and clicking its menu again", async () => {
    const fetchMock = mockDecisionLogRoutes();
    const originalFetch = fetchMock.getMockImplementation()!;
    let pages = [contextPage(PAGE_ID, "Decision Log", "decision-log")];
    let listReads = 0;
    let treeReads = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(input.toString());
      if (url.pathname === `/api/v1/workspaces/${WORKSPACE_ID}/context/pages`) {
        listReads += 1;
        return jsonResponse({ data: pages, total: pages.length });
      }
      if (url.pathname === `/api/v1/workspaces/${WORKSPACE_ID}/tree`) {
        treeReads += 1;
        return jsonResponse({ data: treeResponse("context", pages.map((page) =>
          treeNode(`node-${page.id}`, page.id, page.title),
        )) });
      }
      return originalFetch(input, init);
    });
    const view = render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
        <Link to={`/workspaces/${WORKSPACE_ID}/context`}>Main menu Context</Link>
        <Routes>
          <Route path="/workspaces/:workspaceId/context" element={<ContextScreen />} />
          <Route path="/workspaces/:workspaceId/context/pages/:pageId" element={<ContextScreen />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("Needle body");
    const main = within(view.container.querySelector(".feature-main") as HTMLElement);
    expect([listReads, treeReads]).toEqual([1, 1]);

    pages = [contextPage("fresh-1", "Fresh after reader", "fresh-1"), ...pages];
    await userEvent.click(screen.getByRole("link", { name: "Main menu Context" }));
    await screen.findByRole("treeitem", { name: "Fresh after reader" });
    expect(await main.findByRole("button", { name: /Fresh after reader/ })).toBeInTheDocument();
    expect(screen.queryByText("Needle body")).not.toBeInTheDocument();
    expect([listReads, treeReads]).toEqual([2, 2]);

    pages = [contextPage("fresh-2", "Fresh while home", "fresh-2"), ...pages];
    await userEvent.click(screen.getByRole("link", { name: "Main menu Context" }));
    await screen.findByRole("treeitem", { name: "Fresh while home" });
    expect(await main.findByRole("button", { name: /Fresh while home/ })).toBeInTheDocument();
    expect([listReads, treeReads]).toEqual([3, 3]);
  });

  it("drops appended and in-flight pages when a menu refresh returns the same first page and cursor", async () => {
    const fetchMock = mockDecisionLogRoutes();
    const originalFetch = fetchMock.getMockImplementation()!;
    const stalePage = deferredResponse();
    let firstPageReads = 0;
    let moreReads = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(input.toString());
      if (url.pathname === `/api/v1/workspaces/${WORKSPACE_ID}/context/pages`) {
        if (url.searchParams.has("cursor")) {
          moreReads += 1;
          return moreReads === 1 ? jsonResponse({
            data: [contextPage("old-tail", "Previously loaded tail", "old-tail")],
            next_cursor: null, total: 3, has_more: false,
          }) : stalePage.promise;
        }
        firstPageReads += 1;
        const response = await originalFetch(input, init);
        const body = await response.json();
        return jsonResponse({ ...body, next_cursor: "same-cursor", total: 3, has_more: true });
      }
      return originalFetch(input, init);
    });
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
        <Link to={`/workspaces/${WORKSPACE_ID}/context`}>Main menu Context</Link>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("Previously loaded tail");
    await userEvent.click(screen.getByRole("link", { name: "Main menu Context" }));
    await waitFor(() => expect(screen.queryByText("Previously loaded tail")).not.toBeInTheDocument());
    expect(firstPageReads).toBe(2);
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await userEvent.click(screen.getByRole("link", { name: "Main menu Context" }));
    await waitFor(() => expect(firstPageReads).toBe(3));
    await waitFor(() => expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled());
    await act(async () => stalePage.resolve(jsonResponse({
      data: [contextPage("stale-tail", "Stale in-flight tail", "stale-tail")],
      next_cursor: null, total: 3, has_more: false,
    })));
    expect(screen.queryByText("Stale in-flight tail")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
  });

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
    ).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/context/pages/new`);
    expect(screen.queryByRole("link", { name: "Context home" })).toBeNull();
    expect(screen.getByText("Unpublished draft")).toBeInTheDocument();
  });

  it("shows a publish arrival notice once and clears navigation state", async () => {
    mockDecisionLogRoutes();
    render(
      <MemoryRouter
        initialEntries={[
          {
            pathname: `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`,
            search: "",
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
          `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=children&page=2`,
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
      `?tab=children&page=2`,
    );

    await userEvent.click(screen.getByRole("tab", { name: "View" }));
    await waitFor(() => expect(searches.at(-1)).toBe(""));
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
          `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=children&page=2`,
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
    await waitFor(() => expect(searches.at(-1)).toBe(""));

    await userEvent.click(screen.getByRole("button", { name: "Go back" }));
    expect(
      await screen.findByRole("tabpanel", { name: /Children/ }),
    ).toBeInTheDocument();
    expect(searches.at(-1)).toBe(
      `?tab=children&page=2`,
    );
    expect(
      screen.getByRole("button", { name: "Child 06" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Go forward" }));
    expect(
      await screen.findByRole("tabpanel", { name: "View" }),
    ).toBeInTheDocument();
    expect(searches.at(-1)).toBe("");
  });

  it("falls a stale Children deep link back to View when the page has no direct children", async () => {
    const searches: string[] = [];
    mockDecisionLogRoutes({ decisionLogChildCount: 0 });
    render(
      <MemoryRouter
        initialEntries={[
          `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=children&page=3`,
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
    await waitFor(() => expect(searches.at(-1)).toBe(""));
  });

  it("synchronizes History with Back and resets to View when the page changes", async () => {
    mockDecisionLogRoutes({ nestedSecondPage: true });
    render(
      <MemoryRouter
        initialEntries={[
          `/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}`,
          `/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}?tab=history`,
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

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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

  it("renders an actor-hidden or invalid target as the uniform unavailable surface", async () => {
    const fetchMock = mockDecisionLogRoutes();
    const currentImplementation = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/context/pages/44444444-4444-4444-8444-444444444444")) {
        return jsonResponse({
          error_code: "not_found",
          message: "Context page not found.",
          request_id: "request-hidden-context-page",
          warnings: [],
        }, 404);
      }
      return currentImplementation(input, init);
    });
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/44444444-4444-4444-8444-444444444444`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "This item can't be opened" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Needle body")).not.toBeInTheDocument();
  });

  it("renders selected markdown with page actions, favorite, and copy link", async () => {
    const writeText = installClipboardStub();
    ctxAuth.current = { user: { user_id: "viewer-1" } };
    setTreeFavoriteActor("viewer-1");
    const fetchMock = await renderDecisionLog();
    const routeFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) =>
      init?.method === "PUT"
        ? jsonResponse({
            data: {
              target_kind: "context_page",
              target_id: PAGE_ID,
              is_favorite: true,
            },
          })
        : routeFetch(input, init),
    );

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
    await userEvent.click(
      screen.getByRole("menuitem", { name: "Add to my favorites" }),
    );
    expect(
      fetchMock.mock.calls.find(([, init]) => init?.method === "PUT"),
    ).toEqual([
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/favorites/context_page/${PAGE_ID}`,
      expect.objectContaining({ body: JSON.stringify({ is_favorite: true }) }),
    ]);
    // The reader menu and the rail share one favorite store.
    expect(document.querySelectorAll(".tree-row-favorite")).toHaveLength(1);
    await userEvent.click(moreAction);
    expect(
      await screen.findByRole("menuitem", { name: "Remove from my favorites" }),
    ).toBeEnabled();
    await userEvent.click(screen.getByRole("menuitem", { name: "Copy link" }));
    expect(writeText).toHaveBeenCalledWith(
      expect.stringMatching(
        new RegExp(`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}$`),
      ),
    );
    expect(screen.getByText("Link copied")).toBeInTheDocument();
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    // Activity loads when its tab opens, so a plain page view neither requests
    // it nor shows its count.
    expect(
      readerTabs.getAllByRole("tab").map((tab) => tab.textContent),
    ).toEqual(["View", "History1", "GraphPreview", "Activity"]);
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/activity")),
    ).toBe(false);
    expect(readerTabs.queryByRole("tab", { name: "Bundle" })).toBeNull();
  });

  it("marks the Graph tab as a localized Preview surface still in development", async () => {
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
    await screen.findByText("Needle body");
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await screen.findByText("Needle body");
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
        <ContextScreen />
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
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
      within(draftHead).queryByRole("button", { name: "Start a new draft from this version" }),
    ).toBeNull();
    expect(publishedHead).not.toHaveAttribute("aria-current");
    expect(
      within(publishedHead).getByRole("button", { name: "Show changes in v7 since v6" }),
    ).toBeInTheDocument();
    expect(
      within(publishedHead).getByRole("button", {
        name: "Start a new draft from this version",
      }),
    ).toBeInTheDocument();
    expect(
      within(panel).getAllByRole("button", { name: "Discard" }),
    ).toHaveLength(1);
    // Discard comes after the buttons every row shares, so a row that can be
    // discarded keeps Restore and Changes where every other row has them.
    // (Visible text is the short label; the full names are the accessible
    // names the rest of this suite queries by.)
    const discardActions = within(panel)
      .getByRole("button", { name: "Discard" })
      .closest(".history-card-actions") as HTMLElement;
    expect(
      Array.from(discardActions.querySelectorAll("button")).map(
        (button) => button.textContent,
      ),
    ).toEqual(["Restore", "Changes", "Discard"]);
  });

  it("discards a preserved alternative with an explicit reason", async () => {
    const fetchMock = await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
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
      within(alternative).queryByRole("button", { name: "Start a new draft from this version" }),
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

    await screen.findByText("Selected second page");
    expect(screen.queryByRole("button", { name: "Reconcile" })).toBeNull();
    expect(screen.queryByText(/requires reconciliation/i)).toBeNull();
  });

  it("renders Activity failure separately from its empty state", async () => {
    await renderDecisionLog({ secondActivityFails: true });
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
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
    ).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/library/artifacts/artifact-1`);
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

  it("does not query the heavyweight graph until its reader tab is first activated", async () => {
    const fetchMock = await renderDecisionLog();
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/graph/query")))
      .toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) =>
      String(input).endsWith(`/pages/${PAGE_ID}/neighbors`))).toHaveLength(1));
  });

  it("renders body and authoritative Inspector links while a first graph request remains pending", async () => {
    const pending = deferredValue<{ data: ReturnType<typeof contextGraphPayload> }>();
    const fetchMock = mockDecisionLogRoutes({ graphQueryHandler: () => pending.promise });
    render(<MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=graph`]}>
      <ContextScreen />
    </MemoryRouter>);
    try {
      expect(await screen.findByRole("tablist", { name: "Context reader views" })).toBeInTheDocument();
      await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) =>
        String(input).endsWith("/graph/query"))).toHaveLength(1));
      expect(screen.getByText(i18n.t("context:neighbors.graphLoading"))).toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
      const inspector = within(screen.getByRole("region", { name: "Context detail" }));
      await userEvent.click(inspector.getByRole("tab", { name: /Backlinks/ }));
      expect(await inspector.findByRole("link", { name: /Deployment Runbook/ }))
        .toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/library/artifacts/artifact-1`);
      expect(inspector.getAllByRole("link", { name: /Architecture Notes/ })).toHaveLength(2);
      await userEvent.click(within(await screen.findByRole("tablist", { name: "Context reader views" }))
        .getByRole("tab", { name: "View" }));
      expect(await screen.findByText("Needle body")).toBeVisible();
    } finally {
      await act(async () => { pending.resolve({ data: contextGraphPayload(PAGE_ID, "current") }); });
    }
  });

  it("keeps body and Inspector backlinks usable when the deferred graph fails", async () => {
    await renderDecisionLog({ graphQueryHandler: () => ({ error: new Error("Graph failed") }) });
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    const readerTabs = within(await screen.findByRole("tablist", { name: "Context reader views" }));
    await userEvent.click(readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }));
    expect(await screen.findByText("Graph could not be loaded.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const inspector = within(screen.getByRole("region", { name: "Context detail" }));
    await userEvent.click(inspector.getByRole("tab", { name: /Backlinks/ }));
    expect(await inspector.findByRole("link", { name: /Deployment Runbook/ })).toBeInTheDocument();
    expect(inspector.queryByRole("alert")).toBeNull();
    await userEvent.click(readerTabs.getByRole("tab", { name: /View/ }));
    expect(screen.getByText("Needle body")).toBeVisible();
  });

  it("does not hold the body or Graph behind a pending independent Inspector request", async () => {
    const pending = deferredValue<unknown>();
    await renderDecisionLog({ neighborsHandler: () => pending.promise });
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    expect(await screen.findByText("Needle body")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const inspector = within(screen.getByRole("region", { name: "Context detail" }));
    await userEvent.click(inspector.getByRole("tab", { name: /Backlinks/ }));
    expect(inspector.getByRole("status")).toHaveTextContent(i18n.t("context:neighbors.graphLoading"));
    expect(inspector.queryByText("No visible pages link here yet.")).toBeNull();
    await userEvent.click(within(await screen.findByRole("tablist", { name: "Context reader views" }))
      .getByRole("tab", { name: /^Graph, Preview$/ }));
    expect(await screen.findByTestId("graph-view")).toBeVisible();
    await act(async () => { pending.resolve({ data: { backlinks: [], outbound: [], citations: [], co_access: [] } }); });
    expect(inspector.getByText("No visible pages link here yet.")).toBeInTheDocument();
  });

  it("aborts a first graph request when the selected page changes and ignores its late completion", async () => {
    const pending = deferredValue<unknown>();
    let initialSignal: AbortSignal | null | undefined;
    const fetchMock = await renderDecisionLog({
      graphQueryHandler: (request, init) => {
        if (request.seeds?.[0]?.id === PAGE_ID) {
          initialSignal = init?.signal;
          return pending.promise;
        }
        return { data: contextGraphPayload(SECOND_PAGE_ID, "current") };
      },
    });
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    await screen.findByText("Needle body");
    await userEvent.click(within(await screen.findByRole("tablist", { name: "Context reader views" }))
      .getByRole("tab", { name: /^Graph, Preview$/ }));
    await waitFor(() => expect(initialSignal?.aborted).toBe(false));
    await userEvent.click(screen.getByRole("treeitem", { name: "Architecture Notes" }));
    expect(await screen.findByText("Selected second page")).toBeVisible();
    expect(initialSignal?.aborted).toBe(true);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/graph/query")))
      .toHaveLength(1);
    await userEvent.click(within(await screen.findByRole("tablist", { name: "Context reader views" }))
      .getByRole("tab", { name: /^Graph, Preview$/ }));
    const nextGraph = await screen.findByTestId("graph-view");
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-nodes", "1");
    await act(async () => { pending.resolve({ data: contextGraphPayload(PAGE_ID, "current") }); });
    expect(screen.getByTestId("graph-view")).toBe(nextGraph);
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-nodes", "1");
    expect(within(nextGraph).queryByText("Deployment Runbook", {
      selector: ".graph-relationship-list > details > ul > li > button strong",
    })).toBeNull();
  });

  it("discards a previous page's late Inspector response without showing its private links", async () => {
    const pending = deferredValue<unknown>();
    await renderDecisionLog({ neighborsHandler: (pageId) => pageId === PAGE_ID
      ? pending.promise
      : { data: { backlinks: [], outbound: [], citations: [], co_access: [] } } });
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    await screen.findByText("Needle body");
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const initialInspector = within(screen.getByRole("region", { name: "Context detail" }));
    await userEvent.click(initialInspector.getByRole("tab", { name: /Backlinks/ }));
    expect(initialInspector.getByRole("status")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("treeitem", { name: "Architecture Notes" }));
    await screen.findByText("Selected second page");
    const inspector = within(screen.getByRole("region", { name: "Context detail" }));
    await userEvent.click(inspector.getByRole("tab", { name: /Backlinks/ }));
    expect(await inspector.findByText("No visible pages link here yet.")).toBeInTheDocument();
    await act(async () => { pending.resolve({ data: {
      backlinks: [{ kind: "context_page", id: PAGE_ID, title: "Previous page private link", edge_kind: "wikilink" }],
      outbound: [], citations: [], co_access: [],
    } }); });
    expect(screen.queryByText("Previous page private link")).toBeNull();
    expect(inspector.queryByRole("alert")).toBeNull();
  });

  it("keeps Graph usable when the independent Inspector request fails", async () => {
    await renderDecisionLog({ neighborsHandler: () => ({ error: new Error("Neighbors failed") }) });
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    await screen.findByText("Needle body");
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const inspector = within(screen.getByRole("region", { name: "Context detail" }));
    await userEvent.click(inspector.getByRole("tab", { name: /Backlinks/ }));
    expect(await inspector.findByRole("alert")).toHaveTextContent("Neighbor details could not be loaded");
    await userEvent.click(within(await screen.findByRole("tablist", { name: "Context reader views" }))
      .getByRole("tab", { name: /^Graph, Preview$/ }));
    expect(await screen.findByTestId("graph-view")).toBeVisible();
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-nodes", "3");
    expect(inspector.getByRole("alert")).toHaveTextContent("Neighbor details could not be loaded");
  });

  it("aborts the old workspace scene and rejects its late payload even for the same page id", async () => {
    const nextWorkspaceId = SECOND_WORKSPACE_ID;
    const pending = deferredValue<unknown>();
    let initialSignal: AbortSignal | null | undefined;
    const fetchMock = mockDecisionLogRoutes({ graphQueryHandler: (_request, init) => {
      initialSignal = init?.signal;
      return pending.promise;
    } });
    const original = fetchMock.getMockImplementation()!;
    let currentWorkspaceId = WORKSPACE_ID;
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: currentWorkspaceId, workspaces: [] } });
      }
      if (url.includes(`/workspaces/${nextWorkspaceId}/`)) {
        if (url.endsWith("/graph/query")) {
          const payload = contextGraphPayload(PAGE_ID, "current");
          return jsonResponse({ data: { ...payload, nodes: payload.nodes.slice(0, 1), edges: [] } });
        }
        const response = await original(url.replace(`/workspaces/${nextWorkspaceId}/`, `/workspaces/${WORKSPACE_ID}/`), init);
        const body = await response.json() as { data?: Record<string, unknown> };
        if (body.data?.workspace_id) body.data.workspace_id = nextWorkspaceId;
        return jsonResponse(body);
      }
      return original(input, init);
    });
    ctxWorkspace.current = { ...ctxWorkspace.current, selectedId: WORKSPACE_ID };
    const content = <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=graph`]}>
      <ContextScreen />
      <Link to={`/workspaces/${nextWorkspaceId}/context/pages/${PAGE_ID}?tab=graph`}>Next workspace</Link>
    </MemoryRouter>;
    const view = render(content);
    await screen.findByRole("tablist", { name: "Context reader views" });
    await waitFor(() => expect(initialSignal?.aborted).toBe(false));
    currentWorkspaceId = nextWorkspaceId;
    ctxWorkspace.current = { ...ctxWorkspace.current, selectedId: nextWorkspaceId };
    view.rerender(content);
    await userEvent.click(screen.getByRole("link", { name: "Next workspace" }));
    const nextGraph = await screen.findByTestId("graph-view");
    expect(initialSignal?.aborted).toBe(true);
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-nodes", "1");
    await act(async () => { pending.resolve({ data: contextGraphPayload(PAGE_ID, "current") }); });
    expect(screen.getByTestId("graph-view")).toBe(nextGraph);
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-nodes", "1");
  });

  it.each([
    { change: "observed role", actorId: "actor-a", role: "viewer" },
    { change: "actor", actorId: "actor-b", role: "owner" },
  ])("discards the entire Context owner on $change change before fresh page reads settle", async ({ actorId, role }) => {
    ctxAuth.current = { user: { user_id: "actor-a" } };
    ctxWorkspace.current = {
      ...ctxWorkspace.current,
      selectedId: WORKSPACE_ID,
      selectedWorkspace: { id: WORKSPACE_ID, role: "owner", visibility: "public" },
    };
    const pendingGraph = deferredValue<unknown>();
    const pendingDetail = deferredResponse();
    let graphRequests = 0;
    let previousSignal: AbortSignal | null | undefined;
    const fetchMock = mockDecisionLogRoutes({
      bodyMarkdown: "Old authority body",
      graphQueryHandler: (request, init) => {
        graphRequests += 1;
        if (graphRequests === 2) {
          previousSignal = init?.signal;
          return pendingGraph.promise;
        }
        return { data: contextGraphPayload(PAGE_ID, "current", request.seeds) };
      },
    });
    const original = fetchMock.getMockImplementation()!;
    const detailUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`;
    const freshDetailBody = await (await original(detailUrl)).json() as {
      data: Record<string, unknown>;
    };
    freshDetailBody.data.title = "Fresh authority reader";
    freshDetailBody.data.body_md = "Fresh authority body";
    let authorityChanged = false;
    let freshDetailRequested = false;
    fetchMock.mockImplementation((input, init) => {
      if (authorityChanged && String(input) === detailUrl) {
        freshDetailRequested = true;
        return pendingDetail.promise;
      }
      return original(input, init);
    });
    const content = () => <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=graph`]}>
      <ContextScreen />
    </MemoryRouter>;
    const view = render(content());
    const oldGraph = await screen.findByTestId("graph-view");
    const oldReader = oldGraph.closest(".context-reader");
    const details = await selectGraphNodeDetails(oldGraph, "Architecture Notes");
    await userEvent.click(within(details).getByRole("button", { name: "Pin" }));
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-pinned", `node:${SECOND_PAGE_ID}`);
    await userEvent.click(within(details).getByRole("button", { name: "Expand" }));
    expect(previousSignal?.aborted).toBe(false);

    authorityChanged = true;
    ctxAuth.current = { user: { user_id: actorId } };
    ctxWorkspace.current = {
      ...ctxWorkspace.current,
      selectedWorkspace: { id: WORKSPACE_ID, role, visibility: "public" },
    };
    view.rerender(content());
    // The owner is replaced in this render, not after a fresh payload arrives.
    expect(oldReader).not.toBeInTheDocument();
    expect(screen.queryByTestId("graph-view")).toBeNull();
    expect(screen.queryByRole("tablist", { name: "Context reader views" })).toBeNull();
    expect(previousSignal?.aborted).toBe(true);
    await waitFor(() => expect(freshDetailRequested).toBe(true));
    const stalePayload = contextGraphPayload(PAGE_ID, "current");
    await act(async () => {
      pendingGraph.resolve({ data: {
        ...stalePayload,
        nodes: stalePayload.nodes.map((node) => ({ ...node, label: "Late old authority node" })),
      } });
    });
    expect(screen.queryByTestId("graph-view")).toBeNull();
    expect(screen.queryByText("Late old authority node")).toBeNull();
    expect(graphRequests).toBe(2);

    await act(async () => { pendingDetail.resolve(jsonResponse(freshDetailBody)); });
    const freshGraph = await screen.findByTestId("graph-view");
    expect(freshGraph).not.toBe(oldGraph);
    expect(screen.getByRole("heading", { name: "Fresh authority reader" })).toBeInTheDocument();
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-pinned", "");
    expect(within(freshGraph).queryByRole("complementary", { name: "Details" })).toBeNull();
    const freshDetails = await selectGraphNodeDetails(freshGraph, "Architecture Notes");
    expect(within(freshDetails).getByRole("button", { name: "Pin" })).toBeEnabled();
    expect(within(freshDetails).queryByRole("button", { name: "Unpin" })).toBeNull();
    expect(graphRequests).toBe(3);
  });

  it.each([
    { change: "observed role", actorId: "actor-a", role: "viewer" },
    { change: "actor", actorId: "actor-b", role: "owner" },
  ])("ignores a late Context page response from the previous $change owner", async ({ actorId, role }) => {
    ctxAuth.current = { user: { user_id: "actor-a" } };
    ctxWorkspace.current = {
      ...ctxWorkspace.current,
      selectedId: WORKSPACE_ID,
      selectedWorkspace: { id: WORKSPACE_ID, role: "owner", visibility: "public" },
    };
    const pendingDetail = deferredResponse();
    const fetchMock = mockDecisionLogRoutes({ bodyMarkdown: "Late old authority body" });
    const original = fetchMock.getMockImplementation()!;
    const detailUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`;
    const oldDetailBody = await (await original(detailUrl)).json() as {
      data: Record<string, unknown>;
    };
    const freshDetailBody = { data: {
      ...oldDetailBody.data,
      title: "Fresh authority reader",
      body_md: "Fresh authority body",
    } };
    let authorityChanged = false;
    let previousDetailRequested = false;
    fetchMock.mockImplementation((input, init) => {
      if (String(input) === detailUrl) {
        if (authorityChanged) return Promise.resolve(jsonResponse(freshDetailBody));
        previousDetailRequested = true;
        return pendingDetail.promise;
      }
      return original(input, init);
    });
    const content = () => <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
      <ContextScreen />
    </MemoryRouter>;
    const view = render(content());
    await waitFor(() => expect(previousDetailRequested).toBe(true));
    authorityChanged = true;
    ctxAuth.current = { user: { user_id: actorId } };
    ctxWorkspace.current = {
      ...ctxWorkspace.current,
      selectedWorkspace: { id: WORKSPACE_ID, role, visibility: "public" },
    };
    view.rerender(content());
    expect(await screen.findByText("Fresh authority body")).toBeVisible();
    const freshReader = screen.getByRole("heading", { name: "Fresh authority reader" });
    await act(async () => { pendingDetail.resolve(jsonResponse(oldDetailBody)); });
    expect(screen.getByRole("heading", { name: "Fresh authority reader" })).toBe(freshReader);
    expect(screen.queryByText("Late old authority body")).toBeNull();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/graph/query"))).toHaveLength(0);
  });

  it("retains the Context owner for metadata-only authority refreshes", async () => {
    ctxAuth.current = { user: { user_id: "actor-a" } };
    ctxWorkspace.current = {
      ...ctxWorkspace.current,
      selectedId: WORKSPACE_ID,
      selectedWorkspace: { id: WORKSPACE_ID, role: "owner", visibility: "public" },
    };
    const fetchMock = mockDecisionLogRoutes();
    const content = () => <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=graph`]}>
      <ContextScreen />
    </MemoryRouter>;
    const view = render(content());
    const graph = await screen.findByTestId("graph-view");
    const details = await selectGraphNodeDetails(graph, "Architecture Notes");
    await userEvent.click(within(details).getByRole("button", { name: "Pin" }));
    const reads = fetchMock.mock.calls.length;
    ctxAuth.current = { user: { user_id: "actor-a" } };
    ctxWorkspace.current = {
      ...ctxWorkspace.current,
      selectedWorkspace: { id: WORKSPACE_ID, role: "owner", visibility: "public", title: "Renamed workspace" },
    };
    view.rerender(content());
    expect(screen.getByTestId("graph-view")).toBe(graph);
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-pinned", `node:${SECOND_PAGE_ID}`);
    expect(within(details).getByRole("button", { name: "Unpin" })).toBeEnabled();
    expect(fetchMock.mock.calls).toHaveLength(reads);
  });

  it("renders the body-width graph from one cached query projection", async () => {
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
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    const graph = await screen.findByTestId("graph-view");
    expect(
      within(graph).getByRole("heading", { name: "Graph relationships" }),
    ).toBeInTheDocument();
    const architectureDetails = await selectGraphNodeDetails(graph, "Architecture Notes");
    expect(within(architectureDetails).getByRole("link", { name: "Open" })).toHaveAttribute(
      "href", `/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}`,
    );
    const libraryDetails = await selectGraphNodeDetails(graph, "Deployment Runbook");
    expect(
      within(libraryDetails).getByRole("link", { name: "Open" }),
    ).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/library/artifacts/artifact-1`);
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute(
      "data-nodes",
      "3",
    );
    await userEvent.click(readerTabs.getByRole("tab", { name: /View/ }));
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        String(input).includes(`/workspaces/${WORKSPACE_ID}/graph/query`),
      ),
    ).toHaveLength(1);
  });

  it("navigates Local rows without querying and hands keyboard selections to Details", async () => {
    const fetchMock = await renderDecisionLog();
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    await userEvent.click(within(await screen.findByRole("tablist", { name: "Context reader views" }))
      .getByRole("tab", { name: /^Graph, Preview$/ }));
    const graph = await screen.findByTestId("graph-view");
    const nodeList = within(graph).getByRole("list", { name: "Nodes" });
    const rows = within(nodeList).getAllByRole("button");
    expect(rows).toHaveLength(3);
    expect(rows.filter((row) => row.tabIndex === 0)).toHaveLength(1);
    expect(within(nodeList).queryByRole("link")).toBeNull();
    const queries = () => fetchMock.mock.calls.filter(([input]) =>
      String(input).endsWith(`/workspaces/${WORKSPACE_ID}/graph/query`));
    await act(async () => rows[0].focus());
    await userEvent.keyboard("{End}");
    expect(rows[2]).toHaveFocus();
    await userEvent.keyboard("{Home}{ArrowDown}");
    expect(rows[1]).toHaveFocus();
    expect(within(graph).queryByRole("complementary", { name: "Details" })).toBeNull();
    expect(queries()).toHaveLength(1);
    await userEvent.keyboard("{Enter}");
    const details = within(graph).getByRole("complementary", { name: "Details" });
    expect(details).toHaveFocus();
    expect(within(details).getByRole("heading", { level: 5 })).toHaveTextContent("Architecture Notes");
    await userEvent.tab();
    expect(within(details).getByRole("link", { name: "Open" })).toHaveFocus();
    // Re-selecting the same ID must hand off even when selection state does
    // not change and the original main-list button remains connected.
    await act(async () => rows[1].focus());
    await userEvent.keyboard(" ");
    expect(details).toHaveFocus();
    expect(queries()).toHaveLength(1);
  });

  it("requeries bounded seeds for expand and replaces the graph scene on collapse", async () => {
    const fetchMock = await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    const graph = await screen.findByTestId("graph-view");
    const relatedDetails = await selectGraphNodeDetails(graph, "Architecture Notes");
    expect(
      within(graph).getByText("Deployment Runbook", {
        selector: ".graph-relationship-list > details > ul > li > button strong",
      }),
    ).toBeInTheDocument();

    await userEvent.click(
      within(relatedDetails).getByRole("button", { name: "Expand" }),
    );
    await waitFor(() => {
      expect(
        within(relatedDetails).getByRole("button", { name: "Collapse" }),
      ).toBeEnabled();
    });
    // A multi-seed query is an actor-authorized replacement scene. This
    // fixture removes an old node after expansion to make stale-union bugs
    // visible even if the projection revision did not change.
    expect(
      within(graph).queryByText("Deployment Runbook", {
        selector: ".graph-relationship-list > details > ul > li > button strong",
      }),
    ).not.toBeInTheDocument();

    const graphCallsAfterExpand = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes(`/workspaces/${WORKSPACE_ID}/graph/query`),
    );
    expect(
      JSON.parse(String(graphCallsAfterExpand.at(-1)?.[1]?.body)),
    ).toMatchObject({
      seeds: [
        { kind: "context_page", id: PAGE_ID },
        { kind: "context_page", id: SECOND_PAGE_ID },
      ],
    });

    await userEvent.click(
      within(relatedDetails).getByRole("button", { name: "Collapse" }),
    );
    await waitFor(() => {
      expect(
        within(relatedDetails).getByRole("button", { name: "Expand" }),
      ).toBeEnabled();
    });
    const graphCallsAfterCollapse = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes(`/workspaces/${WORKSPACE_ID}/graph/query`),
    );
    expect(
      JSON.parse(String(graphCallsAfterCollapse.at(-1)?.[1]?.body)),
    ).toMatchObject({
      seeds: [{ kind: "context_page", id: PAGE_ID }],
    });
  });

  it("requests and displays the server's current Local depth instead of a client-only filter", async () => {
    const fetchMock = await renderDecisionLog();
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    const readerTabs = within(await screen.findByRole("tablist", { name: "Context reader views" }));
    await userEvent.click(readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }));
    const graph = await screen.findByTestId("graph-view");
    const graphUi = within(graph);
    expect(graphUi.getByRole("button", { name: "Depth 2" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(graphUi.getByRole("button", { name: "Depth 1" }));
    await waitFor(() => expect(graphUi.getByRole("button", { name: "Depth 1" }))
      .toHaveAttribute("aria-pressed", "true"));
    const requests = () => fetchMock.mock.calls.filter(([input]) =>
      String(input).endsWith(`/workspaces/${WORKSPACE_ID}/graph/query`),
    ).map(([, init]) => JSON.parse(String(init?.body)));
    expect(requests().at(-1)).toMatchObject({
      seeds: [{ kind: "context_page", id: PAGE_ID }],
      depth: 1, time: { mode: "current" }, budget: { nodes: 250, edges: 750 },
    });
    await userEvent.click(graphUi.getByRole("button", { name: "Depth 2" }));
    await waitFor(() => expect(graphUi.getByRole("button", { name: "Depth 2" }))
      .toHaveAttribute("aria-pressed", "true"));
    expect(requests().at(-1)).toMatchObject({ depth: 2, time: { mode: "current" } });
    await userEvent.click(readerTabs.getByRole("tab", { name: /^View$/ }));
    await userEvent.click(readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }));
    expect(requests()).toHaveLength(3);
  });

  it("rebuilds supported history queries while retaining expanded seeds and the user's current depth", async () => {
    const fetchMock = await renderDecisionLog();
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    await userEvent.click(within(await screen.findByRole("tablist", { name: "Context reader views" }))
      .getByRole("tab", { name: /^Graph, Preview$/ }));
    const graph = await screen.findByTestId("graph-view");
    const graphUi = within(graph);
    await userEvent.click(graphUi.getByRole("button", { name: "Depth 1" }));
    await waitFor(() => expect(graphUi.getByRole("button", { name: "Depth 1" }))
      .toHaveAttribute("aria-pressed", "true"));
    const relatedDetails = await selectGraphNodeDetails(graph, "Architecture Notes");
    await userEvent.click(within(relatedDetails).getByRole("button", { name: "Expand" }));
    await waitFor(() => expect(within(relatedDetails).getByRole("button", { name: "Collapse" })).toBeEnabled());
    await userEvent.selectOptions(graphUi.getByLabelText("History mode"), "appearance");
    await waitFor(() => expect(graphUi.getByRole("button", { name: "Depth 2" })).toBeDisabled());
    const requests = () => fetchMock.mock.calls.filter(([input]) =>
      String(input).endsWith(`/workspaces/${WORKSPACE_ID}/graph/query`),
    ).map(([, init]) => JSON.parse(String(init?.body)));
    const seeds = [{ kind: "context_page", id: PAGE_ID }, { kind: "context_page", id: SECOND_PAGE_ID }];
    expect(requests().at(-1)).toMatchObject({
      seeds, depth: 1, time: { mode: "appearance" }, budget: { nodes: 50, edges: 150 }, cursor: null,
    });
    await userEvent.selectOptions(graphUi.getByLabelText("History mode"), "current");
    await waitFor(() => expect(graphUi.getByRole("button", { name: "Depth 2" })).toBeEnabled());
    expect(graphUi.getByRole("button", { name: "Depth 1" })).toHaveAttribute("aria-pressed", "true");
    expect(requests().at(-1)).toMatchObject({
      seeds, depth: 1, time: { mode: "current" }, budget: { nodes: 250, edges: 750 },
    });
  });

  it("cannot replace a newer history scene when an aborted depth query settles late", async () => {
    const pendingDepth = deferredValue<{ data: ReturnType<typeof contextGraphPayload> }>();
    let depthSignal: AbortSignal | null | undefined;
    await renderDecisionLog({
      graphQueryHandler: (request, init) => {
        if (request.time?.mode === "current" && request.depth === 1) {
          depthSignal = init?.signal;
          return pendingDepth.promise;
        }
        return { data: contextGraphPayload(PAGE_ID, request.time?.mode ?? "current") };
      },
    });
    await userEvent.click(screen.getByRole("treeitem", { name: "Decision Log" }));
    await userEvent.click(within(await screen.findByRole("tablist", { name: "Context reader views" }))
      .getByRole("tab", { name: /^Graph, Preview$/ }));
    const graph = await screen.findByTestId("graph-view");
    const graphUi = within(graph);
    await userEvent.click(graphUi.getByRole("button", { name: "Depth 1" }));
    await waitFor(() => expect(depthSignal).toBeDefined());
    await userEvent.selectOptions(graphUi.getByLabelText("History mode"), "appearance");
    await waitFor(() => expect(depthSignal?.aborted).toBe(true));
    await waitFor(() => expect(graphUi.getByText("History views currently support depth 1 only."))
      .toBeInTheDocument());
    await act(async () => {
      pendingDepth.resolve({ data: contextGraphPayload(PAGE_ID, "current", undefined, undefined, 1) });
    });
    expect(graphUi.getByLabelText("History mode")).toHaveValue("appearance");
    expect(graphUi.getByRole("button", { name: "Depth 2" })).toBeDisabled();
  });

  it("keeps the newest graph action when an aborted expand settles after a history query", async () => {
    const expanded = deferredValue<{
      data: ReturnType<typeof contextGraphPayload>;
    }>();
    const appearance = deferredValue<{
      data: ReturnType<typeof contextGraphPayload>;
    }>();
    let expandSignal: AbortSignal | null | undefined;
    const fetchMock = await renderDecisionLog({
      graphQueryHandler: (request, init) => {
        if ((request.seeds?.length ?? 0) > 1) {
          expandSignal = init?.signal;
          return expanded.promise;
        }
        if (request.time?.mode === "appearance") {
          return appearance.promise;
        }
        return {
          data: contextGraphPayload(
            request.seeds?.[0]?.id ?? PAGE_ID,
            request.time?.mode ?? "appearance",
            request.seeds,
          ),
        };
      },
    });
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    const graph = await screen.findByTestId("graph-view");
    const relatedDetails = await selectGraphNodeDetails(graph, "Architecture Notes");

    await userEvent.click(
      within(relatedDetails).getByRole("button", { name: "Expand" }),
    );
    await waitFor(() => expect(expandSignal).toBeDefined());
    await userEvent.selectOptions(screen.getByLabelText("History mode"), "appearance");
    await waitFor(() => expect(expandSignal?.aborted).toBe(true));

    await act(async () => {
      appearance.resolve({ data: contextGraphPayload(PAGE_ID, "appearance") });
    });
    await waitFor(() =>
      expect(screen.getByText(/Observations/)).toBeInTheDocument(),
    );
    // History changes deliberately clear selection. Re-select the exact
    // current row before settling the aborted query, so its selected action
    // still proves the newer single-seed history scene was not overwritten.
    const historyDetails = await selectGraphNodeDetails(graph, "Architecture Notes");
    expect(within(historyDetails).getByRole("button", { name: "Expand" })).toBeEnabled();

    await act(async () => {
      expanded.resolve({
        data: contextGraphPayload(PAGE_ID, "current", [
          { kind: "context_page", id: PAGE_ID },
          { kind: "context_page", id: SECOND_PAGE_ID },
        ]),
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/Observations/)).toBeInTheDocument();
      expect(
        within(historyDetails).getByRole("button", { name: "Expand" }),
      ).toBeEnabled();
    });
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        String(input).includes(`/workspaces/${WORKSPACE_ID}/graph/query`),
      ),
    ).toHaveLength(3);
  });

  it("shows a localized generic warning for independently bounded Inspector relationship lists", async () => {
    await renderDecisionLog({ neighborsBounded: true });
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    expect(await screen.findByText("Needle body")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const inspector = within(
      screen.getByRole("region", { name: "Context detail" }),
    );
    await userEvent.click(inspector.getByRole("tab", { name: /Backlinks/ }));

    const warning = inspector.getByRole("alert");
    expect(warning).toHaveTextContent(
      "Some relationship results were limited. The lists below may be incomplete.",
    );
    expect(warning.textContent).not.toMatch(/\d/);
    expect(
      inspector.queryByText(/graph_budget_applied|private_internal_reason/),
    ).not.toBeInTheDocument();

    await act(async () => {
      await i18n.changeLanguage("ko");
    });
    expect(inspector.getByRole("alert")).toHaveTextContent(
      "일부 관계 결과가 제한되었습니다. 아래 목록은 완전하지 않을 수 있습니다.",
    );
    await act(async () => {
      await i18n.changeLanguage("en");
    });
  });

  it("exposes Context-to-Workspace Graph links only when the feature is enabled", async () => {
    workspaceGraphEnabled.current = true;
    await renderDecisionLog();
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );

    const graph = await screen.findByTestId("graph-view");
    for (const [label, focus] of [
      ["Decision Log", PAGE_ID], ["Architecture Notes", SECOND_PAGE_ID], ["Deployment Runbook", "library-1"],
    ]) {
      const details = await selectGraphNodeDetails(graph, label);
      const workspaceLinks = within(details).getAllByRole("link", { name: "Show in Knowledge Map" });
      expect(workspaceLinks).toHaveLength(1);
      expect(workspaceLinks[0]).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/graph?focus=${focus}`);
    }
  });

  it("maps canonical Library version paths to their Library-item Knowledge Map focus", async () => {
    workspaceGraphEnabled.current = true;
    await renderDecisionLog({ libraryGraphNodeKind: "library_item_version" });
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Decision Log" }),
    );
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );

    const graph = await screen.findByTestId("graph-view");
    const libraryDetails = await selectGraphNodeDetails(graph, "Deployment Runbook");
    expect(
      within(libraryDetails).getByRole("link", { name: "Show in Knowledge Map" }),
    ).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/graph?focus=library-1`);
  });

  it("does not link an unpublished current Context page to the published-only Knowledge Map", async () => {
    workspaceGraphEnabled.current = true;
    await renderDecisionLog({ unpublishedSecondPage: true });
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );
    expect(await screen.findByText("Selected second page")).toBeInTheDocument();
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );

    const graph = await screen.findByTestId("graph-view");
    const details = await selectGraphNodeDetails(graph, "Architecture Notes");
    expect(
      within(details).queryByRole("link", {
        name: "Show in Knowledge Map",
      }),
    ).toBeNull();
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
      selectedWorkspace: { id: WORKSPACE_ID, role: "owner", visibility: "private" },
      workspaceSlug: "demo",
      workspaces: [{ id: WORKSPACE_ID, role: "owner", visibility: "private" }],
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Failed to load Context")).toBeInTheDocument();
    expect(screen.queryByText(/SQLSTATE/)).toBeNull();
  });

  it("uses the shared Mermaid renderer for Context View diagrams", async () => {
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
        expect.any(HTMLElement),
      ),
    );

    expect(contextMermaid.initialize).toHaveBeenCalledTimes(1);
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

    await userEvent.click(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    );

    expect(await screen.findByText("Selected second page")).toBeInTheDocument();
    expect(screen.getAllByText("CITATION HEALTH").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("treeitem", { name: "Architecture Notes" }),
    ).toHaveAttribute("aria-selected", "true");
    const readerTabs = within(
      await screen.findByRole("tablist", { name: "Context reader views" }),
    );
    await userEvent.click(
      readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }),
    );
    expect(await screen.findByTestId("graph-view")).toBeInTheDocument();
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute(
      "data-nodes",
      "1",
    );
    await userEvent.click(readerTabs.getByRole("tab", { name: /^Activity$/ }));
    expect(
      await screen.findByText("Nothing has happened to this page yet."),
    ).toBeInTheDocument();
    const inspector = within(screen.getByRole("region", { name: "Context detail" }));
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
      `/workspaces/${WORKSPACE_ID}/context`,
    );
    expect(breadcrumb.getByRole("link", { name: "Inbox" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/context/folders/55555555-5555-4555-8555-555555555555`,
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors`]: {
        error: new Error("neighbors unavailable"),
      },
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      screen.queryByRole("heading", { name: "This item can't be opened" }),
    ).not.toBeInTheDocument();
    expect(listReads).toBeGreaterThanOrEqual(2);
  });

  it("keeps a routed Context page explicit when detail returns 404", async () => {
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      await screen.findByRole("heading", { name: "This item can't be opened" }),
    ).toBeInTheDocument();
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
    ctxWorkspace.current = {
      selectedWorkspace: { id: WORKSPACE_ID, role: "viewer", visibility: "public" },
      workspaces: [{ id: WORKSPACE_ID, role: "viewer", visibility: "public" }],
    };
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      screen.queryByRole("button", { name: "Start a new draft from this version" }),
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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

    ctxWorkspace.current = {
      selectedWorkspace: { id: WORKSPACE_ID, role: "owner", visibility: "public" },
      workspaces: [{ id: WORKSPACE_ID, role: "owner", visibility: "public" }],
    };
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
    const folderId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    let folderCreated = false;
    const createdFolder = folderNode(folderId, "Research");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`)) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({
          data: treeResponse("context", folderCreated ? [createdFolder] : []),
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes` &&
        init?.method === "POST"
      ) {
        folderCreated = true;
        return jsonResponse({ data: createdFolder });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
    ).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/context/pages/new`);

    view.unmount();
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/folders/new`]}>
        <ContextScreen />
      </MemoryRouter>,
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

  it("does not navigate back after a pending folder create resolves after workspace switch", async () => {
    const pendingCreate = deferredResponse();
    const createdFolder = folderNode(
      "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      "Research",
    );
    let createStarted = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`)) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=context`) {
        return jsonResponse({ data: treeResponse("context", []) });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes` &&
        init?.method === "POST"
      ) {
        createStarted = true;
        return pendingCreate.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/folders/new`]}>
        <Link to={`/workspaces/${SECOND_WORKSPACE_ID}/dashboard`}>
          Switch workspace
        </Link>
        <Routes>
          <Route
            path="/workspaces/:workspaceId/context/folders/new"
            element={<ContextScreen />}
          />
          <Route
            path="/workspaces/:workspaceId/dashboard"
            element={<h1>Workspace dashboard</h1>}
          />
        </Routes>
        <CurrentPathProbe />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("radio", { name: "Folder" }));
    await userEvent.type(
      await screen.findByRole("textbox", { name: "Folder name" }),
      "Research",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create folder" }));
    await waitFor(() => expect(createStarted).toBe(true));

    await userEvent.click(screen.getByRole("link", { name: "Switch workspace" }));
    expect(await screen.findByRole("heading", { name: "Workspace dashboard" })).toBeInTheDocument();
    expect(screen.getByTestId("current-path")).toHaveTextContent(
      `/workspaces/${SECOND_WORKSPACE_ID}/dashboard`,
    );

    await act(async () => {
      pendingCreate.resolve(jsonResponse({ data: createdFolder }));
      await pendingCreate.promise;
    });

    expect(screen.getByTestId("current-path")).toHaveTextContent(
      `/workspaces/${SECOND_WORKSPACE_ID}/dashboard`,
    );
    expect(screen.getByRole("heading", { name: "Workspace dashboard" })).toBeInTheDocument();
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${SECOND_PAGE_ID}`]}>
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
        if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/graph/query`) {
          return graphQueryResponse(init);
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));

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
        name: "Start a new draft from this version",
      }),
    ).toBeNull();
    expect(
      within(versionOne).getByRole("button", { name: "Start a new draft from this version" }),
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
      within(dialog).queryByRole("button", { name: "Start a new draft from this version" }),
    ).toBeNull();

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Close diff" }),
    );
    await userEvent.click(
      within(versionOne).getByRole("button", { name: "Start a new draft from this version" }),
    );

    expect(confirmSpy).toHaveBeenCalledWith(
      "Create a new draft from v1? The published version stays unchanged.",
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
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/graph/query`) {
        return graphQueryResponse(init);
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/graph/query`) {
        return graphQueryResponse(init);
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
          folderNode("66666666-6666-4666-8666-666666666666", "Workspace", { children_count: 3 }),
          {
            ...treeNode("node-1", PAGE_ID, "Decision Log"),
            parent_id: "66666666-6666-4666-8666-666666666666",
          },
          {
            ...treeNode("node-2", SECOND_PAGE_ID, "Architecture Notes"),
            parent_id: "66666666-6666-4666-8666-666666666666",
            target_meta: {
              page_type: "overview",
              lifecycle_status: "published",
            },
          },
          folderNode("77777777-7777-4777-8777-777777777777", "Archive", { parent_id: "66666666-6666-4666-8666-666666666666" }),
        ]),
      },
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
        fixtureUuid(index + 10),
        fixtureUuid(index + 20),
        `Folder page ${index + 1}`,
      ),
      parent_id: "66666666-6666-4666-8666-666666666666",
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
          folderNode("66666666-6666-4666-8666-666666666666", "Workspace", { children_count: 7 }),
          ...pageChildren,
          folderNode("77777777-7777-4777-8777-777777777777", "Archive", {
            parent_id: "66666666-6666-4666-8666-666666666666",
            sort_key: "0007",
          }),
        ]),
      },
    });
    const searches: string[] = [];
    render(
      <MemoryRouter
        initialEntries={[`/workspaces/${WORKSPACE_ID}/context/folders/66666666-6666-4666-8666-666666666666?childrenPage=2`]}
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
    expect(searches.at(-1)).toBe("?childrenPage=2");
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
          folderNode("66666666-6666-4666-8666-666666666666", "Workspace", { children_count: 3 }),
          {
            ...treeNode("node-1", PAGE_ID, "Decision Log"),
            parent_id: "66666666-6666-4666-8666-666666666666",
          },
          folderNode("88888888-8888-4888-8888-888888888888", "Archive", { parent_id: "66666666-6666-4666-8666-666666666666" }),
          folderNode("99999999-9999-4999-8999-999999999999", "Backups", { parent_id: "66666666-6666-4666-8666-666666666666" }),
        ]),
      },
    });

    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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

  it("revalidates an activated graph and Inspector after approving the same visible version", async () => {
    const responses: Record<string, MockFetchResponse> = pendingReviewFetch();
    responses[`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/rev-1/decision`] = {
      data: { id: "rev-1", status: "approved", published_version_id: "version-2", superseded_review_count: 0 },
    };
    responses[`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}/neighbors`] = {
      data: { backlinks: [], outbound: [], citations: [], co_access: [] },
    };
    let graphCalls = 0;
    responses[`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/graph/query`] = () => {
      const payload = contextGraphPayload(PAGE_ID, "current");
      graphCalls += 1;
      return { data: graphCalls === 1 ? payload : { ...payload, nodes: payload.nodes.slice(0, 1), edges: [] } };
    };
    const fetchMock = mockFetch(responses);
    render(<MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}><ContextScreen /></MemoryRouter>);
    await screen.findByText("Body");
    await userEvent.click(screen.getByRole("button", { name: "Show Details" }));
    const readerTabs = within(await screen.findByRole("tablist", { name: "Context reader views" }));
    await userEvent.click(readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }));
    await screen.findByTestId("graph-view");
    expect(screen.getByTestId("context-graph-canvas")).toHaveAttribute("data-nodes", "3");
    const approve = await screen.findByRole("button", { name: "Approve" });
    await waitFor(() => expect(approve).toBeEnabled());
    await userEvent.click(approve);
    await waitFor(() => expect(screen.getByTestId("context-graph-canvas"))
      .toHaveAttribute("data-nodes", "1"));
    expect(await screen.findByText("Version published.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith(`/pages/${PAGE_ID}/neighbors`)))
      .toHaveLength(2);
    await userEvent.click(readerTabs.getByRole("tab", { name: "View" }));
    await userEvent.click(readerTabs.getByRole("tab", { name: /^Graph, Preview$/ }));
    expect(graphCalls).toBe(2);
  });

  it("opens the pending draft comparison with the newer draft on the right in one click", async () => {
    mockFetch(pendingReviewFetch());
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      within(draftCard).queryByRole("button", { name: "Start a new draft from this version" }),
    ).toBeNull();
    expect(
      within(publishedCard).queryByRole("button", {
        name: "Start a new draft from this version",
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
        <SearchProbe onSearch={(search) => searches.push(search)} />
        <ContextScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Decision Log" }),
    );
    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
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
          `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=history&compareFrom=empty&compareTo=version-1`,
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
    const reversed = `?tab=history&compareFrom=version-2&compareTo=version-1`;
    const normalized = `?tab=history&compareFrom=version-1&compareTo=version-2`;
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}${reversed}`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=history`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
        initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=history`]}
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
        initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}?tab=history`]}
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      ).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/review/rev-1`);
      expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    },
  );

  it("shows no extra settlement copy when no earlier review will be settled", async () => {
    ctxWorkspace.current = {
      selectedWorkspace: { role: "owner", id: WORKSPACE_ID },
    };
    mockFetch(pendingReviewFetch(0));
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
    ).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/review`);
  });

  describe("P10.1 canonical route", () => {
    function renderAtCanonicalRoute(path: string) {
      return render(
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/context" element={<div>Context overview route</div>} />
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
    const folderChild = folderNode("77777777-7777-4777-8777-777777777777", "Child folder", {
      parent_id: parent.id,
    });
    const onSelectNode = vi.fn();
    render(
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context?page=99`]}>
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/context?page=not-a-page`]}>
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

function CurrentPathProbe() {
  const location = useLocation();
  return <output data-testid="current-path">{location.pathname}</output>;
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

type MockFetchResponse =
  | unknown
  | ((input: RequestInfo | URL, init?: RequestInit) => unknown);

function mockFetch(responses: Record<string, MockFetchResponse>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input.toString();
    // The context/pages list endpoint now carries pagination query params
    // (?limit=…&cursor=…); fall back to the query-stripped path so existing
    // mocks keyed on the bare endpoint still match.
    let response = responses[url] ?? responses[url.split("?")[0]];
    if (
      response === undefined &&
      url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/graph/query`
    ) {
      const request = JSON.parse(String(init?.body)) as {
        seeds?: Array<{ kind: "context_page"; id: string }>;
        time?: { mode?: "current" | "appearance" | "action" | "as_of" };
        depth?: 1 | 2;
      };
      response = {
        data: contextGraphPayload(
          request.seeds?.[0]?.id ?? PAGE_ID,
          request.time?.mode ?? "appearance",
          request.seeds,
          undefined,
          request.depth,
        ),
      };
    }
    const body =
      typeof response === "function" ? await response(input, init) : response;
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

function graphQueryResponse(init?: RequestInit) {
  const request = JSON.parse(String(init?.body)) as {
    seeds?: Array<{ kind: "context_page"; id: string }>;
    time?: { mode?: "current" | "appearance" | "action" | "as_of" };
  };
  return jsonResponse({
    data: contextGraphPayload(
      request.seeds?.[0]?.id ?? PAGE_ID,
      request.time?.mode ?? "appearance",
      request.seeds,
    ),
  });
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function deferredValue<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function contextGraphPayload(
  pageId: string,
  mode: "current" | "appearance" | "action" | "as_of",
  seeds: Array<{ kind: "context_page"; id: string }> = [
    { kind: "context_page", id: pageId },
  ],
  libraryGraphNodeKind: "library_artifact" | "library_item_version" =
    "library_artifact",
  depth: 1 | 2 = 2,
) {
  const isDecisionLog = pageId === PAGE_ID;
  const seedTitle = isDecisionLog ? "Decision Log" : "Architecture Notes";
  const seedNode = {
    id: `node:${pageId}`,
    entity_kind: "context_page",
    entity_id: pageId,
    family: "document",
    label: seedTitle,
    status: "published",
    path: isDecisionLog
      ? "/context/decision-log.md"
      : "/context/architecture-notes.md",
    is_seed: true,
    occurred_at: "2026-05-09T00:00:00Z",
    actor_label: null,
    evidence: null,
  };
  const nodes = isDecisionLog
    ? [
        seedNode,
        {
          id: `node:${SECOND_PAGE_ID}`,
          entity_kind: "context_page",
          entity_id: SECOND_PAGE_ID,
          family: "document",
          label: "Architecture Notes",
          status: "draft",
          path: "/context/architecture-notes.md",
          is_seed: false,
          occurred_at: "2026-05-09T00:01:00Z",
          actor_label: null,
          evidence: null,
        },
        {
          id: "node:library-1",
          entity_kind: libraryGraphNodeKind,
          entity_id: "artifact-1",
          family: "document",
          label: "Deployment Runbook",
          status: "published",
          path: "/library/library-1.md",
          is_seed: false,
          occurred_at: "2026-05-09T00:02:00Z",
          actor_label: null,
          evidence: null,
        },
      ]
    : [seedNode];
  const edges = isDecisionLog
    ? [
        {
          id: "edge:backlink",
          source: `node:${SECOND_PAGE_ID}`,
          target: `node:${PAGE_ID}`,
          edge_kind: "wikilink",
          action_kind: null,
          label: "linked from",
          layer: "knowledge",
          provenance: "curated",
          direction: "directed",
          weight: 1,
          occurred_at: "2026-05-09T00:01:00Z",
          sequence: 1,
          aggregate: false,
          count: null,
          window_from: null,
          window_to: null,
          evidence: null,
        },
        {
          id: "edge:citation",
          source: `node:${PAGE_ID}`,
          target: "node:library-1",
          edge_kind: "citation_source",
          action_kind: "cited",
          label: "cites source",
          layer: "evidence",
          provenance: "curated",
          direction: "directed",
          weight: 1,
          occurred_at: "2026-05-09T00:02:00Z",
          sequence: 2,
          aggregate: false,
          count: null,
          window_from: null,
          window_to: null,
          evidence: null,
        },
        {
          id: "edge:co-access",
          source: `node:${PAGE_ID}`,
          target: `node:${SECOND_PAGE_ID}`,
          edge_kind: "co_access",
          action_kind: null,
          label: "often read together",
          layer: "activity",
          provenance: "usage",
          direction: "undirected",
          weight: 2,
          occurred_at: "2026-05-09T00:03:00Z",
          sequence: 3,
          aggregate: true,
          count: 2,
          window_from: null,
          window_to: null,
          evidence: null,
        },
      ]
    : [];
  // A fresh multi-seed query can be actor-filtered more tightly than the
  // original one. The fixture deliberately models that so the screen test
  // detects accidental scene union after an expansion.
  const actorVisibleNodes = seeds.length > 1
    ? nodes.filter(({ id }) => id !== "node:library-1")
    : nodes;
  const actorVisibleNodeIds = new Set(actorVisibleNodes.map(({ id }) => id));
  const actorVisibleEdges = edges.filter(({ source, target }) =>
    actorVisibleNodeIds.has(source) && actorVisibleNodeIds.has(target),
  );
  return {
    schema_version: "dtxwiki.graph-scene.v1",
    query: {
      schema_version: "dtxwiki.graph-query.v1",
      seeds,
      scope: "local",
      preset: "all",
      layers: ["knowledge", "evidence", "activity", "action"],
      node_families: [],
      node_kinds: [],
      edge_kinds: [],
      action_kinds: [],
      direction: "both",
      depth: mode === "current" ? depth : 1,
      time: { mode },
      budget: mode === "current"
        ? { nodes: 250, edges: 750 }
        : { nodes: 50, edges: 150 },
      cursor: null,
    },
    projection_revision: 3,
    generated_at: "2026-05-10T00:00:00Z",
    nodes: actorVisibleNodes,
    edges: actorVisibleEdges,
    timeline: [],
    mermaid: isDecisionLog
      ? "flowchart LR\n  related --> seed"
      : "flowchart LR\n  seed",
    facets: {
      node_families: ["document"],
      node_kinds: isDecisionLog
        ? ["context_page", libraryGraphNodeKind]
        : ["context_page"],
      edge_kinds: isDecisionLog
        ? ["wikilink", "citation_source", "co_access"]
        : [],
      action_kinds: isDecisionLog ? ["cited"] : [],
      layers: isDecisionLog ? ["knowledge", "evidence", "activity"] : [],
      statuses: isDecisionLog ? ["published", "draft"] : ["published"],
      time_from: "2026-05-09T00:00:00Z",
      time_to: "2026-05-09T00:03:00Z",
    },
    budget: {
      requested: mode === "current"
        ? { nodes: 250, edges: 750 }
        : { nodes: 50, edges: 150 },
      applied: mode === "current"
        ? { nodes: 250, edges: 750 }
        : { nodes: 50, edges: 150 },
    },
    completeness: "complete",
    truncated: false,
    warnings: [],
    history: {
      mode,
      status: "complete",
      basis: mode === "current" ? "current_projection" : "observations",
      coverage_from: "2026-05-09T00:00:00Z",
      watermark: "2026-05-10T00:00:00Z",
      warnings: [],
    },
    capabilities: {
      timeline: true,
      expand: true,
      evidence: true,
      export: true,
      saved_view: false,
      as_of: false,
    },
  };
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
