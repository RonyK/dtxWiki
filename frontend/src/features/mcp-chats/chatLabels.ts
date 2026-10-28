import type { TFunction } from "i18next";

import type {
  McpChatArtifactLink,
  McpChatSessionRef,
} from "../../shared/api/operations";
import { artifactVerbRank, isArtifactVerb, repeatedCount } from "./chatArtifacts";

// Legacy chats may still carry a count-only summary ("3 tool calls, 1 write")
// that just duplicates the count chips on the card/detail — suppress it and fall
// back to a semantic linked-artifact line. New chats get a backend summary
// instead. Shared by the list (card summary) and the detail screen so both heal
// the same legacy encoding the same way.
export const COUNT_SUMMARY_RE = /^\d+ tool calls?(, \d+ writes?)?(, \d+ errors?)?$/;

export function isCountSummary(summary: string) {
  return COUNT_SUMMARY_RE.test(summary.trim());
}

// This module has no i18n context to thread through (it runs outside any
// component), so it cannot render the localized labels.artifactKind.unknown
// string from chats.json. It still must not fall back to the raw
// resource_kind wire value (e.g. "context_media_asset") -- that is an
// internal identifier, not a user-facing label -- so it falls back to this
// fixed English word instead, matching what labels.artifactKind.unknown
// says in the en locale.
const UNTITLED_ARTIFACT_LABEL = "Artifact";
const NON_SEMANTIC_ARTIFACT_VERBS = new Set(["referenced", "search_match"]);

// Semantic fallback for legacy count-only summaries: the primary linked
// artifact as "Verb: Title" (e.g. "Created: Policy Analysis").
export function linkedArtifactSummary(
  artifacts: McpChatSessionRef["linked_artifacts"],
): string | null {
  const primary =
    artifacts.find(
      (artifact) => !NON_SEMANTIC_ARTIFACT_VERBS.has(artifact.verb),
    ) ?? artifacts[0];
  if (!primary) {
    return null;
  }
  const title = primary.artifact_title ?? UNTITLED_ARTIFACT_LABEL;
  const verb = primary.verb.replace(/_/g, " ");
  return `${verb.charAt(0).toUpperCase()}${verb.slice(1)}: ${title}`;
}

export function artifactVerbLabel(
  verb: string,
  t: TFunction<"chats">,
): string {
  const normalized = verb.trim().toLowerCase();
  return isArtifactVerb(normalized)
    ? t(`detail.artifactVerbs.${normalized}`)
    : t("detail.artifactVerbs.unknown");
}

/** A verb pill's label: "Edited ×3", or just "Edited" when it happened once. */
export function artifactVerbPillLabel(
  link: Pick<McpChatArtifactLink, "verb" | "occurrence_count">,
  t: TFunction<"chats">,
): string {
  const verb = artifactVerbLabel(link.verb, t);
  const count = repeatedCount(link);
  return count === null ? verb : `${verb} ×${count}`;
}

/**
 * The pill's fact in words ("Edited 3 links") for tooltips and screen
 * readers, which would otherwise have to pronounce the "×".
 */
export function artifactVerbDescription(
  link: Pick<McpChatArtifactLink, "verb" | "occurrence_count">,
  t: TFunction<"chats">,
): string {
  const verb = artifactVerbLabel(link.verb, t);
  const count = repeatedCount(link);
  return count === null ? verb : `${verb} ${t("detail.artifactLinks", { count })}`;
}

/**
 * "Created 1 · Edited 2": how many artifacts carry each verb, in verb order.
 * Rows are unique per (artifact, verb), so a verb's row count already is that
 * number of artifacts.
 */
export function artifactVerbSummary(
  links: readonly Pick<McpChatArtifactLink, "verb">[],
  t: TFunction<"chats">,
): string {
  const counts = new Map<string, number>();
  for (const link of links) {
    counts.set(link.verb, (counts.get(link.verb) ?? 0) + 1);
  }
  return Array.from(counts.entries())
    .sort(([a], [b]) => artifactVerbRank(a) - artifactVerbRank(b))
    .map(([verb, count]) => `${artifactVerbLabel(verb, t)} ${count}`)
    .join(" · ");
}

/** The artifact's title, or its localized kind when the title is hidden or unset. */
export function artifactDisplayTitle(
  artifact: Pick<McpChatArtifactLink, "artifact_title" | "resource_kind">,
  t: TFunction<"chats">,
): string {
  return artifact.artifact_title
    ?? t(`labels.artifactKind.${artifact.resource_kind}`, {
      defaultValue: t("labels.artifactKind.unknown"),
    });
}

/**
 * The one line a card or header shows under the title.
 *
 * Only the agent's attributable intent remains a secondary display line. The
 * stored English semantic summary and legacy artifact fallback are debugging
 * data now; the primary visible line is composed from presentation fields.
 */
export function chatSummaryLine(chat: {
  title: string;
  summary?: string | null;
  intent_summary?: string | null;
  intent_source?: McpChatSessionRef["intent_source"];
  subject?: string | null;
  linked_artifacts: McpChatSessionRef["linked_artifacts"];
}): { text: string; source: McpChatSessionRef["intent_source"] | null; artifactFallback: boolean } | null {
  const title = normalizeComparable(chat.title);
  if (chat.intent_summary) {
    const intent = normalizeComparable(chat.intent_summary);
    const subject = chat.subject ? normalizeComparable(chat.subject) : null;
    return intent === title || (subject !== null && intent === subject)
      ? null
      : { text: chat.intent_summary, source: chat.intent_source ?? null, artifactFallback: false };
  }
  return null;
}

function normalizeComparable(value: string): string {
  return value.normalize("NFKC").trim().replace(/\s+/gu, " ").toLocaleLowerCase();
}

/**
 * Failure chips, or a single combined one when the breakdown is unknown.
 *
 * `error_count` predates the split and sums failed, rejected and conflict.
 * Rows written before the split have zeros in the new counters, and showing
 * "0 refused" for a chat that was refused would be a lie — so when the parts
 * do not account for the total, report the total alone.
 */
export function chatFailureCounts(chat: {
  error_count: number;
  failed_count?: number;
  rejected_count?: number;
}): { failed: number; rejected: number } | { combined: number } | null {
  if (chat.error_count <= 0) {
    return null;
  }
  const failed = chat.failed_count ?? 0;
  const rejected = chat.rejected_count ?? 0;
  if (failed + rejected !== chat.error_count) {
    return { combined: chat.error_count };
  }
  return { failed, rejected };
}
