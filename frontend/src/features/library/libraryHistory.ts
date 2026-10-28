import type { LibraryItemDetail } from "../../shared/api/library";
import type { HistoryGraphNode } from "../../shared/lib/historyGraph";

type LibraryVersion = LibraryItemDetail["versions"][number];

// Library versions name the upload they replaced (previous_version_id), and
// every version of an item is visible to anyone who can open the item. The
// item detail lists them in no guaranteed order, so this sorts newest first,
// the order the layout reads.
export function libraryHistoryNodes(
  versions: LibraryVersion[],
): HistoryGraphNode<LibraryVersion>[] {
  return [...versions]
    .sort((left, right) => right.version_no - left.version_no)
    .map((entry): HistoryGraphNode<LibraryVersion> => ({
      entry,
      id: entry.id,
      versionNo: entry.version_no,
      parentId: entry.previous_version_id,
      parentRelation: entry.previous_version_id
        ? "visible"
        : entry.version_no <= 1
          ? "root"
          : "missing",
    }));
}
