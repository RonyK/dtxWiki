import type { TFunction } from "i18next";

import toolPresentationRegistry from "./mcpToolPresentationRegistry.json";

/**
 * How MCP Chats and Activity name one tool call: the tool's title, its result
 * status, and a failure's error code. Both screens read this module so the
 * same call is never described in two vocabularies. The copy lives in the
 * `chats` namespace. To add a tool, register its title key in
 * mcpToolPresentationRegistry.json (backend/tests/mcp_protocol checks it
 * against the server's tool list); to add a status or error code, add it to
 * the set below and to `chats:detail.*` in every locale.
 */
const TOOL_TITLE_KEYS: Readonly<Record<string, string>> = toolPresentationRegistry;

const KNOWN_STATUSES = new Set([
  "accepted",
  "succeeded",
  "partial",
  "needs_review",
  "failed",
  "rejected_by_policy",
  "conflict",
  "pending",
  "running",
]);

// Error codes with a localized label. Keep in step with the codes the MCP
// envelope emits (backend/mcp/envelope.py ERROR_CODE_GUIDANCE) and the public
// codes Activity carries (backend/services/mcp_read_projection.py
// _PUBLIC_ERRORS); the rest fall through to the raw code.
const KNOWN_ERROR_CODES = new Set([
  "tool_failed",
  "internal_error",
  "rejected_by_policy",
  "conflict",
  "validation_failed",
  "stale_base_version",
  "operation_unsupported",
  "operations_overlap",
  "block_reflow",
  "text_occurrence_mismatch",
  "text_match_not_found",
  "text_match_ambiguous",
  "text_match_low_confidence",
  "text_not_found",
  "block_not_found",
  "block_hash_mismatch",
  "section_not_found",
  "scope_denied",
  "not_found",
  "workspace_role_insufficient",
  "workspace_access_denied",
  "restricted_library_denied",
  "unsupported_retrieval_mode",
]);

// Codes whose label alone does not say what happens next. tool_failed is the
// only labelled code without guidance: it names no cause to act on.
const ERROR_CODES_WITH_GUIDANCE = new Set(
  [...KNOWN_ERROR_CODES].filter((code) => code !== "tool_failed"),
);

// A machine error code: lowercase snake_case, as every code the envelope
// emits is. Anything else (a request id, free text) is not echoed.
const ERROR_CODE_SHAPE = /^[a-z][a-z0-9_]{0,63}$/;

/** The tool's localized title, or null for a tool this build does not list. */
export function mcpToolTitle(toolName: string, t: TFunction<"chats">): string | null {
  const titleKey = Object.hasOwn(TOOL_TITLE_KEYS, toolName) ? TOOL_TITLE_KEYS[toolName] : undefined;
  return titleKey ? t(`presentation.toolTitles.${titleKey}`) : null;
}

export function mcpStatusLabel(status: string, t: TFunction<"chats">): string {
  const normalized = status.trim().toLowerCase();
  return KNOWN_STATUSES.has(normalized)
    ? t(`detail.statuses.${normalized}`)
    : t("detail.statuses.unknown");
}

export type McpStatusTone = "success" | "partial" | "failure" | "neutral";

const STATUS_TONES: Readonly<Record<string, McpStatusTone>> = {
  succeeded: "success",
  partial: "partial",
  accepted: "partial",
  needs_review: "partial",
  pending: "partial",
  running: "partial",
  failed: "failure",
  rejected_by_policy: "failure",
  conflict: "failure",
};

export function mcpStatusTone(status: string): McpStatusTone {
  const normalized = status.trim().toLowerCase();
  return Object.hasOwn(STATUS_TONES, normalized) ? STATUS_TONES[normalized] : "neutral";
}

/** The code's localized name; an unlisted code is shown as itself if it is code-shaped. */
export function mcpErrorCodeLabel(errorCode: string, t: TFunction<"chats">): string {
  const code = errorCode.trim();
  if (KNOWN_ERROR_CODES.has(code)) {
    return t(`detail.errorCodes.${code}`);
  }
  // A code this build has no label for is still a stable identifier the
  // reader can search for or quote to an operator, which "Tool call error"
  // was not.
  return ERROR_CODE_SHAPE.test(code) ? code : t("detail.errorCodes.unknown");
}

/** What the code means for the reader and what to do next, when its name alone does not say. */
export function mcpErrorGuidance(errorCode: string, t: TFunction<"chats">): string | null {
  const code = errorCode.trim();
  return ERROR_CODES_WITH_GUIDANCE.has(code) ? t(`detail.errorGuidance.${code}`) : null;
}

/** Whether a code has its own localized name (otherwise only the code itself can be shown). */
export function isKnownMcpErrorCode(errorCode: string): boolean {
  return KNOWN_ERROR_CODES.has(errorCode.trim());
}
