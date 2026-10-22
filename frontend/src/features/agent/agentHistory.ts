import type { AgentVersionEntry } from "../../shared/api/agent";
import type { HistoryGraphNode } from "../../shared/lib/historyGraph";

// Agent page versions carry no parent link: the backend only ever appends the
// next version_no of one linear sequence (AgentPageService._next_version_no),
// so a version was made from the version numbered right before it. A gap in
// the loaded run is a version this reader may not see; a gap below the oldest
// loaded row is simply not loaded, which the layout draws as an edge running
// on to the end of the list.
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
