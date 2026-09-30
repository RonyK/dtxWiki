import type { TFunction } from "i18next";

import type {
  McpChatSessionRef,
  McpChatTimelineItem,
} from "../../shared/api/operations";
import toolPresentationRegistry from "./toolPresentationRegistry.json";

type ChatPresentation = McpChatSessionRef | McpChatTimelineItem;

const toolTitleKeys: Readonly<Record<string, string>> = toolPresentationRegistry;

export function chatPresentationLine(
  presentation: ChatPresentation,
  t: TFunction<"chats">,
): string {
  if (presentation.title_source === "user" && presentation.subject) {
    return presentation.subject;
  }

  const action = t(`presentation.actions.${presentation.action}`);
  const subjectKind = presentation.subject_kind
    ? subjectKindLabel(presentation.subject_kind, t)
    : null;

  if (presentation.title_source === "artifact" && presentation.subject) {
    const template = presentation.action === "create"
      ? "created"
      : presentation.action === "edit"
        ? "edited"
        : presentation.action === "read"
          ? "read"
          : presentation.action === "cite"
            ? "cited"
            : "actionSubject";
    return t(`presentation.templates.${template}`, {
      action,
      subject: presentation.subject,
    });
  }

  if (
    presentation.title_source === "query"
    && presentation.action === "search"
    && presentation.subject
  ) {
    return t("presentation.templates.search", {
      subject: presentation.subject,
    });
  }

  if (presentation.subject) {
    return t("presentation.templates.actionSubject", {
      action,
      subject: presentation.subject,
    });
  }
  if (presentation.title_source === "target_kind" && subjectKind) {
    return t("presentation.templates.actionKind", {
      action,
      kind: subjectKind,
    });
  }

  const toolName = presentation.primary_tool_name;
  const titleKey = toolName ? toolTitleKeys[toolName] : undefined;
  if (presentation.title_source === "tool" && titleKey) {
    return t(`presentation.toolTitles.${titleKey}`);
  }

  return t("presentation.genericActivity");
}

/** The localized tool title, or the generic "Tool" fallback for an unlisted name. */
export function toolTitleLabel(toolName: string, t: TFunction<"chats">): string {
  const titleKey = toolTitleKeys[toolName];
  return titleKey
    ? t(`presentation.toolTitles.${titleKey}`)
    : t("presentation.genericTool");
}

function subjectKindLabel(kind: string, t: TFunction<"chats">): string {
  const key = `presentation.subjectKinds.${kind}`;
  const translated = t(key, { defaultValue: "" });
  return translated || t("presentation.unknownSubject");
}

/**
 * A match similarity (0..1) as a whole percent that never overstates it:
 * rounded down, and a text that differed stays below 100% however close.
 */
export function similarityPercent(similarity: number): number {
  const percent = Math.floor(similarity * 100);
  return similarity < 1 ? Math.min(percent, 99) : percent;
}

export function humanizeIdentifier(value: string): string {
  const words = value.replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  if (!words) {
    return value;
  }
  return `${words.charAt(0).toLocaleUpperCase()}${words.slice(1)}`;
}
