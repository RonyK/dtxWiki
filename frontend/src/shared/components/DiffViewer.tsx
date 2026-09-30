import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { components } from "../api/types.gen";

export type DiffViewerDiff = Pick<
  components["schemas"]["PageVersionDiff"],
  "from_version_no" | "to_version_no" | "lines" | "additions" | "deletions"
>;

export type DiffViewerMode = "split" | "unified";
type DiffCellKind = "add" | "context" | "del" | "empty";
type SplitRow =
  | {
      id: string;
      kind: "hunk";
      label: string;
    }
  | {
      id: string;
      kind: "line";
      oldKind: DiffCellKind;
      oldText: string;
      newKind: DiffCellKind;
      newText: string;
    };

type DiffViewerProps = {
  ariaLabel?: string;
  baseEndpointLabel?: string;
  className?: string;
  comparedEndpointLabel?: string;
  defaultMode?: DiffViewerMode;
  diff: DiffViewerDiff;
  emptyMessage?: string;
  publishedEndpoint?: "from" | "to" | null;
  publishedVersionLabel?: string;
  showChangeMarkers?: boolean;
};

export function DiffViewer({
  ariaLabel,
  baseEndpointLabel,
  className = "",
  comparedEndpointLabel,
  defaultMode = "split",
  diff,
  emptyMessage,
  publishedEndpoint = null,
  publishedVersionLabel,
  showChangeMarkers = false,
}: DiffViewerProps) {
  const { t } = useTranslation("shell");
  const [mode, setMode] = useState<DiffViewerMode>(defaultMode);
  const lines = useMemo(() => diff.lines ?? [], [diff.lines]);
  const splitRows = useMemo(() => buildSplitRows(lines), [lines]);
  const hasChanges = lines.length > 0;
  const metadataChanges = Reflect.get(diff as object, "metadata_changes");
  const hasMetadataChanges =
    Array.isArray(metadataChanges) && metadataChanges.length > 0;

  return (
    <div
      className={`diff-viewer ${className}`.trim()}
      aria-label={ariaLabel ?? `Diff v${diff.from_version_no} to v${diff.to_version_no}`}
    >
      {publishedVersionLabel ? (
        <p className="diff-viewer-published-version">
          <strong>{publishedVersionLabel}</strong>
        </p>
      ) : null}
      <div className="diff-viewer-summary">
        <div>
          <strong>
            v{diff.from_version_no} -&gt; v{diff.to_version_no}
          </strong>
          <span>
            +{diff.additions} / -{diff.deletions}
          </span>
        </div>
        {hasChanges ? (
          <div
            className="diff-viewer-mode-toggle"
            role="group"
            aria-label={t("diff.viewMode", { defaultValue: "Diff view mode" })}
          >
            <button
              aria-pressed={mode === "split"}
              onClick={() => setMode("split")}
              type="button"
            >
              {t("diff.split", { defaultValue: "Split" })}
            </button>
            <button
              aria-pressed={mode === "unified"}
              onClick={() => setMode("unified")}
              type="button"
            >
              {t("diff.unified", { defaultValue: "Unified" })}
            </button>
          </div>
        ) : null}
      </div>
      {!hasChanges ? (
        <p className="diff-viewer-empty">
          {/* Still reachable after the self-compare guard: two different
              versions can differ only in metadata, leaving the body diff empty. */}
          {emptyMessage ??
            (hasMetadataChanges
              ? t("diff.metadataOnlyChanges", {
                  defaultValue:
                    "Page metadata changed between these versions; the body did not.",
                })
              : t("diff.noBodyChanges", {
                  defaultValue: "No body changes between these versions.",
                }))}
        </p>
      ) : mode === "split" ? (
        <div
          className="diff-viewer-split"
          role="table"
          aria-label={t("diff.sideBySide", { defaultValue: "Side-by-side diff" })}
        >
          <div className="diff-viewer-split-header" role="row">
            <span
              className={publishedEndpoint === "from" ? "is-current-published" : undefined}
              data-current-published={publishedEndpoint === "from" ? "true" : undefined}
              role="columnheader"
            >
              {/* The chronological wording is opt-in: it asserts that this
                  side really is the earlier version, and only a caller that
                  normalizes its pair can promise that. Screens that hand the
                  viewer an unordered pair keep the older, weaker wording. */}
              {baseEndpointLabel ??
                t("diff.previous", { defaultValue: "Previous" })}
              {publishedEndpoint === "from" ? (
                <>
                  {" "}
                  <strong className="diff-viewer-published-badge">
                    {t("diff.currentPublishedBadge", { defaultValue: "Current published" })}
                  </strong>
                </>
              ) : null}
            </span>
            <span
              className={publishedEndpoint === "to" ? "is-current-published" : undefined}
              data-current-published={publishedEndpoint === "to" ? "true" : undefined}
              role="columnheader"
            >
              {comparedEndpointLabel ??
                t("diff.current", { defaultValue: "Current" })}
              {publishedEndpoint === "to" ? (
                <>
                  {" "}
                  <strong className="diff-viewer-published-badge">
                    {t("diff.currentPublishedBadge", { defaultValue: "Current published" })}
                  </strong>
                </>
              ) : null}
            </span>
          </div>
          {splitRows.map((row) => {
            if (row.kind === "hunk") {
              return (
                <div className="diff-viewer-hunk" key={row.id} role="row">
                  {row.label}
                </div>
              );
            }
            return (
              <div className="diff-viewer-row" key={row.id} role="row">
                <code className={`diff-viewer-cell diff-viewer-cell-${row.oldKind}`} role="cell">
                  <DiffCellContent
                    addedLabel={t("diff.addedLine", { defaultValue: "Added line" })}
                    kind={row.oldKind}
                    removedLabel={t("diff.removedLine", { defaultValue: "Removed line" })}
                    showChangeMarkers={showChangeMarkers}
                    text={row.oldText}
                  />
                </code>
                <code className={`diff-viewer-cell diff-viewer-cell-${row.newKind}`} role="cell">
                  <DiffCellContent
                    addedLabel={t("diff.addedLine", { defaultValue: "Added line" })}
                    kind={row.newKind}
                    removedLabel={t("diff.removedLine", { defaultValue: "Removed line" })}
                    showChangeMarkers={showChangeMarkers}
                    text={row.newText}
                  />
                </code>
              </div>
            );
          })}
        </div>
      ) : (
        <pre className="diff-viewer-unified">
          {lines.map((line, index) => (
            <code className={diffLineClass(line)} key={`${index}-${line}`}>
              {line || " "}
            </code>
          ))}
        </pre>
      )}
    </div>
  );
}

function DiffCellContent({
  addedLabel,
  kind,
  removedLabel,
  showChangeMarkers,
  text,
}: {
  addedLabel: string;
  kind: DiffCellKind;
  removedLabel: string;
  showChangeMarkers: boolean;
  text: string;
}) {
  if (!showChangeMarkers || (kind !== "add" && kind !== "del")) {
    return text || " ";
  }
  return (
    <>
      <span aria-hidden="true" className="diff-change-marker">
        {kind === "add" ? "+" : "−"}
      </span>
      <span className="visually-hidden">
        {kind === "add" ? addedLabel : removedLabel}
        {": "}
      </span>
      {text || " "}
    </>
  );
}

export function buildSplitRows(lines: string[]): SplitRow[] {
  const rows: SplitRow[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (isFileHeader(line)) {
      index += 1;
      continue;
    }
    if (line.startsWith("@@")) {
      rows.push({
        id: `hunk-${index}`,
        kind: "hunk",
        label: line,
      });
      index += 1;
      continue;
    }
    if (isRemovedLine(line)) {
      const removed: string[] = [];
      const startIndex = index;
      while (index < lines.length && isRemovedLine(lines[index])) {
        removed.push(stripDiffPrefix(lines[index]));
        index += 1;
      }
      const added: string[] = [];
      while (index < lines.length && isAddedLine(lines[index])) {
        added.push(stripDiffPrefix(lines[index]));
        index += 1;
      }
      const rowCount = Math.max(removed.length, added.length);
      for (let offset = 0; offset < rowCount; offset += 1) {
        rows.push({
          id: `change-${startIndex}-${offset}`,
          kind: "line",
          oldKind: removed[offset] === undefined ? "empty" : "del",
          oldText: removed[offset] ?? "",
          newKind: added[offset] === undefined ? "empty" : "add",
          newText: added[offset] ?? "",
        });
      }
      continue;
    }
    if (isAddedLine(line)) {
      rows.push({
        id: `add-${index}`,
        kind: "line",
        oldKind: "empty",
        oldText: "",
        newKind: "add",
        newText: stripDiffPrefix(line),
      });
      index += 1;
      continue;
    }
    rows.push({
      id: `context-${index}`,
      kind: "line",
      oldKind: "context",
      oldText: stripDiffPrefix(line),
      newKind: "context",
      newText: stripDiffPrefix(line),
    });
    index += 1;
  }
  return rows;
}

export function diffLineClass(line: string) {
  if (isAddedLine(line)) {
    return "diff-line diff-line-add";
  }
  if (isRemovedLine(line)) {
    return "diff-line diff-line-del";
  }
  return "diff-line";
}

function isAddedLine(line: string) {
  return line.startsWith("+") && !line.startsWith("+++");
}

function isRemovedLine(line: string) {
  return line.startsWith("-") && !line.startsWith("---");
}

function isFileHeader(line: string) {
  return line.startsWith("---") || line.startsWith("+++");
}

function stripDiffPrefix(line: string) {
  if (line.startsWith("+") || line.startsWith("-") || line.startsWith(" ")) {
    return line.slice(1);
  }
  return line;
}
