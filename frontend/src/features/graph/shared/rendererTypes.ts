import type { GraphNodeFamily } from "../../../shared/api/graph";

export type GraphPoint = { x: number; y: number };

// Both interactive surfaces use canonical graph-space camera centers. This
// envelope is not interchangeable with a legacy renderer's normalized camera.
export type GraphViewport = {
  zoom: number;
  pan: GraphPoint;
};

export type GraphCanvasHandle = {
  centerOn(id: string): void;
  /** Bring a document into view; the Local map also traces the current document's ring once. */
  locate(id: string): void;
  fit(): void;
  pauseLayout(): void;
  resumeLayout(): void;
  pin(id: string, pinned: boolean): GraphPoint | null;
  reset(): void;
  setViewport(viewport: GraphViewport): void;
  zoomIn(): void;
  zoomOut(): void;
};

export type RendererStatus = "loading" | "ready" | "error";

export type GraphRenderNode = {
  hidden?: boolean;
  muted?: boolean;
  highlighted?: boolean;
  id: string;
  label: string;
  canvasLabel?: string;
  family: GraphNodeFamily;
  status?: string | null;
  is_seed?: boolean;
  entity_kind?: string;
  entity_id?: string;
  entityKind?: string;
  /** Who acted, for a conversation: its client ("Claude Code") or person. */
  actor_label?: string | null;
  grain?: string;
  referenceCount?: number;
  metricSized?: boolean;
  nodeWidth?: number;
  nodeHeight?: number;
};

export type GraphRenderEdge = {
  eventCaption?: string;
  hidden?: boolean;
  muted?: boolean;
  highlighted?: boolean;
  removed?: boolean;
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
  // The node of the document the graph is opened from (the primary seed),
  // drawn with the legend's "This document" glyph; seeds added by expanding
  // keep their own. Left undefined, every seed keeps the seed glyph.
  currentId?: string | null;
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
