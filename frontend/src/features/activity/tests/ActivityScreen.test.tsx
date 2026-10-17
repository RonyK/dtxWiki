import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  formatRelativeTimestamp,
  formatVersionTimestamp,
} from "../../../shared/lib/datetime";
import { ActivityLanePage, ActivityScreen } from "../ActivityScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";
const activityPreferences = vi.hoisted(() => ({
  chats: false,
  agent: false,
  role: "editor",
}));

vi.mock("../../../shared/hooks/useLabsPreferences", () => ({
  useLabsPreferences: () => ({ preferences: activityPreferences }),
}));
vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => ({
    selectedWorkspace: { role: activityPreferences.role },
  }),
}));

describe("ActivityScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    activityPreferences.chats = false;
    activityPreferences.role = "editor";
  });

  it("renders three activity lanes and review deeplinks", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: {
          items: [
            activityItem("activity-1", "pending", "Review required", "review-1"),
            activityItem("activity-2", "advisory", "Draft created", null),
            activityItem("activity-3", "history", "Review approved", null),
          ],
          next_cursor: null,
          unread_count: 2,
        },
      },
    });

    render(
      <MemoryRouter>
        <ActivityScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect(screen.getByText("Review required")).toBeInTheDocument();
    expect(screen.getByText("Draft created")).toBeInTheDocument();
    expect(screen.getByText("Review approved")).toBeInTheDocument();
    // The Review affordance deep-links to the exact item via ?review=<id>,
    // which ReviewScreen restores even beyond the first page.
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute(
      "href",
      "/review?review=review-1",
    );
  });

  it("renders the row time as a relative label while keeping the long-format tooltip", async () => {
    const createdAt = "2026-05-09T00:00:00Z";
    // Pin "now" two hours after the fixture instant so it lands in the relative
    // (<7d) window the shared formatter uses. Mock Date.now() (not fake timers)
    // so testing-library's async findBy polling still advances.
    vi.spyOn(Date, "now").mockReturnValue(
      new Date("2026-05-09T02:00:00Z").getTime(),
    );

    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: {
          items: [activityItem("activity-1", "history", "Review approved", null)],
          next_cursor: null,
          unread_count: 0,
        },
      },
    });

    render(
      <MemoryRouter>
        <ActivityScreen />
      </MemoryRouter>,
    );

    const time = (await screen.findByText("Review approved"))
      .closest(".activity-row")
      ?.querySelector("time.activity-relative-time");
    expect(time).not.toBeNull();
    // Visible text is the shared relative label, not the absolute compact one.
    expect(time).toHaveTextContent(formatRelativeTimestamp(createdAt, "en"));
    expect(time?.textContent).not.toBe(formatVersionTimestamp(createdAt));
    // The hover tooltip still carries the full long-format instant.
    expect(time).toHaveAttribute(
      "title",
      formatVersionTimestamp(createdAt, "long"),
    );
    expect(time).toHaveAttribute("dateTime", createdAt);
  });

  it("labels each actor kind and never repeats the row's own title in its chip", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: {
          items: [
            {
              ...wsActivityItem("row-human", "Human Page", "context_page.publish"),
              actor: { actor_kind: "human", actor_id: "u1", display_name: "Dohyun Kim" },
            },
            {
              // The AI actor's identity splits: who granted the access, which
              // AI ran under it. Both facts land in one pill, not the chip.
              ...wsActivityItem("row-mcp-owned", "MCP Owned Page", "context_page.publish"),
              actor: {
                actor_kind: "mcp_client",
                actor_id: "client-1",
                display_name: "Claude Desktop",
                credential_owner_display_name: "Dohyun Kim",
              },
            },
            {
              // No owner on record (legacy key / deleted user): the AI side
              // alone, not a split pill with an empty half.
              ...wsActivityItem("row-mcp-orphan", "MCP Orphan Page", "context_page.publish"),
              actor: {
                actor_kind: "mcp_client",
                actor_id: "client-2",
                display_name: "Legacy Key",
                credential_owner_display_name: null,
              },
            },
            {
              ...wsActivityItem("row-agent", "Agent Page", "context_page.publish"),
              actor: { actor_kind: "agent", actor_id: null, display_name: "ignored" },
            },
            {
              ...wsActivityItem("row-system", "System Page", "context_page.publish"),
              actor: { actor_kind: "system", actor_id: null, display_name: "ignored" },
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
    });

    render(
      <MemoryRouter>
        <ActivityScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Human Page")).toBeInTheDocument();
    const rowFor = (title: string) =>
      within(screen.getByText(title).closest(".activity-row") as HTMLElement);

    expect(rowFor("Human Page").getByText("Dohyun Kim")).toBeInTheDocument();
    expect(
      rowFor("MCP Owned Page").getByText("Dohyun Kim"),
    ).toBeInTheDocument();
    expect(
      rowFor("MCP Owned Page").getByText("Claude Desktop"),
    ).toBeInTheDocument();
    expect(
      rowFor("MCP Orphan Page").getByText("Legacy Key"),
    ).toBeInTheDocument();
    // No owner on record: the split pill degrades to the AI side alone, so
    // the owner name from other rows must not leak in here.
    expect(rowFor("MCP Orphan Page").queryByText("Dohyun Kim")).toBeNull();
    expect(
      rowFor("Agent Page").getByText("Internal agent worker"),
    ).toBeInTheDocument();
    // "System" also names the Type filter option, so this must be scoped to
    // the row -- an unscoped query would be ambiguous.
    expect(rowFor("System Page").getByText("System")).toBeInTheDocument();

    // "Dohyun Kim" grants two different MCP clients, so it must appear twice
    // (once per split pill) -- but never a third time, which would mean the
    // owner name leaked into some other row's chip or title.
    expect(screen.getAllByText("Dohyun Kim")).toHaveLength(2);

    // Every row's target chip is VISIBLY labelled by kind ("Context page"),
    // never by repeating the row's own title text a second time -- but every
    // row of the same kind would then share one accessible name, so the
    // link's a11y name still carries the specific target title.
    for (const title of [
      "Human Page",
      "MCP Owned Page",
      "MCP Orphan Page",
      "Agent Page",
      "System Page",
    ]) {
      expect(screen.getAllByText(title)).toHaveLength(1);
      expect(
        screen.getByRole("link", { name: `Open target ${title}` }),
      ).toHaveTextContent("Context page");
    }
  });

  it("shows the real target name when the headline doesn't already name it", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: {
          items: [
            {
              // An event_key row's headline is a bare verb phrase translated
              // from eventTitles.* ("Published page") -- it never names the
              // page. The chip is the only place that name can show, so it
              // must show the real title here, not the generic kind label.
              id: "activity-publish",
              lane: "history",
              title: "ignored -- event_key wins",
              body: "context_page.draft.publish",
              target_title: "Reranking Decision Log",
              event_key: "context_page.draft.publish",
              event_args: {},
              actor: { actor_kind: "human", actor_id: "u1", display_name: "Dohyun Kim" },
              target: { target_kind: "context_page", target_id: "page-1" },
              created_at: "2026-05-09T00:00:00Z",
              review_item_id: null,
              affordances: ["open"],
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
    });

    render(
      <MemoryRouter>
        <ActivityScreen />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Published page")).toBeInTheDocument();
    // The chip itself shows the real title, not the generic kind label --
    // presentActivityItem separately puts the kind label in the detail line
    // (.activity-row__detail) when an event_key row has no specific body
    // text; that's a different mechanism this test isn't about.
    expect(
      screen.getByRole("link", { name: "Open target Reranking Decision Log" }),
    ).toHaveTextContent("Reranking Decision Log");
  });

  it("exposes the period filter as an accessible radio group", async () => {
    const { container } = renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=week&limit=50`]: {
        data: {
          items: [],
          next_cursor: null,
          unread_count: 0,
        },
      },
    }, "/activity?period=week");

    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect(screen.getByText("Filtered")).toBeInTheDocument();
    // Activity is a full-width list screen, so its filters render expanded like
    // Chats and Lint — no click needed to see what can be narrowed.
    expect(screen.getByText("Period")).toBeInTheDocument();
    expect(screen.getByText("Type")).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Period" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Week" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "Week" })).toHaveAttribute(
      "title",
      "Show week activity",
    );
    expect((await axe(container)).violations).toEqual([]);
  });

  it("filters system activity out of the Workspace activity view", async () => {
    // The Workspace view (kind=workspace) sends sub_tab=undefined, which
    // encodeQuery drops, so the real request is ?period=all&limit=50. Mock that
    // exact URL and seed BOTH a system event (must be filtered out) and a
    // non-system workspace event (positive control that must remain) so the
    // filter is exercised rather than passing vacuously against empty data (S1).
    // Default kind is now "all"; this test forces the Workspace view via URL.
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&kind=workspace&limit=50`]: {
        data: {
          items: [
            activityItem("activity-draft", "history", "Draft created", null),
            {
              ...activityItem("activity-auth", "history", "auth.session.created", null),
              body: "auth.session.created",
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
    }, "/activity?kind=workspace");

    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "Type" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Workspace" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "Workspace" })).toHaveAttribute(
      "title",
      "Workspace activity from pages, sources, review, Ask, and MCP tools.",
    );
    // Positive control: the non-system workspace event is shown.
    expect(await screen.findByText("Draft created")).toBeInTheDocument();
    // The system (auth.*) event is filtered out of the workspace view.
    expect(screen.queryByText("auth.session.created")).not.toBeInTheDocument();
    expect(screen.queryByText("Signed in")).not.toBeInTheDocument();
  });

  it("defaults to the All kind and includes system events", async () => {
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: {
          items: [
            activityItem("activity-draft", "history", "Draft created", null),
            {
              ...activityItem("activity-auth", "history", "auth.session.created", null),
              body: "auth.session.created",
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
    });

    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    // No active filters: the "Filtered" summary eyebrow stays hidden instead
    // of labelling the default view.
    expect(screen.queryByText("Filtered")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "All" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    // The All view shows both workspace and system events (no kind filter).
    expect(await screen.findByText("Draft created")).toBeInTheDocument();
    expect(screen.getByText("Signed in")).toBeInTheDocument();
  });

  it("shows a sub-type filter for Workspace and filters by it client-side", async () => {
    renderWithActivity(
      {
        [`${BASE}/api/v1/workspaces`]: {
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&kind=workspace&limit=50`]:
          {
            data: {
              items: [
                wsActivityItem("a-ctx", "Page updated", "context_page.update"),
                wsActivityItem("a-lib", "Source registered", "library.item.register"),
              ],
              next_cursor: null,
              unread_count: 0,
            },
          },
      },
      "/activity?kind=workspace",
    );

    expect(
      await screen.findByRole("radiogroup", { name: "Sub-type" }),
    ).toBeInTheDocument();
    // Under the default "All" sub-type both rows show.
    expect(screen.getByText("Page updated")).toBeInTheDocument();
    expect(screen.getByText("Source registered")).toBeInTheDocument();
    // Selecting "Library" filters to library rows only (no refetch needed).
    await userEvent.click(screen.getByRole("radio", { name: "Library" }));
    expect(screen.getByText("Source registered")).toBeInTheDocument();
    expect(screen.queryByText("Page updated")).not.toBeInTheDocument();
  });

  it("omits the sub-type filter for the All view", async () => {
    renderWithActivity(
      {
        [`${BASE}/api/v1/workspaces`]: {
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
          data: { items: [], next_cursor: null, unread_count: 0 },
        },
      },
      "/activity",
    );

    expect(
      await screen.findByRole("heading", { name: "Activity" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("radiogroup", { name: "Sub-type" }),
    ).not.toBeInTheDocument();
  });

  it("previews the latest 5 lane items with a See all link to the dedicated page", async () => {
    const items = Array.from({ length: 7 }, (_, index) =>
      activityItem(`activity-${index}`, "history", `Event ${index}`, null),
    );
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: { items, next_cursor: null, unread_count: 0 },
      },
    });

    // Only the first 5 of the 7 history items render in the overview preview.
    expect(await screen.findByText("Event 0")).toBeInTheDocument();
    expect(screen.getByText("Event 4")).toBeInTheDocument();
    expect(screen.queryByText("Event 5")).not.toBeInTheDocument();
    // A "See all (7)" link points to the dedicated lane page.
    const seeAll = screen.getByRole("link", { name: "See all 7" });
    expect(seeAll).toHaveAttribute("href", "/activity/history");
  });

  it("pins review decisions into the pending preview even past the 5-row cutoff", async () => {
    // 5 newer failed jobs + 1 older review decision = 6 pending rows, but the
    // overview previews only 5. The review arrives LAST in the recency feed
    // (row 6), so a naive "sort only the already-rendered slice" would drop it.
    // The review-first pin must lift it into the visible 5 and push the oldest
    // excess job past the cut-off.
    const items = [
      activityItem("job-5", "pending", "Failed Job 5", null),
      activityItem("job-4", "pending", "Failed Job 4", null),
      activityItem("job-3", "pending", "Failed Job 3", null),
      activityItem("job-2", "pending", "Failed Job 2", null),
      activityItem("job-1", "pending", "Failed Job 1", null),
      activityItem("review-older", "pending", "Review Decision", "review-1"),
    ];
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: { items, next_cursor: null, unread_count: 1 },
      },
    });

    // The review is pinned into the visible 5 (it would be row 6 = hidden
    // without the pin).
    const review = await screen.findByText("Review Decision");
    // A newer job is still visible...
    const job5 = screen.getByText("Failed Job 5");
    // ...and the oldest excess job is pushed past the 5-row cut-off.
    expect(screen.queryByText("Failed Job 1")).not.toBeInTheDocument();
    // The pin is a position guarantee, not just presence: the review decision
    // renders BEFORE the newest job in DOM order (a presence-only assertion
    // would pass even if the pin landed it at the bottom of the preview).
    expect(
      review.compareDocumentPosition(job5) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("renders human labels for raw auth activity events in the system activity view", async () => {
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&kind=system&limit=50`]: {
        data: {
          items: [
            {
              ...activityItem("activity-auth", "history", "auth.session.created", null),
              body: "auth.session.created",
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
    }, "/activity?kind=system");

    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "System" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "System" })).toHaveAttribute(
      "title",
      "Platform & operations: sign-ins, settings, automated checks, and background jobs.",
    );
    expect(screen.getByText("A user session was created.")).toBeInTheDocument();
    expect(screen.queryByText("auth.session.created")).not.toBeInTheDocument();
  });

  it("renders OAuth access requests as system security activity", async () => {
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&kind=system&limit=50`]: {
        data: {
          items: [
            {
              ...activityItem(
                "activity-oauth",
                "history",
                "Codex CLI requested MCP OAuth access",
                null,
              ),
              body: "oauth.authorization.requested",
              target: {
                target_kind: "oauth_authorization",
                target_id: "user-1",
              },
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
    }, "/activity?kind=system");

    expect(await screen.findByText("Codex CLI requested MCP OAuth access"))
      .toBeInTheDocument();
    expect(screen.getByText("An MCP client requested access to this workspace."))
      .toBeInTheDocument();
    expect(screen.queryByText("oauth.authorization.requested")).not.toBeInTheDocument();
  });

  it("filters MCP tool calls into a dedicated activity view", async () => {
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&sub_tab=traces&limit=50`]: {
        data: {
          items: [
            activityItem("activity-workspace", "history", "Page updated", null),
            {
              ...activityItem("activity-mcp", "history", "MCP search succeeded", null),
              actor: { actor_kind: "mcp_client", actor_id: "client-1" },
              target_title: "Codex CLI",
              target: { target_kind: "mcp_tool_call", target_id: "call-1" },
            },
          ],
          next_cursor: null,
          unread_count: 0,
        },
      },
    }, "/activity?kind=mcp");

    expect(await screen.findByText("MCP search succeeded")).toBeInTheDocument();
    expect(screen.queryByText("Page updated")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "MCP" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "MCP" })).toHaveAttribute(
      "title",
      "External MCP traces from Codex CLI, Claude Code, Cursor, and other clients.",
    );
  });

  it("filters to the review sub-type when deep-linked via kind=workspace&subtype=review", async () => {
    // Review's "More activities" link lands here. The server returns the whole
    // workspace slice; the review sub-type is narrowed client-side, so only the
    // review.* row survives and the Review sub-type tab starts active.
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&kind=workspace&limit=50`]:
        {
          data: {
            items: [
              wsActivityItem(
                "activity-review-decision",
                "Approved Pricing page",
                "review.decision",
              ),
              wsActivityItem(
                "activity-library",
                "Source registered",
                "library.item.register",
              ),
            ],
            next_cursor: null,
            unread_count: 0,
          },
        },
    }, "/activity?kind=workspace&subtype=review");

    expect(
      await screen.findByText("Approved Pricing page"),
    ).toBeInTheDocument();
    // The non-review workspace row is filtered out by the review sub-type.
    expect(screen.queryByText("Source registered")).not.toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: "Review" }),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("keeps the active filter chips and Reset visible when the panel is folded", async () => {
    // A deep link (Review's "More activities", the Dashboard lane cards) lands
    // here pre-filtered. Folding the panel must not reduce that to a bare
    // "Filtered" eyebrow: the chips name the cut and Reset undoes it.
    renderWithActivity(
      {
        [`${BASE}/api/v1/workspaces`]: {
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]:
          {
            data: { items: [], next_cursor: null, unread_count: 0 },
          },
      },
      "/activity?period=today&kind=workspace",
    );

    expect(
      await screen.findByRole("heading", { name: "Activity" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Filters (2)" }));
    // The controls are folded away...
    expect(screen.queryByRole("radiogroup", { name: "Period" })).toBeNull();
    // ...but what is applied stays legible and undoable.
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset" })).toBeInTheDocument();
  });

  it("announces a saturated overview without assigning truncation to a lane", async () => {
    // A full OVERVIEW_LIMIT (50) fetch only proves that the whole overview is
    // truncated. It does not prove that this individual lane has more rows.
    const items = Array.from({ length: 50 }, (_, index) =>
      activityItem(`activity-${index}`, "history", `Event ${index}`, null),
    );
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: { items, next_cursor: null, unread_count: 0 },
      },
    });

    expect(await screen.findByText("Event 0")).toBeInTheDocument();
    expect(screen.getAllByText("50").length).toBeGreaterThan(0);
    expect(screen.getByText("50 events")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Showing the newest 50 events. Open a lane to load older activity.",
      ),
    ).toBeInTheDocument();
    // "See all" drops the exact count because items.length is a lower bound.
    const seeAll = screen.getByRole("link", { name: "See all" });
    expect(seeAll).toHaveAttribute("href", "/activity/history");
    expect(screen.queryByRole("link", { name: "See all 50" })).toBeNull();
  });

  it("starts mobile filters folded while keeping active filters visible", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === "(max-width: 640px)",
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    renderWithActivity(
      {
        [`${BASE}/api/v1/workspaces`]: {
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        },
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=today&kind=workspace&limit=50`]: {
          data: { items: [], next_cursor: null, unread_count: 0 },
        },
      },
      "/activity?period=today&kind=workspace",
    );

    expect(
      await screen.findByRole("heading", { name: "Activity" }),
    ).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Filters (2)" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("radiogroup", { name: "Period" })).toBeNull();
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset" })).toBeInTheDocument();
  });

  it("preserves an unkeyed pending review summary", async () => {
    // _pending_reviews puts the raw ReviewItem.decision_summary in body AND
    // target_title, and sets title to "Review required: <review_type>" — the
    // Without a stable event key, the stored audit text is the fallback.
    const summary = "sensitive AgentPage draft requires owner activation review";
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&limit=50`]: {
        data: {
          items: [
            {
              ...activityItem(
                "activity-pending",
                "pending",
                "Review required: agent_page_activation",
                "review-1",
              ),
              body: summary,
              target_title: summary,
            },
          ],
          next_cursor: null,
          unread_count: 1,
        },
      },
    });

    expect(await screen.findByText(summary)).toBeInTheDocument();
    expect(screen.getByText("Change set")).toBeInTheDocument();
    expect(screen.queryByText("change set")).toBeNull();
  });

  it.each([
    { role: "editor", chats: false, visible: false },
    { role: "editor", chats: true, visible: true },
    { role: "viewer", chats: true, visible: false },
  ])("shows empty MCP Chats actions only when enabled for editors ($role, enabled=$chats)", async ({ role, chats, visible }) => {
    activityPreferences.role = role;
    activityPreferences.chats = chats;
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&sub_tab=traces&limit=50`]: {
        data: { items: [], next_cursor: null, unread_count: 0 },
      },
    }, "/activity?kind=mcp");

    await screen.findByRole("heading", { name: "Activity" });
    expect(screen.getAllByRole("link", { name: "Open MCP setup" })).toHaveLength(3);
    const links = screen.queryAllByRole("link", { name: "Open Chats" });
    expect(links).toHaveLength(visible ? 3 : 0);
    for (const link of links) expect(link).toHaveAttribute("href", "/chats");
  });

  it("does not offer Context creation actions for empty system activity", async () => {
    renderWithActivity({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&kind=system&limit=50`]: {
        data: {
          items: [],
          next_cursor: null,
          unread_count: 0,
        },
      },
    }, "/activity?kind=system");

    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect(screen.getAllByText("No system events for this period."))
      .toHaveLength(3);
    expect(screen.queryByRole("link", { name: "Browse Context pages" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create a new page" }))
      .not.toBeInTheDocument();
  });
});

function renderWithActivity(responses: Record<string, unknown>, initialEntry = "/") {
  mockFetch(responses);
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ActivityScreen />
    </MemoryRouter>,
  );
}

describe("ActivityLanePage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    activityPreferences.chats = false;
    activityPreferences.role = "editor";
  });

  it("appends the next cursor page when Load more is clicked", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&lane=history&limit=50`]:
        {
          data: {
            items: [activityItem("h1", "history", "Event 1", null)],
            next_cursor: "CURSOR2",
            unread_count: 0,
          },
        },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&lane=history&limit=50&cursor=CURSOR2`]:
        {
          data: {
            items: [activityItem("h2", "history", "Event 2", null)],
            next_cursor: null,
            unread_count: 0,
          },
        },
    });

    render(
      <MemoryRouter initialEntries={["/activity/history?period=all"]}>
        <Routes>
          <Route path="/activity/:lane" element={<ActivityLanePage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Event 1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    // The next page is APPENDED (first page stays), and the exhausted cursor
    // hides the button.
    expect(await screen.findByText("Event 2")).toBeInTheDocument();
    expect(screen.getByText("Event 1")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
  });

  it("uses localized fallback copy when loading the next page fails unexpectedly", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&lane=history&limit=50`
      ) {
        return jsonResponse({
          data: {
            items: [activityItem("h1", "history", "Event 1", null)],
            next_cursor: "CURSOR2",
            unread_count: 0,
          },
        });
      }
      if (url.endsWith("&cursor=CURSOR2")) {
        throw new Error("raw req-secret");
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/activity/history?period=all"]}>
        <Routes>
          <Route path="/activity/:lane" element={<ActivityLanePage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Event 1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("Failed to load more")).toBeInTheDocument();
    expect(screen.queryByText("raw req-secret")).toBeNull();
    expect(screen.getByText("Event 1")).toBeInTheDocument();
  });

  it("keeps Load more reachable when a client-side filter empties the page", async () => {
    // The kind=system filter runs AFTER server paging: a page of only workspace
    // rows is emptied client-side, but later pages may hold system rows, so the
    // Load more button must stay reachable instead of being swallowed by the
    // empty state.
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: {
        data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
      },
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity?period=all&lane=history&kind=system&limit=50`]:
        {
          data: {
            items: [activityItem("h1", "history", "Workspace only", null)],
            next_cursor: "CURSOR2",
            unread_count: 0,
          },
        },
    });

    render(
      <MemoryRouter initialEntries={["/activity/history?period=all&kind=system"]}>
        <Routes>
          <Route path="/activity/:lane" element={<ActivityLanePage />} />
        </Routes>
      </MemoryRouter>,
    );

    // The lone workspace row is filtered out of the system view, so the page
    // shows the filtered-empty copy rather than the row.
    expect(await screen.findByText("No matches on this page")).toBeInTheDocument();
    expect(screen.queryByText("Workspace only")).not.toBeInTheDocument();
    // ...but Load more is still rendered because next_cursor is set.
    expect(
      screen.getByRole("button", { name: "Load more" }),
    ).toBeInTheDocument();
  });
});

function mockFetch(responses: Record<string, unknown>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    const body = responses[url];
    if (!body) {
      throw new Error(`Unexpected request: ${url}`);
    }
    return new Response(JSON.stringify(body), {
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

function activityItem(
  id: string,
  lane: string,
  title: string,
  reviewItemId: string | null,
) {
  return {
    id,
    lane,
    title,
    body: `${title} body`,
    actor: { actor_kind: "user", actor_id: null },
    target: { target_kind: "change_set", target_id: "change-1" },
    created_at: "2026-05-09T00:00:00Z",
    review_item_id: reviewItemId,
    affordances: ["open"],
  };
}

// A workspace audit row whose body carries the action prefix the sub-type
// classifier keys on (body = "domain.entity.operation"). The human-facing name
// arrives via target_title (presentActivityItem prefers it over the audit-code
// label resolved from the body prefix).
function wsActivityItem(id: string, title: string, action: string) {
  return {
    id,
    lane: "history",
    title,
    body: action,
    target_title: title,
    actor: { actor_kind: "user", actor_id: null },
    target: { target_kind: action.split(".")[0], target_id: "target-1" },
    created_at: "2026-05-09T00:00:00Z",
    review_item_id: null,
    affordances: ["open"],
  };
}
