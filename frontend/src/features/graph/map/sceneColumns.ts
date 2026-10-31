import type {
  WorkspaceGraphNode,
  WorkspaceGraphScene,
} from "../../../shared/api/workspaceGraph";
import { GRAPH_NODE_RADIUS_PX } from "../shared/graphTokens";
import type { GraphVisibleIdentity } from "../shared/rendererTypes";

export const WORKSPACE_MAP_NODE_KIND = {
  context: 0,
  library: 1,
  cluster: 2,
} as const;

export const WORKSPACE_MAP_EDGE_KIND = {
  link: 0,
  citation: 1,
} as const;

const COLUMN_VERSION = "workspace-map-relation-columns-v3";
const MAX_EDGE_COUNT = 0xffffffff;

export type WorkspaceMapBuildOptions = {
  workspaceId: string;
  actorId: string;
  /** Stable serialized lens + server-side data filters, never local search. */
  selectionKey: string;
  /** Confirmed unlinked documents hidden by the explicit client display setting. */
  excludedNodeIds?: ReadonlySet<string>;
};

/**
 * Columnar, actor-visible input for the future WebGL Map renderer. Node and
 * edge IDs remain the current API IDs for selection/list parity. Stable node
 * keys and indexed endpoints are for GPU buffers, layout and cache identity.
 * Positions/community hints are never accepted from the server.
 */
export type WorkspaceMapColumns = {
  identity: GraphVisibleIdentity;
  nodeIds: string[];
  nodeKeys: string[];
  nodeLabels: string[];
  nodeFolderKeys: Array<string | null>;
  nodeKinds: Uint8Array;
  nodeSizes: Float32Array;
  /**
   * Documents a node stands for: 1 for a document, the server's member count
   * for a cluster. Render emphasis only, so it is not part of the fingerprint.
   * Optional: older callers and fixtures that omit it draw every node as before.
   */
  nodeDocumentCounts?: Uint32Array;
  edgeIds: string[];
  edgeSources: Uint32Array;
  edgeTargets: Uint32Array;
  edgeKinds: Uint8Array;
  edgeCounts: Uint32Array;
  /** Authored occurrences, not the number of distinct relation identities. */
  edgeWeights: Float64Array;
  edgeCrossFolder: Uint8Array;
};

type VisibleNode = { node: WorkspaceGraphNode; key: string; kind: number };
type VisibleEdge = {
  id: string;
  source: number;
  target: number;
  kind: number;
  count: number;
  weight: number;
  crossFolder: number;
};

function compare(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function semanticNodeKey(node: WorkspaceGraphNode): string | null {
  if (node.grain === "document") {
    return node.entity_id.length > 0 ? `${node.entity_kind}:${node.entity_id}` : null;
  }
  return node.id.length > 0 ? node.id : null;
}

function nodeKind(node: WorkspaceGraphNode): number {
  if (node.grain === "cluster") return WORKSPACE_MAP_NODE_KIND.cluster;
  return node.entity_kind === "context"
    ? WORKSPACE_MAP_NODE_KIND.context
    : WORKSPACE_MAP_NODE_KIND.library;
}

function relationKind(kind: string): number | null {
  switch (kind) {
    case "wikilink":
      return WORKSPACE_MAP_EDGE_KIND.link;
    case "citation_page":
    case "citation_source":
    case "ask_citation":
      return WORKSPACE_MAP_EDGE_KIND.citation;
    default:
      // Map's relation layout omits co-access and membership strokes.
      return null;
  }
}

function positiveCount(count: number): number | null {
  return Number.isSafeInteger(count) && count > 0 && count <= MAX_EDGE_COUNT
    ? count : null;
}

/**
 * Convert the authoritative server-provided visible scene once. Local search
 * is presentation-only and must not alter geometry or its layout identity.
 * Unknown relation kinds and dangling endpoints are omitted, never inferred
 * from `layer` and never fetched to fill the gap. This function does not make
 * the current 2k/6k overview API into a future 20k endpoint.
 */
export function buildWorkspaceMapColumns(
  scene: WorkspaceGraphScene,
  options: WorkspaceMapBuildOptions,
): WorkspaceMapColumns {
  if (!options.workspaceId || !options.actorId || !options.selectionKey) {
    throw new Error("Workspace Map identity requires workspace, actor and selection keys.");
  }
  const visible = scene.nodes
    .filter((node) => !options.excludedNodeIds?.has(node.id))
    .map((node): VisibleNode | null => {
      const key = semanticNodeKey(node);
      return key && node.id ? { node, key, kind: nodeKind(node) } : null;
    })
    .filter((value): value is VisibleNode => value !== null)
    .sort((left, right) => compare(left.key, right.key)
      || compare(left.node.id, right.node.id));

  const nodes: VisibleNode[] = [];
  const seenKeys = new Set<string>();
  const seenIds = new Set<string>();
  for (const entry of visible) {
    if (seenKeys.has(entry.key) || seenIds.has(entry.node.id)) continue;
    seenKeys.add(entry.key);
    seenIds.add(entry.node.id);
    nodes.push(entry);
  }

  const indexById = new Map(nodes.map((entry, index) => [entry.node.id, index]));
  const nodeKeys = nodes.map((entry) => entry.key);
  const nodeIds = nodes.map((entry) => entry.node.id);
  const nodeLabels = nodes.map((entry) => entry.node.label);
  const nodeFolderKeys = nodes.map((entry) => entry.node.grain === "document"
    ? entry.node.primary_folder_id : null);
  const nodeKinds = Uint8Array.from(nodes.map((entry) => entry.kind));
  const nodeDocumentCounts = Uint32Array.from(nodes.map((entry) => entry.node.grain === "cluster"
    ? positiveCount(entry.node.member_count) ?? 1 : 1));

  const candidateEdges = scene.edges.flatMap((edge): VisibleEdge[] => {
    const source = indexById.get(edge.source);
    const target = indexById.get(edge.target);
    const kind = relationKind(edge.relation_kind);
    const count = positiveCount(edge.relation_count);
    if (source === undefined || target === undefined || source === target
      || kind === null || count === null || !edge.id) return [];
    const weight = edge.occurrence_weight ?? count;
    if (!Number.isSafeInteger(weight) || weight < 1) {
      throw new Error("Workspace Map requires positive safe integer occurrence weights.");
    }
    return [{
      id: edge.id,
      source,
      target,
      kind,
      count,
      weight,
      crossFolder: edge.cross_folder ? 1 : 0,
    }];
  }).sort((left, right) => compare(nodeKeys[left.source], nodeKeys[right.source])
    || compare(nodeKeys[left.target], nodeKeys[right.target])
    || left.kind - right.kind
    || left.count - right.count
    || left.weight - right.weight
    || compare(left.id, right.id));
  const edges: VisibleEdge[] = [];
  const seenEdgeIds = new Set<string>();
  for (const edge of candidateEdges) {
    if (seenEdgeIds.has(edge.id)) continue;
    seenEdgeIds.add(edge.id);
    edges.push(edge);
  }

  const degree = new Float64Array(nodes.length);
  for (const edge of edges) {
    degree[edge.source] += edge.count;
    degree[edge.target] += edge.count;
  }
  const { min, max } = GRAPH_NODE_RADIUS_PX.workspace;
  const nodeSizes = Float32Array.from(nodes.map((_entry, index) =>
    Math.min(max, min + Math.sqrt(degree[index]))));

  // Only inputs that can affect this browser's layout are hashed. Metadata
  // such as graph_revision, generated_at, facet counts and projection-row
  // IDs cannot alter coordinates or make an invisible decoy affect the key.
  // Directory membership is retained as display metadata below, never a
  // relation-layout input or a reason to reorder parallel springs.
  const fingerprintSource = JSON.stringify([
    COLUMN_VERSION,
    GRAPH_NODE_RADIUS_PX.workspace,
    nodes.map((entry, index) => [
      entry.key,
      entry.kind,
      nodeSizes[index],
    ]),
    edges.map((edge) => [
      nodeKeys[edge.source],
      nodeKeys[edge.target],
      edge.kind,
      edge.count,
      edge.weight,
    ]),
  ]);

  return {
    identity: {
      workspaceId: options.workspaceId,
      actorId: options.actorId,
      surface: "workspace",
      selectionKey: options.selectionKey,
      visibleFingerprint: sha256Hex(fingerprintSource),
    },
    nodeIds,
    nodeKeys,
    nodeLabels,
    nodeFolderKeys,
    nodeKinds,
    nodeSizes,
    nodeDocumentCounts,
    edgeIds: edges.map((edge) => edge.id),
    edgeSources: Uint32Array.from(edges.map((edge) => edge.source)),
    edgeTargets: Uint32Array.from(edges.map((edge) => edge.target)),
    edgeKinds: Uint8Array.from(edges.map((edge) => edge.kind)),
    edgeCounts: Uint32Array.from(edges.map((edge) => edge.count)),
    edgeWeights: Float64Array.from(edges.map((edge) => edge.weight)),
    edgeCrossFolder: Uint8Array.from(edges.map((edge) => edge.crossFolder)),
  };
}

// SHA-256 is synchronous here so identity and columns are built atomically.
// No Node crypto dependency enters the browser bundle. This is a digest for
// cache identity, not an authorization check; actor visibility is established
// by the API before this function is called.
const SHA256_K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
] as const;

function rotateRight(value: number, count: number): number {
  return (value >>> count) | (value << (32 - count));
}

export function sha256Hex(source: string): string {
  const sourceBytes = new TextEncoder().encode(source);
  const bytes = new Uint8Array(Math.ceil((sourceBytes.length + 9) / 64) * 64);
  bytes.set(sourceBytes);
  bytes[sourceBytes.length] = 0x80;
  const view = new DataView(bytes.buffer);
  const bitLength = BigInt(sourceBytes.length) * 8n;
  view.setUint32(bytes.length - 8, Number((bitLength >> 32n) & 0xffffffffn));
  view.setUint32(bytes.length - 4, Number(bitLength & 0xffffffffn));

  const h = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  const w = new Uint32Array(64);
  for (let offset = 0; offset < bytes.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      w[index] = view.getUint32(offset + index * 4);
    }
    for (let index = 16; index < 64; index += 1) {
      const s0 = rotateRight(w[index - 15], 7)
        ^ rotateRight(w[index - 15], 18) ^ (w[index - 15] >>> 3);
      const s1 = rotateRight(w[index - 2], 17)
        ^ rotateRight(w[index - 2], 19) ^ (w[index - 2] >>> 10);
      w[index] = (w[index - 16] + s0 + w[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, x] = h;
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temp1 = (x + sum1 + choose + SHA256_K[index] + w[index]) >>> 0;
      const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (sum0 + majority) >>> 0;
      x = g; g = f; f = e; e = (d + temp1) >>> 0;
      d = c; c = b; b = a; a = (temp1 + temp2) >>> 0;
    }
    h[0] = (h[0] + a) >>> 0;
    h[1] = (h[1] + b) >>> 0;
    h[2] = (h[2] + c) >>> 0;
    h[3] = (h[3] + d) >>> 0;
    h[4] = (h[4] + e) >>> 0;
    h[5] = (h[5] + f) >>> 0;
    h[6] = (h[6] + g) >>> 0;
    h[7] = (h[7] + x) >>> 0;
  }
  return h.map((value) => value.toString(16).padStart(8, "0")).join("");
}
