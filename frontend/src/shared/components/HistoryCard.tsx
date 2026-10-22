import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { HistoryGraphRow } from "../lib/historyGraph";

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
  if (!row.parent) {
    return (
      <>
        {t("history.parentOlder", {
          defaultValue: "Continues from an older version",
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

// A row's own diff: what this version changed from the version it was made
// from. Named "Changes" rather than "Compare" because it is not a free choice
// of two versions -- that is "Compare two versions" in the toolbar -- and not
// "previous" either, since a forked version's base is its parent, not the
// next-lower version number. The accessible name says which two it diffs.
export function HistoryChangesButton({
  baseVersionNo,
  loading,
  onToggle,
  open,
  versionNo,
}: {
  // Unknown while the parent is still on an older, unloaded page.
  baseVersionNo: number | null;
  loading: boolean;
  onToggle: () => void;
  open: boolean;
  versionNo: number;
}) {
  const { t } = useTranslation("shell");
  const label = open
    ? t("history.closeChangesFor", {
        defaultValue: "Close changes for v{{version}}",
        version: versionNo,
      })
    : baseVersionNo !== null
      ? t("history.changesFor", {
          defaultValue: "Show changes in v{{version}} since v{{base}}",
          base: baseVersionNo,
          version: versionNo,
        })
      : t("history.changesForParent", {
          defaultValue:
            "Show changes in v{{version}} since the version it was based on",
          version: versionNo,
        });
  return (
    <button
      aria-busy={loading}
      aria-label={label}
      className="text-button compact-button history-changes-action"
      disabled={loading}
      onClick={onToggle}
      type="button"
    >
      {loading
        ? t("history.loadingChanges", { defaultValue: "Loading changes" })
        : open
          ? t("history.closeChanges", { defaultValue: "Close changes" })
          : t("history.changes", { defaultValue: "Changes" })}
    </button>
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
