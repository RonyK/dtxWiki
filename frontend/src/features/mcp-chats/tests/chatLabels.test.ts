import { describe, expect, it } from "vitest";

import type { McpChatSessionRef } from "../../../shared/api/operations";
import {
  COUNT_SUMMARY_RE,
  isCountSummary,
  linkedArtifactSummary,
} from "../chatLabels";

function artifact(
  overrides: Partial<McpChatSessionRef["linked_artifacts"][number]> = {},
): McpChatSessionRef["linked_artifacts"][number] {
  return {
    id: "artifact-link-1",
    resource_kind: "context_page",
    resource_id: "page-1",
    artifact_title: "Authentication policy",
    navigation_path: "/context?target=page-1",
    verb: "referenced",
    first_occurred_at: "2026-05-27T09:01:00Z",
    last_occurred_at: "2026-05-27T09:02:00Z",
    occurrence_count: 2,
    last_mcp_tool_call_id: "tool-1",
    ...overrides,
  };
}

describe("isCountSummary", () => {
  it("matches legacy count-only summaries", () => {
    expect(isCountSummary("3 tool calls")).toBe(true);
    expect(isCountSummary("1 tool call")).toBe(true);
    expect(isCountSummary("3 tool calls, 1 write")).toBe(true);
    expect(isCountSummary("3 tool calls, 2 writes, 1 error")).toBe(true);
    // Whitespace is trimmed before matching.
    expect(isCountSummary("  3 tool calls  ")).toBe(true);
  });

  it("leaves semantic summaries untouched", () => {
    expect(isCountSummary("Search and read activity")).toBe(false);
    expect(isCountSummary("Created: Policy Analysis")).toBe(false);
    // A human summary that merely mentions tool calls is not a count string.
    expect(isCountSummary("Ran 3 tool calls to verify the policy")).toBe(false);
  });

  it("exports the underlying regex for reuse", () => {
    expect(COUNT_SUMMARY_RE.test("3 tool calls")).toBe(true);
  });
});

describe("linkedArtifactSummary", () => {
  it("returns null when there are no linked artifacts", () => {
    expect(linkedArtifactSummary([])).toBeNull();
  });

  it("prefers a non-referenced artifact and renders Verb: Title", () => {
    const summary = linkedArtifactSummary([
      artifact({ verb: "referenced", artifact_title: "Read page" }),
      artifact({ verb: "created", artifact_title: "Policy Analysis" }),
    ]);
    expect(summary).toBe("Created: Policy Analysis");
  });

  it("skips search matches when a semantic artifact is available", () => {
    const summary = linkedArtifactSummary([
      artifact({ verb: "search_match", artifact_title: "Search result" }),
      artifact({ verb: "edited", artifact_title: "Reviewed policy" }),
    ]);
    expect(summary).toBe("Edited: Reviewed policy");
  });

  it("falls back to the first artifact when all are referenced", () => {
    const summary = linkedArtifactSummary([
      artifact({ verb: "referenced", artifact_title: "Authentication policy" }),
    ]);
    expect(summary).toBe("Referenced: Authentication policy");
  });

  it("falls back to a generic label, not the raw kind, when no title is present", () => {
    // resource_kind is an internal wire identifier (e.g. "library_item",
    // "context_media_asset"), not a user-facing string -- it must never
    // appear in this line even when the artifact has no title.
    const summary = linkedArtifactSummary([
      artifact({
        verb: "edited",
        artifact_title: null,
        resource_kind: "library_item",
      }),
    ]);
    expect(summary).toBe("Edited: Artifact");
  });
});
