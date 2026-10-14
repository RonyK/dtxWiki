import type { GraphNodeFamily } from "../../../shared/api/graph";

export type GraphPoint = { x: number; y: number };

// This common shape is a transport envelope only. Local (Sigma) stores a
// normalized camera; Workspace Map (Cosmos) stores a graph-space center. Never
// restore one engine's `pan` into another without an engine-version tag.
export type GraphViewport = {
  zoom: number;
  pan: GraphPoint;
};

export type GraphRenderNode = {
  id: string;
  label: string;
  canvasLabel?: string;
  family: GraphNodeFamily;
  status?: string | null;
  is_seed?: boolean;
  entity_kind?: string;
  entity_id?: string;
  entityKind?: string;
  grain?: string;
  referenceCount?: number;
  metricSized?: boolean;
  nodeWidth?: number;
  nodeHeight?: number;
};

export type GraphRenderEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  layer: string;
  edge_kind?: string;
  edgeKind?: string;
  action_kind?: string | null;
  actionKind?: string;
  actionFamily?: string;
  direction?: string;
  directed?: string;
  aggregate?: boolean | string;
  count?: number | null;
  crossFolder?: string;
};

export type GraphRenderScene = {
  nodes: GraphRenderNode[];
  edges: GraphRenderEdge[];
  // Client-only Local selection identity. Projection revision and generated
  // timestamps do not change selection. Context separately fences authority.
  scopeKey?: string;
};

// Never reuse layout coordinates across actors, changed visibility, or a
// different selection/lens. The fingerprint is computed from visible inputs
// by the browser, not supplied as a server-side layout or community hint.
export type GraphVisibleIdentity = {
  workspaceId: string;
  actorId: string;
  surface: "local" | "workspace";
  selectionKey: string;
  visibleFingerprint: string;
};

export function sameGraphVisibleIdentity(
  left: GraphVisibleIdentity,
  right: GraphVisibleIdentity,
): boolean {
  return left.workspaceId === right.workspaceId
    && left.actorId === right.actorId
    && left.surface === right.surface
    && left.selectionKey === right.selectionKey
    && left.visibleFingerprint === right.visibleFingerprint;
}

export type GraphRendererEvents<TCamera> = {
  onHover: (id: string | null) => void;
  onSelect: (id: string | null) => void;
  onCameraChange: (camera: TCamera) => void;
  onContextStatus: (status: "lost" | "restored") => void;
};

// Scene and camera types are renderer-specific: a 20k-point Map may pass
// column buffers without materializing Cytoscape-style node objects.
export interface GraphRendererAdapter<TScene, TCamera> {
  ready(signal: AbortSignal): Promise<void>;
  load(scene: TScene, identity: GraphVisibleIdentity): void;
  update(scene: TScene, identity: GraphVisibleIdentity): void;
  destroy(): void;
  pause(): void;
  resume(): void;
  fit(): void;
  zoomBy(factor: number): void;
  reset(): void;
  pin(id: string, position?: GraphPoint): GraphPoint | null;
  unpin(id: string): void;
  getCamera(): TCamera;
  setCamera(camera: TCamera): void;
  setReducedMotion(enabled: boolean): void;
  subscribe(events: GraphRendererEvents<TCamera>): () => void;
}
