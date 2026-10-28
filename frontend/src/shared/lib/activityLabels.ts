import type { ActivityItem } from "../api/operations";
import { TREE_TARGET_PARAM } from "../components/treeRouteState";
import { i18n } from "../i18n";
import { humanizeEnum } from "./domainLabel";

// F-8: deep-link an activity entry to the context/agent/library file it
// touched. Audit-history items carry the entity target_kind + id; every tree
// screen opens an entity via ?target=<id> (TREE_TARGET_PARAM).
//
// Shared rather than screen-local: Activity and Dashboard render the same
// feed, and when only Activity had this, a Dashboard row had no way to reach
// its target at all.
export function activityTargetHref(item: ActivityItem): string | null {
  const target = item.target;
  if (!target?.target_id) {
    return null;
  }
  switch (target.target_kind) {
    case "context_page":
      return `/context?${TREE_TARGET_PARAM}=${target.target_id}`;
    case "agent_page":
      return `/agent?${TREE_TARGET_PARAM}=${target.target_id}`;
    case "source":
    case "library_item":
      return `/library?${TREE_TARGET_PARAM}=${target.target_id}`;
    default:
      return null;
  }
}

const ACTIVITY_LABELS: Record<string, { title: string; body: string }> = {
  "auth.session.created": {
    title: "Signed in",
    body: "A user session was created.",
  },
  "auth.session.destroyed": {
    title: "Signed out",
    body: "A user session ended.",
  },
  "auth.session.renewed": {
    title: "Session extended",
    body: "A remembered browser session was renewed.",
  },
  "auth.user.signed_up": {
    title: "Account created",
    body: "A new account joined the workspace.",
  },
  "auth.password.changed": {
    title: "Password updated",
    body: "Account security settings changed.",
  },
  "oauth.authorization.requested": {
    title: "OAuth access requested",
    body: "An MCP client requested access to this workspace.",
  },
  "oauth.consent.granted": {
    title: "OAuth access granted",
    body: "An MCP client was authorized for this workspace.",
  },
  "oauth.permission.denied": {
    title: "OAuth access denied",
    body: "An MCP client access request was denied.",
  },
  "oauth.consent.revoked": {
    title: "OAuth access revoked",
    body: "An MCP client authorization was revoked.",
  },
};

const AUDIT_CODE_PATTERN = /^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$/i;

export function presentActivityItem(item: ActivityItem) {
  if (item.event_key) {
    return distinctPresentation({
      title: presentActivityEventTitle(item),
      body:
        item.body && AUDIT_CODE_PATTERN.test(item.body)
          ? targetKindLabel(item)
          : item.body,
    });
  }
  if (isMcpActivityItem(item)) {
    return distinctPresentation({
      title: item.title,
      body:
        item.body && item.body !== item.title
          ? item.body
          : item.target_title ?? targetKindLabel(item),
    });
  }
  const label = Object.hasOwn(ACTIVITY_LABELS, item.title)
    ? ACTIVITY_LABELS[item.title]
    : Object.hasOwn(ACTIVITY_LABELS, item.body ?? "") ? ACTIVITY_LABELS[item.body ?? ""] : undefined;
  if (!label) {
    // No hand-authored copy: resolve a translated label from the workspace
    // audit code (body "domain.entity.op") instead of leaking the raw code.
    const targetTitle =
      item.target?.target_kind === "job" && item.target_title
        ? jobTypeLabel(item.target_title)
        : item.target_title;
    const title = item.body
      && AUDIT_CODE_PATTERN.test(item.body)
      && !AUDIT_CODE_PATTERN.test(item.title)
      ? item.title
      : targetTitle ?? auditCodeLabel(item.title, item.body);
    const body = item.body && AUDIT_CODE_PATTERN.test(item.body)
      ? targetTitle ?? auditCodeLabel(item.title, item.body)
      : item.body && item.body !== item.title && item.body !== item.target_title
        ? item.body
        : targetKindLabel(item);
    return distinctPresentation({
      title,
      body,
    });
  }
  const hasSpecificSummary =
    item.title !== item.body && !Object.hasOwn(ACTIVITY_LABELS, item.title);
  return distinctPresentation({
    title: item.target_title ?? (hasSpecificSummary ? item.title : label.title),
    body: !item.body || item.body === item.title || Object.hasOwn(ACTIVITY_LABELS, item.body)
      || AUDIT_CODE_PATTERN.test(item.body)
      ? label.body
      : item.body,
  });
}

/** Translate keyed events; legacy/unrecognized rows preserve their audit text. */
export function presentActivityEventTitle(item: ActivityItem): string {
  if (!item.event_key) {
    return item.title;
  }
  return i18n.t(
    `activity:eventTitles.${item.event_key.replace(/\./g, "_")}`,
    {
      defaultValue: item.title,
      ...item.event_args,
    },
  );
}

function distinctPresentation(presentation: {
  title: string;
  body: string | null | undefined;
}) {
  const title = presentation.title;
  const body = formatActivityDetail(presentation.body ?? "");
  return {
    title,
    body: body === title ? "" : body,
  };
}

// Detail bodies arrive as semicolon-joined diagnostics
// ("trigger=library_registration; progress=0%", "Codex CLI; status=failed;
// Tool call failed."). Segments that are not `key=value` (client name, result
// sentence) are user-facing already and pass through untouched.
const DETAIL_FIELD_PATTERN = /^([a-z][a-z0-9_]*)=(.*)$/;
const DETAIL_SEPARATOR = " · ";
const RAW_VALIDATION_DETAIL_PATTERN =
  /validation_failed|field required|extra inputs are not permitted|request_id/i;

function formatActivityDetail(detail: string): string {
  if (RAW_VALIDATION_DETAIL_PATTERN.test(detail)) {
    return i18n.t("activity:details.validationFailed", {
      defaultValue:
        "The action could not be completed because its input was invalid.",
    });
  }
  const segments = detail.split(";").map((segment) => segment.trim());
  if (!segments.some((segment) => DETAIL_FIELD_PATTERN.test(segment))) {
    return detail;
  }
  // M-12 (260908 UI audit): "0% complete" reads as stalled/failed for a job
  // that simply hasn't started yet. It stays literal when an error_code sits
  // alongside it -- that combination means the job failed AT 0%, not that
  // it's merely queued, and "Starting" next to a failure sentence would
  // contradict it.
  const hasErrorCode = segments.some(
    (segment) => DETAIL_FIELD_PATTERN.exec(segment)?.[1] === "error_code",
  );
  const parts: string[] = [];
  for (const segment of segments) {
    const field = DETAIL_FIELD_PATTERN.exec(segment);
    if (!field) {
      if (segment) {
        parts.push(segment);
      }
      continue;
    }
    const [, key, value] = field;
    // The row title already ends with the outcome ("MCP update draft failed",
    // "Extraction failed"), so a status field only repeats it.
    if (key === "status") {
      continue;
    }
    if (key === "progress" && value === "0%" && !hasErrorCode) {
      parts.push(
        i18n.t("activity:details.progressStarting", { defaultValue: "Starting" }),
      );
      continue;
    }
    parts.push(detailFieldLabel(key, value));
  }
  return parts.join(DETAIL_SEPARATOR);
}

function detailFieldLabel(key: string, value: string): string {
  if (key === "trigger") {
    return i18n.t("activity:details.trigger", {
      defaultValue: "Triggered by {{value}}",
      // Trigger types are lowercase snake_case; a few also use a dotted audit
      // action ("feedback.submit"), which would nest as an i18n key path.
      value: i18n.t(`activity:jobs.trigger.${value.replace(/\./g, "_")}`, {
        defaultValue: value.replace(/[_.]/g, " "),
      }),
    });
  }
  if (key === "error_code") {
    // Job failure codes are a closed backend vocabulary
    // (job_read_service.PUBLIC_JOB_FAILURE_CODES). An unknown/new code must
    // degrade to a generic sentence, never to humanizeEnum's raw English key —
    // the point of the code is that no diagnostic text ships, and that the
    // copy is rendered in the reader's own language.
    return i18n.t(`activity:jobs.errorCode.${value}`, {
      defaultValue: i18n.t("activity:details.errorCodeFallback", {
        defaultValue: "The job reported a failure.",
      }),
    });
  }
  return i18n.t(`activity:details.${key}`, {
    defaultValue: `${humanizeEnum(key)}: {{value}}`,
    value,
  });
}

// "System" activity = platform/operations, not user-authored content:
// authentication (auth.*), OAuth (oauth.*), automated quality checks (lint.*),
// background jobs (target_kind=job), and workspace settings/admin (settings.*).
// Mirrors backend _is_system_activity_item (activity_service.py); both MUST agree
// because the server applies the same split in SQL before the recency cap.
const SYSTEM_ACTION_PREFIXES = ["auth.", "oauth.", "lint.", "settings."];

export function isSystemActivityItem(item: ActivityItem) {
  if (item.target?.target_kind === "job") {
    return true;
  }
  const title = item.title ?? "";
  const body = item.body ?? "";
  return SYSTEM_ACTION_PREFIXES.some(
    (prefix) => title.startsWith(prefix) || body.startsWith(prefix),
  );
}

export function isMcpActivityItem(item: ActivityItem) {
  return (
    item.actor?.actor_kind === "mcp_client" ||
    item.target?.target_kind === "mcp_tool_call" ||
    item.title.startsWith("MCP ")
  );
}

// ---------------------------------------------------------------------------
// Second-level sub-type filter (v3). Each top-level Type (kind) splits into a
// small, fixed set of sub-types; "all" is the per-kind default. Classification
// is purely client-side off existing item fields (audit action prefix in body,
// MCP tool name parsed from the title, job via target_kind).
export const ACTIVITY_SUBTYPES: Record<string, readonly string[]> = {
  workspace: ["all", "library", "context", "agent", "ask", "review", "chat"],
  mcp: ["all", "read", "write", "search"],
  system: ["all", "auth", "oauth", "settings", "lint", "jobs"],
};

// Workspace audit prefix (body = "domain.entity.operation") → menu-based sub-type.
const WORKSPACE_SUBTYPE_BY_PREFIX: Record<string, string> = {
  library: "library",
  context_page: "context",
  page: "context",
  context_ingress: "context",
  tree: "context",
  feedback: "context",
  agent_page: "agent",
  agent: "agent",
  bundle: "agent",
  ask: "ask",
  review: "review",
  mcp_chat_session: "chat",
};

/**
 * Resolve a workspace audit code ("domain.entity.op", carried in the activity
 * body) to a translated, human-facing label. The code's leading domain segment
 * is mapped to a sub-type via {@link WORKSPACE_SUBTYPE_BY_PREFIX}, then looked
 * up under the `activity:auditCode.<subtype>` convention. Falls back to an
 * acronym-aware humanized form of the activity title so a row never renders the
 * raw "ask.session.create" code.
 *
 * The activity.json `auditCode.<subtype>` keys are owned by the Dashboard agent.
 */
export function auditCodeLabel(
  title: string,
  body: string | null | undefined,
): string {
  if (body === "review.decision.undo") {
    return i18n.t("activity:auditCode.reviewUndo");
  }
  const prefix = (body ?? "").split(".")[0];
  const subtype = Object.hasOwn(WORKSPACE_SUBTYPE_BY_PREFIX, prefix)
    ? WORKSPACE_SUBTYPE_BY_PREFIX[prefix] : undefined;
  const humanized = humanizeEnum(title);
  if (!subtype) {
    return humanized;
  }
  return i18n.t(`activity:auditCode.${subtype}`, { defaultValue: humanized });
}

// MCP tools that mutate (read_only=False in backend ToolMeta). Everything else
// read-only is "read"; "search" is broken out. Names are space-normalized to
// match the activity title "MCP {tool_name with spaces} {status}".
const MCP_WRITE_TOOLS = new Set([
  "write page",
  "write draft",
  "update draft",
  "submit feedback",
  "write agent page draft",
  "build agent input bundle",
  "submit revision for review",
]);
const MCP_STATUSES = ["succeeded", "failed", "rejected_by_policy", "conflict"];

function mcpToolName(title: string): string {
  if (!title.startsWith("MCP ")) {
    return "";
  }
  let rest = title.slice(4).trim();
  for (const status of MCP_STATUSES) {
    if (rest.endsWith(` ${status}`)) {
      rest = rest.slice(0, rest.length - status.length - 1);
      break;
    }
  }
  return rest.trim();
}

function systemSubtype(item: ActivityItem): string {
  if (item.target?.target_kind === "job") {
    return "jobs";
  }
  const action = item.body || item.title || "";
  if (action.startsWith("oauth.")) {
    return "oauth";
  }
  if (action.startsWith("lint.")) {
    return "lint";
  }
  if (action.startsWith("settings.")) {
    return "settings";
  }
  return "auth";
}

/**
 * The sub-type key an item belongs to within its Type. Each item maps to exactly
 * one sub-type; returns "" when the item's Type has no sub-types (all / review).
 */
export function activitySubtype(item: ActivityItem): string {
  if (isMcpActivityItem(item)) {
    const tool = mcpToolName(item.title);
    if (tool === "search") {
      return "search";
    }
    return MCP_WRITE_TOOLS.has(tool) ? "write" : "read";
  }
  if (isSystemActivityItem(item)) {
    return systemSubtype(item);
  }
  const prefix = (item.body ?? "").split(".")[0];
  return Object.hasOwn(WORKSPACE_SUBTYPE_BY_PREFIX, prefix) ? WORKSPACE_SUBTYPE_BY_PREFIX[prefix] : "";
}

// M-11 (260908 UI audit): the backend titles a job row by title-casing its
// internal job_type ("lint_tick" -> "Lint Tick", "feedback_resolution" ->
// "Feedback Resolution"), which reads as an internal class name. Translate
// the known types to a reader-facing label; an unrecognised/future job_type
// still falls back to that title-cased string rather than erroring.
function jobTypeLabel(targetTitle: string): string {
  const key = targetTitle.toLowerCase().replace(/\s+/g, "_");
  return i18n.t(`activity:jobs.title.${key}`, { defaultValue: targetTitle });
}

function targetKindLabel(item: ActivityItem) {
  const kind = item.target?.target_kind;
  if (!kind) {
    return item.body;
  }
  // Same wording the Review rail uses for the same entity: the raw enum
  // ("change_set") is a storage detail, not a subtitle.
  return i18n.t(`activity:targetKinds.${kind}`, {
    defaultValue: humanizeEnum(kind),
  });
}
