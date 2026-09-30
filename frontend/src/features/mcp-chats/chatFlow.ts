import type { McpChatTimelineItem } from "../../shared/api/operations";

export type ChatFlowPhase = "find" | "read" | "write" | "other";

export type ChatFlowStep = {
  /** Stable key: the id of the step's first call. */
  id: string;
  phase: ChatFlowPhase;
  items: McpChatTimelineItem[];
  hasError: boolean;
};

export type ChatFlowGrouping = {
  /** Orientation calls, excluded from `steps` (see BOOTSTRAP_TOOL_NAMES). */
  bootstrap: McpChatTimelineItem[];
  steps: ChatFlowStep[];
};

// These calls carry no reader-facing content (their only captured input is
// the workspace id), so grouping them into a flow step would render a step
// whose detail is just noise. They still count toward the session totals;
// they are only excluded from the flow steps and summarized separately.
//
// Deliberately narrower than backend/services/mcp_chat_service.py:
// _BOOTSTRAP_TOOLS, which decides title evidence rather than flow membership.
// `guide` is a title-less orientation call there, but here it is a step of
// its own: reading the workspace guide is something the agent did, and a
// guide-only chat should show that call instead of an empty flow.
const BOOTSTRAP_TOOL_NAMES = new Set([
  "get_index",
  "list_agent_categories",
  "list_my_workspaces",
  "list_pages",
]);

const WRITE_ACTIONS = new Set<McpChatTimelineItem["action"]>([
  "create",
  "edit",
  "cite",
]);

const ERROR_STATUSES = new Set(["failed", "rejected_by_policy", "conflict"]);

export function chatFlowPhaseForAction(
  action: McpChatTimelineItem["action"],
): ChatFlowPhase {
  if (action === "search") return "find";
  if (action === "read") return "read";
  if (WRITE_ACTIONS.has(action)) return "write";
  return "other";
}

export function isChatFlowError(status: string): boolean {
  return ERROR_STATUSES.has(status);
}

/**
 * Groups a chat's timeline into phase runs: consecutive non-bootstrap calls
 * sharing the same phase collapse into one step. Bootstrap calls are removed
 * from the sequence first, so a bootstrap call between two calls of the same
 * phase does not split them into separate steps.
 */
export function groupChatFlow(
  timeline: McpChatTimelineItem[],
): ChatFlowGrouping {
  const bootstrap: McpChatTimelineItem[] = [];
  const steps: ChatFlowStep[] = [];

  for (const item of timeline) {
    if (BOOTSTRAP_TOOL_NAMES.has(item.tool_name)) {
      bootstrap.push(item);
      continue;
    }
    const phase = chatFlowPhaseForAction(item.action);
    const hasError = isChatFlowError(item.result_status);
    const last = steps[steps.length - 1];
    if (last && last.phase === phase) {
      last.items.push(item);
      last.hasError = last.hasError || hasError;
    } else {
      steps.push({ id: item.id, phase, items: [item], hasError });
    }
  }

  return { bootstrap, steps };
}

/** Sums latency across calls that recorded one; null when none did. */
export function totalLatencyMs(items: McpChatTimelineItem[]): number | null {
  const known = items.filter(
    (item): item is McpChatTimelineItem & { latency_ms: number } =>
      item.latency_ms !== null,
  );
  if (known.length === 0) {
    return null;
  }
  return known.reduce((sum, item) => sum + item.latency_ms, 0);
}

/**
 * The call whose headline should represent a step in the rail.
 *
 * A step's first call is not always its best summary: "tried, conflicted,
 * retried, succeeded" is a common write pattern, and a failed call never
 * gets an artifact-derived title from the backend (see
 * PRESENTATION_ELIGIBLE_STATUSES in mcp_chat_service.py), so leading with it
 * would show a generic tool name for a step that actually succeeded. Prefer
 * the most recent non-error call; if every call in the step failed, fall
 * back to the most recent one so the step still reads as what was last
 * attempted.
 */
export function chatFlowRepresentativeItem(
  items: McpChatTimelineItem[],
): McpChatTimelineItem {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (!isChatFlowError(items[index].result_status)) {
      return items[index];
    }
  }
  return items[items.length - 1];
}


/** Distinct, truthy `subject` values in call order, for "and N more" suffixes. */
export function distinctSubjects(items: McpChatTimelineItem[]): string[] {
  const seen = new Set<string>();
  const subjects: string[] = [];
  for (const item of items) {
    if (item.subject && !seen.has(item.subject)) {
      seen.add(item.subject);
      subjects.push(item.subject);
    }
  }
  return subjects;
}
