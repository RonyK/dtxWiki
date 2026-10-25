import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ActivityFeedData, ActivityFeedEntry, ActivityFeedEvent } from "../../../shared/api/operations";
import { formatRelativeTimestamp, formatVersionTimestamp } from "../../../shared/lib/datetime";
import { ActivityLanePage, ActivityScreen } from "../ActivityScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";
const WORKSPACES_URL = `${BASE}/api/v1/workspaces`;
const workspace = { data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } };
const activityPreferences = vi.hoisted(() => ({ role: "editor" }));

vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => ({ selectedWorkspace: { role: activityPreferences.role } }),
}));

function feedUrl(scope: { period?: string; kind?: string; subtype?: string; group_reads?: boolean; lane?: string; cursor?: string } = {}) {
  const params = new URLSearchParams({ period: scope.period ?? "all", kind: scope.kind ?? "all", subtype: scope.subtype ?? "all", group_reads: scope.group_reads === false ? "false" : "true" });
  if (scope.lane) params.set("lane", scope.lane);
  params.set("limit", "50");
  if (scope.cursor) params.set("cursor", scope.cursor);
  return `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/activity/feed?${params}`;
}
function event(id: string, title: string, overrides: Partial<ActivityFeedEvent> = {}): ActivityFeedEvent {
  return { id, lane: "history", title, body: `${title} details`, actor: { actor_kind: "human", actor_id: "user-1", display_name: "Dohyun Kim", is_current_user: false }, target: { target_kind: "context_page", target_id: `page-${id}` }, target_title: null, created_at: "2026-05-09T00:00:00Z", review_item_id: null, affordances: ["open"], event_key: null, event_args: {}, operation: "log", ...overrides };
}
function entry(value: ActivityFeedEvent, count = 1): ActivityFeedEntry {
  return { entry_type: count > 1 ? "read_group" : "event", id: value.id, event: value, event_count: count, first_occurred_at: value.created_at, last_occurred_at: value.created_at, group_token: count > 1 ? `group-${value.id}` : null };
}
function data(entries: ActivityFeedEntry[], overrides: Partial<ActivityFeedData> = {}): { data: ActivityFeedData } {
  const countLane = (lane: string) => {
    const matching = entries.filter(item => item.event.lane === lane);
    return { returned_entry_count: matching.length, represented_event_count: matching.reduce((sum, item) => sum + item.event_count, 0), has_more: null };
  };
  return { data: { entries, next_cursor: null, unread_count: 0, returned_entry_count: entries.length, represented_event_count: entries.reduce((sum, item) => sum + item.event_count, 0), has_more: false, lanes: { pending: countLane("pending"), advisory: countLane("advisory"), history: countLane("history") }, ...overrides } };
}
function jsonResponse(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }); }
function mockFetch(responses: Record<string, unknown>) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
    const url = input.toString();
    const body = url === WORKSPACES_URL ? workspace : responses[url];
    if (!body) throw new Error(`Unexpected request: ${url}`);
    return jsonResponse(body);
  });
}
function Navigation() {
  const navigate = useNavigate();
  const location = useLocation();
  return <><output aria-label="Current location">{location.pathname}{location.search}</output><button onClick={() => navigate(-1)}>Back</button><button onClick={() => navigate(1)}>Forward</button><button onClick={() => navigate("/workspaces/workspace-1/activity?period=week&kind=mcp&subtype=read&group_reads=0")}>Open scoped activity</button></>;
}
function show(responses: Record<string, unknown>, initial = "/workspaces/workspace-1/activity", navigation = false) {
  const fetch = mockFetch(responses);
  return { ...render(<MemoryRouter initialEntries={[initial]}>{navigation && <Navigation />}<Routes><Route path="/workspaces/workspace-1/activity" element={<ActivityScreen />} /><Route path="/workspaces/:workspaceId/activity/:lane" element={<ActivityLanePage />} /></Routes></MemoryRouter>), fetch };
}
beforeEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); activityPreferences.role = "editor"; });

describe("ActivityScreen", () => {
  it("renders three lanes and gives the pending title and footer the same exact review link", async () => {
    show({ [feedUrl()]: data([entry(event("pending", "Review required", { lane: "pending", actor: null, review_item_id: "review-1" })), entry(event("advisory", "Draft created", { lane: "advisory" })), entry(event("history", "Review approved"))]) });
    expect(await screen.findByText("Draft created")).toBeInTheDocument();
    expect(screen.getByText("Review approved")).toBeInTheDocument();
    const title = screen.getByRole("link", { name: "Open review: Review required" });
    const chip = screen.getByRole("link", { name: "Review required: Review required" });
    expect(title).toHaveAttribute("href", "/workspaces/workspace-1/review/review-1");
    expect(chip).toHaveAttribute("href", title.getAttribute("href"));
    expect(screen.queryByRole("button", { name: "Review" })).not.toBeInTheDocument();
  });

  it("retains relative time, absolute tooltip and datetime", async () => {
    vi.spyOn(Date, "now").mockReturnValue(new Date("2026-05-09T02:00:00Z").getTime());
    show({ [feedUrl()]: data([entry(event("time", "Review approved"))]) });
    const time = (await screen.findByText("Review approved")).closest("article")!.querySelector("time")!;
    expect(time).toHaveTextContent(formatRelativeTimestamp("2026-05-09T00:00:00Z", "en"));
    expect(time).toHaveAttribute("title", formatVersionTimestamp("2026-05-09T00:00:00Z", "long"));
    expect(time).toHaveAttribute("datetime", "2026-05-09T00:00:00Z");
  });

  it("keeps actor identities compact and puts plain type before them without a duplicate target chip", async () => {
    const actor = (actor_kind: string, display_name: string, owner?: string | null) => ({ actor_kind, display_name, actor_id: null, is_current_user: false, credential_owner_display_name: owner });
    show({ [feedUrl()]: data([
      entry(event("human", "Human Page")),
      entry(event("owned", "MCP Owned Page", { actor: actor("mcp_client", "Claude Desktop", "Dohyun Kim") })),
      entry(event("orphan", "MCP Orphan Page", { actor: actor("mcp_client", "Legacy Key", null) })),
      entry(event("agent", "Agent Page", { actor: actor("agent", "ignored") })),
      entry(event("system", "System Page", { actor: actor("system", "ignored") })),
    ]) });
    await screen.findByText("Human Page");
    for (const [title, names] of [["Human Page", ["Dohyun Kim"]], ["MCP Owned Page", ["Dohyun Kim", "Claude Desktop"]], ["MCP Orphan Page", ["Legacy Key"]], ["Agent Page", ["Internal agent worker"]], ["System Page", ["System"]]] as const) {
      const row = screen.getByText(title).closest("article")!;
      const identity = row.querySelector(".activity-entry__identity")!;
      expect(identity.firstElementChild).toHaveTextContent("Context");
      for (const name of names) expect(within(row).getByText(name)).toBeInTheDocument();
      expect(within(row).getAllByText(title)).toHaveLength(1);
      expect(within(row).getAllByRole("link")).toHaveLength(1);
    }
    expect(within(screen.getByText("MCP Orphan Page").closest("article")!).queryByText("Dohyun Kim")).not.toBeInTheDocument();
  });

  it("uses the actual resource name as the main link when a keyed headline is only a verb", async () => {
    show({ [feedUrl()]: data([entry(event("publish", "ignored", { event_key: "context_page.draft.publish", target_title: "Reranking Decision Log", body: "context_page.draft.publish", operation: "publish" }))]) });
    expect(await screen.findByRole("link", { name: "Reranking Decision Log" })).toHaveAttribute("href", "/workspaces/workspace-1/context/pages/page-publish");
    expect(screen.getAllByText("Reranking Decision Log")).toHaveLength(1);
    expect(screen.getByText("Published page")).toBeInTheDocument();
  });

  it("exposes accessible period/type radio groups and a labeled native grouping checkbox", async () => {
    const { container } = show({ [feedUrl({ period: "week" })]: data([]) }, "/workspaces/workspace-1/activity?period=week");
    await screen.findByRole("heading", { name: "Activity" });
    expect(screen.getByRole("radiogroup", { name: "Period" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Week" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Week" })).toHaveAttribute("title", "Show week activity");
    expect(screen.getByRole("checkbox", { name: "Group repeated reads" })).toBeChecked();
    expect((await axe(container)).violations).toEqual([]);
  });

  it("requests the workspace scope from the server before rendering its bounded result", async () => {
    const { fetch } = show({ [feedUrl({ kind: "workspace" })]: data([entry(event("draft", "Draft created"))]) }, "/workspaces/workspace-1/activity?kind=workspace");
    expect(await screen.findByText("Draft created")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Workspace" })).toHaveAttribute("aria-checked", "true");
    expect(fetch).toHaveBeenCalledWith(feedUrl({ kind: "workspace" }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(screen.queryByText("Signed in")).not.toBeInTheDocument();
  });

  it("marks a lane card as a lower bound only from that lane's server has_more", async () => {
    const body = data([entry(event("history", "History event"))]);
    body.data.lanes.history.has_more = true;
    show({ [feedUrl()]: body });
    await screen.findByText("History event");
    expect(document.querySelector('.metric-card[href^="/workspaces/workspace-1/activity/history"] strong')).toHaveTextContent(/^1\+$/);
    expect(document.querySelector('.metric-card[href^="/workspaces/workspace-1/activity/advisory"] strong')).toHaveTextContent(/^0$/);
  });
  it("defaults to All and renders both workspace and system events", async () => {
    show({ [feedUrl()]: data([entry(event("draft", "Draft created")), entry(event("auth", "auth.session.created", { body: "auth.session.created" }))]) });
    expect(await screen.findByText("Draft created")).toBeInTheDocument();
    expect(screen.getByText("Signed in")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "All" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("Repeated reads grouped")).toBeInTheDocument();
    expect(screen.queryByText("Filtered")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reset" })).not.toBeInTheDocument();
  });

  it("refetches a selected subtype from the server and renders that explicit response", async () => {
    const { fetch } = show({
      [feedUrl({ kind: "workspace" })]: data([entry(event("context", "Page updated")), entry(event("library", "Source registered"))]),
      [feedUrl({ kind: "workspace", subtype: "library" })]: data([entry(event("library", "Source registered"))]),
    }, "/workspaces/workspace-1/activity?kind=workspace");
    await screen.findByText("Page updated");
    await userEvent.click(screen.getByRole("radio", { name: "Library" }));
    expect(await screen.findByText("Source registered")).toBeInTheDocument();
    expect(screen.queryByText("Page updated")).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(feedUrl({ kind: "workspace", subtype: "library" }), expect.any(Object));
  });

  it("omits subtypes for All and rejects inherited or incompatible filter keys", async () => {
    show({ [feedUrl()]: data([]) }, "/workspaces/workspace-1/activity?kind=constructor&subtype=__proto__&period=never");
    await screen.findByRole("heading", { name: "Activity" });
    expect(screen.queryByRole("radiogroup", { name: "Sub-type" })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "All" })).toHaveAttribute("aria-checked", "true");
  });

  it("previews five entries but keeps an always-visible scoped See all link", async () => {
    const entries = Array.from({ length: 7 }, (_, n) => entry(event(`event-${n}`, `Event ${n}`)));
    show({ [feedUrl({ period: "week", kind: "workspace", subtype: "context", group_reads: false })]: data(entries) }, "/workspaces/workspace-1/activity?period=week&kind=workspace&subtype=context&group_reads=0");
    expect(await screen.findByText("Event 4")).toBeInTheDocument();
    expect(screen.queryByText("Event 5")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View all history activity" })).toHaveAttribute("href", "/workspaces/workspace-1/activity/history?period=week&kind=workspace&subtype=context&group_reads=0");
    expect(screen.getByRole("link", { name: "View all pending activity" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View all advisory activity" })).toBeInTheDocument();
  });

  it("pins pending reviews before other pending entries prior to the five-entry cutoff", async () => {
    const entries = [...Array.from({ length: 5 }, (_, n) => entry(event(`job-${5-n}`, `Failed Job ${5-n}`, { lane: "pending" }))), entry(event("review", "Review Decision", { lane: "pending", review_item_id: "review-1" }))];
    show({ [feedUrl()]: data(entries) });
    const review = await screen.findByText("Review Decision");
    const job = screen.getByText("Failed Job 5");
    expect(screen.queryByText("Failed Job 1")).not.toBeInTheDocument();
    expect(review.compareDocumentPosition(job) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("renders auth events as human-readable system activity", async () => {
    show({ [feedUrl({ kind: "system" })]: data([entry(event("auth", "auth.session.created", { body: "auth.session.created" }))]) }, "/workspaces/workspace-1/activity?kind=system");
    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    expect(screen.getByText("A user session was created.")).toBeInTheDocument();
    expect(screen.queryByText("auth.session.created")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "System" })).toHaveAttribute("aria-checked", "true");
  });

  it("keeps OAuth requests in the system security scope", async () => {
    show({ [feedUrl({ kind: "system" })]: data([entry(event("oauth", "Codex CLI requested MCP OAuth access", { body: "oauth.authorization.requested", target: { target_kind: "oauth_authorization", target_id: "user-1" } }))]) }, "/workspaces/workspace-1/activity?kind=system");
    expect(await screen.findByText("Codex CLI requested MCP OAuth access")).toBeInTheDocument();
    expect(screen.getByText("An MCP client requested access to this workspace.")).toBeInTheDocument();
    expect(screen.queryByText("oauth.authorization.requested")).not.toBeInTheDocument();
  });

  it("uses structured MCP operation/result copy and keeps unknown tool IDs out of page links", async () => {
    show({ [feedUrl({ kind: "mcp" })]: data([entry(event("search", "MCP search succeeded", { operation: "search", result_status: "succeeded", target: { target_kind: "mcp_tool_call", target_id: "call-1" }, actor: { actor_kind: "mcp_client", actor_id: "client-1", display_name: "Codex CLI", is_current_user: false } }))]) }, "/workspaces/workspace-1/activity?kind=mcp");
    expect(await screen.findByText("Searched workspace resources")).toBeInTheDocument();
    expect(screen.queryByText("MCP search succeeded")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View MCP activity" })).toHaveAttribute("href", "/workspaces/workspace-1/activity/history?period=all&kind=mcp&subtype=all&group_reads=1");
  });

  it("requests a deep-linked review subtype explicitly", async () => {
    const { fetch } = show({ [feedUrl({ kind: "workspace", subtype: "review" })]: data([entry(event("review", "Approved Pricing page"))]) }, "/workspaces/workspace-1/activity?kind=workspace&subtype=review");
    expect(await screen.findByText("Approved Pricing page")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Review" })).toHaveAttribute("aria-checked", "true");
    expect(fetch).toHaveBeenCalledWith(feedUrl({ kind: "workspace", subtype: "review" }), expect.any(Object));
  });

  it("keeps active scope, grouping mode and Reset visible when filters are folded", async () => {
    show({ [feedUrl({ period: "today", kind: "workspace", group_reads: false })]: data([]) }, "/workspaces/workspace-1/activity?period=today&kind=workspace&group_reads=0");
    await screen.findByRole("heading", { name: "Activity" });
    await userEvent.click(screen.getByRole("button", { name: "Filters (2)" }));
    expect(screen.queryByRole("radiogroup", { name: "Period" })).not.toBeInTheDocument();
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("Individual events")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Group repeated reads" })).not.toBeChecked();
  });

  it("uses the server has_more signal and does not infer a total or per-lane plus from 50 entries", async () => {
    const entries = Array.from({ length: 50 }, (_, n) => entry(event(`event-${n}`, `Event ${n}`)));
    const first = show({ [feedUrl()]: data(entries, { has_more: false }) });
    await screen.findByText("Event 0");
    expect(screen.queryByText(/display entries in the current query range/)).not.toBeInTheDocument();
    expect(screen.getByText("50 entries · 50 original events")).toBeInTheDocument();
    expect(first.container.textContent).not.toContain("50+");
    first.unmount();
    show({ [feedUrl()]: data(entries, { has_more: true, next_cursor: "next" }) });
    expect(await screen.findByText(/Showing 50 display entries in the current query range/)).toBeInTheDocument();
  });

  it("reports the actual number of display entries for a truncated grouped query", async () => {
    const grouped = entry(event("group", "MCP read succeeded", { operation: "read", result_status: "succeeded", target: { target_kind: "mcp_tool_call", target_id: "call-1" } }), 4);
    show({ [feedUrl()]: data([grouped, entry(event("single", "One separate activity"))], { has_more: true, next_cursor: "next", returned_entry_count: 2 }) });
    expect(await screen.findByText("Showing 2 display entries in the current query range. Open a lane to load older activity.")).toBeInTheDocument();
    expect(screen.queryByText(/newest 50 events/)).not.toBeInTheDocument();
  });

  it("starts mobile filters folded while retaining active scope and grouping control", async () => {
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: query === "(max-width: 640px)", media: query, onchange: null, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn() })));
    show({ [feedUrl({ period: "today", kind: "workspace" })]: data([]) }, "/workspaces/workspace-1/activity?period=today&kind=workspace");
    await screen.findByRole("heading", { name: "Activity" });
    expect(screen.getByRole("button", { name: "Filters (2)" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("radiogroup", { name: "Period" })).not.toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Group repeated reads" })).toBeChecked();
  });

  it("preserves pending review summaries without inventing an actor", async () => {
    const summary = "Agent activation for editorial review";
    show({ [feedUrl()]: data([entry(event("pending", "Review required: agent_page_activation", { body: summary, target_title: summary, target: { target_kind: "change_set", target_id: "change-1" }, actor: null, lane: "pending", review_item_id: "review-1" }))]) });
    const row = (await screen.findByText(summary)).closest("article")!;
    expect(within(row).getByText("Review request")).toBeInTheDocument();
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  });

  it("keeps See all available on empty system lanes without Context creation actions", async () => {
    show({ [feedUrl({ kind: "system" })]: data([]) }, "/workspaces/workspace-1/activity?kind=system");
    await screen.findByRole("heading", { name: "Activity" });
    expect(screen.getAllByText("No system events for this period.")).toHaveLength(3);
    expect(screen.getByRole("link", { name: "View all pending activity" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View all advisory activity" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View all history activity" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create a new page" })).not.toBeInTheDocument();
  });

  it.each([
    { role: "editor", visible: true },
    { role: "viewer", visible: false },
  ])("shows empty MCP Chats actions only for editors ($role)", async ({ role, visible }) => {
    activityPreferences.role = role;
    show({ [feedUrl({ kind: "mcp" })]: data([]) }, "/workspaces/workspace-1/activity?kind=mcp");
    await screen.findByRole("heading", { name: "Activity" });
    expect(screen.getAllByRole("link", { name: "Open MCP setup" })).toHaveLength(3);
    const links = screen.queryAllByRole("link", { name: "Open Chats" });
    expect(links).toHaveLength(visible ? 3 : 0);
    for (const link of links) expect(link).toHaveAttribute("href", "/workspaces/workspace-1/chats");
  });

  it("distinguishes a grouped entry from its four represented original events and resets on grouping change", async () => {
    const read = event("read", "MCP read succeeded", { operation: "read", result_status: "succeeded", target: { target_kind: "mcp_tool_call", target_id: "call-1" }, navigation: { target_kind: "context_page", target_id: "page-1", title: "Guide", href: "/workspaces/workspace-1/context/pages/page-1" } });
    const { fetch } = show({ [feedUrl()]: data([entry(read, 4)]), [feedUrl({ group_reads: false })]: data(Array.from({ length: 4 }, (_, n) => entry({ ...read, id: `read-${n}` }))) });
    expect((await screen.findAllByText("1 entry · 4 original events")).length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Read Guide 4 times" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "Group repeated reads" }));
    expect(await screen.findAllByRole("link", { name: "Read Guide" })).toHaveLength(4);
    expect(screen.getAllByText("4 entries · 4 original events").length).toBeGreaterThan(0);
    expect(fetch).toHaveBeenCalledWith(feedUrl({ group_reads: false }), expect.any(Object));
  });

  it("restores period, kind, subtype and grouping from Back/Forward navigation", async () => {
    show({ [feedUrl()]: data([entry(event("default", "Default activity"))]), [feedUrl({ period: "week", kind: "mcp", subtype: "read", group_reads: false })]: data([entry(event("scoped", "Scoped activity"))]) }, "/workspaces/workspace-1/activity", true);
    await screen.findByText("Default activity");
    await userEvent.click(screen.getByRole("button", { name: "Open scoped activity" }));
    expect(await screen.findByText("Scoped activity")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Read" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("checkbox", { name: "Group repeated reads" })).not.toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByText("Default activity")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Group repeated reads" })).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(await screen.findByText("Scoped activity")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Week" })).toHaveAttribute("aria-checked", "true");
  });
});

describe("ActivityLanePage", () => {
  it("appends the cursor page without replacing prior entries and retains scoped overview breadcrumb", async () => {
    show({ [feedUrl({ lane: "history", kind: "mcp", subtype: "read", period: "week", group_reads: false })]: data([entry(event("first", "Event 1"))], { next_cursor: "CURSOR2", has_more: true }), [feedUrl({ lane: "history", kind: "mcp", subtype: "read", period: "week", group_reads: false, cursor: "CURSOR2" })]: data([entry(event("second", "Event 2"))]) }, "/workspaces/workspace-1/activity/history?period=week&kind=mcp&subtype=read&group_reads=0");
    await screen.findByText("Event 1");
    expect(screen.getByRole("link", { name: "Activity" })).toHaveAttribute("href", "/workspaces/workspace-1/activity?period=week&kind=mcp&subtype=read&group_reads=0");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Event 2")).toBeInTheDocument();
    expect(screen.getByText("Event 1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("All matching activity in this snapshot is shown");
  });

  it("preserves entries and retryable pagination after an unexpected next-page error without raw error text", async () => {
    let attempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === WORKSPACES_URL) return jsonResponse(workspace);
      if (url === feedUrl({ lane: "history" })) return jsonResponse(data([entry(event("first", "Event 1"))], { next_cursor: "CURSOR2", has_more: true }));
      if (url === feedUrl({ lane: "history", cursor: "CURSOR2" })) {
        if (attempts++ === 0) throw new Error("raw req-secret");
        return jsonResponse(data([entry(event("second", "Event 2"))]));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    render(<MemoryRouter initialEntries={["/workspaces/workspace-1/activity/history"]}><Routes><Route path="/workspaces/:workspaceId/activity/:lane" element={<ActivityLanePage />} /></Routes></MemoryRouter>);
    await screen.findByText("Event 1");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Failed to load more")).toBeInTheDocument();
    expect(screen.queryByText("raw req-secret")).not.toBeInTheDocument();
    expect(screen.getByText("Event 1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Event 2")).toBeInTheDocument();
  });

  it("keeps pagination reachable when an empty server page still has a cursor", async () => {
    show({ [feedUrl({ lane: "history", kind: "system" })]: data([], { next_cursor: "CURSOR2", has_more: true }), [feedUrl({ lane: "history", kind: "system", cursor: "CURSOR2" })]: data([entry(event("later", "Later system activity"))]) }, "/workspaces/workspace-1/activity/history?kind=system");
    await screen.findByText("No system events for this period.");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Later system activity")).toBeInTheDocument();
  });

  it("discards a pending pagination response after the filter scope changes", async () => {
    let complete!: (response: Response) => void;
    const old = data([entry(event("late", "Stale old-scope event"))]);
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === WORKSPACES_URL) return jsonResponse(workspace);
      if (url === feedUrl({ lane: "history" })) return jsonResponse(data([entry(event("first", "First scope event"))], { next_cursor: "CURSOR2", has_more: true }));
      if (url === feedUrl({ lane: "history", cursor: "CURSOR2" })) return new Promise<Response>(resolve => { complete = resolve; });
      if (url === feedUrl({ lane: "history", kind: "system" })) return jsonResponse(data([entry(event("new", "New system scope"))]));
      throw new Error(`Unexpected request: ${url}`);
    });
    render(<MemoryRouter initialEntries={["/workspaces/workspace-1/activity/history"]}><Routes><Route path="/workspaces/:workspaceId/activity/:lane" element={<ActivityLanePage />} /></Routes></MemoryRouter>);
    await screen.findByText("First scope event");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await userEvent.click(screen.getByRole("radio", { name: "System" }));
    expect(await screen.findByText("New system scope")).toBeInTheDocument();
    await act(async () => complete(jsonResponse(old)));
    await waitFor(() => expect(screen.queryByText("Stale old-scope event")).not.toBeInTheDocument());
    expect(screen.queryByText("First scope event")).not.toBeInTheDocument();
  });
});

describe.each([undefined, "history"] as const)("Activity recovery (%s)", lane => {
  const initial = lane ? `/workspaces/workspace-1/activity/${lane}` : "/workspaces/workspace-1/activity";
  function renderRoute() { return render(<MemoryRouter initialEntries={[initial]}><Routes><Route path="/workspaces/workspace-1/activity" element={<ActivityScreen />} /><Route path="/workspaces/:workspaceId/activity/:lane" element={<ActivityLanePage />} /></Routes></MemoryRouter>); }

  it("shows loading before data rather than an empty activity message or setup action", async () => {
    let complete!: (response: Response) => void;
    const pending = new Promise<Response>(resolve => { complete = resolve; });
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === WORKSPACES_URL) return jsonResponse(workspace);
      if (url === feedUrl({ lane })) return pending;
      throw new Error(`Unexpected request: ${url}`);
    });
    renderRoute();
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(screen.queryByText(/No .* activity/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create a new page" })).not.toBeInTheDocument();
    await act(async () => complete(jsonResponse(data([entry(event("loaded", "Loaded activity"))]))));
    expect(await screen.findByText("Loaded activity")).toBeInTheDocument();
  });

  it("distinguishes an initial forbidden response from emptiness and supports retry", async () => {
    let attempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === WORKSPACES_URL) return jsonResponse(workspace);
      if (url === feedUrl({ lane })) return attempts++ === 0
        ? jsonResponse({ error_code: "forbidden", message: "Denied", request_id: "test", warnings: [] }, 403)
        : jsonResponse(data([entry(event("recovered", "Recovered activity"))]));
      throw new Error(`Unexpected request: ${url}`);
    });
    renderRoute();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/No .* activity/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create a new page" })).not.toBeInTheDocument();
    expect(screen.queryByText(/0 entries/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reload activity" }));
    expect(await screen.findByText("Recovered activity")).toBeInTheDocument();
  });

  it.each([0, 1])("reconciles %s visible read-group members in place without re-snapshotting the feed", async visibleCount => {
    const grouped = entry(event("group", "MCP read succeeded", { operation: "read", result_status: "succeeded", target: { target_kind: "mcp_tool_call", target_id: "call-1" }, navigation: { target_kind: "context_page", target_id: "old-page", title: "Old private title", href: "/workspaces/workspace-1/context/pages/old-page" } }), 4);
    const survivor = { ...grouped.event, id: "visible-member", navigation: { target_kind: "context_page", target_id: "visible-page", title: "Visible survivor", href: "/workspaces/workspace-1/context/pages/visible-page" } };
    let feedAttempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === WORKSPACES_URL) return jsonResponse(workspace);
      if (url === feedUrl({ lane })) return feedAttempts++ === 0
        ? jsonResponse(data([grouped, entry(event("other", "Other visible activity"))]))
        : jsonResponse({ error_code: "service_unavailable", message: "Unavailable", request_id: "test", warnings: [] }, 503);
      if (url.endsWith("/workspaces/workspace-1/activity/read-group?token=group-group")) return jsonResponse({ data: { events: visibleCount ? [survivor] : [], event_count: visibleCount, group_id: grouped.id } });
      throw new Error(`Unexpected request: ${url}`);
    });
    renderRoute();
    await screen.findByRole("link", { name: "Read Old private title 4 times" });
    await userEvent.click(screen.getByRole("button", { name: "Expand 4 original events" }));
    await waitFor(() => expect(screen.queryByText(/Old private title/)).not.toBeInTheDocument());
    expect(screen.getByText("Other visible activity")).toBeInTheDocument();
    expect(screen.queryByText(/5 original events/)).not.toBeInTheDocument();
    if (visibleCount) {
      expect(screen.getByRole("link", { name: "Read Visible survivor" })).toHaveAttribute("href", "/workspaces/workspace-1/context/pages/visible-page");
      expect(screen.getAllByText("2 entries · 2 original events").length).toBeGreaterThan(0);
    } else expect(screen.getAllByText("1 entry · 1 original event").length).toBeGreaterThan(0);
    // A re-snapshot would collapse the opened group and drop loaded pages.
    expect(feedAttempts).toBe(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
  it("keeps the loaded group and counts when an invalid token is followed by a failed reload", async () => {
    const grouped = entry(event("group", "MCP read succeeded", { operation: "read", result_status: "succeeded", target: { target_kind: "mcp_tool_call", target_id: "call-1" }, navigation: { target_kind: "context_page", target_id: "page-1", title: "Known guide", href: "/workspaces/workspace-1/context/pages/page-1" } }), 4);
    let feedAttempts = 0;
    let detailAttempts = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
      const url = input.toString();
      if (url === WORKSPACES_URL) return jsonResponse(workspace);
      if (url === feedUrl({ lane })) return feedAttempts++ === 0
        ? jsonResponse(data([grouped, entry(event("other", "Other activity"))]))
        : jsonResponse({ error_code: "service_unavailable", message: "Unavailable", request_id: "test", warnings: [] }, 503);
      if (url.endsWith("/workspaces/workspace-1/activity/read-group?token=group-group")) {
        detailAttempts++;
        return jsonResponse({ error_code: "invalid_activity_token", message: "Expired token", request_id: "test", warnings: [] }, 400);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    renderRoute();
    await screen.findByRole("link", { name: "Read Known guide 4 times" });
    await userEvent.click(screen.getByRole("button", { name: "Expand 4 original events" }));
    expect(await screen.findByText("Original events could not be loaded. Try again.")).toBeVisible();
    expect(await screen.findByText("The service is temporarily unavailable. Try again shortly.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Read Known guide 4 times" })).toBeVisible();
    expect(screen.getAllByText("2 entries · 5 original events").length).toBeGreaterThan(0);
    expect(screen.getByText("Other activity")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Original events could not be loaded. Try again.")).toBeVisible();
    expect(await screen.findByText("The service is temporarily unavailable. Try again shortly.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Read Known guide 4 times" })).toBeVisible();
    expect(screen.getAllByText("2 entries · 5 original events").length).toBeGreaterThan(0);
    expect(detailAttempts).toBe(2);
  });

});


describe("Activity overview lane count boundaries", () => {
  it.each([
    [0, 0, "0 entries · 0 original events"],
    [1, 1, "1 entry · 1 original event"],
    [5, 5, "5 entries · 5 original events"],
    [6, 5, "5 entries · 5 original events"],
  ] as const)("previews %s fetched history entries as %s display entries", async (returned, visible, label) => {
    show({ [feedUrl({ period: "week", kind: "workspace", group_reads: false })]: data(Array.from({ length: returned }, (_, n) => entry(event(`boundary-${n}`, `Boundary event ${n}`)))) }, "/workspaces/workspace-1/activity?period=week&kind=workspace&group_reads=0");
    const link = await screen.findByRole("link", { name: "View all history activity" });
    expect(link).toHaveAttribute("href", "/workspaces/workspace-1/activity/history?period=week&kind=workspace&subtype=all&group_reads=0");
    const section = link.closest("section")!;
    expect(section.querySelectorAll(".activity-feed-entry")).toHaveLength(visible);
    expect(within(section).getByText(label)).toBeInTheDocument();
    expect(section.querySelector(".count-chip")).toHaveTextContent(`${visible} ${visible === 1 ? "entry" : "entries"}`);
    expect(section.textContent).not.toContain("+");
    if (returned === 6) expect(screen.queryByText("Boundary event 5")).not.toBeInTheDocument();
  });
});
