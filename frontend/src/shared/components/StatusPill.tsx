import { useTranslation } from "react-i18next";

type StatusPillProps = {
  status: string;
  compact?: boolean;
  title?: string;
  // Explicit overrides for domain values the status heuristic can't classify
  // (e.g. "not ready" would otherwise match /ready/ and render green, and
  // blast-radius values fall through to neutral). `tone` sets the color;
  // `label` sets the displayed (e.g. translated) text.
  tone?: string;
  label?: string;
};

const TONE_BY_STATUS: Array<[RegExp, string]> = [
  [/active|published|ready|synced|completed|approved/i, "ok"],
  [/medium|moderate|draft|pending|review|partial|unknown|registered/i, "warn"],
  [/high|critical|failed|rejected|denied|archived/i, "danger"],
  [/low|safe|stale|superseded|expired/i, "muted"],
];
const TONE_BY_EXACT_STATUS: Record<string, string> = {
  active: "ok",
  approved: "ok",
  completed: "ok",
  created: "ok",
  published: "ok",
  ready: "ok",
  succeeded: "ok",
  synced: "ok",
  changes_requested: "warn",
  draft: "warn",
  edited: "warn",
  medium: "warn",
  needs_changes: "warn",
  pending: "warn",
  review_required: "warn",
  cited: "info",
  info: "info",
  acknowledged: "info",
  triaged: "info",
  referenced: "info",
  search_match: "muted",
  warning: "warn",
  open: "warn",
  high: "danger",
  critical: "danger",
  error: "danger",
  denied: "danger",
  failed: "danger",
  rejected: "danger",
  rejected_by_policy: "danger",
  archived: "muted",
  expired: "muted",
  never_used: "info",
  revoked: "muted",
  dismissed: "muted",
  low: "muted",
  "signed out": "muted",
  stale: "muted",
  superseded: "muted",
  fixed: "ok",
  resolved: "ok",
};

export function StatusPill({
  status,
  compact = false,
  title,
  tone,
  label,
}: StatusPillProps) {
  const { t } = useTranslation("shell");
  const normalizedStatus = status.trim().toLowerCase();
  const resolvedTone = tone
    ?? TONE_BY_EXACT_STATUS[normalizedStatus]
    ?? TONE_BY_STATUS.find(([pattern]) => pattern.test(status))?.[1]
    ?? "info";
  // Self-translate the raw enum (A3): every `<StatusPill status={enum}/>` without
  // an explicit `label` leaked English in ko because the value never hit i18n.
  // Resolve a shared `status.<value>` label and fall back to the de-underscored
  // value for non-enum statuses (counts, already-localized strings) that have no
  // key, preserving prior behavior.
  const resolvedLabel =
    label ?? t(`status.${normalizedStatus}`, { defaultValue: labelForStatus(status) });
  return (
    <span
      className={`status-pill status-pill-${resolvedTone} ${compact ? "compact" : ""}`}
      title={title ?? status}
    >
      <span aria-hidden="true" />
      {resolvedLabel}
    </span>
  );
}

function labelForStatus(status: string) {
  return status.replace(/_/g, " ");
}
