import type { AgentVersionEntry } from "../../shared/api/agent";
import type { HistoryGraphNode } from "../../shared/lib/historyGraph";

// Agent page versions record no parent: a new draft is based on whatever the
// page's draft or active head is at that moment, and activating an older
// leftover draft makes that older version the next base. So v3 may well have
// been made from v1. All the history can say is the order -- each version is
// linked to the one numbered right before it as its *previous* version
// (parentKind "previous"), never as what it was made from. A gap in the loaded
// run is a version this reader may not see; a gap below the oldest loaded row
// is simply not loaded, which the layout draws as an edge running on to the
// end of the list.
export function agentHistoryNodes(
  versions: AgentVersionEntry[],
): HistoryGraphNode<AgentVersionEntry>[] {
  const byVersionNo = new Map(
    versions.map((entry) => [entry.version_no, entry]),
  );
  const oldestLoaded = Math.min(...versions.map((entry) => entry.version_no));
  return versions.map((entry): HistoryGraphNode<AgentVersionEntry> => {
    const previousNo = entry.version_no - 1;
    const previous = byVersionNo.get(previousNo);
    const node = {
      entry,
      id: entry.version_id,
      versionNo: entry.version_no,
      parentKind: "previous" as const,
    };
    if (previous) {
      return {
        ...node,
        parentId: previous.version_id,
        parentRelation: "visible",
      };
    }
    if (entry.version_no <= 1) {
      return { ...node, parentId: null, parentRelation: "root" };
    }
    return previousNo < oldestLoaded
      ? // Not loaded yet: any id no loaded node carries says so.
        {
          ...node,
          parentId: `agent-version-no:${previousNo}`,
          parentRelation: "visible",
        }
      : { ...node, parentId: null, parentRelation: "hidden" };
  });
}
