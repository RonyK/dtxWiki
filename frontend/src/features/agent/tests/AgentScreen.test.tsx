import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createContext, useContext } from "react";
import { Link, MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";
import { AgentScreen } from "../AgentScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

// AgentScreen reads role via useOptionalWorkspaceContext to gate write
// affordances; the tests render it without a provider, so mock the role here.
const { agentWorkspace } = vi.hoisted(() => ({
  agentWorkspace: {
    current: { selectedWorkspace: { role: "owner" } } as Record<string, unknown>,
  },
}));

const DummyWorkspaceCtx = createContext<unknown>(null);

vi.mock("../../../shared/workspace/WorkspaceContext", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../../shared/workspace/WorkspaceContext")>();
  return {
    ...actual,
    // Keep real React hook semantics (calls useContext) so a Rules-of-Hooks
    // violation in AgentScreen — e.g. calling this after an early return — is
    // surfaced as a hooks-order warning rather than masked by a plain function.
    useOptionalWorkspaceContext: () => {
      useContext(DummyWorkspaceCtx);
      return agentWorkspace.current;
    },
  };
});

describe("AgentScreen", () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.localStorage.setItem("dtxwiki:workspace-layout:v2:agent:left", "true");
    agentWorkspace.current = { selectedWorkspace: { role: "owner" } };
    consoleErrorSpy = vi.spyOn(console, "error");
  });

  it("refreshes the overview and tree after leaving the reader and clicking its menu again", async () => {
    let pages = [agentPage("agent-1", "persona", "Existing item")];
    let listReads = 0;
    let treeReads = 0;
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: { data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: () => {
        listReads += 1;
        return { data: pages, total: pages.length };
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: () => {
        treeReads += 1;
        return { data: treeResponse("agent", pages.map((page) =>
          treeNode(`node-${page.id}`, page.id, page.title),
        )) };
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`]: { data: agentDetail("agent-1", "persona", "Existing item", "Selected reader body") },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: { data: [category("persona", "always")] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`]: {
        data: { versions: [], changesets: [], review_decisions: [], diffs: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/bundles`]: { data: [] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/chats?limit=50`]: { data: { items: [], next_cursor: null } },
    });
    const view = render(
      <MemoryRouter initialEntries={["/agent?target=agent-1"]}>
        <Link to="/agent">Main menu agent</Link>
        <AgentScreen />
      </MemoryRouter>,
    );
    await screen.findByText("Selected reader body");
    const main = within(view.container.querySelector(".feature-main") as HTMLElement);
    expect([listReads, treeReads]).toEqual([1, 1]);

    pages = [agentPage("fresh-1", "persona", "Fresh after reader"), ...pages];
    await userEvent.click(screen.getByRole("link", { name: "Main menu agent" }));
    await screen.findByRole("treeitem", { name: "Fresh after reader" });
    expect(await main.findByRole("button", { name: /Fresh after reader/ })).toBeInTheDocument();
    expect(screen.queryByText("Selected reader body")).not.toBeInTheDocument();
    expect([listReads, treeReads]).toEqual([2, 2]);

    pages = [agentPage("fresh-2", "persona", "Fresh while home"), ...pages];
    await userEvent.click(screen.getByRole("link", { name: "Main menu agent" }));
    await screen.findByRole("treeitem", { name: "Fresh while home" });
    expect(await main.findByRole("button", { name: /Fresh while home/ })).toBeInTheDocument();
    expect([listReads, treeReads]).toEqual([3, 3]);
  });

  it("provides a wide, long-form Agent editor and bounded reader measure", () => {
    const styles = readAppStyles();

    expect(styles).toContain(".dialog-panel.agent-editor-dialog");
    expect(styles).toContain("width: min(880px, 100%);");
    // The body field no longer reserves half the viewport: at 1440px tall that
    // pushed Sensitivity, Reason and the submit button below the fold.
    expect(styles).toContain(".agent-editor-dialog .agent-create-form textarea");
    expect(styles).not.toContain("min-height: min(52vh, 520px);");
    expect(styles).toContain(".feature-main--reader > *");
    expect(styles).toContain("width: min(100%, 1200px);");
  });

  afterEach(() => {
    // Fail if React logged a Rules-of-Hooks regression during the render.
    const hooksError = consoleErrorSpy.mock.calls.find((args: unknown[]) => {
      const msg = String(args[0]);
      return (
        msg.includes("order of Hooks") || msg.includes("Rendered more hooks")
      );
    });
    expect(hooksError, "Rules-of-Hooks violation in AgentScreen").toBeUndefined();
  });

  it("renders category summaries, page status, and selected reader detail", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [
          category("persona", "always"),
          category("skill", "task_matched"),
          category("memory", "task_matched"),
          category("agent_guide", "always"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [
          agentPage("agent-1", "persona", "Default Persona"),
          agentPage("agent-2", "agent_guide", "Default System Prompt"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", [
          treeNode("node-1", "agent-1", "Default Persona"),
          folderNode("ancestor-0", "Advanced", "advanced", null, [], false, 1),
          treeNode("node-2", "agent-2", "Default System Prompt", [
            "Advanced",
          ]),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`]: {
        data: agentDetail("agent-1", "persona", "Default Persona", "# Persona"),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`]: {
        data: {
          // Two versions: the active one (which offers no Restore, since that
          // would act on the version already being read) and a superseded one
          // that does. A single-version timeline would make the editor/viewer
          // restore assertion below vacuous.
          versions: [
            {
              version_id: "version-1",
              version_no: 1,
              version_status: "active_snapshot",
              actor_type: "user",
              created_at: "2026-05-09T00:00:00Z",
              is_current: true,
            },
            {
              version_id: "version-0",
              version_no: 0,
              version_status: "superseded_snapshot",
              actor_type: "user",
              created_at: "2026-05-08T00:00:00Z",
              is_current: false,
            },
          ],
          changesets: [],
          review_decisions: [],
          diffs: null,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/bundles`]: {
        data: [
          {
            bundle_id: "bundle-12345678",
            status: "used",
            created_at: "2026-05-09T00:00:00Z",
            valid_until: "2026-05-09T01:00:00Z",
            used_at: "2026-05-09T00:10:00Z",
            expired_at: null,
            included: true,
          },
          {
            bundle_id: "bundle-expired",
            status: "expired",
            created_at: "2026-05-08T00:00:00Z",
            valid_until: "2026-05-08T01:00:00Z",
            used_at: "2026-05-08T00:10:00Z",
            expired_at: "2026-05-08T01:00:00Z",
            included: false,
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-2?version=active`]: {
        data: agentDetail(
          "agent-2",
          "agent_guide",
          "Default System Prompt",
          "# System",
        ),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-2`]: {
        data: {
          versions: [
            {
              version_id: "version-1",
              version_no: 1,
              version_status: "active_snapshot",
              actor_type: "user",
              created_at: "2026-05-09T00:00:00Z",
              is_current: true,
            },
          ],
          changesets: [],
          review_decisions: [],
          diffs: null,
        },
      },
    });

    render(
      <MemoryRouter>
        <AgentScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Agent pages" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Select an Agent page from the tree or table/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Agent pages store instructions, skills, and memory/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/not injected into prompts automatically/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Injection settings" }))
      .toHaveAttribute("href", "/settings?section=agent-injection");
    expect(screen.getAllByText("Default Persona").length).toBeGreaterThan(1);
    expect(screen.getAllByText("Default System Prompt")).toHaveLength(2);
    // UI-10: enum tokens are mapped through i18n labels, not shown raw.
    expect(screen.getAllByText("Agent guide").length).toBeGreaterThan(0);
    expect(screen.queryByText("agent_guide")).toBeNull();
    expect(screen.getAllByText("Always").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Task matched").length).toBeGreaterThan(0);
    expect(screen.queryByText("task_matched")).toBeNull();
    // M-21 (260908 UI audit): the injection-policy phrases and the Sensitive
    // badge carried no explanation of what they mean -- each now has a title.
    expect(screen.getAllByText("Always")[0]).toHaveAttribute(
      "title",
      expect.stringContaining("included in AI bundles"),
    );
    const sensitiveBadge = screen.getAllByText("Sensitive")[0].closest("span");
    expect(sensitiveBadge).toHaveAttribute(
      "title",
      expect.stringContaining("only workspace owners"),
    );

    await userEvent.click(
      screen.getByRole("treeitem", { name: "Default Persona" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Persona" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Bundle" })).toBeEnabled();
    await userEvent.click(screen.getByRole("tab", { name: "Bundle" }));
    expect(
      await screen.findByText("Bundle bundle-12345678"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Page included/)).toBeInTheDocument();
    expect(screen.getByText(/Page omitted/)).toBeInTheDocument();
    expect(screen.getAllByText(/Expires/)).toHaveLength(2);
    expect(screen.getByText(/^used$/i)).toBeInTheDocument();
    expect(screen.getByText(/^expired$/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Activity" }));
    expect(
      await screen.findByText(/No per-page activity stream yet/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: /History/ }));
    // One actor line per version card now that the timeline has two.
    expect(screen.getAllByText("Human editor").length).toBeGreaterThan(0);
    expect(screen.queryByText("# Persona")).not.toBeInTheDocument();
    // An editor (owner) sees the restore affordance in History.
    expect(
      screen.getByRole("button", { name: "Restore this version" }),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("treeitem", { name: "Default System Prompt" }),
    );

    expect(
      await screen.findByRole("heading", { name: "System" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("treeitem", { name: "Default System Prompt" }),
    ).toHaveAttribute("aria-selected", "true");
    const breadcrumb = within(screen.getByLabelText("Breadcrumb"));
    expect(breadcrumb.getByRole("link", { name: "Agent" }))
      .toHaveAttribute("href", "/agent");
    expect(breadcrumb.getByRole("link", { name: "Advanced" }))
      .toHaveAttribute("href", "/agent?target=ancestor-0");

    await userEvent.click(breadcrumb.getByRole("link", { name: "Advanced" }));

    expect(await screen.findByRole("heading", { name: "Advanced" }))
      .toBeInTheDocument();
    expect(screen.queryByText("# System")).not.toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: "Advanced" }))
      .toHaveAttribute("aria-selected", "true");
  });

  it("does not repeat the empty state a sixth time when no pages exist (M-20)", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [category("persona", "always")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", []),
      },
    });

    render(
      <MemoryRouter>
        <AgentScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Agent pages" });
    expect((await screen.findAllByText("0 pages")).length).toBeGreaterThan(0);
    expect(screen.queryByText("No Agent pages yet")).not.toBeInTheDocument();
  });

  it("appends the next page of agent pages via Load more (A8)", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [category("persona", "always")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [agentPage("agent-1", "persona", "First Persona")],
        next_cursor: "cursor-2",
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages?cursor=cursor-2`]: {
        data: [agentPage("agent-2", "persona", "Older Persona")],
        next_cursor: null,
        has_more: false,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", []),
      },
    });

    render(
      <MemoryRouter>
        <AgentScreen />
      </MemoryRouter>,
    );

    // First page only: the older page is not loaded yet.
    await screen.findByRole("heading", { name: "Agent pages" });
    expect(screen.queryByText("Older Persona")).not.toBeInTheDocument();

    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));

    // The cursor page is appended and the exhausted control disappears.
    expect((await screen.findAllByText("Older Persona")).length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
  });

  it("removes a recently deleted Agent page when detail returns 404", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`
      ) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`
      ) {
        return jsonResponse({
          data: [agentPage("agent-1", "persona", "Deleted elsewhere")],
          next_cursor: null,
          total: 1,
          has_more: false,
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`
      ) {
        return jsonResponse({
          data: treeResponse("agent", [
            treeNode("node-1", "agent-1", "Deleted elsewhere"),
          ]),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`
      ) {
        return jsonResponse({
          data: {
            versions: [
              {
                version_id: "version-1",
                version_no: 1,
                version_status: "active_snapshot",
                actor_type: "user",
                created_at: "2026-05-09T00:00:00Z",
                is_current: true,
              },
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`
      ) {
        return jsonResponse(
          {
            error_code: "not_found",
            message: "agent page not found",
            request_id: "request-agent-deleted-elsewhere",
            warnings: [],
          },
          404,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <AgentScreen />
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
        "That Agent page is no longer available. It was removed from the list.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Deleted elsewhere")).not.toBeInTheDocument();
  });

  it("resolves a tree node whose page is past the flat-list cap by reading it by id", async () => {
    // The flat agent/pages list is capped and only returns agent-1; the tree is
    // unbounded and also exposes node-60 -> agent-60. Selecting node-60 must
    // read the detail by target_id (active selector) instead of being a no-op.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [category("persona", "always")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [agentPage("agent-1", "persona", "Default Persona")],
        next_cursor: "cursor-page-2",
        total: 60,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", [
          treeNode("node-1", "agent-1", "Default Persona"),
          treeNode("node-60", "agent-60", "Hidden Persona"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-60?version=active`]: {
        data: agentDetail("agent-60", "persona", "Hidden Persona", "# Hidden"),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-60`]: {
        data: {
          versions: [agentVersion("version-1", 1, "active_snapshot")],
          changesets: [],
          review_decisions: [],
          diffs: null,
        },
      },
    });

    render(
      <MemoryRouter>
        <AgentScreen />
      </MemoryRouter>,
    );

    // The page is not in the flat list, so the row only exists in the tree.
    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Hidden Persona" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Hidden" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("treeitem", { name: "Hidden Persona" }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("deep-links to a page past the flat-list cap via the tree node target", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [category("persona", "always")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [agentPage("agent-1", "persona", "Default Persona")],
        next_cursor: "cursor-page-2",
        total: 60,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", [
          treeNode("node-1", "agent-1", "Default Persona"),
          treeNode("node-60", "agent-60", "Hidden Persona"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-60?version=active`]: {
        data: agentDetail("agent-60", "persona", "Hidden Persona", "# Hidden"),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-60`]: {
        data: {
          versions: [agentVersion("version-1", 1, "active_snapshot")],
          changesets: [],
          review_decisions: [],
          diffs: null,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=node-60"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Hidden" }),
    ).toBeInTheDocument();
  });

  it("falls back to the draft for an inactive page past the flat-list cap", async () => {
    const activeWithoutVersion = {
      ...agentDetail("agent-60", "persona", "Hidden Draft Persona", ""),
      lifecycle_status: "draft",
      current_active_version_id: null,
      latest_draft_version_id: "draft-version-60",
      body_markdown: null,
      version_info: null,
    };
    const draftDetail = {
      ...agentDetail(
        "agent-60",
        "persona",
        "Hidden Draft Persona",
        "# Draft-only Agent body",
      ),
      lifecycle_status: "draft",
      current_active_version_id: null,
      latest_draft_version_id: "draft-version-60",
      version_info: {
        ...agentDetail("agent-60", "persona", "Hidden Draft Persona", "")
          .version_info,
        version_id: "draft-version-60",
        version_status: "draft",
      },
    };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [category("persona", "always")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [agentPage("agent-1", "persona", "Default Persona")],
        next_cursor: "cursor-page-2",
        total: 60,
        has_more: true,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", [
          treeNode("node-1", "agent-1", "Default Persona"),
          treeNode("node-60", "agent-60", "Hidden Draft Persona"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-60?version=active`]: {
        data: activeWithoutVersion,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-60?version=draft`]: {
        data: draftDetail,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-60`]: {
        data: {
          versions: [agentVersion("draft-version-60", 1, "draft")],
          changesets: [],
          review_decisions: [],
          diffs: null,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=node-60"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Draft-only Agent body" }),
    ).toBeInTheDocument();
  });

  it("hides create and edit affordances for a viewer", async () => {
    agentWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [category("persona", "always")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [agentPage("agent-1", "persona", "Default Persona")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", [
          treeNode("node-1", "agent-1", "Default Persona"),
        ]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`]: {
        data: agentDetail("agent-1", "persona", "Default Persona", "# Persona"),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`]: {
        data: {
          versions: [agentVersion("version-1", 1, "active_snapshot")],
          changesets: [],
          review_decisions: [],
          diffs: null,
        },
      },
    });

    render(
      <MemoryRouter>
        <AgentScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Agent pages" });
    // category card renders as a static tile, not a create button, for a viewer
    expect(
      screen.queryByRole("button", { name: "Create Persona Agent page" }),
    ).toBeNull();

    await userEvent.click(
      screen.getByRole("treeitem", { name: "Default Persona" }),
    );
    await screen.findByRole("heading", { name: "Persona" });
    // Draft-writing actions are hidden for viewers.
    expect(screen.queryByRole("link", { name: "Create draft" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Edit pending draft" })).toBeNull();
    // and the History tab's restore (a write) is hidden for viewers (#2)
    await userEvent.click(screen.getByRole("tab", { name: /History/ }));
    expect(
      screen.queryByRole("button", { name: "Restore this version" }),
    ).toBeNull();
  });

  it("retries the detail load when the same node is clicked again after a failure", async () => {
    let detailCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({
          data: [agentPage("agent-1", "persona", "Default Persona")],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse("agent", [
            treeNode("node-1", "agent-1", "Default Persona"),
          ]),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`
      ) {
        detailCalls += 1;
        if (detailCalls === 1) {
          return jsonResponse(
            {
              error_code: "internal_error",
              message: "Agent detail unavailable",
              request_id: "req-test",
              warnings: [],
            },
            500,
          );
        }
        return jsonResponse({
          data: agentDetail("agent-1", "persona", "Default Persona", "# Persona"),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`
      ) {
        return jsonResponse({
          data: { versions: [], changesets: [], review_decisions: [], diffs: null },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    await userEvent.click(
      await screen.findByRole("treeitem", { name: "Default Persona" }),
    );
    expect(
      await screen.findByText(
        "The server could not complete the request. Please try again.",
      ),
    ).toBeInTheDocument();

    // Re-selecting the SAME node must retry the failed load (the route param
    // and state are unchanged, so the failure has to be tracked explicitly).
    await userEvent.click(
      screen.getByRole("treeitem", { name: "Default Persona" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Persona" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Agent detail unavailable")).toBeNull();
    expect(detailCalls).toBe(2);
  });

  it("marks manual tree refresh busy and announces completion", async () => {
    const treeRefresh = deferredResponse();
    let treeCallCount = 0;
    let pageCallCount = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        pageCallCount += 1;
        return jsonResponse({ data: [agentPage("agent-1", "persona", "Default Persona")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        treeCallCount += 1;
        if (treeCallCount === 1) {
          return jsonResponse({
            data: treeResponse("agent", [treeNode("node-1", "agent-1", "Default Persona")]),
          });
        }
        if (treeCallCount === 3) {
          throw new Error("raw req-secret");
        }
        return treeRefresh.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent"]}>
        <AgentNavigationProbe />
        <AgentScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Agent pages" });
    const originalLocationKey = screen.getByTestId("agent-navigation-key").textContent;
    // Refresh is a direct rail-header button now (no "⋯" overflow menu).
    await userEvent.click(
      screen.getByRole("button", { name: "Refresh Agent tree" }),
    );

    const refreshingButton = screen.getByRole("button", {
      name: "Refreshing Agent tree…",
    });
    expect(refreshingButton).toHaveAttribute("aria-busy", "true");
    expect(refreshingButton).toBeDisabled();

    treeRefresh.resolve(jsonResponse({
      data: treeResponse("agent", [treeNode("node-1", "agent-1", "Default Persona")]),
    }));

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Agent tree refreshed");
    });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Refresh Agent tree" }),
      ).toBeEnabled();
    });

    expect(pageCallCount).toBe(2);
    expect(treeCallCount).toBe(2);
    expect(screen.getByTestId("agent-navigation-key")).toHaveTextContent(originalLocationKey!);

    await userEvent.click(
      screen.getByRole("button", { name: "Refresh Agent tree" }),
    );
    expect(
      await screen.findByText("Failed to refresh Agent tree"),
    ).toBeInTheDocument();
    expect(screen.queryByText("raw req-secret")).toBeNull();
  });

  it("preserves a manual collapse of the selected path across tree refresh", async () => {
    const nestedTree = [
      folderNode(
        "folder-advanced",
        "Advanced",
        "advanced",
        null,
        [],
        false,
        1,
      ),
      treeNode(
        "node-2",
        "agent-2",
        "Default System Prompt",
        ["Advanced"],
        "folder-advanced",
      ),
    ];
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [category("agent_guide", "always")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [
          agentPage("agent-2", "agent_guide", "Default System Prompt"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", nestedTree),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-2?version=active`]: {
        data: agentDetail(
          "agent-2",
          "agent_guide",
          "Default System Prompt",
          "# System",
        ),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-2`]: {
        data: {
          versions: [agentVersion("version-1", 1, "active_snapshot")],
          changesets: [],
          review_decisions: [],
          diffs: null,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=node-2"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "System" });
    expect(
      screen.getByRole("treeitem", { name: "Default System Prompt" }),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Collapse Advanced" }),
    );
    expect(
      screen.queryByRole("treeitem", { name: "Default System Prompt" }),
    ).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Refresh Agent tree" }),
    );

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Expand Advanced" }),
      ).toBeEnabled();
    });
    expect(
      screen.queryByRole("treeitem", { name: "Default System Prompt" }),
    ).not.toBeInTheDocument();
  });

  it("creates an Agent draft from a category card", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({
          data: [
            category("persona", "always"),
            category("skill", "task_matched"),
            category("memory", "task_matched"),
          ],
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: {
            agent_page_id: "agent-new",
            draft_version_id: "version-new",
            change_set_id: "change-new",
            review_item_id: null,
            next_action: "owner_activation_required",
          },
        }, 201);
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        const created = fetchMock.mock.calls.some(
          (call) =>
            call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages` &&
            call[1]?.method === "POST",
        );
        return jsonResponse({
          data: created ? [agentPage("agent-new", "persona", "Research Persona")] : [],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        const created = fetchMock.mock.calls.some(
          (call) =>
            call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages` &&
            call[1]?.method === "POST",
        );
        return jsonResponse({
          data: treeResponse(
            "agent",
            created ? [treeNode("node-new", "agent-new", "Research Persona")] : [],
          ),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-new?version=draft`) {
        return jsonResponse({
          data: agentDetail("agent-new", "persona", "Research Persona", "# Research Persona"),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-new`
      ) {
        return jsonResponse({ data: { versions: [], changesets: [], review_decisions: [], diffs: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Agent pages" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create Persona Agent page" }));

    const dialog = await screen.findByRole("dialog", { name: "Create Agent draft" });
    const closeButton = within(dialog).getByRole("button", {
      name: "Close agent dialog",
    });
    expect(closeButton).toHaveTextContent("");
    expect(within(dialog).queryByRole("button", { name: "Close" }))
      .not.toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText("Title"), "Research Persona");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create draft" }));

    await screen.findByRole("heading", { name: "Research Persona" });
    const postCall = fetchMock.mock.calls.find(
      (call) =>
        call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages` &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
      category: "persona",
      slug: "research-persona",
      title: "Research Persona",
      sensitivity: "workspace",
    });
  });

  it("offers the Karpathy template under Agent guide and keeps the body across a category change", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [
          category("agent_guide", "always"),
          category("persona", "default_on"),
          category("skill", "task_matched"),
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: { data: [] },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", []),
      },
    });

    render(
      <MemoryRouter initialEntries={["/agent"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Agent pages" });
    await userEvent.click(
      screen.getByRole("button", { name: "Create Agent guide Agent page" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Create Agent draft" });

    const body = within(dialog).getByLabelText("Body") as HTMLTextAreaElement;
    // F-10: category defaults to agent_guide → the Karpathy template is the
    // default-generated prompt, so the body starts with its guidelines.
    expect(body.value).toContain("Think Before Coding");
    expect(within(dialog).getByText(/Karpathy-inspired guardrails/)).toBeInTheDocument();

    // Switch to Skill (no templates): the picker hides and the body is kept (guard).
    await userEvent.selectOptions(
      within(dialog).getByLabelText("Category"),
      "skill",
    );
    expect(within(dialog).queryByLabelText("Template")).toBeNull();
    expect(
      (within(dialog).getByLabelText("Body") as HTMLTextAreaElement).value,
    ).toContain("Think Before Coding");
  });

  it("creates an Agent page under the selected tree folder", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      const created = fetchMock.mock.calls.some(
        (call) =>
          call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages` &&
          call[1]?.method === "POST",
      );
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: {
            agent_page_id: "agent-nested",
            draft_version_id: "version-nested",
            change_set_id: "change-nested",
            review_item_id: null,
            next_action: "owner_activation_required",
          },
        }, 201);
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({
          data: created ? [agentPage("agent-nested", "persona", "Nested Persona")] : [],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse(
            "agent",
            created
              ? [
                  folderNode("node-persona", "Persona", "persona", null, [], true, 1),
                  treeNode("node-nested", "agent-nested", "Nested Persona", ["Persona"], "node-persona"),
                ]
              : [folderNode("node-persona", "Persona", "persona", null, [], true, 0)],
          ),
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-nested?version=draft`
      ) {
        return jsonResponse({
          data: agentDetail("agent-nested", "persona", "Nested Persona", "# Nested Persona"),
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-nested`
      ) {
        return jsonResponse({ data: { versions: [], changesets: [], review_decisions: [], diffs: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Create item under Persona" }));
    // F-11: an Agent page opens the dedicated "New Agent page" editor directly,
    // not the generic item dialog.
    const dialog = await screen.findByRole("dialog", { name: "Create Agent draft" });
    await userEvent.type(within(dialog).getByLabelText("Title"), "Nested Persona");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create draft" }));

    await screen.findByRole("heading", { name: "Nested Persona" });
    const postCall = fetchMock.mock.calls.find(
      (call) =>
        call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages` &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
      category: "persona",
      parent_node_id: "node-persona",
      slug: "nested-persona",
      title: "Nested Persona",
    });
  });

  it("updates the selected Agent page from the Edit draft action", async () => {
    let updated = false;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({
          data: [
            {
              ...agentPage("agent-1", "persona", "Default Persona"),
              latest_draft_version_id: updated ? "version-2" : null,
            },
          ],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse("agent", [treeNode("node-1", "agent-1", "Default Persona")]),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`) {
        return jsonResponse({
          data: agentDetail("agent-1", "persona", "Default Persona", "# Persona"),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=draft`) {
        return jsonResponse({
          data: {
            ...agentDetail("agent-1", "persona", "Default Persona", "# Persona\n\nEdited"),
            latest_draft_version_id: "version-2",
            version_info: {
              ...agentDetail("agent-1", "persona", "Default Persona", "").version_info,
              version_id: "version-2",
              version_no: 2,
              version_status: "draft",
            },
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/draft` &&
        init?.method === "POST"
      ) {
        updated = true;
        return jsonResponse({
          data: {
            agent_page_id: "agent-1",
            draft_version_id: "version-2",
            change_set_id: "change-edit",
            review_item_id: "review-agent-1",
            next_action: "review_required",
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`
      ) {
        return jsonResponse({ data: { versions: [], changesets: [], review_decisions: [], diffs: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=agent-1"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Default Persona" });
    const editLink = screen.getByRole("link", { name: "Create draft" });
    expect(editLink).toHaveAttribute("href", "/agent?target=agent-1&edit=agent-1");
    expect(editLink.closest(".detail-heading-actions")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Show Explorer" })).toBeNull();
    expect(screen.getByRole("treeitem", { name: "Default Persona" })).toBeVisible();
    await userEvent.click(editLink);

    const dialog = await screen.findByRole("dialog", { name: "Edit Agent draft" });
    await userEvent.clear(within(dialog).getByLabelText("Body"));
    await userEvent.type(within(dialog).getByLabelText("Body"), "# Persona\n\nEdited");
    await userEvent.click(within(dialog).getByRole("button", { name: "Update draft" }));

    await screen.findByText(/Edited/);
    expect(await screen.findByText("Draft saved and sent to Review."))
      .toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(
      (call) =>
        call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/draft` &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
      base_version_id: "version-1",
      body_markdown: "# Persona\n\nEdited",
      category: "persona",
      slug: "default-persona",
      title: "Default Persona",
    });
  });

  it("activates an owner-approved pending Agent draft from the reader", async () => {
    let activated = false;
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({
          data: [
            {
              ...agentPage("agent-1", "persona", "Default Persona"),
              current_active_version_id: activated ? "version-2" : "version-1",
              latest_draft_version_id: activated ? null : "version-2",
            },
          ],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse("agent", [treeNode("node-1", "agent-1", "Default Persona")]),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`) {
        return jsonResponse({
          data: {
            ...agentDetail("agent-1", "persona", "Default Persona", "# Persona"),
            latest_draft_version_id: "version-2",
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/activate` &&
        init?.method === "POST"
      ) {
        activated = true;
        return jsonResponse({
          data: {
            ...agentDetail("agent-1", "persona", "Default Persona", "# Persona\n\nActive"),
            current_active_version_id: "version-2",
            latest_draft_version_id: null,
            lifecycle_status: "active",
            version_info: {
              ...agentDetail("agent-1", "persona", "Default Persona", "").version_info,
              version_id: "version-2",
              version_no: 2,
              version_status: "active_snapshot",
            },
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`
      ) {
        return jsonResponse({
          data: {
            versions: [
              agentVersion("version-2", 2, "draft"),
              agentVersion("version-1", 1, "active_snapshot"),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=agent-1"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Viewing active v1")).toBeInTheDocument();
    expect(await screen.findByText("Pending draft v2")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Activate draft" }));

    expect(confirmSpy).toHaveBeenCalledWith(
      "Activate the pending draft for Default Persona?",
    );
    expect(await screen.findByText("Default Persona is now active.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Activate draft" })).toBeNull();
    const activationCall = fetchMock.mock.calls.find(
      (call) =>
        call[0] ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/activate` &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(activationCall?.[1]?.body))).toEqual({
      reason: "Activate Agent draft from the Agent reader",
      version_id: "version-2",
    });
  });

  it("prefills the edit modal with the pending draft body when one exists", async () => {
    const firstDraftLoad = deferredResponse();
    let draftAttempts = 0;
    const draftDetail = {
      ...agentDetail("agent-1", "persona", "Default Persona", "# Persona\n\nDraft body"),
      latest_draft_version_id: "version-2",
      version_info: {
        ...agentDetail("agent-1", "persona", "Default Persona", "").version_info,
        version_id: "version-2",
        version_no: 2,
        version_status: "draft",
      },
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({
          data: [
            {
              ...agentPage("agent-1", "persona", "Default Persona"),
              latest_draft_version_id: "version-2",
            },
          ],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse("agent", [treeNode("node-1", "agent-1", "Default Persona")]),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`) {
        return jsonResponse({
          data: {
            ...agentDetail("agent-1", "persona", "Default Persona", "# Persona\n\nActive body"),
            latest_draft_version_id: "version-2",
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=draft`) {
        draftAttempts += 1;
        if (draftAttempts === 1) {
          return firstDraftLoad.promise;
        }
        return jsonResponse({ data: draftDetail });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/draft` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: {
            agent_page_id: "agent-1",
            draft_version_id: "version-2",
            change_set_id: "change-edit",
            review_item_id: null,
            next_action: "owner_activation_required",
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`
      ) {
        return jsonResponse({ data: { versions: [], changesets: [], review_decisions: [], diffs: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=agent-1"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Default Persona" });
    expect(screen.getByText("Active body")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Edit pending draft" }));

    const loadingDialog = await screen.findByRole("dialog", {
      name: "Loading pending draft",
    });
    expect(
      within(loadingDialog).getByText("Loading the latest pending draft…"),
    ).toHaveAttribute("aria-busy", "true");
    firstDraftLoad.resolve(
      jsonResponse(
        {
          error_code: "service_unavailable",
          message: "Draft unavailable",
          request_id: "req-draft",
          warnings: [],
        },
        503,
      ),
    );
    await userEvent.click(await within(loadingDialog).findByRole("button", {
      name: "Retry",
    }));

    // The modal waits for the pending draft read so the body it shows matches
    // the base_version_id it submits (not the active body).
    const dialog = await screen.findByRole("dialog", { name: "Edit Agent draft" });
    expect(within(dialog).getByLabelText("Body")).toHaveValue("# Persona\n\nDraft body");
    expect(draftAttempts).toBe(2);
    await userEvent.click(within(dialog).getByRole("button", { name: "Update draft" }));

    await screen.findByText(/Owner activation is required/);
    const postCall = fetchMock.mock.calls.find(
      (call) =>
        call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/draft` &&
        call[1]?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
      base_version_id: "version-2",
      body_markdown: "# Persona\n\nDraft body",
    });
  });

  it("supports Agent tree rename, move, and delete from the context menu", async () => {
    let folderTitle = "Research Agents";
    let deleted = false;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse(
            "agent",
            deleted
              ? [folderNode("node-persona", "Persona", "persona", null, [], true, 0)]
              : [
                  folderNode("node-persona", "Persona", "persona", null, [], true, 1),
                  folderNode(
                    "node-folder",
                    folderTitle,
                    "research-agents",
                    "node-persona",
                    ["Persona"],
                    false,
                    0,
                  ),
                  folderNode("node-archive", "Archive", "archive", null, [], false, 0),
                ],
          ),
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes/node-folder` &&
        init?.method === "PATCH"
      ) {
        folderTitle = "Research Unit";
        return jsonResponse({
          data: folderNode("node-folder", folderTitle, "research-agents", "node-persona", ["Persona"]),
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/move` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: folderNode("node-folder", folderTitle, "research-agents", "node-persona", ["Persona"]),
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes/node-folder` &&
        init?.method === "DELETE"
      ) {
        deleted = true;
        return jsonResponse({
          data: folderNode("node-folder", folderTitle, "research-agents", "node-persona", ["Persona"]),
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Expand Persona" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open actions for Research Agents" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const renameDialog = await screen.findByRole("dialog", { name: "Rename" });
    await userEvent.clear(within(renameDialog).getByLabelText("Display name"));
    await userEvent.type(within(renameDialog).getByLabelText("Display name"), "Research Unit");
    await userEvent.click(within(renameDialog).getByRole("button", { name: "Rename" }));

    await screen.findByRole("button", { name: "Open actions for Research Unit" });
    await userEvent.click(screen.getByRole("button", { name: "Open actions for Research Unit" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Move" }));
    const moveDialog = await screen.findByRole("dialog", { name: "Move Research Unit" });
    await userEvent.type(
      within(moveDialog).getByLabelText(/Reason/),
      "Reorder agent folders",
    );
    // The current parent is preselected; submitting it would silently reassign
    // the sort key, so the dialog keeps Move disabled until a new parent is picked.
    expect(within(moveDialog).getByRole("button", { name: "Move" })).toBeDisabled();
    await userEvent.click(within(moveDialog).getByRole("button", { name: "Target parent" }));
    await userEvent.click(
      within(moveDialog).getByRole("treeitem", { name: /Archive/ }),
    );
    await userEvent.click(within(moveDialog).getByRole("button", { name: "Move" }));

    await screen.findByRole("button", { name: "Open actions for Research Unit" });
    await userEvent.click(screen.getByRole("button", { name: "Open actions for Research Unit" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    const deleteDialog = await screen.findByRole("dialog", { name: "Delete Research Unit" });
    await userEvent.click(within(deleteDialog).getByRole("button", { name: "Delete" }));

    await screen.findByRole("button", { name: "Create item under Persona" });
    expect(fetchMock.mock.calls.some(
      (call) =>
        call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes/node-folder` &&
        call[1]?.method === "PATCH" &&
        JSON.parse(String(call[1]?.body)).display_name === "Research Unit",
    )).toBe(true);
    expect(fetchMock.mock.calls.some(
      (call) =>
        call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/move` &&
        call[1]?.method === "POST" &&
        JSON.parse(String(call[1]?.body)).reason === "Reorder agent folders" &&
        JSON.parse(String(call[1]?.body)).new_parent_id === "node-archive",
    )).toBe(true);
    expect(fetchMock.mock.calls.some(
      (call) =>
        call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree/nodes/node-folder` &&
        call[1]?.method === "DELETE",
    )).toBe(true);
    expect(
      fetchMock.mock.calls.filter(
        (call) =>
          call[0] ===
          `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`,
      ),
    ).toHaveLength(2);
  });

  it("shows a version's Changes since the previous version and holds one restore from the diff modal", async () => {
    let restored = false;
    const restoreResponse = deferredResponse();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({
          data: [
            {
              ...agentPage("agent-1", "persona", "Default Persona"),
              current_active_version_id: "version-3",
              latest_draft_version_id: restored ? "version-4" : null,
            },
          ],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse("agent", [treeNode("node-1", "agent-1", "Default Persona")]),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`) {
        return jsonResponse({
          data: {
            ...agentDetail("agent-1", "persona", "Default Persona", "# Persona\n\nCurrent"),
            current_active_version_id: "version-3",
            version_info: {
              ...agentDetail("agent-1", "persona", "Default Persona", "").version_info,
              version_id: "version-3",
              version_no: 3,
            },
          },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=draft`) {
        return jsonResponse({
          data: {
            ...agentDetail("agent-1", "persona", "Default Persona", "# Persona\n\nOriginal"),
            current_active_version_id: "version-3",
            latest_draft_version_id: "version-4",
            version_info: {
              ...agentDetail("agent-1", "persona", "Default Persona", "").version_info,
              version_id: "version-4",
              version_no: 4,
              version_status: "draft",
            },
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`
      ) {
        return jsonResponse({
          data: {
            versions: [
              ...(restored ? [agentVersion("version-4", 4, "draft")] : []),
              agentVersion("version-3", 3, "active_snapshot"),
              agentVersion("version-2", 2, "superseded_snapshot"),
              agentVersion("version-1", 1, "superseded_snapshot"),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/diff?from_version_id=version-1&to_version_id=version-2`
      ) {
        return jsonResponse({
          data: {
            from_version_id: "version-1",
            to_version_id: "version-2",
            from_version_no: 1,
            to_version_no: 2,
            lines: ["--- v1", "+++ v2", "@@ -1 +1 @@", "-Original", "+Current"],
            additions: 1,
            deletions: 1,
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/restore` &&
        init?.method === "POST"
      ) {
        restored = true;
        return restoreResponse.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter>
        <AgentScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("treeitem", { name: "Default Persona" }));
    await userEvent.click(screen.getByRole("tab", { name: /History/ }));

    const historyPanel = screen.getByRole("tabpanel", { name: /History/ });
    const versionTwo = within(historyPanel).getByText("v2").closest(".history-card") as HTMLElement;
    expect(versionTwo.tagName).toBe("LI");
    // The first version has nothing before it, so it offers no Changes.
    const versionOne = within(historyPanel).getByText("v1").closest(".history-card") as HTMLElement;
    expect(within(versionOne).queryByRole("button", { name: /Show changes/ })).toBeNull();
    // Agent versions record no parent -- v2 may have been made from any
    // earlier head -- so the row names v1 only as the previous version.
    expect(versionTwo).toHaveTextContent("Previous version: v1");
    expect(versionTwo).not.toHaveTextContent("Continues from");
    await userEvent.click(
      within(versionTwo).getByRole("button", {
        name: "Show changes in v2 since the previous version, v1",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v2 since the previous version, v1",
    });
    expect(within(dialog).getByText("v1 -> v2")).toBeInTheDocument();
    // Same oldest-first column labels as Context's History.
    expect(within(dialog).getByRole("columnheader", { name: "Base version (earlier)" }))
      .toBeInTheDocument();
    expect(within(dialog).getByRole("columnheader", { name: "Compared version (later)" }))
      .toBeInTheDocument();
    expect(within(dialog).getByText("Original")).toBeInTheDocument();
    expect(within(dialog).getAllByText("Current").length).toBeGreaterThan(0);

    await userEvent.click(within(dialog).getByRole("button", { name: "Restore this version" }));

    expect(confirm).toHaveBeenCalledWith(
      "Restore Agent v2 as a new draft? The active version stays unchanged.",
    );
    const restoreCalls = () =>
      fetchMock.mock.calls.filter(
        (call) =>
          call[0] === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1/restore`,
      );
    await waitFor(() => expect(restoreCalls()).toHaveLength(1));
    expect(JSON.parse(String(restoreCalls()[0][1]?.body))).toEqual({
      version_id: "version-2",
    });

    // While that restore is in flight the lock holds whatever the pickers
    // say: moving the later end used to re-enable Restore and Close, letting
    // a second restore start before the first finished.
    const later = within(dialog).getByRole("combobox", { name: "Later version" });
    expect(later).toBeDisabled();
    expect(within(dialog).getByRole("combobox", { name: "Earlier version" })).toBeDisabled();
    fireEvent.change(later, { target: { value: "version-3" } });
    expect(dialog).toHaveAccessibleName("Changes in v2 since the previous version, v1");
    expect(within(dialog).getByRole("button", { name: "Restoring" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Close diff" })).toBeDisabled();

    restoreResponse.resolve(
      jsonResponse({
        data: {
          agent_page_id: "agent-1",
          draft_version_id: "version-4",
          change_set_id: "change-restore",
          review_item_id: null,
          next_action: "owner_activation_required",
        },
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(restoreCalls()).toHaveLength(1);
  });

  it("loads the selected Agent history version and returns to the active head", async () => {
    const activeDetail = {
      ...agentDetail("agent-1", "persona", "Default Persona", "# Persona\n\nActive body"),
      current_active_version_id: "version-2",
      version_info: {
        ...agentDetail("agent-1", "persona", "Default Persona", "").version_info,
        version_id: "version-2",
        version_no: 2,
      },
    };
    const historicalDetail = {
      ...activeDetail,
      body_markdown: "# Persona\n\nHistorical body",
      version_info: {
        ...activeDetail.version_info,
        version_id: "version-1",
        version_no: 1,
        version_status: "superseded_snapshot",
      },
    };
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
        data: [category("persona", "always")],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
        data: [
          {
            ...agentPage("agent-1", "persona", "Default Persona"),
            current_active_version_id: "version-2",
          },
        ],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
        data: treeResponse("agent", [treeNode("node-1", "agent-1", "Default Persona")]),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`]: {
        data: activeDetail,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=1`]: {
        data: historicalDetail,
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/changesets?target_type=agent_page&target_id=agent-1`]: {
        data: {
          versions: [
            agentVersion("version-2", 2, "active_snapshot"),
            agentVersion("version-1", 1, "superseded_snapshot"),
          ],
          changesets: [],
          review_decisions: [],
          diffs: null,
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=agent-1"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const historyPanel = screen.getByRole("tabpanel", { name: /History/ });
    const v1Card = within(historyPanel).getByText("v1").closest(".history-card") as HTMLElement;
    await userEvent.click(
      within(v1Card).getByRole("button", { name: "Select version" }),
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=1`,
        expect.anything(),
      );
    });
    await userEvent.click(screen.getByRole("tab", { name: "View" }));
    expect(await screen.findByText("Historical body")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /History/ }));
    const selectedV1Card = within(
      screen.getByRole("tabpanel", { name: /History/ }),
    ).getByText("v1").closest(".history-card") as HTMLElement;
    await userEvent.click(
      within(selectedV1Card).getByRole("button", { name: "Clear selection" }),
    );
    await userEvent.click(screen.getByRole("tab", { name: "View" }));
    expect(await screen.findByText("Active body")).toBeInTheDocument();
  });

  it("hides the previous Agent body while a newly selected page is loading", async () => {
    const secondDetail = deferredResponse();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({
          data: [
            agentPage("agent-1", "persona", "First Persona"),
            {
              ...agentPage("agent-2", "persona", "Second Persona"),
              current_active_version_id: "version-2",
            },
          ],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse("agent", [
            treeNode("node-1", "agent-1", "First Persona"),
            treeNode("node-2", "agent-2", "Second Persona"),
          ]),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`) {
        return jsonResponse({
          data: agentDetail("agent-1", "persona", "First Persona", "# First\n\nFirst body"),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-2?version=active`) {
        return secondDetail.promise;
      }
      if (url.includes("/changesets?target_type=agent_page&target_id=")) {
        const pageId = url.endsWith("agent-2") ? "version-2" : "version-1";
        return jsonResponse({
          data: {
            versions: [agentVersion(pageId, 1, "active_snapshot")],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=agent-1"]}>
        <AgentScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("First body")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("treeitem", { name: "Second Persona" }));

    expect(screen.queryByText("First body")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Loading Agent detail")).toBeInTheDocument();

    secondDetail.resolve(jsonResponse({
      data: {
        ...agentDetail("agent-2", "persona", "Second Persona", "# Second\n\nSecond body"),
        version_info: {
          ...agentDetail("agent-2", "persona", "Second Persona", "").version_info,
          version_id: "version-2",
        },
      },
    }));
    expect(await screen.findByText("Second body")).toBeInTheDocument();
  });

  it("ignores a stale Agent diff response after another version is selected", async () => {
    const olderDiff = deferredResponse();
    const newerSelectionDiff = deferredResponse();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`) {
        return jsonResponse({ data: [category("persona", "always")] });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`) {
        return jsonResponse({
          data: [{
            ...agentPage("agent-1", "persona", "Default Persona"),
            current_active_version_id: "version-3",
          }],
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`) {
        return jsonResponse({
          data: treeResponse("agent", [treeNode("node-1", "agent-1", "Default Persona")]),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages/agent-1?version=active`) {
        return jsonResponse({
          data: {
            ...agentDetail("agent-1", "persona", "Default Persona", "# Current"),
            current_active_version_id: "version-3",
            version_info: {
              ...agentDetail("agent-1", "persona", "Default Persona", "").version_info,
              version_id: "version-3",
              version_no: 3,
            },
          },
        });
      }
      if (url.endsWith("/changesets?target_type=agent_page&target_id=agent-1")) {
        return jsonResponse({
          data: {
            versions: [
              agentVersion("version-3", 3, "active_snapshot"),
              agentVersion("version-2", 2, "superseded_snapshot"),
              agentVersion("version-1", 1, "superseded_snapshot"),
            ],
            changesets: [],
            review_decisions: [],
            diffs: null,
          },
        });
      }
      if (url.includes("from_version_id=version-2")) {
        return olderDiff.promise;
      }
      if (url.includes("from_version_id=version-1")) {
        return newerSelectionDiff.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/agent?target=agent-1"]}>
        <AgentScreen />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("tab", { name: /History/ }));
    const panel = screen.getByRole("tabpanel", { name: /History/ });
    const v2 = within(panel).getByText("v2").closest(".history-card") as HTMLElement;
    const v1 = within(panel).getByText("v1").closest(".history-card") as HTMLElement;

    // v3 is the active version this reader is showing: it still has its own
    // Changes (against v2), but restoring it would only stack an identical
    // version, so that action is dropped there.
    const v3 = within(panel).getByText("v3").closest(".history-card") as HTMLElement;
    expect(within(v3).queryByRole("button", { name: "Restore this version" })).toBeNull();

    await userEvent.click(
      within(v3).getByRole("button", {
        name: "Show changes in v3 since the previous version, v2",
      }),
    );
    await userEvent.click(
      within(v2).getByRole("button", {
        name: "Show changes in v2 since the previous version, v1",
      }),
    );
    expect(within(v1).queryByRole("button", { name: /Show changes/ })).toBeNull();

    newerSelectionDiff.resolve(jsonResponse({
      data: {
        ...versionDiff("version-1", 1, "Second selection"),
        to_version_id: "version-2",
        to_version_no: 2,
      },
    }));
    const dialog = await screen.findByRole("dialog", {
      name: "Changes in v2 since the previous version, v1",
    });
    expect(dialog).toHaveTextContent("Second selection");

    olderDiff.resolve(jsonResponse({
      data: versionDiff("version-2", 2, "Stale first response"),
    }));
    await waitFor(() => {
      expect(dialog).not.toHaveTextContent("Stale first response");
    });
  });
});

function mockFetch(responses: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    const body = responses[url];
    if (!body) {
      throw new Error(`Unexpected request: ${url}`);
    }
    const value = typeof body === "function" ? await body() : body;
    return new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
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

function category(categoryName: string, policy: string) {
  return {
    category: categoryName,
    description: categoryName,
    default_injection_policy: policy,
  };
}

function agentPage(id: string, categoryName: string, title: string) {
  return {
    id,
    category: categoryName,
    title,
    slug: title.toLowerCase().replace(/\s+/g, "-"),
    lifecycle_status: "active",
    injection_policy: "always",
    priority: 10,
    sensitivity: "normal",
    current_active_version_id: "version-1",
    latest_draft_version_id: null,
    updated_at: "2026-05-09T00:00:00Z",
  };
}

function agentDetail(id: string, categoryName: string, title: string, body: string) {
  return {
    ...agentPage(id, categoryName, title),
    body_markdown: body,
    frontmatter: {},
    version_info: {
      version_id: "version-1",
      version_no: 1,
      version_status: "active_snapshot",
      graph_status: "linear",
      parent_version_id: null,
      content_hash: "sha256:agent",
      created_at: "2026-05-09T00:00:00Z",
    },
    tree_path: [],
    parent_node_id: null,
    children_count: 0,
    warnings: [],
  };
}

function agentVersion(versionId: string, versionNo: number, versionStatus: string) {
  return {
    version_id: versionId,
    version_no: versionNo,
    version_status: versionStatus,
    actor_type: "user",
    actor_display_name: "Test User",
    created_at: "2026-05-09T00:00:00Z",
    prior_version_id: null,
    actor_id: "user-1",
    lines_added: null,
    lines_removed: null,
    is_current: versionStatus === "active_snapshot",
  };
}

function versionDiff(versionId: string, versionNo: number, body: string) {
  return {
    from_version_id: versionId,
    to_version_id: "version-3",
    from_version_no: versionNo,
    to_version_no: 3,
    lines: [`--- v${versionNo}`, "+++ v3", "@@ -1 +1 @@", `-${body}`, "+Current"],
    additions: 1,
    deletions: 1,
  };
}

function treeNode(
  id: string,
  targetId: string,
  title: string,
  breadcrumb: string[] = [],
  parentId: string | null = null,
) {
  return {
    id,
    section: "agent",
    title,
    slug: null,
    node_kind: "page_ref",
    parent_id: parentId,
    sort_key: "0001",
    target_kind: "agent_page",
    target_id: targetId,
    is_system_node: false,
    can_have_children: false,
    primary_placement: true,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: breadcrumb.map((ancestor, index) => ({
      id: `ancestor-${index}`,
      section: "agent",
      title: ancestor,
      slug: ancestor.toLowerCase(),
      node_kind: "folder",
    })),
  };
}

function folderNode(
  id: string,
  title: string,
  slug: string,
  parentId: string | null,
  breadcrumb: string[] = [],
  isSystemNode = false,
  childrenCount = 0,
) {
  return {
    id,
    section: "agent",
    title,
    slug,
    node_kind: "folder",
    parent_id: parentId,
    sort_key: "0001",
    target_kind: null,
    target_id: null,
    is_system_node: isSystemNode,
    can_have_children: true,
    primary_placement: false,
    visibility_status: "visible",
    children_count: childrenCount,
    breadcrumb: breadcrumb.map((ancestor, index) => ({
      id: `ancestor-${index}`,
      section: "agent",
      title: ancestor,
      slug: ancestor.toLowerCase(),
      node_kind: "folder",
    })),
  };
}

function treeResponse(
  section: "agent",
  nodes: Array<ReturnType<typeof treeNode> | ReturnType<typeof folderNode>>,
) {
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


test("renders the shared folder card for a selected agent category (C-6)", async () => {
  // coverage gap noted in Sprint-3 exploration: no Agent folder-card test
  const user = userEvent.setup();
  mockFetch({
    [`${BASE}/api/v1/workspaces`]: {
      data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
    },
    [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
      data: [category("persona", "always")],
    },
    [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
      data: [],
    },
    [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
      data: {
        nodes: [
          {
            id: "folder-persona",
            section: "agent",
            title: "Persona",
            slug: "persona",
            node_kind: "folder",
            parent_id: null,
            sort_key: "0001",
            nav_summary: null,
            target_kind: null,
            target_id: null,
            is_system_node: false,
            can_have_children: true,
            primary_placement: false,
            visibility_status: "visible",
            children_count: 1,
            breadcrumb: [],
          },
          {
            ...treeNode(
              "node-1",
              "agent-1",
              "Default Persona",
              [],
              "folder-persona",
            ),
            target_meta: {
              category: "persona",
              injection_policy: "always",
              lifecycle_status: "active",
            },
          },
        ],
        policies: { agent: { allow_user_folder_create: true } },
      },
    },
  });

  render(
    <MemoryRouter>
      <AgentScreen />
    </MemoryRouter>,
  );
  await screen.findByRole("heading", { name: "Agent pages" });
  await user.click(await screen.findByRole("treeitem", { name: "Persona" }));
  // taxonomy description renders as the nav_summary fallback with auto badge
  const badges = await screen.findAllByText(/^auto$|^자동 생성$/);
  expect(badges.length).toBeGreaterThan(0);
  // shared card lists the child page row
  expect(
    await screen.findByRole("button", { name: /Default Persona/ }),
  ).toBeInTheDocument();
  expect(screen.getByText(/Persona - Always/)).toBeInTheDocument();
});

test("relabels the seeded category folders with the shared category label", async () => {
  // The seed stores "Agent Guide" while every enum-driven surface renders
  // "Agent guide"; the tree, the breadcrumb and the folder card must not be the
  // one place that disagrees with the cards and the table on the same screen.
  const user = userEvent.setup();
  mockFetch({
    [`${BASE}/api/v1/workspaces`]: {
      data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
    },
    [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/categories`]: {
      data: [category("agent_guide", "always")],
    },
    [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/agent/pages`]: {
      data: [],
    },
    [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/tree?section=agent`]: {
      data: treeResponse("agent", [
        folderNode(
          "folder-agent-guide",
          "Agent Guide",
          "agent_guide",
          null,
          [],
          true,
          0,
        ),
        folderNode("folder-persona", "Team Voice", "persona", null, [], true, 0),
      ]),
    },
  });

  render(
    <MemoryRouter>
      <AgentScreen />
    </MemoryRouter>,
  );

  await screen.findByRole("heading", { name: "Agent pages" });
  expect(
    await screen.findByRole("treeitem", { name: "Agent guide" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("treeitem", { name: "Agent Guide" })).toBeNull();
  // A folder the workspace renamed keeps its own title — the relabel only
  // covers the untouched seed name, or a rename would look like it did nothing.
  expect(screen.getByRole("treeitem", { name: "Team Voice" })).toBeInTheDocument();
  expect(screen.queryByRole("treeitem", { name: "Persona" })).toBeNull();

  await user.click(screen.getByRole("treeitem", { name: "Agent guide" }));

  expect(
    await screen.findByRole("heading", { name: "System-managed Agent guide" }),
  ).toBeInTheDocument();
});

function AgentNavigationProbe() {
  const location = useLocation();
  return <span data-testid="agent-navigation-key">{location.key}</span>;
}
