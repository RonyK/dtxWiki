import type { GraphNode } from "../../shared/api/graph";
import type { GraphScene } from "./graphModel";
import { localEdgeVisualKind } from "./local/edgeKinds";

export type SceneNavigation = {
  neighborsByNode: Map<string, GraphNode[]>;
  membersByFolder: Map<string, GraphNode[]>;
  foldersByMember: Map<string, GraphNode[]>;
};

/** Only the displayed scene is authoritative, never the full query or tree. */
export function buildSceneNavigation(scene: GraphScene): SceneNavigation {
  const nodesById = new Map(scene.nodes.map((node) => [node.id, node]));
  const neighbors = new Map<string, Set<string>>();
  const members = new Map<string, Set<string>>();
  const folders = new Map<string, Set<string>>();
  for (const node of nodesById.values()) {
    neighbors.set(node.id, new Set());
    if (node.entity_kind === "workspace_tree_node" && node.family === "structural") {
      members.set(node.id, new Set());
    }
  }
  for (const edge of scene.edges) {
    const source = nodesById.get(edge.source);
    const target = nodesById.get(edge.target);
    if (!source || !target || !localEdgeVisualKind(edge.edge_kind)) continue;
    // The renderer omits self-links, including aliases of one semantic node.
    if (source.entity_kind === target.entity_kind && source.entity_id === target.entity_id) continue;
    neighbors.get(source.id)!.add(target.id);
    neighbors.get(target.id)!.add(source.id);
    // Stored tree projections are parent -> child. They can contain visible
    // secondary placements and nested folders, with no primary flag on wire.
    if (edge.edge_kind !== "tree" || !members.has(source.id)) continue;
    members.get(source.id)!.add(target.id);
    if (!folders.has(target.id)) folders.set(target.id, new Set());
    folders.get(target.id)!.add(source.id);
  }
  // Preserve the stable scene edge order without re-scanning or sorting every
  // node's neighbors. Total work and storage are O(nodes + relationships).
  const resolve = (index: Map<string, Set<string>>) => new Map(
    [...index].map(([id, related]) => [id, [...related].map((relatedId) => nodesById.get(relatedId)!)]),
  );
  return {
    neighborsByNode: resolve(neighbors),
    membersByFolder: resolve(members),
    foldersByMember: resolve(folders),
  };
}
