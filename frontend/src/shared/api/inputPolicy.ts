import { i18n } from "../i18n";
import { inputTextError, markdownInputError, trimShortText } from "../lib/inputPolicy";

/** Validate only known content/metadata writes, before fetch. Authentication
 * and OAuth credentials have separate policies and never pass through here;
 * OAuth consent settings carry only workspace ids and TTL fields. */
export function outgoingContentError(path: string, body: BodyInit | null | undefined) {
  if (typeof body !== "string" || !/^\/(api\/v1\/(workspaces(?:\/|$)|auth\/tokens)|oauth\/consents(?:\?|$))/.test(path)) return "";
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(body) as Record<string, unknown>;
  } catch {
    return "";
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return "";
  const comments = path.includes("/comments");
  const workspaceCreate = /\/workspaces\/?$/.test(path);
  const chats = path.includes("/mcp-chats/");
  const folder = path.includes("/tree/") && data.node_kind === "folder";
  const limits: Record<string, [number, boolean]> = {
    title: [workspaceCreate ? 128 : chats ? 120 : folder ? 80 : 256, true],
    display_name: [256, true], final_title: [256, true],
    name: [128, true], custom_app_label: [160, true], goal: [512, false],
    reason: [2000, false], decision_reason: [2000, false], note: [comments ? 5000 : 2000, false],
    risk_notes: [2000, false], patch_description: [2000, false], suggested_action: [2000, false],
    decision_note: [500, false], nav_summary: [160, false], user_message: [8000, false],
    rationale: [1000, false], anchor_text: [2000, false], context_before: [500, false], context_after: [500, false],
  };
  for (const [key, [maximum, singleLine]] of Object.entries(limits)) {
    if (typeof data[key] === "string") {
      const raw = data[key];
      const value = key === "user_message" ? raw.trim() : raw;
      const error = inputTextError(raw, Number.MAX_SAFE_INTEGER, singleLine) || inputTextError(value, maximum, singleLine);
      if (error) return error;
    }
  }
  for (const key of ["body_md", "body_markdown", "merged_body_markdown"]) {
    const value = data[key];
    if (typeof value !== "string") continue;
    const error = path.includes("/context/")
      ? markdownInputError(value)
      : inputTextError(value, 1_000_000, false);
    if (error) return error;
    if (path.includes("semantic-markdown") && new TextEncoder().encode(value).length > 1_000_000) {
      return i18n.resolvedLanguage?.startsWith("ko") ? "최대 1,000,000 UTF-8 바이트까지 입력할 수 있습니다." : "Use 1,000,000 UTF-8 bytes or fewer.";
    }
  }
  if (path.endsWith("/feedback") && typeof data.body === "string") {
    const error = inputTextError(data.body, 10000, false);
    if (error) return error;
  }
  for (const [key, maximum] of [["tags", 32], ["aliases", 32], ["default_topic_tags", 32], ["operations", 20], ["context_refs", 128], ["library_artifact_refs", 128], ["requested_categories", 4], ["workspace_grants", 256], ["workspace_ids", 256]] as const) {
    const value = data[key];
    if (!Array.isArray(value)) continue;
    if (value.length > maximum) return i18n.resolvedLanguage?.startsWith("ko") ? `최대 ${maximum}개까지 입력할 수 있습니다.` : `Use ${maximum} items or fewer.`;
    if (["tags", "aliases", "default_topic_tags"].includes(key)) {
      for (const label of value) {
        if (typeof label !== "string") continue;
        const error = inputTextError(label, key === "aliases" ? 80 : 48);
        if (error) return error;
        if (!trimShortText(label)) return i18n.resolvedLanguage?.startsWith("ko") ? "빈 항목은 사용할 수 없습니다." : "Empty items are not allowed.";
      }
    }
  }
  return "";
}
