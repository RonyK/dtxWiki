import {
  isWorkspaceGraphFolderTopology,
  type WorkspaceGraphDocumentKind,
  type WorkspaceGraphFolder,
  type WorkspaceGraphScene,
} from "../../../shared/api/workspaceGraph";
import { sha256Hex } from "./sceneColumns";

const FOLDER_SCENE_VERSION = "workspace-map-folder-scene.v1";

export type WorkspaceMapFolderPoint = {
  id: string;
  entityKey: string;
  sourceNodeId: string;
  grain: "document" | "fragment";
  entityKind: WorkspaceGraphDocumentKind;
  folderId: string | null;
  memberCount: number;
  label: string;
  /** Aggregate fragments do not invent per-document metrics. */
  referenceCount: number | null;
  evidenceCount: number | null;
};

export type WorkspaceMapVisibleFolder = WorkspaceGraphFolder & {
  childFolderIds: string[];
  pointIds: string[];
};

export type WorkspaceMapFolderRelation = {
  id: string;
  sourceEdgeId: string;
  source: string;
  target: string;
  relationKind: string;
  direction: "directed" | "undirected";
  relationCount: number;
  occurrenceWeight: number;
};

/** Undirected layout inputs; directed authored rows remain in relations. */
export type WorkspaceMapFolderBundle = {
  sourceFolderId: string | null;
  targetFolderId: string | null;
  relationCount: number;
  occurrenceWeight: number;
};

export type WorkspaceMapFolderSceneOptions = {
  budgets?: {
    /** Includes document/fragment points and the actual structural folders. */
    points?: number;
    /** Includes correlated authored rows and nested-folder parent strokes. */
    relations?: number;
  };
};

export type WorkspaceMapPreparedFolderScene = {
  supported: true;
  points: WorkspaceMapFolderPoint[];
  folders: WorkspaceMapVisibleFolder[];
  unplacedPointIds: string[];
  sourceNodeFragments: Record<string, string[]>;
  /** Includes same-fragment loops for exact count/list accounting. */
  relations: WorkspaceMapFolderRelation[];
  /** Only same-fragment loops are excluded, after cluster fragmentation. */
  physicsRelations: WorkspaceMapFolderRelation[];
  folderBundles: WorkspaceMapFolderBundle[];
  requiredPoints: number;
  requiredRelations: number;
  totals: {
    memberCount: number;
    relationCount: number;
    occurrenceWeight: number;
    loopRelationCount: number;
    loopOccurrenceWeight: number;
  };
  geometryFingerprint: string;
};

export type WorkspaceMapFolderSceneResult = WorkspaceMapPreparedFolderScene | {
  supported: false;
  reason: "legacy_topology" | "topology_budget" | "invalid_topology"
    | "point_budget" | "relation_budget";
  requiredPoints?: number;
  requiredRelations?: number;
  limit?: number;
};

function compare(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function compareFolder(left: string | null, right: string | null): number {
  return left === null ? (right === null ? 0 : -1)
    : right === null ? 1 : compare(left, right);
}

function partitionKey(nodeId: string, folderId: string | null, kind: string): string {
  return JSON.stringify([nodeId, folderId, kind]);
}

function safeSum(values: readonly number[]): number | null {
  let sum = 0;
  for (const value of values) {
    sum += value;
    if (!Number.isSafeInteger(sum)) return null;
  }
  return sum;
}

/**
 * Prepare lossless visible inputs for browser-only folder layout. This is not
 * a Directory lens conversion: no facets, secondary placements or inferred
 * folders enter the forest. Older servers explicitly lack this capability.
 */
export function prepareWorkspaceMapFolderScene(
  scene: WorkspaceGraphScene,
  options: WorkspaceMapFolderSceneOptions = {},
): WorkspaceMapFolderSceneResult {
  for (const limit of [options.budgets?.points, options.budgets?.relations]) {
    if (limit !== undefined && (!Number.isSafeInteger(limit) || limit < 0)) {
      throw new Error("Workspace Map folder budgets must be nonnegative safe integers.");
    }
  }
  const topology = scene.folder_topology;
  if (topology === undefined || topology === null) {
    return {
      supported: false,
      reason: scene.warnings.includes("folder_topology_row_budget_exceeded")
        ? "topology_budget" : "legacy_topology",
    };
  }
  if (!isWorkspaceGraphFolderTopology(topology, scene.nodes, scene.edges)) {
    return { supported: false, reason: "invalid_topology" };
  }

  const nodesById = new Map(scene.nodes.map((node) => [node.id, node]));
  const points = topology.memberships.map((membership): WorkspaceMapFolderPoint => {
    const node = nodesById.get(membership.node_id)!;
    const fragmentKey = partitionKey(node.id, membership.folder_id, membership.entity_kind);
    const id = node.grain === "document" ? node.id
      : `folder-fragment:${sha256Hex(fragmentKey)}`;
    return {
      id,
      entityKey: node.grain === "document" ? `${node.entity_kind}:${node.entity_id}` : id,
      sourceNodeId: node.id,
      grain: node.grain === "document" ? "document" : "fragment",
      entityKind: membership.entity_kind,
      folderId: membership.folder_id,
      memberCount: membership.member_count,
      label: node.label,
      referenceCount: node.grain === "document" ? node.reference_count : null,
      evidenceCount: node.grain === "document" ? node.evidence_count : null,
    };
  }).sort((left, right) => compare(left.entityKey, right.entityKey));
  const pointById = new Map(points.map((point) => [point.id, point]));
  if (pointById.size !== points.length) {
    return { supported: false, reason: "invalid_topology" };
  }
  const pointByPartition = new Map(points.map((point) => [
    partitionKey(point.sourceNodeId, point.folderId, point.entityKind), point,
  ]));
  const pointsBySourceNode = new Map<string, string[]>();
  const pointsByFolder = new Map<string | null, string[]>();
  for (const point of points) {
    const nodePoints = pointsBySourceNode.get(point.sourceNodeId) ?? [];
    nodePoints.push(point.id);
    pointsBySourceNode.set(point.sourceNodeId, nodePoints);
    const folderPoints = pointsByFolder.get(point.folderId) ?? [];
    folderPoints.push(point.id);
    pointsByFolder.set(point.folderId, folderPoints);
  }
  const childrenByFolder = new Map<string, string[]>();
  for (const folder of topology.folders) {
    if (folder.parent_folder_id === null) continue;
    const children = childrenByFolder.get(folder.parent_folder_id) ?? [];
    children.push(folder.id);
    childrenByFolder.set(folder.parent_folder_id, children);
  }
  const folders = topology.folders.map((folder): WorkspaceMapVisibleFolder => ({
    id: folder.id,
    label: folder.label,
    parent_folder_id: folder.parent_folder_id,
    direct_member_count: folder.direct_member_count,
    childFolderIds: (childrenByFolder.get(folder.id) ?? []).sort(compare),
    pointIds: pointsByFolder.get(folder.id) ?? [],
  })).sort((left, right) => compare(left.id, right.id));

  const edgesById = new Map(scene.edges.map((edge) => [edge.id, edge]));
  const relations = topology.edge_slices.map((slice): WorkspaceMapFolderRelation => {
    const edge = edgesById.get(slice.edge_id)!;
    const source = pointByPartition.get(partitionKey(
      edge.source, slice.source_folder_id, slice.source_entity_kind,
    ))!;
    const target = pointByPartition.get(partitionKey(
      edge.target, slice.target_folder_id, slice.target_entity_kind,
    ))!;
    return {
      id: `folder-relation:${sha256Hex(JSON.stringify([
        edge.id, source.entityKey, target.entityKey,
      ]))}`,
      sourceEdgeId: edge.id,
      source: source.id,
      target: target.id,
      relationKind: edge.relation_kind,
      direction: edge.direction,
      relationCount: slice.relation_count,
      occurrenceWeight: slice.occurrence_weight,
    };
  }).sort((left, right) => compare(
    pointById.get(left.source)!.entityKey, pointById.get(right.source)!.entityKey,
  ) || compare(pointById.get(left.target)!.entityKey, pointById.get(right.target)!.entityKey)
    || compare(left.relationKind, right.relationKind)
    || compare(left.direction, right.direction)
    || left.relationCount - right.relationCount
    || left.occurrenceWeight - right.occurrenceWeight
    || compare(left.id, right.id));

  const physicsRelations = relations.filter((relation) => relation.source !== relation.target);
  const loops = relations.filter((relation) => relation.source === relation.target);
  const memberCount = safeSum(points.map((point) => point.memberCount));
  const relationCount = safeSum(relations.map((relation) => relation.relationCount));
  const occurrenceWeight = safeSum(relations.map((relation) => relation.occurrenceWeight));
  const loopRelationCount = safeSum(loops.map((relation) => relation.relationCount));
  const loopOccurrenceWeight = safeSum(loops.map((relation) => relation.occurrenceWeight));
  if (memberCount === null || relationCount === null || occurrenceWeight === null
    || loopRelationCount === null || loopOccurrenceWeight === null) {
    return { supported: false, reason: "invalid_topology" };
  }
  const bundles = new Map<string, WorkspaceMapFolderBundle>();
  for (const relation of relations) {
    const sourceFolder = pointById.get(relation.source)!.folderId;
    const targetFolder = pointById.get(relation.target)!.folderId;
    if (sourceFolder === targetFolder) continue;
    const [sourceFolderId, targetFolderId] = compareFolder(sourceFolder, targetFolder) < 0
      ? [sourceFolder, targetFolder] : [targetFolder, sourceFolder];
    const key = JSON.stringify([sourceFolderId, targetFolderId]);
    const previous = bundles.get(key);
    bundles.set(key, {
      sourceFolderId,
      targetFolderId,
      relationCount: (previous?.relationCount ?? 0) + relation.relationCount,
      occurrenceWeight: (previous?.occurrenceWeight ?? 0) + relation.occurrenceWeight,
    });
  }
  const folderBundles = [...bundles.values()].sort((left, right) =>
    compareFolder(left.sourceFolderId, right.sourceFolderId)
      || compareFolder(left.targetFolderId, right.targetFolderId));
  const requiredPoints = points.length + folders.length;
  const requiredRelations = relations.length
    + folders.filter((folder) => folder.parent_folder_id !== null).length;
  if (options.budgets?.points !== undefined && requiredPoints > options.budgets.points) {
    return { supported: false, reason: "point_budget", requiredPoints, requiredRelations,
      limit: options.budgets.points };
  }
  if (options.budgets?.relations !== undefined && requiredRelations > options.budgets.relations) {
    return { supported: false, reason: "relation_budget", requiredPoints, requiredRelations,
      limit: options.budgets.relations };
  }

  const geometryFingerprint = sha256Hex(JSON.stringify([
    FOLDER_SCENE_VERSION,
    points.map((point) => [point.entityKey, point.entityKind, point.folderId,
      point.memberCount, point.referenceCount, point.evidenceCount]),
    folders.map((folder) => [folder.id, folder.parent_folder_id, folder.direct_member_count]),
    relations.map((relation) => [pointById.get(relation.source)!.entityKey,
      pointById.get(relation.target)!.entityKey, relation.relationKind,
      relation.direction, relation.relationCount, relation.occurrenceWeight]),
    folderBundles.map((bundle) => [bundle.sourceFolderId, bundle.targetFolderId,
      bundle.relationCount, bundle.occurrenceWeight]),
  ]));
  return {
    supported: true,
    points,
    folders,
    unplacedPointIds: pointsByFolder.get(null) ?? [],
    sourceNodeFragments: Object.fromEntries([...pointsBySourceNode.entries()]
      .sort(([left], [right]) => compare(left, right))),
    relations,
    physicsRelations,
    folderBundles,
    requiredPoints,
    requiredRelations,
    totals: { memberCount, relationCount, occurrenceWeight, loopRelationCount, loopOccurrenceWeight },
    geometryFingerprint,
  };
}
