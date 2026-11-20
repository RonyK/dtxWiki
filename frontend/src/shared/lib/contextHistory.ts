import type { VersionEntry } from "../api/context";
import type { HistoryGraphNode, PriorRelation } from "./historyGraph";

export const CONTEXT_HISTORY_STATUSES = [
  "closed",
  "state_inconsistent",
  "current_draft",
  "published_snapshot",
  "alternative_draft",
  "discarded_draft",
  "superseded_snapshot",
  "superseded_draft",
] as const;

export type ContextHistoryStatus = (typeof CONTEXT_HISTORY_STATUSES)[number];

export type ContextHistoryTone =
  | "danger"
  | "warn"
  | "ok"
  | "info"
  | "muted"
  | "neutral";

/** Why a closed draft ended: the server derives it from recorded facts. */
export type ContextClosedReason = "discarded" | "merged" | "rejected";

type ContextHistoryLabel = ContextHistoryStatus | `closed_${ContextClosedReason}`;

export type ContextHistoryPresentation = {
  descriptionKey: `history.states.${ContextHistoryLabel}.description`;
  labelKey: `history.states.${ContextHistoryLabel}.label`;
  tone: ContextHistoryTone;
};

// Keep this explicit and exhaustive. These states describe different lifecycle
// positions even when their persisted version_status values happen to match;
// substring heuristics would collapse current, alternative, and superseded
// drafts into the same visual meaning.
export const CONTEXT_HISTORY_PRESENTATION: Record<
  ContextHistoryStatus,
  ContextHistoryPresentation
> = {
  closed: {
    labelKey: "history.states.closed.label",
    descriptionKey: "history.states.closed.description",
    tone: "neutral",
  },
  state_inconsistent: {
    labelKey: "history.states.state_inconsistent.label",
    descriptionKey: "history.states.state_inconsistent.description",
    tone: "danger",
  },
  current_draft: {
    labelKey: "history.states.current_draft.label",
    descriptionKey: "history.states.current_draft.description",
    tone: "warn",
  },
  published_snapshot: {
    labelKey: "history.states.published_snapshot.label",
    descriptionKey: "history.states.published_snapshot.description",
    tone: "ok",
  },
  alternative_draft: {
    labelKey: "history.states.alternative_draft.label",
    descriptionKey: "history.states.alternative_draft.description",
    tone: "info",
  },
  discarded_draft: {
    labelKey: "history.states.discarded_draft.label",
    descriptionKey: "history.states.discarded_draft.description",
    tone: "muted",
  },
  superseded_snapshot: {
    labelKey: "history.states.superseded_snapshot.label",
    descriptionKey: "history.states.superseded_snapshot.description",
    tone: "neutral",
  },
  superseded_draft: {
    labelKey: "history.states.superseded_draft.label",
    descriptionKey: "history.states.superseded_draft.description",
    tone: "neutral",
  },
};

// Finished Context versions are Git commits; a new draft can start from one.
const CONTEXT_COMMIT_STATUSES: ReadonlySet<string> = new Set(["closed", "published_snapshot", "superseded_snapshot"]);

export function isContextCommit(versionStatus: string | null | undefined): boolean {
  return CONTEXT_COMMIT_STATUSES.has(versionStatus ?? "");
}

// A closed draft says why it ended: discarded, merged into a published (or
// another) version, or rejected in review. Without a reason it stays "closed".
const CONTEXT_CLOSED_PRESENTATION: Record<ContextClosedReason, ContextHistoryPresentation> = {
  discarded: {
    labelKey: "history.states.closed_discarded.label",
    descriptionKey: "history.states.closed_discarded.description",
    tone: "muted",
  },
  merged: {
    labelKey: "history.states.closed_merged.label",
    descriptionKey: "history.states.closed_merged.description",
    tone: "ok",
  },
  rejected: {
    labelKey: "history.states.closed_rejected.label",
    descriptionKey: "history.states.closed_rejected.description",
    tone: "danger",
  },
};

export function contextHistoryPresentation(
  status: ContextHistoryStatus,
  closedReason?: ContextClosedReason | null,
): ContextHistoryPresentation {
  return status === "closed" && closedReason
    ? CONTEXT_CLOSED_PRESENTATION[closedReason]
    : CONTEXT_HISTORY_PRESENTATION[status];
}

export function contextHistoryPillTone(
  status: ContextHistoryStatus,
  closedReason?: ContextClosedReason | null,
) {
  const tone = contextHistoryPresentation(status, closedReason).tone;
  return tone === "neutral" ? "muted" : tone;
}

export function priorRelationForVersion(entry: VersionEntry): PriorRelation {
  const explicit = (entry as { prior_relation?: PriorRelation }).prior_relation;
  if (explicit) {
    return explicit;
  }
  if (entry.prior_version_id) {
    return "visible";
  }
  return entry.version_no <= 1 ? "root" : "missing";
}

// Context versions name their own parent, so lineage comes straight from the
// API: prior_version_id (already projected to what this actor may see) and
// the reconcile merge recorded on the merged branch.
export function contextHistoryNodes(
  versions: VersionEntry[],
): HistoryGraphNode<VersionEntry>[] {
  // The History API already lists open drafts first (the tip of the history),
  // so the layout keeps its order.
  return versions.map((entry) => ({
    entry,
    id: entry.version_id,
    versionNo: entry.version_no,
    parentId: entry.prior_version_id ?? null,
    parentRelation: priorRelationForVersion(entry),
    // Merged-in parents, whatever order the reader's filtered list has: every
    // parent except the first one.
    mergeParentIds: (entry.parent_version_ids ?? []).filter((id) =>
      id !== entry.parent_version_id && id !== entry.prior_version_id),
    pending: entry.version_status === "draft",
    branched:
      entry.graph_status === "branched" ||
      entry.history_status === "alternative_draft" ||
      entry.history_status === "discarded_draft",
  }));
}
