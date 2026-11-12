import { expect } from "@playwright/test";

import type { WorkspaceGraphNode, WorkspaceGraphScene } from "../../frontend/src/shared/api/workspaceGraph";

/** Containment is an ordinary scene relation; relation counts and budgets leave it out. */
export function sceneRelations(scene: WorkspaceGraphScene) {
  return scene.edges.filter((edge) => edge.relation_kind !== "tree");
}

/** What the 2K/10K budgets count: documents and clusters, never folders. */
export function admittedNodes(scene: WorkspaceGraphScene) {
  return scene.nodes.filter((node) => node.grain !== "folder");
}

/** The scene shape the list helpers read, so specs with a local scene type can share them. */
type ListedScene = {
  nodes: ReadonlyArray<{ id: string; grain: string }>;
  edges: ReadonlyArray<{ source: string; target: string; relation_kind: string }>;
};

/** The Map list holds the shown nodes and every folder that holds a listed node. */
export function listedNodeIds(scene: ListedScene, shown: ReadonlySet<string>): Set<string> {
  const listed = new Set(shown);
  const folders = new Set(scene.nodes.filter((node) => node.grain === "folder").map((node) => node.id));
  for (let grew = true; grew;) {
    grew = false;
    for (const edge of scene.edges) {
      if (edge.relation_kind !== "tree" || !folders.has(edge.source) || listed.has(edge.source)
        || !listed.has(edge.target)) continue;
      listed.add(edge.source);
      grew = true;
    }
  }
  return listed;
}

export function listedEdgeCount(scene: ListedScene, listed: ReadonlySet<string>): number {
  return scene.edges.filter((edge) => listed.has(edge.source) && listed.has(edge.target)).length;
}

/**
 * What native GL draws for every node of a scene: a point per document,
 * cluster and folder, and a stroke per relation and containment row. Aggregate
 * self-loops stay in the list and export but get no stroke.
 */
export function nativeDrawCounts(scene: WorkspaceGraphScene): { points: number; links: number } {
  return { points: scene.nodes.length, links: scene.edges.filter((edge) => edge.source !== edge.target).length };
}

/** The grain and entity kind a visible-map export gives an API node. */
export function exportedNodeShape(node: WorkspaceGraphNode): { grain: string; entityKind: string } {
  if (node.grain === "cluster") return { grain: node.cluster_kind, entityKind: "cluster" };
  if (node.grain === "folder") return { grain: "folder", entityKind: "folder" };
  return { grain: "document",
    entityKind: node.entity_kind === "context" ? "context_page" : node.entity_kind === "agent" ? "agent_page" : "library_item" };
}

/**
 * The current containment contract: folders are nodes and each visible
 * TreeRail placement is one `tree` relation. Exactly one incoming tree
 * relation per contained node is primary; cycles and multiple parents are
 * allowed, so none is rejected here.
 */
export function expectCurrentContainment(scene: WorkspaceGraphScene) {
  // Collect failures and record one assertion: a plain expect per row creates
  // tens of thousands of traced steps and blocks the runner.
  const failures: string[] = [];
  const check = (condition: boolean, message: string) => {
    if (!condition && failures.length < 30) failures.push(message);
  };
  const nodes = new Map(scene.nodes.map((node) => [node.id, node]));
  for (const node of scene.nodes) {
    if (node.grain === "folder") check(node.id === `folder:${node.folder_id}`, `folder ${node.id}: id names its folder`);
  }
  const primaries = new Map<string, number>();
  const contained = new Set<string>();
  for (const edge of scene.edges) {
    if (edge.relation_kind !== "tree") {
      check(edge.primary !== true, `edge ${edge.id}: only containment names a primary placement`);
      continue;
    }
    check(nodes.has(edge.source) && nodes.has(edge.target), `containment ${edge.id}: both ends are rendered nodes`);
    check(edge.id === `contains:${edge.source}|${edge.target}`, `containment ${edge.id}: id names its ends`);
    check(Number.isSafeInteger(edge.relation_count) && edge.relation_count > 0,
      `containment ${edge.id}: positive safe placement count`);
    contained.add(edge.target);
    if (edge.primary) primaries.set(edge.target, (primaries.get(edge.target) ?? 0) + 1);
  }
  for (const id of contained) check(primaries.get(id) === 1, `node ${id}: exactly one primary container`);
  for (const node of scene.nodes) {
    if (node.grain === "document" && node.primary_folder_id !== null) {
      check(contained.has(node.id), `document ${node.id}: its primary placement is a containment relation`);
    }
  }
  expect(failures, "Every real overview containment row must satisfy the wire contract").toEqual([]);
}
