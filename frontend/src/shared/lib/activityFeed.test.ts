import { afterEach, describe, expect, it } from "vitest";

import { i18n } from "../i18n";
import type { ActivityFeedEvent } from "./activityFeed";
import { activityAction, activityCountLabel, activityEntryCounts, activityHref, activityNavigation, parseActivityQuery, presentActivityFeedEvent } from "./activityFeed";

function feedEvent(overrides: Partial<ActivityFeedEvent> = {}): ActivityFeedEvent {
  return { id: "event-1", lane: "history", title: "MCP update_context_edit succeeded", body: "private raw input must not be used", actor: { actor_kind: "mcp_client", display_name: "Codex", credential_owner_display_name: "Owner", actor_id: "actor-1", is_current_user: false }, target: { target_kind: "mcp_tool_call", target_id: "tool-call-not-chat-id" }, target_title: null, review_item_id: null, affordances: [], created_at: "2026-10-20T04:56:00Z", event_key: null, event_args: {}, operation: "edit", result_status: "succeeded", ...overrides };
}

afterEach(async () => { await i18n.changeLanguage("en"); });

describe("activity feed presenter", () => {
  it.each(["failed", "conflict", "rejected_by_policy"])("does not describe a %s write as a successful edit", (result_status) => {
    const event = feedEvent({ result_status });
    expect(activityAction(event)).toBe("failed");
    expect(presentActivityFeedEvent(event).title).not.toContain("Updated a draft");
  });
  it.each([["partial", "partial"], ["accepted", "accepted"], ["needs_review", "accepted"], ["unknown", "logged"], [null, "logged"]] as const)("preserves %s outcomes", (result_status, action) => {
    expect(activityAction(feedEvent({ result_status }))).toBe(action);
  });
  it.each([["delete", "deleted"], ["review", "reviewed"], ["log", "logged"]] as const)("gives the %s operation its own badge", (operation, action) => {
    expect(activityAction(feedEvent({ operation }))).toBe(action);
  });
  it("uses a closed vocabulary and does not expose raw tool payloads or guessed hidden names", () => {
    const shown = presentActivityFeedEvent(feedEvent({ operation: "__proto__", tool_name: "feed.mcpOperations.edit", result_status: "constructor", target_title: "Hidden title" }));
    expect(shown.title).toBe("MCP tool execution record");
    expect(shown.body).toBe("");
    expect(shown.action).toBe("logged");
  });
  it("uses the same codes for translated titles and repeated-read labels", async () => {
    const event = feedEvent({ operation: "read", navigation: { target_kind: "context_page", target_id: "page-1", title: "Guide", href: "/workspaces/workspace-1/context/pages/page-1" } });
    expect(presentActivityFeedEvent(event, 4).title).toBe("Read Guide 4 times");
    await i18n.changeLanguage("ko");
    expect(presentActivityFeedEvent(event, 4).title).toContain("4회 읽었습니다");
    expect(presentActivityFeedEvent(event).action).toBe("read");
  });
  it("requires both a pending lane and a review id for the review action", () => {
    expect(presentActivityFeedEvent(feedEvent({ lane: "pending", review_item_id: "review-1" })).pendingReview).toBe(true);
    expect(presentActivityFeedEvent(feedEvent({ lane: "history", review_item_id: "review-1" })).pendingReview).toBe(false);
  });
});

describe("activity navigation and query scope", () => {
  it("routes review title and chip from one exact URL", () => {
    expect(activityNavigation(feedEvent({ lane: "pending", review_item_id: "review-1" }), parseActivityQuery(new URLSearchParams()), "workspace-1").href).toBe("/workspaces/workspace-1/review/review-1");
  });
  it("never treats a tool call ID as a Chat or document id", () => {
    const query = parseActivityQuery(new URLSearchParams("period=week&kind=workspace&subtype=context&group_reads=0"));
    const navigation = activityNavigation(feedEvent(), query, "workspace-1");
    expect(navigation.href).toBeNull();
    expect(navigation.fallbackHref).toBe("/workspaces/workspace-1/activity/history?period=week&kind=mcp&subtype=all&group_reads=0");
  });
  it("accepts only matching resource routes from the projection", () => {
    const navigation = { target_kind: "context_page", target_id: "page-1", title: "Guide", href: "/workspaces/workspace-1/context/pages/page-1" };
    expect(activityNavigation(feedEvent({ navigation }), parseActivityQuery(new URLSearchParams()), "workspace-1").href).toBe(navigation.href);
    expect(activityNavigation(feedEvent({ navigation: { ...navigation, href: "https://example.com" } }), parseActivityQuery(new URLSearchParams()), "workspace-1").href).toBeNull();
  });
  it("normalizes unknown filter values and preserves all scope on overview/lane links", () => {
    expect(parseActivityQuery(new URLSearchParams("kind=constructor&subtype=__proto__&period=never"))).toEqual({ kind: "all", subtype: "all", period: "all", group_reads: true });
    const query = parseActivityQuery(new URLSearchParams("kind=mcp&subtype=read&period=month&group_reads=0"));
    expect(activityHref(query, undefined, "workspace-1")).toBe("/workspaces/workspace-1/activity?period=month&kind=mcp&subtype=read&group_reads=0");
    expect(activityHref(query, "history", "workspace-1")).toContain("/workspaces/workspace-1/activity/history?");
  });
  it("distinguishes displayed entries from represented original events without a fake total", () => {
    const event = feedEvent();
    const entries = [1, 4].map((event_count, index) => ({ entry_type: "event" as const, id: String(index), event, event_count, first_occurred_at: event.created_at, last_occurred_at: event.created_at }));
    expect(activityEntryCounts(entries)).toEqual({ returned_entry_count: 2, represented_event_count: 5 });
    expect(activityCountLabel(2, 5)).toBe("2 entries · 5 original events");
  });
});
