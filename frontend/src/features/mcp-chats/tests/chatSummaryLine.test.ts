import { describe, expect, it } from "vitest";

import { chatFailureCounts, chatSummaryLine } from "../chatLabels";

import type { McpChatSessionRef } from "../../../shared/api/operations";

const artifacts: McpChatSessionRef["linked_artifacts"] = [
  {
    id: "link-1",
    resource_kind: "context_page",
    resource_id: "page-1",
    artifact_title: "Retention Policy",
    navigation_path: null,
    verb: "created",
    occurrence_count: 1,
    first_occurred_at: "2026-07-01T00:00:00Z",
    last_occurred_at: "2026-07-01T00:00:00Z",
    last_mcp_tool_call_id: "call-1",
  },
];

describe("chatSummaryLine", () => {
  it("returns the agent's stated reason with its source so the UI can attribute it", () => {
    // MCP never delivers the user's own prompt. A reason presented bare reads
    // like a sentence they wrote, which is the misreading the label prevents.
    expect(
      chatSummaryLine({
        title: "Retention audit",
        summary: "3 tool calls, 1 write",
        intent_summary: "capture the retention decision",
        intent_source: "write_reason",
        linked_artifacts: artifacts,
      }),
    ).toEqual({ text: "capture the retention decision", source: "write_reason", artifactFallback: false });
  });

  it("does not display the stored English semantic summary", () => {
    expect(
      chatSummaryLine({
        title: "Retention audit",
        summary: "Created: Retention Policy",
        intent_summary: null,
        intent_source: null,
        linked_artifacts: artifacts,
      }),
    ).toBeNull();
  });

  it("does not turn a legacy artifact fallback into a display sentence", () => {
    expect(
      chatSummaryLine({
        title: "Retention audit",
        summary: "3 tool calls, 1 write",
        intent_summary: null,
        intent_source: null,
        linked_artifacts: artifacts,
      }),
    ).toBeNull();
  });

  it("returns nothing when there is nothing truthful to show", () => {
    expect(
      chatSummaryLine({
        title: "Retention audit",
        summary: "3 tool calls",
        intent_summary: null,
        intent_source: null,
        linked_artifacts: [],
      }),
    ).toBeNull();
  });

  it("suppresses a summary that repeats the title", () => {
    expect(
      chatSummaryLine({
        title: "Layout audit seed",
        summary: "Layout audit seed",
        intent_summary: null,
        intent_source: null,
        linked_artifacts: [],
      }),
    ).toBeNull();
  });

  it("suppresses agent evidence already used as the observable title subject", () => {
    expect(
      chatSummaryLine({
        title: "Search: “OAuth refresh”",
        summary: "1 tool call",
        intent_summary: "OAuth refresh",
        intent_source: "search_query",
        subject: "OAuth refresh",
        linked_artifacts: [],
      }),
    ).toBeNull();
  });
});

describe("chatFailureCounts", () => {
  it("splits refusals from breakages when the parts account for the total", () => {
    expect(
      chatFailureCounts({ error_count: 3, failed_count: 2, rejected_count: 1 }),
    ).toEqual({ failed: 2, rejected: 1 });
  });

  it("reports the total alone when the breakdown is unknown", () => {
    // Rows written before the split have zeros in the new counters. Showing
    // "0 refused" for a chat that was refused would be a lie.
    expect(
      chatFailureCounts({ error_count: 3, failed_count: 0, rejected_count: 0 }),
    ).toEqual({ combined: 3 });
  });

  it("shows nothing when nothing failed", () => {
    expect(
      chatFailureCounts({ error_count: 0, failed_count: 0, rejected_count: 0 }),
    ).toBeNull();
  });
});
