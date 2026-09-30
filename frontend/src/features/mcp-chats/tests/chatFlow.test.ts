import { describe, expect, it } from "vitest";

import type { McpChatTimelineItem } from "../../../shared/api/operations";
import {
  chatFlowPhaseForAction,
  chatFlowRepresentativeItem,
  distinctSubjects,
  groupChatFlow,
  isChatFlowError,
  totalLatencyMs,
} from "../chatFlow";

function call(overrides: Partial<McpChatTimelineItem> = {}): McpChatTimelineItem {
  return {
    id: `tool-${Math.random()}`,
    tool_name: "search",
    input_summary: {},
    input_availability: "available",
    result_status: "succeeded",
    result_summary: null,
    result_availability: "available",
    error_code: null,
    started_at: "2026-05-27T09:00:00Z",
    completed_at: "2026-05-27T09:00:01Z",
    latency_ms: 20,
    headline: "headline",
    title_source: "query",
    action: "search",
    subject: null,
    subject_kind: null,
    primary_tool_name: "search",
    schema_version: "v2",
    linked_verbs: [],
    linked_artifacts: [],
    ...overrides,
  };
}

describe("chatFlowPhaseForAction", () => {
  it("maps search to find and read to read", () => {
    expect(chatFlowPhaseForAction("search")).toBe("find");
    expect(chatFlowPhaseForAction("read")).toBe("read");
  });

  it("maps create, edit and cite to write", () => {
    expect(chatFlowPhaseForAction("create")).toBe("write");
    expect(chatFlowPhaseForAction("edit")).toBe("write");
    expect(chatFlowPhaseForAction("cite")).toBe("write");
  });

  it("maps guide and other to other", () => {
    expect(chatFlowPhaseForAction("guide")).toBe("other");
    expect(chatFlowPhaseForAction("other")).toBe("other");
  });
});

describe("isChatFlowError", () => {
  it("flags failed, rejected_by_policy and conflict", () => {
    expect(isChatFlowError("failed")).toBe(true);
    expect(isChatFlowError("rejected_by_policy")).toBe(true);
    expect(isChatFlowError("conflict")).toBe(true);
  });

  it("does not flag succeeded or partial", () => {
    expect(isChatFlowError("succeeded")).toBe(false);
    expect(isChatFlowError("partial")).toBe(false);
  });
});

describe("groupChatFlow", () => {
  it("collapses consecutive same-phase calls into one step", () => {
    const timeline = [
      call({ id: "1", tool_name: "search", action: "search" }),
      call({ id: "2", tool_name: "search", action: "search" }),
      call({ id: "3", tool_name: "read", action: "read" }),
    ];
    const { steps, bootstrap } = groupChatFlow(timeline);
    expect(bootstrap).toHaveLength(0);
    expect(steps).toHaveLength(2);
    expect(steps[0].phase).toBe("find");
    expect(steps[0].items.map((item) => item.id)).toEqual(["1", "2"]);
    expect(steps[1].phase).toBe("read");
    expect(steps[1].items.map((item) => item.id)).toEqual(["3"]);
  });

  it("excludes bootstrap tool calls from steps and reports them separately", () => {
    const timeline = [
      call({ id: "1", tool_name: "list_my_workspaces", action: "read" }),
      call({ id: "2", tool_name: "get_index", action: "read" }),
      call({ id: "3", tool_name: "search", action: "search" }),
    ];
    const { steps, bootstrap } = groupChatFlow(timeline);
    expect(bootstrap.map((item) => item.id)).toEqual(["1", "2"]);
    expect(steps).toHaveLength(1);
    expect(steps[0].items.map((item) => item.id)).toEqual(["3"]);
  });

  it("does not let a bootstrap call in between split a same-phase run", () => {
    // read, get_index (bootstrap, filtered out), read -> one continuous read step
    const timeline = [
      call({ id: "1", tool_name: "read_page", action: "read" }),
      call({ id: "2", tool_name: "get_index", action: "read" }),
      call({ id: "3", tool_name: "read_page", action: "read" }),
    ];
    const { steps, bootstrap } = groupChatFlow(timeline);
    expect(bootstrap.map((item) => item.id)).toEqual(["2"]);
    expect(steps).toHaveLength(1);
    expect(steps[0].items.map((item) => item.id)).toEqual(["1", "3"]);
  });

  it("splits a step and flags it when a call in it errors", () => {
    const timeline = [
      call({ id: "1", tool_name: "update_draft", action: "edit", result_status: "conflict" }),
      call({ id: "2", tool_name: "update_draft", action: "edit", result_status: "succeeded" }),
    ];
    const { steps } = groupChatFlow(timeline);
    expect(steps).toHaveLength(1);
    expect(steps[0].hasError).toBe(true);
  });

  it("keeps a workspace guide call as its own step, like any other call", () => {
    const timeline = [
      call({ id: "1", tool_name: "guide", action: "guide" }),
      call({ id: "2", tool_name: "search", action: "search" }),
    ];
    const { steps, bootstrap } = groupChatFlow(timeline);
    expect(bootstrap).toHaveLength(0);
    expect(steps.map((step) => step.phase)).toEqual(["other", "find"]);
    expect(steps[0].items.map((item) => item.id)).toEqual(["1"]);
  });

  it("gives a guide-only timeline a step instead of an empty flow", () => {
    const { steps, bootstrap } = groupChatFlow([
      call({ id: "1", tool_name: "guide", action: "guide" }),
    ]);
    expect(bootstrap).toHaveLength(0);
    expect(steps).toHaveLength(1);
    expect(steps[0].phase).toBe("other");
  });

  it("returns an all-bootstrap timeline as zero steps", () => {
    const timeline = [
      call({ id: "1", tool_name: "get_index", action: "read" }),
      call({ id: "2", tool_name: "list_my_workspaces", action: "read" }),
    ];
    const { steps, bootstrap } = groupChatFlow(timeline);
    expect(steps).toHaveLength(0);
    expect(bootstrap).toHaveLength(2);
  });

  it("returns no steps and no bootstrap for an empty timeline", () => {
    expect(groupChatFlow([])).toEqual({ bootstrap: [], steps: [] });
  });
});

describe("totalLatencyMs", () => {
  it("sums recorded latencies", () => {
    expect(totalLatencyMs([call({ latency_ms: 10 }), call({ latency_ms: 15 })])).toBe(25);
  });

  it("returns null when no call recorded a latency", () => {
    expect(totalLatencyMs([call({ latency_ms: null })])).toBeNull();
  });

  it("ignores calls with a null latency when summing the rest", () => {
    expect(totalLatencyMs([call({ latency_ms: null }), call({ latency_ms: 30 })])).toBe(30);
  });
});

describe("chatFlowRepresentativeItem", () => {
  it("returns the sole item unchanged", () => {
    const only = call({ id: "1", result_status: "succeeded" });
    expect(chatFlowRepresentativeItem([only])).toBe(only);
  });

  it("prefers a later successful call over an earlier failed attempt", () => {
    const failed = call({ id: "1", result_status: "conflict" });
    const retried = call({ id: "2", result_status: "succeeded" });
    expect(chatFlowRepresentativeItem([failed, retried])).toBe(retried);
  });

  it("prefers the most recent success when several calls preceded it", () => {
    const first = call({ id: "1", result_status: "succeeded" });
    const failed = call({ id: "2", result_status: "failed" });
    const latest = call({ id: "3", result_status: "succeeded" });
    expect(chatFlowRepresentativeItem([first, failed, latest])).toBe(latest);
  });

  it("falls back to the most recent call when every attempt failed", () => {
    const first = call({ id: "1", result_status: "failed" });
    const last = call({ id: "2", result_status: "rejected_by_policy" });
    expect(chatFlowRepresentativeItem([first, last])).toBe(last);
  });
});

describe("distinctSubjects", () => {
  it("keeps first-seen order and drops duplicates and empties", () => {
    const items = [
      call({ subject: "policy" }),
      call({ subject: "policy" }),
      call({ subject: null }),
      call({ subject: "rollback" }),
    ];
    expect(distinctSubjects(items)).toEqual(["policy", "rollback"]);
  });
});
