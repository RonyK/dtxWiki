import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../i18n";
import type { ActivityFeedEntry, ActivityFeedEvent } from "../../lib/activityFeed";
import { ActivityEntryRow } from "../ActivityEntryRow";

const owner = "A very long user name with no role suffix";
const client = "Codex CLI — a very long registered client name";
function entry(overrides: Partial<ActivityFeedEvent> = {}): ActivityFeedEntry {
  const event: ActivityFeedEvent = {
    id: "event-1", lane: "history", title: "MCP read succeeded", body: "Raw input=secret must not be visible", actor: { actor_kind: "mcp_client", display_name: client, credential_owner_display_name: owner, actor_id: "actor-1", is_current_user: false }, target: { target_kind: "mcp_tool_call", target_id: "call-1" }, review_item_id: null, affordances: [], created_at: "2026-10-20T04:56:00Z", event_key: null, event_args: {}, operation: "read", result_status: "succeeded", navigation: { target_kind: "context_page", target_id: "page-1", title: "Guide", href: "/context?target=page-1" }, ...overrides,
  };
  return { entry_type: "event", id: event.id, event, event_count: 1, first_occurred_at: event.created_at, last_occurred_at: event.created_at };
}
function show(value: ActivityFeedEntry, loadGroup?: (token: string) => Promise<ActivityFeedEvent[]>) {
  return render(<MemoryRouter><ActivityEntryRow entry={value} loadGroup={loadGroup} /></MemoryRouter>);
}
afterEach(async () => { await i18n.changeLanguage("en"); });

describe("shared activity entry", () => {
  it("puts type before the actor and makes the title the sole direct resource link", () => {
    const { container } = show(entry());
    const identity = container.querySelector(".activity-entry__identity")!;
    expect(identity.firstElementChild).toHaveTextContent("Context");
    expect(identity.lastElementChild?.tagName).toBe("BUTTON");
    expect(screen.getByRole("link", { name: "Read Guide" })).toHaveAttribute("href", "/context?target=page-1");
    expect(container.querySelectorAll("a a, a button, button a, button button")).toHaveLength(0);
    expect(screen.queryByText(/Raw input/)).not.toBeInTheDocument();
  });
  it("shows both full names by focus/click and closes with Escape without navigating", async () => {
    const user = userEvent.setup();
    show(entry());
    const actor = screen.getByRole("button", { name: `Show full names: ${owner} · ${client}` });
    await user.tab();
    expect(actor).toHaveFocus();
    expect(actor).toHaveAttribute("aria-expanded", "true");
    const popup = screen.getByRole("dialog", { name: "Full activity actor names" });
    expect(within(popup).getByText(owner)).toBeVisible();
    expect(within(popup).getByText(client)).toBeVisible();
    await user.keyboard("{Enter}");
    expect(actor).toHaveAttribute("aria-expanded", "true");
    await user.keyboard(" ");
    expect(actor).toHaveAttribute("aria-expanded", "false");
    await user.click(actor);
    expect(actor).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    expect(actor).toHaveFocus();
    expect(actor).toHaveAttribute("aria-expanded", "false");
  });
  it("lets keyboard users enter and scroll the full-name surface, then closes without reopening on focus return", async () => {
    const user = userEvent.setup();
    show(entry());
    const actor = screen.getByRole("button", { name: /^Show full names/ });
    await user.tab();
    const popup = screen.getByRole("dialog", { name: "Full activity actor names" });
    expect(actor).toHaveFocus();
    expect(popup).not.toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(popup).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(actor).toHaveFocus();
    expect(actor).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("dialog")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Close names" })).toHaveFocus();
    await user.keyboard(" ");
    expect(actor).toHaveFocus();
    expect(actor).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it.each(["Escape", "close button"])("stays dismissed through pointer entry and deferred focus after %s", async (dismissal) => {
    const user = userEvent.setup();
    show(entry());
    const actor = screen.getByRole("button", { name: /^Show full names/ });
    await user.tab();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("dialog")).toHaveFocus();
    if (dismissal === "close button") {
      await user.tab();
      expect(screen.getByRole("button", { name: "Close names" })).toHaveFocus();
    }

    // Deliver focus restoration after dismissal, without depending on a timer
    // or the browser's native focus-event ordering.
    const nativeFocus = actor.focus.bind(actor);
    let deferredFocus: (() => void) | undefined;
    const focusSpy = vi.spyOn(actor, "focus").mockImplementationOnce(() => {
      deferredFocus = nativeFocus;
    });
    await user.keyboard(dismissal === "Escape" ? "{Escape}" : " ");
    expect(actor).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(focusSpy).toHaveBeenCalledOnce();

    // Focus restoration can scroll the chip under a stationary pointer.
    fireEvent.pointerEnter(actor, { pointerType: "mouse" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => deferredFocus?.());
    focusSpy.mockRestore();
    expect(actor).toHaveFocus();
    expect(actor).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // Leaving by keyboard also restores hover previews, even when the pointer
    // never left the trigger after dismissal.
    await user.tab();
    fireEvent.pointerEnter(actor, { pointerType: "mouse" });
    expect(screen.getByRole("dialog")).toBeVisible();

    await user.keyboard("{Escape}");
    expect(actor).toHaveFocus();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.pointerEnter(actor, { pointerType: "mouse" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.pointerLeave(actor, { pointerType: "mouse" });
    fireEvent.pointerEnter(actor, { pointerType: "mouse" });
    expect(screen.getByRole("dialog")).toBeVisible();

    await user.tab();
    await user.tab({ shift: true });
    expect(actor).toHaveFocus();
    expect(screen.getByRole("dialog")).toBeVisible();
  });
  it("supports touch activation and outside dismissal of the combined name disclosure", async () => {
    const user = userEvent.setup();
    show(entry());
    const actor = screen.getByRole("button", { name: /^Show full names/ });
    await user.pointer([{ keys: "[TouchA>]", target: actor }, { keys: "[/TouchA]" }]);
    expect(actor).toHaveAttribute("aria-expanded", "true");
    fireEvent.pointerDown(document.body);
    expect(actor).toHaveAttribute("aria-expanded", "false");
  });
  it("uses the same review URL for the title and lower amber chip, without an extra review button or invented actor", async () => {
    await i18n.changeLanguage("ko");
    const { container } = show(entry({ actor: null, lane: "pending", review_item_id: "review-1", operation: "review" }));
    const title = screen.getByRole("link", { name: /^검토 열기:/ });
    const chip = screen.getByRole("link", { name: /^검토필요:/ });
    expect(title).toHaveAttribute("href", "/review?review=review-1");
    expect(chip).toHaveAttribute("href", title.getAttribute("href"));
    expect(within(chip).getByText("검토필요")).toBeVisible();
    expect(chip.querySelector("[data-action=pending]")).toBeInTheDocument();
    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(screen.queryByText(/작성자 정보 없음/)).not.toBeInTheDocument();
  });
  it("does not label a decided review as pending", () => {
    show(entry({ review_item_id: "review-1" }));
    expect(screen.queryByRole("link", { name: /^Review required:/ })).not.toBeInTheDocument();
  });
  it("retains a neutral unlinked title and scoped fallback for an unknown target", () => {
    show(entry({ navigation: null, operation: "tool", result_status: "unknown" }));
    expect(screen.getByText("MCP tool execution record")).toBeVisible();
    expect(screen.queryByRole("link", { name: "MCP tool execution record" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View MCP activity" })).toHaveAttribute("href", "/activity/history?period=all&kind=mcp&subtype=all&group_reads=1");
    expect(screen.getByText("No target link")).toBeVisible();
  });
  it("keeps only permitted technical metadata in an independent disclosure", async () => {
    const user = userEvent.setup();
    show(entry({ tool_name: "read_context", public_error_code: "validation_failed", duration_ms: 42 }));
    const disclosure = screen.getByText("Technical information");
    await user.click(disclosure);
    const details = disclosure.closest("details")!;
    expect(within(details).getByText("read_context")).toBeVisible();
    expect(within(details).getByText("42ms")).toBeVisible();
    expect(within(details).getByText("validation_failed")).toBeVisible();
    expect(details.closest("a")).toBeNull();
    expect(screen.getByRole("button", { name: /^Show full names/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/Raw input/)).not.toBeInTheDocument();
  });
});

describe("repeated read disclosure", () => {
  it("loads lazily, keeps the group after an error, retries, and returns focus on collapse", async () => {
    const user = userEvent.setup();
    const grouped = { ...entry(), entry_type: "read_group" as const, event_count: 4, group_token: "opaque-token" };
    const member = entry({ id: "member-1" }).event;
    const load = vi.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce([member]);
    const { container } = show(grouped, load);
    expect(load).not.toHaveBeenCalled();
    const expand = screen.getByRole("button", { name: "Expand 4 original events" });
    await user.click(expand);
    expect(await screen.findByRole("alert")).toHaveTextContent("Original events could not be loaded");
    expect(screen.getByRole("link", { name: "Read Guide 4 times" })).toBeVisible();
    expect(expand).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "Read Guide" })).toBeVisible();
    expect(load).toHaveBeenNthCalledWith(2, "opaque-token");
    expect(container.querySelectorAll(".activity-feed-entry")).toHaveLength(2);
    const collapse = screen.getAllByRole("button", { name: "Collapse events" });
    await user.click(collapse.at(-1)!);
    expect(expand).toHaveFocus();
    expect(expand).toHaveAttribute("aria-expanded", "false");
    await user.click(expand);
    await waitFor(() => expect(screen.getByRole("link", { name: "Read Guide" })).toBeVisible());
    expect(load).toHaveBeenCalledTimes(2);
  });
  it("preserves the group heading and loading status until the request completes", async () => {
    let resolve!: (events: ActivityFeedEvent[]) => void;
    const load = vi.fn(() => new Promise<ActivityFeedEvent[]>((done) => { resolve = done; }));
    show({ ...entry(), entry_type: "read_group", event_count: 4, group_token: "opaque-token" }, load);
    await userEvent.click(screen.getByRole("button", { name: "Expand 4 original events" }));
    expect(screen.getByRole("status")).toHaveTextContent("Loading original events");
    expect(screen.getByRole("link", { name: "Read Guide 4 times" })).toBeVisible();
    resolve([entry({ id: "member-1" }).event]);
    expect(await screen.findByRole("link", { name: "Read Guide" })).toBeVisible();
  });
});
