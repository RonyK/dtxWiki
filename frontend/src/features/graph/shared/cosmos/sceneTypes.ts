import type { GraphEdgeVisualKind, GraphNodeVisualKind } from "../graphTokens";
import type { GraphVisibleIdentity } from "../rendererTypes";
import type { LocalLayoutInput } from "../physics/layoutTypes";

/**
 * Actor-visible columns consumed by the shared Cosmos renderer. Original IDs
 * and relation metadata stay separate from their legend appearance: several
 * citation kinds may look alike without becoming the same relationship.
 *
 * Numeric nodeKinds/edgeKinds belong to the supplying layout adapter. The
 * renderer reads visual kinds or prepared GPU buffers instead of interpreting
 * those codes. Group keys and container rows are optional layout/label input,
 * never a server-supplied coordinate or community hint.
 */
export type CosmosSceneColumns = {
  identity: GraphVisibleIdentity;
  nodeIds: string[];
  nodeKeys: string[];
  nodeLabels: string[];
  nodeFolderKeys: Array<string | null>;
  nodeContainerIndices?: Int32Array;
  nodeKinds: Uint8Array;
  nodeSizes: Float32Array;
  /** Document multiplicity is presentation metadata, not layout identity. */
  nodeDocumentCounts?: Uint32Array;
  /** Original wire grain, distinct from entity subtype and legend appearance. */
  nodeGrains?: Array<string | null>;
  /** Original entity kinds, independent of the supplying adapter's codes. */
  nodeEntityKinds?: string[];
  nodeVisualKinds?: GraphNodeVisualKind[];
  /** Local's force input uses semantic kinds, not the Map's numeric enum. */
  layoutInput?: LocalLayoutInput;
  localGeometryKey?: string | null;
  nodeIsSeed?: Uint8Array;
  nodeIsCurrent?: Uint8Array;
  nodeSeedGlyph?: Uint8Array;
  nodeMuted?: Uint8Array;
  nodeHighlighted?: Uint8Array;
  nodeSessions?: Uint8Array;
  nodeBadges?: Array<string | null>;
  edgeIds: string[];
  edgeSources: Uint32Array;
  edgeTargets: Uint32Array;
  edgeKinds: Uint8Array;
  edgeCounts: Uint32Array;
  /** Occurrences are force weight, not distinct relation identity/count. */
  edgeWeights: Float64Array;
  edgeCrossFolder: Uint8Array;
  edgeRelationKinds?: string[];
  /** Original direction; the indexed source/target order is never reversed. */
  edgeDirections?: Array<string | null>;
  edgeVisualKinds?: GraphEdgeVisualKind[];
  edgeLabels?: string[];
  edgeCaptions?: Array<string | null>;
  edgeMuted?: Uint8Array;
  edgeHighlighted?: Uint8Array;
  edgeRemoved?: Uint8Array;
};

/** The same authority fence is used by layout, picking and deferred scene work. */
// Identities are immutable once a scene's columns are built; a scene's key is
// derived once and reused by every later check, however large the scene.
const identityKeys = new WeakMap<CosmosSceneColumns["identity"], string>();

export function cosmosSceneIdentityKey(columns: Pick<CosmosSceneColumns, "identity">): string {
  const identity = columns.identity;
  let key = identityKeys.get(identity);
  if (key === undefined) {
    key = JSON.stringify([
      identity.workspaceId,
      identity.actorId,
      identity.surface,
      identity.selectionKey,
      identity.visibleFingerprint,
    ]);
    identityKeys.set(identity, key);
  }
  return key;
}

/** A transient snapshot owns its columns, including any presentation metadata. */
export function cloneCosmosSceneColumns(columns: CosmosSceneColumns): CosmosSceneColumns {
  return {
    ...columns,
    identity: { ...columns.identity },
    nodeIds: [...columns.nodeIds],
    nodeKeys: [...columns.nodeKeys],
    nodeLabels: [...columns.nodeLabels],
    nodeFolderKeys: [...columns.nodeFolderKeys],
    nodeContainerIndices: columns.nodeContainerIndices?.slice(),
    nodeKinds: columns.nodeKinds.slice(),
    nodeSizes: columns.nodeSizes.slice(),
    nodeDocumentCounts: columns.nodeDocumentCounts?.slice(),
    nodeGrains: columns.nodeGrains?.slice(),
    nodeEntityKinds: columns.nodeEntityKinds?.slice(),
    nodeVisualKinds: columns.nodeVisualKinds?.slice(),
    layoutInput: columns.layoutInput ? {
      ...columns.layoutInput,
      nodes: columns.layoutInput.nodes.map(node => ({ ...node })),
      edges: columns.layoutInput.edges.map(edge => ({ ...edge })),
    } : undefined,
    nodeIsSeed: columns.nodeIsSeed?.slice(),
    nodeIsCurrent: columns.nodeIsCurrent?.slice(),
    nodeSeedGlyph: columns.nodeSeedGlyph?.slice(),
    nodeMuted: columns.nodeMuted?.slice(),
    nodeHighlighted: columns.nodeHighlighted?.slice(),
    nodeSessions: columns.nodeSessions?.slice(),
    nodeBadges: columns.nodeBadges?.slice(),
    edgeIds: [...columns.edgeIds],
    edgeSources: columns.edgeSources.slice(),
    edgeTargets: columns.edgeTargets.slice(),
    edgeKinds: columns.edgeKinds.slice(),
    edgeCounts: columns.edgeCounts.slice(),
    edgeWeights: columns.edgeWeights.slice(),
    edgeCrossFolder: columns.edgeCrossFolder.slice(),
    edgeRelationKinds: columns.edgeRelationKinds?.slice(),
    edgeDirections: columns.edgeDirections?.slice(),
    edgeVisualKinds: columns.edgeVisualKinds?.slice(),
    edgeLabels: columns.edgeLabels?.slice(),
    edgeCaptions: columns.edgeCaptions?.slice(),
    edgeMuted: columns.edgeMuted?.slice(),
    edgeHighlighted: columns.edgeHighlighted?.slice(),
    edgeRemoved: columns.edgeRemoved?.slice(),
  };
}
