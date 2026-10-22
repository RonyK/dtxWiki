import { FileDiff, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { HistoryGraphRow } from "../lib/historyGraph";
import { Spinner } from "./Spinner";

// The card every History tab renders beside its lane gutter (see
// HistoryGraphList). The layout -- version and state on the first line, what
// happened and where it came from on the left, who and when on the right,
// actions on their own row -- is fixed here so Context, Agent and Library
// read the same; each surface only supplies its own content for the slots.
export function HistoryCard({
  actions,
  author,
  badges,
  description,
  notices,
  row,
  status,
}: {
  actions?: ReactNode;
  author?: ReactNode;
  // Extra marks on the title line after the state pill.
  badges?: ReactNode;
  description?: ReactNode;
  // Extra lines under the parent relation (e.g. why Changes is unavailable).
  notices?: ReactNode;
  row: HistoryGraphRow<unknown>;
  status: ReactNode;
}) {
  return (
    <>
      <div className="history-card-title">
        <span className="history-card-version">v{row.node.versionNo}</span>
        {status}
        {badges}
      </div>
      <div className="history-card-body">
        <div className="history-card-summary">
          {description ? (
            <div className="history-card-desc">{description}</div>
          ) : null}
          <div className="history-parent-relation">
            <HistoryParentRelation row={row} />
          </div>
          {notices}
        </div>
        {author ? <div className="history-card-author">{author}</div> : null}
      </div>
      <div className="history-card-actions">{actions}</div>
    </>
  );
}

function HistoryParentRelation({ row }: { row: HistoryGraphRow<unknown> }) {
  const { t } = useTranslation("shell");
  if (row.relation === "root") {
    return <>{t("history.firstVersion", { defaultValue: "First version" })}</>;
  }
  if (row.relation === "hidden") {
    return (
      <>
        {t("history.parentHidden", {
          defaultValue: "Previous version is not visible",
        })}
      </>
    );
  }
  if (row.relation === "missing") {
    return (
      <>
        {t("history.parentMissing", {
          defaultValue: "Previous version is unavailable",
        })}
      </>
    );
  }
  // A surface that only knows the order (Agent) must not claim ancestry.
  const previousOnly = row.node.parentKind === "previous";
  if (!row.parent) {
    return previousOnly ? (
      <>
        {t("history.previousOlder", {
          defaultValue: "Previous version is not in this list",
        })}
      </>
    ) : (
      <>
        {t("history.parentOlder", {
          defaultValue: "Continues from an older version",
        })}
      </>
    );
  }
  if (previousOnly) {
    return (
      <>
        {t("history.previousVersion", {
          defaultValue: "Previous version: v{{version}}",
          version: row.parent.versionNo,
        })}
      </>
    );
  }
  return row.relation === "branch" ? (
    <>
      {t("history.branchedFrom", {
        defaultValue: "Branched from v{{version}}",
        version: row.parent.versionNo,
      })}
    </>
  ) : (
    <>
      {t("history.continuesFrom", {
        defaultValue: "Continues from v{{version}}",
        version: row.parent.versionNo,
      })}
    </>
  );
}

export function HistoryLineDelta({
  added,
  removed,
}: {
  added?: number | null;
  removed?: number | null;
}) {
  if (added == null && removed == null) {
    return null;
  }
  return (
    <span className="history-card-delta">
      {added != null ? <span className="delta-plus">+{added}</span> : null}
      {removed != null ? <span className="delta-minus">-{removed}</span> : null}
    </span>
  );
}

// Every action on a History row: an icon plus a short label, collapsing to
// the icon alone on narrow screens (see `.history-action` in
// 09-context-audit-fixes.css) so a row's actions fit one line on a phone.
// The accessible name is always the full description -- it contains the
// short visible label, so voice control still matches what is on screen.
export function HistoryActionButton({
  accessibleLabel,
  busy = false,
  className,
  disabled = false,
  icon: Icon,
  label,
  onClick,
  tone,
}: {
  accessibleLabel: string;
  busy?: boolean;
  className?: string;
  disabled?: boolean;
  icon: LucideIcon;
  label: string;
  onClick: () => void;
  // The one destructive action; its class also places it at the row's end.
  tone?: "danger";
}) {
  return (
    <button
      aria-busy={busy}
      aria-label={accessibleLabel}
      className={[
        "text-button compact-button history-action",
        tone === "danger" ? "danger-action" : null,
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      disabled={disabled || busy}
      onClick={onClick}
      title={accessibleLabel}
      type="button"
    >
      {/* The label may be hidden, so progress has to show on the icon. */}
      {busy ? (
        <Spinner className="history-action-icon" decorative size={16} />
      ) : (
        <Icon aria-hidden="true" className="history-action-icon" size={16} />
      )}
      <span className="history-action-label">{label}</span>
    </button>
  );
}

// A row's own diff: what this version changed from the version it was made
// from. Named "Changes" rather than "Compare" because it is not a free choice
// of two versions -- that is "Compare two versions" in the toolbar -- and its
// base is the row's parent, not the next-lower version number, since a forked
// version was made from its parent. Where a surface records only the order
// (parentKind "previous"), the base is the previous version and the
// accessible name says exactly that rather than implying ancestry.
export function HistoryChangesButton({
  loading,
  onToggle,
  open,
  row,
}: {
  loading: boolean;
  onToggle: () => void;
  open: boolean;
  row: HistoryGraphRow<unknown>;
}) {
  const { t } = useTranslation("shell");
  const versionNo = row.node.versionNo;
  // Unknown while the parent is still on an older, unloaded page.
  const baseVersionNo = row.parent?.versionNo ?? null;
  return (
    <HistoryActionButton
      accessibleLabel={
        open
          ? t("history.closeChangesFor", {
              defaultValue: "Close changes for v{{version}}",
              version: versionNo,
            })
          : baseVersionNo !== null
            ? row.node.parentKind === "previous"
              ? t("history.changesSincePrevious", {
                  defaultValue:
                    "Show changes in v{{version}} since the previous version, v{{base}}",
                  base: baseVersionNo,
                  version: versionNo,
                })
              : t("history.changesFor", {
                  defaultValue: "Show changes in v{{version}} since v{{base}}",
                  base: baseVersionNo,
                  version: versionNo,
                })
            : t("history.changesForParent", {
                defaultValue:
                  "Show changes in v{{version}} since the version it was based on",
                version: versionNo,
              })
      }
      busy={loading}
      className="history-changes-action"
      icon={FileDiff}
      label={
        loading
          ? t("history.loadingChanges", { defaultValue: "Loading changes" })
          : open
            ? t("history.closeChanges", { defaultValue: "Close changes" })
            : t("history.changes", { defaultValue: "Changes" })
      }
      onClick={onToggle}
    />
  );
}

export function HistoryChangesUnavailable() {
  const { t } = useTranslation("shell");
  return (
    <span className="history-changes-unavailable">
      {t("history.changesUnavailable", {
        defaultValue: "Changes unavailable",
      })}
    </span>
  );
}
