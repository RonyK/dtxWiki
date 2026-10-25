import type { HistoryGraphRow } from "./historyGraph";

// Two versions to diff. `from: null` is the empty baseline (a first version's
// whole body as additions), which only Context offers.
export type VersionPair = { from: string | null; to: string };

export type VersionRef = { id: string; versionNo: number };

// Oldest first and never a version against itself. Null when either end is
// not among `versions`, so a stale or hand-edited pair fails closed.
export function normalizeVersionPair<P extends VersionPair>(
  versions: ReadonlyArray<VersionRef>,
  pair: P,
): P | null {
  const to = versions.find((version) => version.id === pair.to);
  if (!to) {
    return null;
  }
  if (pair.from === null) {
    return pair;
  }
  const from = versions.find((version) => version.id === pair.from);
  if (!from || from.id === to.id || from.versionNo === to.versionNo) {
    return null;
  }
  return from.versionNo < to.versionNo
    ? pair
    : { ...pair, from: to.id, to: from.id };
}

// What a row's Changes action opens: its recorded source, or its chronological
// predecessor when parentKind is "previous". Null until that version is loaded.
export function changesPairFor(
  row: HistoryGraphRow<unknown>,
): { from: string; to: string } | null {
  return row.parent ? { from: row.parent.id, to: row.node.id } : null;
}
