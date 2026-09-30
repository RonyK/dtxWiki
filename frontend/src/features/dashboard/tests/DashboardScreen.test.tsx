import { render, screen, within } from "@testing-library/react";
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
        data: {
          items: [
            activityItem("activity-1", "pending", "Review queued", undefined, "review-1"),
            activityItem("activity-2", "advisory", "Context page edited", "Draft updated"),
            activityItem("activity-3", "history", "Context page published"),
            activityItem("activity-4", "history", "Version restored"),
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
    expect(screen.getByText("4 events today")).toBeInTheDocument();
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
      "/activity/advisory?period=today&kind=workspace",
    );
    expect(screen.getByRole("link", { name: /History 2/ })).toHaveAttribute(
      "href",
      "/activity/history?period=today&kind=workspace",
    );
    expect(screen.getByText("Context page edited")).toBeInTheDocument();
    expect(screen.getByText("Edited")).toBeInTheDocument();
    expect(screen.getByText("Published")).toBeInTheDocument();
    expect(screen.getByText("Restored")).toBeInTheDocument();
    // Each Spaces tile opens a content area, so its subtitle describes that
    // area rather than the policy card that shares its name.
    expect(screen.getByText("Instructions, skills, and memory")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Agent Instructions, skills, and memory/ }))
      .toHaveAttribute("href", "/agent");
    const spacesHeader = screen.getByText("Spaces").parentElement;
    expect(spacesHeader).not.toHaveTextContent("3");
    // The recent count rides in the shared title+count group (ListPanelHeader),
    // not at the far right of the header band, and keeps its translated unit.
    const recentTitle = screen.getByText("Recent activity");
    expect(recentTitle.closest(".panel-title-with-count")).not.toBeNull();
    expect(within(recentTitle.parentElement!).getByText("4 events"))
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
    const row = title.closest(".activity-row") as HTMLElement;
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
    expect(title.closest(".activity-row__title")).not.toBeNull();
    expect(title).toHaveTextContent("Ask activity");
  });

  it("marks a failed MCP write as failed instead of edited", async () => {
    // The verb keywords see "update draft" in an "MCP update draft failed" row
    // and used to badge the failure as a successful edit.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
        data: {
          items: [
            {
              ...activityItem(
                "activity-mcp-failed",
                "advisory",
                "MCP update draft failed",
                "Claude Code; status=failed; Tool call failed.",
              ),
              actor: { actor_kind: "mcp_client", actor_id: "client-1" },
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

    const row = (await screen.findByText("MCP update draft failed")).closest(
      ".activity-row",
    ) as HTMLElement;
    expect(within(row).getByText("Failed")).toHaveClass("status-pill-danger");
    expect(within(row).queryByText("Edited")).toBeNull();
    // The status token is dropped from the detail: the title already carries it.
    expect(within(row).getByText("Claude Code · Tool call failed."))
      .toBeInTheDocument();
  });

  it("badges an unclassifiable row with a neutral verb, not its lane name", async () => {
    // The badge column answers "what happened". An MCP read carries no verb the
    // classifier recognises, and falling back to the lane ("History") would put
    // a lane noun beside Created/Edited in the same column.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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

    const row = (await screen.findByText("MCP read page succeeded")).closest(
      ".activity-row",
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=week&kind=workspace&limit=50`]: {
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
      "/activity/history?period=week&kind=workspace",
    );
  });

  it("keeps raw auth activity out of the dashboard operations feed", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
        data: {
          items: [],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=system&limit=1`]: {
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
      .toHaveAttribute("href", "/activity?period=today&kind=system");
    expect(screen.queryByText("Signed in")).not.toBeInTheDocument();
    expect(screen.queryByText("auth.session.created")).not.toBeInTheDocument();
  });

  it("preserves the dashboard period when linking to system activity", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=week&kind=workspace&limit=50`]: {
        data: {
          items: [],
          next_cursor: null,
          unread_count: 0,
        },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=week&kind=system&limit=1`]: {
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
      .toHaveAttribute("href", "/activity?period=week&kind=system");
  });

  it("keeps the welcome guide visible when opened from signup", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
    expect(screen.getByRole("link", { name: "Rename Personal" })).toHaveAttribute(
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
  });

  it("moves the welcome next action when Library already has material", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
    ).toHaveAttribute("aria-valuenow", "25");
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
    // decision history keeps it done, so library + review = 2 of 4 complete.
    const setup = await screen.findByLabelText("Workspace setup");
    expect(setup).toHaveTextContent("Next: Write Context page");
    expect(
      screen.getByRole("progressbar", { name: "Workspace setup progress" }),
    ).toHaveAttribute("aria-valuenow", "50");
  });

  it("shows an empty dashboard with a warning when live data load fails", async () => {
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
    expect(screen.getByRole("link", { name: /Pending 0/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Advisory 0/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /History 0/ })).toBeInTheDocument();
    expect(screen.getByText("Start your first workspace")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Review Approve/ })).toHaveAttribute(
      "href",
      "/review",
    );
    expect(screen.getByText("No workspace activity for today")).toBeInTheDocument();
  });

  it("marks capped advisory/history counts and the summary pill as lower bounds", async () => {
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
    expect(screen.getByText("50+ events today")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Advisory 25\+/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /History 25\+/ }),
    ).toBeInTheDocument();
    // The feed stays a short preview even though the count window grew.
    expect(screen.getAllByText(/Filler item/)).toHaveLength(8);
  });

  it("classifies activity actions from audit codes when titles are localized", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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

    expect(await screen.findByText("2 건 · 오늘")).toBeInTheDocument();
  });

  it("hides the setup write CTAs for viewer-role users", async () => {
    dashboardWorkspace.current = { selectedWorkspace: { role: "viewer" } };
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [workspaceSummary()] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
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
});

function mockFetch(responses: Record<string, unknown>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    const body = responses[url] ?? defaultDashboardResponse(url);
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
  if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/ask/sessions`) {
    return { data: { items: [], date_buckets: [], next_cursor: null } };
  }
  if (
    url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?`) &&
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
