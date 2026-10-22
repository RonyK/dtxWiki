import type { TFunction } from "i18next";

// Three or more versions read as ranges, but only over consecutive numbers:
// a range across a gap would imply versions that are not in the list.
export function formatVersionRange(versions: readonly number[], t: TFunction): string {
  const sorted = [...new Set(versions)].sort((left, right) => left - right);
  const runs: number[][] = [];
  for (const version of sorted) {
    const run = runs.at(-1);
    if (run && version === run[run.length - 1] + 1) run.push(version);
    else runs.push([version]);
  }
  const text = runs
    .map((run) => (run.length >= 3
      ? `v${run[0]}–v${run[run.length - 1]}`
      : run.map((version) => `v${version}`).join(", ")))
    .join(", ");
  return sorted.length >= 3 ? t("workflow.versionCount", { versions: text, count: sorted.length }) : text;
}
