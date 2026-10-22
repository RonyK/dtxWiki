import { useId, type ComponentProps } from "react";
import { useTranslation } from "react-i18next";

import type { PageVersionDiff } from "../api/pages";
import { useMediaQuery } from "../hooks/useMediaQuery";
import type { HistoryGraphNode } from "../lib/historyGraph";
import type { VersionPair } from "../lib/historyVersionPair";
import { DiffViewerDialog } from "./DiffViewerDialog";

type PassThrough = Pick<
  ComponentProps<typeof DiffViewerDialog>,
  | "actionError"
  | "onRestore"
  | "publishedEndpoint"
  | "publishedVersionLabel"
  | "restoreDisabled"
  | "warning"
>;

// The diff dialog every History tab opens, from a row's Changes or from
// "Compare two versions": the same endpoint pickers, the same oldest-first
// column labels, and a title that says which of the two it is showing.
export function HistoryDiffDialog({
  diff,
  error,
  loading,
  onClose,
  onPairChange,
  pair,
  restoreDisabled = false,
  versions,
  ...passThrough
}: PassThrough & {
  diff: PageVersionDiff | null;
  error: string | null;
  loading: boolean;
  onClose: () => void;
  // The caller normalizes and decides; this only reports the picked ends.
  onPairChange: (pair: VersionPair) => void;
  pair: VersionPair;
  // The graph's nodes: a pair that is a version and its parent is titled as
  // that version's Changes rather than as a free comparison.
  versions: ReadonlyArray<HistoryGraphNode<unknown>>;
}) {
  const { t } = useTranslation("shell");
  const narrow = useMediaQuery("(max-width: 720px)");
  const helpId = useId();
  const ordered = [...versions].sort(
    (left, right) => left.versionNo - right.versionNo,
  );
  const versionNoOf = (id: string | null) =>
    ordered.find((version) => version.id === id)?.versionNo;
  const fromNo = versionNoOf(pair.from);
  const toNo = versionNoOf(pair.to);
  const toNode = versions.find((version) => version.id === pair.to);
  const title =
    pair.from === null || fromNo === undefined
      ? t("diff.changesFirstTitle", {
          defaultValue: "Changes in v{{to}}",
          to: toNo,
        })
      : toNode?.parentId !== pair.from
        ? t("diff.compareVersionsTitle", {
            defaultValue: "Compare v{{from}} and v{{to}}",
            from: fromNo,
            to: toNo,
          })
        : toNode.parentKind === "previous"
          ? // Only the order is known (Agent): no ancestry claim.
            t("diff.changesSincePreviousTitle", {
              defaultValue: "Changes in v{{to}} since the previous version, v{{from}}",
              from: fromNo,
              to: toNo,
            })
          : t("diff.changesTitle", {
              defaultValue: "Changes in v{{to}} since v{{from}}",
              from: fromNo,
              to: toNo,
            });
  return (
    <DiffViewerDialog
      {...passThrough}
      restoreDisabled={restoreDisabled}
      // Every pair reaching here is normalized oldest-first, so the left
      // column can honestly promise to be the earlier version.
      baseEndpointLabel={t("diff.baseEarlier", {
        defaultValue: "Base version (earlier)",
      })}
      comparedEndpointLabel={t("diff.comparedLater", {
        defaultValue: "Compared version (later)",
      })}
      controls={
        <div
          aria-describedby={helpId}
          aria-label={t("history.versionSelectors", {
            defaultValue: "Versions to compare",
          })}
          className="history-diff-selectors"
          role="group"
        >
          <label>
            <span>
              {t("history.earlierEndpoint", {
                defaultValue: "Earlier version",
              })}
            </span>
            {/* A restore in flight acts on the pair it started from; the
                pickers stay put until it settles, like Close does. */}
            <select
              disabled={restoreDisabled}
              onChange={(event) => {
                if (event.target.value) {
                  onPairChange({ from: event.target.value, to: pair.to });
                }
              }}
              value={pair.from ?? ""}
            >
              {pair.from === null ? (
                <option value="">
                  {t("history.noEarlierEndpoint", {
                    defaultValue: "Empty baseline",
                  })}
                </option>
              ) : null}
              {ordered.map((version) => {
                const disabled = version.id === pair.to;
                return (
                  <option
                    disabled={disabled}
                    key={`from-${version.id}`}
                    value={version.id}
                  >
                    {disabled
                      ? t("history.versionDisabledLater", {
                          version: version.versionNo,
                          defaultValue:
                            "v{{version}} — unavailable: already selected as the later version",
                        })
                      : `v${version.versionNo}`}
                  </option>
                );
              })}
            </select>
          </label>
          <label>
            <span>
              {t("history.laterEndpoint", { defaultValue: "Later version" })}
            </span>
            <select
              disabled={restoreDisabled}
              onChange={(event) =>
                onPairChange({ from: pair.from, to: event.target.value })
              }
              value={pair.to}
            >
              {ordered.map((version) => {
                const disabled = version.id === pair.from;
                return (
                  <option
                    disabled={disabled}
                    key={`to-${version.id}`}
                    value={version.id}
                  >
                    {disabled
                      ? t("history.versionDisabledEarlier", {
                          version: version.versionNo,
                          defaultValue:
                            "v{{version}} — unavailable: already selected as the earlier version",
                        })
                      : `v${version.versionNo}`}
                  </option>
                );
              })}
            </select>
          </label>
          <p id={helpId}>
            {t("history.selectorHelp", {
              defaultValue:
                "A version cannot be compared with itself. The pair is ordered oldest-first automatically.",
            })}
          </p>
        </div>
      }
      defaultMode={narrow ? "unified" : "split"}
      diff={diff}
      error={error}
      fullScreen={narrow}
      loading={loading}
      onClose={onClose}
      showChangeMarkers
      title={title}
    />
  );
}
