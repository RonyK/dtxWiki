import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ActivityFeedEntry, ActivityFeedEvent } from "../../../shared/api/operations";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import {
  formatRelativeTimestamp,
  formatVersionTimestamp,
} from "../../../shared/lib/datetime";
import { DashboardScreen } from "../DashboardScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

// DashboardScreen reads role via useOptionalWorkspaceContext to gate the setup
// write CTAs; the tests render it without a provider, so mock the role here.
const { dashboardWorkspace } = vi.hoisted(() => ({
  dashboardWorkspace: {
    current: { selectedWorkspace: { role: "editor" } } as Record<
      string,
      unknown
    > | null,
  },
}));

vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => dashboardWorkspace.current,
}));

describe("DashboardScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    dashboardWorkspace.current = { selectedWorkspace: { role: "editor" } };
  });

  it("renders triage lanes from live operations data", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [
            activityItem("activity-1", "pending", "Review queued", undefined, "review-1"),
            { ...activityItem("activity-2", "advisory", "Context page edited", "Draft updated"), operation: "edit", result_status: "succeeded" },
            { ...activityItem("activity-3", "history", "Context page published"), operation: "publish", result_status: "succeeded" },
            { ...activityItem("activity-4", "history", "Version restored"), operation: "restore", result_status: "succeeded" },
          ],
          next_cursor: null,
          unread_count: 1,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [reviewItem()], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decisions/recent?limit=1`]: {
        data: [recentDecision()],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Workspace Operations" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("4 entries · 4 original events")).toHaveLength(2);
    expect(screen.getByRole("heading", { name: /Next:/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Ask Ask questions/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Pending 1/ })).toHaveAttribute(
      "href",
      "/review",
    );
    // The lane titles carry no period suffix: the period control states the
    // scope once, and Pending (all waiting reviews) is not period-scoped.
    expect(screen.getByRole("link", { name: /Advisory 1/ })).toHaveAttribute(
      "href",
      "/activity/advisory?period=today&kind=workspace&subtype=all&group_reads=1",
    );
    expect(screen.getByRole("link", { name: /History 2/ })).toHaveAttribute(
      "href",
      "/activity/history?period=today&kind=workspace&subtype=all&group_reads=1",
    );
    expect(screen.getByText("Context page edited")).toBeInTheDocument();
    expect(screen.getByText("Edited")).toBeInTheDocument();
    expect(screen.getByText("Published")).toBeInTheDocument();
    expect(screen.getByText("Restored")).toBeInTheDocument();
    // Each Spaces tile opens a content area, so its subtitle describes that
    // area rather than the policy card that shares its name.
    expect(screen.queryByText("Instructions, skills, and memory")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Agent Instructions, skills, and memory/ }))
      .not.toBeInTheDocument();
    const spacesHeader = screen.getByText("Spaces").parentElement;
    expect(spacesHeader).not.toHaveTextContent("3");
    // The recent count rides in the shared title+count group (ListPanelHeader),
    // not at the far right of the header band, and keeps its translated unit.
    const recentTitle = screen.getByText("Recent activity");
    expect(recentTitle.closest(".panel-title-with-count")).not.toBeNull();
    expect(within(recentTitle.parentElement!).getByText("4 entries"))
      .toBeInTheDocument();
  });

  it("renders a relative timestamp with the long absolute tooltip on each recent row", async () => {
    // C5: the recent-activity feed gains the single date/time convention — a
    // <time> with relative text (formatRelativeTimestamp) and the long absolute
    // formatVersionTimestamp tooltip, mirroring ActivityRow.
    await i18n.changeLanguage("en");
    const createdAt = "2026-05-09T00:00:00Z";
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [
            {
              ...activityItem("activity-time", "history", "Context page published"),
              created_at: createdAt,
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    const title = await screen.findByText("Context page published");
    const row = title.closest(".activity-feed-entry") as HTMLElement;
    const time = within(row).getByText(
      formatRelativeTimestamp(createdAt, "en"),
    );
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("datetime", createdAt);
    expect(time).toHaveAttribute(
      "title",
      formatVersionTimestamp(createdAt, "long"),
    );
  });

  it("resolves a raw workspace audit code in the recent feed to a label", async () => {
    // A row whose body carries a machine audit code ("ask.session.create") and
    // has no hand-authored copy must render a human label via the activityLabels
    // resolver, never the raw "ask.session.create" string.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [
            {
              // No target_title and a raw audit-code title: the resolver must
              // turn "ask.session.create" into a human label, not echo the code.
              ...activityItem("activity-audit", "history", "ask.session.create"),
              body: "ask.session.create",
              body_detail: "An Ask session was created.",
              target_title: null,
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decisions/recent?limit=1`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    // The recent feed row title resolves the audit code to the localized label.
    const resolved = i18n.t("activity:auditCode.ask");
    const title = await screen.findByText(resolved);
    // The headline shares its line with the actor badge now, so it is a
    // <span> inside .activity-row__title rather than a bare <strong>.
    expect(title.closest(".activity-entry__heading")).not.toBeNull();
    expect(title).toHaveTextContent("Ask activity");
  });

  it("marks a failed MCP write as failed instead of edited", async () => {
    // The verb keywords see "update draft" in an "MCP update draft failed" row
    // and used to badge the failure as a successful edit.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [
            {
              ...activityItem(
                "activity-mcp-failed",
                "advisory",
                "MCP update draft failed",
                "Claude Code; status=failed; Tool call failed.",
              ),
              actor: { actor_kind: "mcp_client", actor_id: "client-1", display_name: "Claude Code" },
              operation: "edit", result_status: "failed", tool_name: "update_context_edit",
              target_title: "Claude Code",
              target: { target_kind: "mcp_tool_call", target_id: "call-1" },
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decisions/recent?limit=1`]: {
        data: [],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    const row = (await screen.findByText(i18n.t("activity:feed.mcpResults.failed"))).closest(
      ".activity-feed-entry",
    ) as HTMLElement;
    expect(within(row).getByText("Failed").closest("[data-action]")).toHaveAttribute("data-action", "failed");
    expect(within(row).queryByText("Edited")).toBeNull();
    expect(row).not.toHaveTextContent("Claude Code; status=failed; Tool call failed.");
    expect(row.querySelector("details")).not.toBeNull();
  });

  it("badges an unclassifiable row with a neutral verb, not its lane name", async () => {
    // The badge column answers "what happened". An MCP read carries no verb the
    // classifier recognises, and falling back to the lane ("History") would put
    // a lane noun beside Created/Edited in the same column.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [
            {
              ...activityItem(
                "activity-mcp-read",
                "history",
                "MCP read page succeeded",
                "Codex CLI; status=ok",
              ),
              actor: { actor_kind: "mcp_client", actor_id: "client-1" },
              target_title: "Codex CLI",
              target: { target_kind: "mcp_tool_call", target_id: "call-1" },
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    const row = (await screen.findByText(i18n.t("activity:feed.mcpFallback"))).closest(
      ".activity-feed-entry",
    ) as HTMLElement;
    expect(within(row).getByText("Logged")).toBeInTheDocument();
    expect(within(row).queryByText("History")).toBeNull();
  });

  it("surfaces the editorRequired banner when bounced from a write route", async () => {
    // C2: a viewer bounced off an editor-gated route lands on /dashboard
    // carrying state.editorRequired; the Dashboard explains the redirect with an
    // info banner instead of silently swallowing the reason.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: { items: [], next_cursor: null, unread_count: 0 },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter
        initialEntries={[{ pathname: "/dashboard", state: { editorRequired: true } }]}
      >
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText(
        "Editor access is required to create or edit content in this workspace.",
      ),
    ).toBeInTheDocument();
  });

  it("omits the editorRequired banner without that navigation state", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: { items: [], next_cursor: null, unread_count: 0 },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter initialEntries={["/dashboard"]}>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Workspace Operations" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "Editor access is required to create or edit content in this workspace.",
      ),
    ).not.toBeInTheDocument();
  });

  it("counts pending reviews once even when activity mirrors them", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [
            activityItem("activity-1", "pending", "Review queued", undefined, "review-1"),
            activityItem("activity-2", "pending", "Review queued", undefined, "review-2"),
            activityItem("activity-3", "pending", "Import awaiting processing"),
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [reviewItem("review-1"), reviewItem("review-2")], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Workspace Operations" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Pending 3/ })).toHaveAttribute(
      "href",
      "/review",
    );
  });

  it("loads activity period from URL state", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=week&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [activityItem("activity-4", "history", "Weekly publish")],
          next_cursor: null,
          unread_count: 1,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter initialEntries={["/dashboard?period=week"]}>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Weekly publish")).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Dashboard activity period" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Week" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("link", { name: /History 1/ })).toHaveAttribute(
      "href",
      "/activity/history?period=week&kind=workspace&subtype=all&group_reads=1",
    );
  });

  it("keeps raw auth activity out of the dashboard operations feed", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=system&group_reads=true&limit=1`]: {
        data: {
          items: [
            activityItem(
              "activity-auth",
              "history",
              "auth.session.created",
              "auth.session.created",
            ),
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("No workspace activity for today"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View system events in Activity" }))
      .toHaveAttribute("href", "/activity?period=today&kind=system&subtype=all&group_reads=1");
    expect(screen.queryByText("Signed in")).not.toBeInTheDocument();
    expect(screen.queryByText("auth.session.created")).not.toBeInTheDocument();
  });

  it("preserves the dashboard period when linking to system activity", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=week&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=week&kind=system&group_reads=true&limit=1`]: {
        data: {
          items: [
            activityItem(
              "activity-auth",
              "history",
              "auth.session.created",
              "auth.session.created",
            ),
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter initialEntries={["/dashboard?period=week"]}>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("No workspace activity for week"))
      .toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View system events in Activity" }))
      .toHaveAttribute("href", "/activity?period=week&kind=system&subtype=all&group_reads=1");
  });

  it("keeps the welcome guide visible when opened from signup", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [activityItem("activity-5", "history", "Existing publish")],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter initialEntries={["/dashboard?welcome=1"]}>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Start your first workspace")).toBeInTheDocument();
    expect(screen.getByText("Existing publish")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Find workspaces" })).toHaveAttribute(
      "href",
      "/workspaces/find",
    );
    expect(screen.getByRole("link", { name: "Rename workspace" })).toHaveAttribute(
      "href",
      "/settings?section=workspace",
    );
    expect(
      screen.getByText(/create a separate collaborative workspace/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Browse discoverable workspaces without changing your current selection.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("link").some((link) => link.getAttribute("href")?.startsWith("/ask")))
      .toBe(false);
    expect(vi.mocked(globalThis.fetch).mock.calls.some(([input]) => String(input).includes("/ask/")))
      .toBe(false);
  });

  it("moves the welcome next action when Library already has material", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [activityItem("activity-6", "history", "Source imported")],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    const setup = await screen.findByLabelText("Workspace setup");
    expect(setup).toHaveTextContent("Next: Write Context page");
    expect(
      screen.getByRole("progressbar", { name: "Workspace setup progress" }),
    ).toHaveAttribute("aria-valuenow", "33");
    expect(screen.queryByLabelText("Workspace setup sequence")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Write first Context page" }))
      .toHaveAttribute("href", "/context/new?from=context");
    expect(
      screen.queryByRole("link", { name: "Find workspaces" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the review step complete from decision history once the queue is empty", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: { items: [], next_cursor: null, unread_count: 0 },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decisions/recent?limit=1`]: {
        data: [recentDecision()],
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    // Deciding every pending review must not regress the review step: the
    // decision history keeps it done, so library + review = 2 of 3 complete.
    const setup = await screen.findByLabelText("Workspace setup");
    expect(setup).toHaveTextContent("Next: Write Context page");
    expect(
      screen.getByRole("progressbar", { name: "Workspace setup progress" }),
    ).toHaveAttribute("aria-valuenow", "67");
  });

  it("shows a retryable load error without empty activity, counts, or setup guidance", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
        });
      }
      return jsonResponse(
        {
          error_code: "service_unavailable",
          message: "Operations feed unavailable",
          request_id: "req-test",
          warnings: [],
        },
        503,
      );
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Workspace Operations" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The service is temporarily unavailable. Try again shortly.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Pending 0/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Advisory 0/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /History 0/ })).not.toBeInTheDocument();
    expect(screen.queryByText("Start your first workspace")).not.toBeInTheDocument();
    expect(screen.queryByText("No workspace activity for today")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload activity" })).toBeInTheDocument();
  });

  it("distinguishes capped query counts from totals without inventing per-lane plus signs", async () => {
    const items = Array.from({ length: 50 }, (_, index) => ({
      ...activityItem(
        `activity-${index}`,
        index % 2 === 0 ? "advisory" : "history",
        `Filler item ${index}`,
        "page.update",
      ),
      // The human label arrives via target_title; the body audit code only
      // feeds the lower-bound/lane classification, not the displayed title.
      target_title: `Filler item ${index}`,
    }));
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: { items, next_cursor: null, unread_count: 0 },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Workspace Operations" }),
    ).toBeInTheDocument();
    expect(screen.getByText("50 entries · 50 original events")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Advisory 25/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /History 25/ }),
    ).toBeInTheDocument();
    // The feed stays a short preview even though the count window grew.
    expect(screen.getAllByText(/Filler item/)).toHaveLength(8);
    expect(screen.queryByText(/50\+|25\+/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View all activity" })).toHaveAttribute("href", "/activity?period=today&kind=workspace&subtype=all&group_reads=1");
  });

  it("classifies activity actions from audit codes when titles are localized", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [
            // The human page name arrives via target_title; the audit code lives
            // in the body and drives the action classification (Published/Restored).
            {
              ...activityItem("activity-ko-1", "history", "요금 페이지", "context_page.publish"),
              target_title: "요금 페이지",
            },
            {
              ...activityItem("activity-ko-2", "history", "온보딩 페이지", "page.restore"),
              target_title: "온보딩 페이지",
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("요금 페이지")).toBeInTheDocument();
    expect(screen.getByText("Published")).toBeInTheDocument();
    expect(screen.getByText("Restored")).toBeInTheDocument();
  });

  it("renders the activity count chip in the active locale", async () => {
    await i18n.changeLanguage("ko");
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: {
          items: [
            activityItem("activity-1", "history", "Context page published"),
            activityItem("activity-2", "history", "Version restored"),
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(await screen.findAllByText(i18n.t("activity:feed.count", { count: 2, events: 2 }))).toHaveLength(2);
  });

  it("hides the setup write CTAs for viewer-role users", async () => {
    dashboardWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: { items: [], next_cursor: null, unread_count: 0 },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
    });

    render(
      <MemoryRouter initialEntries={["/dashboard?welcome=1"]}>
        <DashboardScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Start your first workspace")).toBeInTheDocument();
    expect(screen.getByLabelText("Workspace setup sequence")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Add first source" }),
    ).toBeNull();
  });

  it("hides the compact next-step CTA for viewer-role users", async () => {
    dashboardWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`]: {
        data: { items: [], next_cursor: null, unread_count: 0 },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`]: {
        data: { items: [], next_cursor: null },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`]: {
        data: policies(),
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`]: {
        data: [{ id: "source-1" }],
      },
    });

    render(
      <MemoryRouter>
        <DashboardScreen />
      </MemoryRouter>,
    );

    const setup = await screen.findByLabelText("Workspace setup");
    expect(setup).toHaveTextContent("Next: Write Context page");
    expect(
      screen.queryByRole("link", { name: "Write first Context page" }),
    ).toBeNull();
  });
  it("keeps the initial loading state distinct from empty activity and setup", async () => {
    let complete!: (response: Response) => void;
    const pending = new Promise<Response>(resolve => { complete = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === dashboardFeedUrl) return pending;
      return jsonResponse(dashboardResponse(url));
    });
    render(<MemoryRouter><DashboardScreen /></MemoryRouter>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(screen.queryByText("No workspace activity for today")).not.toBeInTheDocument();
    expect(screen.queryByText("Start your first workspace")).not.toBeInTheDocument();
    await act(async () => complete(jsonResponse(dashboardFeed([dashboardEntry("loaded", "Loaded activity")]))));
    expect(await screen.findByText("Loaded activity")).toBeInTheDocument();
  });

  it("recovers from an initial permission error through the explicit retry action", async () => {
    let feedAttempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === dashboardFeedUrl && feedAttempts++ === 0) return jsonResponse({ error_code: "forbidden", message: "Denied", request_id: "test", warnings: [] }, 403);
      return jsonResponse(url === dashboardFeedUrl ? dashboardFeed([dashboardEntry("recovered", "Recovered activity")]) : dashboardResponse(url));
    });
    render(<MemoryRouter><DashboardScreen /></MemoryRouter>);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No workspace activity for today")).not.toBeInTheDocument();
    expect(screen.queryByText("Start your first workspace")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reload activity" }));
    expect(await screen.findByText("Recovered activity")).toBeInTheDocument();
  });

  it.each([0, 1])("reconciles a permission-shrunken read group to %s visible events without reloading", async visibleCount => {
    let feedAttempts = 0;
    const grouped = dashboardReadGroup();
    const survivor = { ...grouped.event, id: "visible-member", navigation: { target_kind: "context_page", target_id: "visible-page", title: "Visible survivor", href: "/context?target=visible-page" } };
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === dashboardFeedUrl) {
        if (feedAttempts++ > 0) return jsonResponse({ error_code: "service_unavailable", message: "Unavailable", request_id: "test", warnings: [] }, 503);
        return jsonResponse(dashboardFeed([grouped, dashboardEntry("other", "Other visible activity")]));
      }
      if (url.endsWith("/activity/read-group?token=opaque-group")) return jsonResponse({ data: { events: visibleCount ? [survivor] : [], event_count: visibleCount, group_id: grouped.id } });
      return jsonResponse(dashboardResponse(url));
    });
    render(<MemoryRouter><DashboardScreen /></MemoryRouter>);
    await screen.findByRole("link", { name: "Read Old private title 4 times" });
    await userEvent.click(screen.getByRole("button", { name: "Expand 4 original events" }));
    await vi.waitFor(() => expect(screen.queryByText(/Old private title/)).not.toBeInTheDocument());
    expect(screen.getByText("Other visible activity")).toBeInTheDocument();
    expect(screen.queryByText(/5 original events/)).not.toBeInTheDocument();
    if (visibleCount) {
      expect(screen.getByRole("link", { name: "Read Visible survivor" })).toHaveAttribute("href", "/context?target=visible-page");
      expect(screen.getAllByText("2 entries · 2 original events")).toHaveLength(2);
    } else expect(screen.getAllByText("1 entry · 1 original event")).toHaveLength(2);
    // The reconciled entry replaces the group in place; nothing re-fetches.
    expect(feedAttempts).toBe(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("marks a lane count as a lower bound only when the server confirms more for that lane", async () => {
    const feed = dashboardFeed([dashboardEntry("history", "History event")]);
    feed.data.lanes.history.has_more = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      return jsonResponse(url === dashboardFeedUrl ? feed : dashboardResponse(url));
    });
    render(<MemoryRouter><DashboardScreen /></MemoryRouter>);
    await screen.findByText("History event");
    expect(document.querySelector('.metric-card[href^="/activity/history"] strong')).toHaveTextContent(/^1\+$/);
    expect(document.querySelector('.metric-card[href^="/activity/advisory"] strong')).toHaveTextContent(/^0$/);
  });

  it("keeps loaded group counts and detail retry when an invalid token refresh also fails", async () => {
    let feedAttempts = 0;
    let detailAttempts = 0;
    const grouped = dashboardReadGroup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === dashboardFeedUrl) {
        if (feedAttempts++ > 0) return jsonResponse({ error_code: "service_unavailable", message: "Unavailable", request_id: "test", warnings: [] }, 503);
        return jsonResponse(dashboardFeed([grouped, dashboardEntry("other", "Other activity")]));
      }
      if (url.endsWith("/activity/read-group?token=opaque-group")) {
        detailAttempts++;
        return jsonResponse({ error_code: "invalid_activity_token", message: "Expired token", request_id: "test", warnings: [] }, 400);
      }
      return jsonResponse(dashboardResponse(url));
    });
    render(<MemoryRouter><DashboardScreen /></MemoryRouter>);
    await screen.findByRole("link", { name: "Read Old private title 4 times" });
    await userEvent.click(screen.getByRole("button", { name: "Expand 4 original events" }));
    expect(await screen.findByText("Original events could not be loaded. Try again.")).toBeVisible();
    expect(await screen.findByText("The service is temporarily unavailable. Try again shortly.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Read Old private title 4 times" })).toBeVisible();
    expect(screen.getAllByText("2 entries · 5 original events")).toHaveLength(2);
    expect(screen.getByText("Other activity")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Original events could not be loaded. Try again.")).toBeVisible();
    expect(await screen.findByText("The service is temporarily unavailable. Try again shortly.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Read Old private title 4 times" })).toBeVisible();
    expect(screen.getAllByText("2 entries · 5 original events")).toHaveLength(2);
    expect(detailAttempts).toBe(2);
  });

  it.each([
    [0, 0, "0 entries · 0 original events"],
    [1, 1, "1 entry · 1 original event"],
    [8, 8, "8 entries · 8 original events"],
    [9, 8, "8 entries · 8 original events"],
    [50, 8, "8 entries · 8 original events"],
  ] as const)("previews %s fetched entries as %s entries with a scoped header link", async (returned, visible, label) => {
    const scopedFeedUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=week&kind=workspace&subtype=all&group_reads=false&limit=50`;
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      return jsonResponse(url === scopedFeedUrl ? dashboardFeed(Array.from({ length: returned }, (_, n) => dashboardEntry(`boundary-${n}`, `Boundary event ${n}`))) : dashboardResponse(url));
    });
    render(<MemoryRouter initialEntries={["/dashboard?period=week&group_reads=0"]}><DashboardScreen /></MemoryRouter>);
    const link = await screen.findByRole("link", { name: "View all activity" });
    expect(link).toHaveAttribute("href", "/activity?period=week&kind=workspace&subtype=all&group_reads=0");
    const panel = link.closest("section")!;
    expect(panel.querySelectorAll(".activity-feed-entry")).toHaveLength(visible);
    expect(within(panel).getByText(label)).toBeInTheDocument();
    expect(panel.querySelector(".count-chip")).toHaveTextContent(`${visible} ${visible === 1 ? "entry" : "entries"}`);
    expect(panel.textContent).not.toContain("+");
    if (returned > 8) expect(screen.queryByText("Boundary event 8")).not.toBeInTheDocument();
  });

});

function mockFetch(responses: Record<string, unknown>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    const body = typedFeedResponse(url, responses[url] ?? defaultDashboardResponse(url));
    if (!body) {
      throw new Error(`Unexpected request: ${url}`);
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

function defaultDashboardResponse(url: string) {
  if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/items`) {
    return { data: [] };
  }
  if (
    url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews/decisions/recent?limit=1`
  ) {
    return { data: [] };
  }
  if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages`) {
    return { data: [] };
  }
  if (
    url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?`) &&
    url.includes("kind=system")
  ) {
    return { data: { items: [], next_cursor: null, unread_count: 0 } };
  }
  return null;
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function workspaceSummary() {
  return {
    id: WORKSPACE_ID,
    slug: "default",
    title: "Default Workspace",
    name: "Default Workspace",
    goal: "",
    default_language: "en",
    visibility: "private",
    role: "owner",
  };
}

function activityItem(
  id: string,
  lane: string,
  title: string,
  body = `${title} body`,
  reviewItemId: string | null = null,
) {
  return {
    id,
    lane,
    title,
    body,
    actor: null,
    target: null,
    created_at: "2026-05-09T00:00:00Z",
    review_item_id: reviewItemId,
    affordances: ["open"],
    event_key: null, event_args: {},
  };
}

function recentDecision(id = "decision-1") {
  return {
    decision_id: id,
    review_item_id: "review-1",
    review_target_title: "Pricing page",
    decision_type: "approve",
    actor_display_name: "Dana Editor",
    decision_reason: null,
    resulting_review_status: "approved",
    risk_level: "low",
    created_at: "2026-05-09T00:00:00Z",
  };
}

function reviewItem(id = "review-1") {
  return {
    id,
    workspace_id: WORKSPACE_ID,
    change_set_id: "change-1",
    review_type: "context_page_publish",
    status: "pending",
    risk_level: "high",
    decision_summary: "Publish requested",
    created_at: "2026-05-09T00:00:00Z",
    updated_at: "2026-05-09T00:00:00Z",
    decisions: [],
  };
}

function policies() {
  const card = (name: string, title: string) => ({
    name,
    title,
    policy: {},
  });
  return {
    workspace: card("workspace", "Workspace policy"),
    library: card("library", "Library policy"),
    citation: card("citation", "Citation policy"),
    context_write: card("context_write", "Context policy"),
    agent_injection: card("agent_injection", "Agent policy"),
    mcp_clients: [],
  };
}

function typedFeedResponse(url: string, body: unknown) {
  if (!body || !url.includes("/activity/feed?")) return body;
  const data = (body as { data?: { items?: Array<Record<string, unknown>>; next_cursor?: string | null; unread_count?: number } }).data;
  if (!data?.items) return body;
  const entries = data.items.map(item => ({
    entry_type: "event", id: item.id, event: {
      event_key: null, event_args: {}, operation: null, result_status: null,
      ...item,
      actor: item.actor ? { display_name: "Editor", is_current_user: false, ...(item.actor as object) } : null,
    }, event_count: 1, first_occurred_at: item.created_at, last_occurred_at: item.created_at, group_token: null,
  }));
  const lanes = Object.fromEntries(["pending", "advisory", "history"].map(lane => {
    const count = data.items!.filter(item => item.lane === lane).length;
    return [lane, { returned_entry_count: count, represented_event_count: count, has_more: null }];
  }));
  return { data: { entries, next_cursor: data.next_cursor ?? null, unread_count: data.unread_count ?? 0,
    returned_entry_count: entries.length, represented_event_count: entries.length, has_more: Boolean(data.next_cursor), lanes } };
}

const dashboardFeedUrl = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?period=today&kind=workspace&subtype=all&group_reads=true&limit=50`;
function dashboardEntry(id: string, title: string): ActivityFeedEntry {
  const value: ActivityFeedEvent = { id, title, lane: "history", body: null, actor: null, target: { target_kind: "context_page", target_id: id }, created_at: "2026-05-09T00:00:00Z", review_item_id: null, affordances: ["open"], event_key: null, event_args: {}, operation: "log" };
  return { entry_type: "event", id, event: value, event_count: 1, first_occurred_at: value.created_at, last_occurred_at: value.created_at };
}
function dashboardReadGroup(): ActivityFeedEntry {
  const single = dashboardEntry("group-entry", "MCP read succeeded");
  return { ...single, entry_type: "read_group", event_count: 4, group_token: "opaque-group", event: { ...single.event, target: { target_kind: "mcp_tool_call", target_id: "call-1" }, operation: "read", result_status: "succeeded", navigation: { target_kind: "context_page", target_id: "old-page", title: "Old private title", href: "/context?target=old-page" } } };
}
function dashboardFeed(entries: ActivityFeedEntry[]) {
  const lane = (name: string) => { const shown = entries.filter(item => item.event.lane === name); return { returned_entry_count: shown.length, represented_event_count: shown.reduce((sum, item) => sum + item.event_count, 0), has_more: false }; };
  return { data: { entries, next_cursor: null, unread_count: 0, returned_entry_count: entries.length, represented_event_count: entries.reduce((sum, item) => sum + item.event_count, 0), has_more: false, lanes: { pending: lane("pending"), advisory: lane("advisory"), history: lane("history") } } };
}
function dashboardResponse(url: string): unknown {
  if (url === `${BASE}/api/v1/workspaces`) return { data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] } };
  if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/reviews?status=pending&limit=20`) return { data: { items: [], next_cursor: null } };
  if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/settings/policies`) return { data: policies() };
  const response = defaultDashboardResponse(url);
  if (response) return typedFeedResponse(url, response);
  throw new Error(`Unexpected request: ${url}`);
}
