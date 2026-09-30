export const CONTEXT_HISTORY_STATUSES = [
  "state_inconsistent",
  "current_editing",
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

export type ContextHistoryPresentation = {
  descriptionKey: `history.states.${ContextHistoryStatus}.description`;
  labelKey: `history.states.${ContextHistoryStatus}.label`;
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
  state_inconsistent: {
    labelKey: "history.states.state_inconsistent.label",
    descriptionKey: "history.states.state_inconsistent.description",
    tone: "danger",
  },
  current_editing: {
    labelKey: "history.states.current_editing.label",
    descriptionKey: "history.states.current_editing.description",
    tone: "info",
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

export function contextHistoryPresentation(
  status: ContextHistoryStatus,
): ContextHistoryPresentation {
  return CONTEXT_HISTORY_PRESENTATION[status];
}

export function contextHistoryPillTone(status: ContextHistoryStatus) {
  const tone = CONTEXT_HISTORY_PRESENTATION[status].tone;
  return tone === "neutral" ? "muted" : tone;
}
