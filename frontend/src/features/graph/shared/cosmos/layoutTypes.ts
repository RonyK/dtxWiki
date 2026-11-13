import type { GraphConfig } from "@cosmos.gl/graph";
import type { D3WorkerLayoutState } from "../physics/d3WorkerLayout";
import type { CosmosSceneColumns } from "./sceneTypes";
import type { GraphPoint } from "../rendererTypes";

/** Scene-owned conversion between CSS glyphs and canonical layout units. */
export type CosmosPositionUnitBasis = Readonly<{
  nodeCount: number;
  referenceViewport: Readonly<{ width: number; height: number }>;
  referenceZoom: number;
  nominalRadiusCss: readonly number[];
  radiusModel: readonly number[];
  calibrationPasses: number;
}>;

export type CosmosCollisionBasis = { radius: number; padding: number };

/** A layout's indexed position channel; native simulation can return null. */
export type CosmosPositionLayout = {
  start(reheat?: boolean): void;
  stop(): void;
  setPositions(positions: Float32Array, pins?: readonly number[], options?: { reheat?: boolean }): void;
  setReducedMotion(enabled: boolean): void;
  /** CPU solvers update their existing Worker; native solvers omit this channel. */
  update?(input: CosmosPositionLayoutInput): void;
  pinWhenSettled?(index: number): void;
  pin?(index: number, position?: GraphPoint, reheat?: boolean): GraphPoint | null;
  unpin?(index: number, reheat?: boolean): void;
  beginDrag?(index: number, position: GraphPoint): GraphPoint | null;
  moveDrag?(index: number, position: GraphPoint): GraphPoint | null;
  endDrag?(index: number): void;
  destroy(): void;
  isRunning(): boolean;
};

export type CosmosPositionLayoutInput = {
  columns: CosmosSceneColumns;
  positions: Float32Array;
  diameters: Float32Array;
  shapes: Float32Array;
  pins: readonly number[];
  reducedMotion: boolean;
  restoredSolved?: boolean;
  unitBasis?: CosmosPositionUnitBasis;
  viewport: { width: number; height: number };
  maxDiameterCss?: number;
};

export type CosmosPositionLayoutOptions = {
  onPositions(positions: Float32Array): void;
  onState(state: D3WorkerLayoutState): void;
  onUnitBasis?(basis: CosmosPositionUnitBasis): void;
  onSettledPin?(index: number, position: GraphPoint): void;
  onError(error: Error): void;
};

export type CosmosCameraPolicy = {
  enabled: boolean;
  framingPolicy: Readonly<{ windowMs: number; intervalMs: number }> | undefined;
  terminalFit: Readonly<{ cooledAlpha: number; windowMs: number }> | undefined;
};

export type CosmosPhysicsPermission = {
  permitted: boolean;
  paused: boolean;
  reducedMotion: boolean;
  hidden: boolean;
  dragging: boolean;
  alpha: number;
  canInitialize: boolean;
  reheat: boolean;
};

export type CosmosPhysicsRuntime = {
  enableNative(enabled: boolean): void;
  pauseNative(): void;
  startNative(alpha: number): void;
  ensureLayout(): CosmosPositionLayout | null;
  stopLayout(): void;
};

/**
 * Operations bound once by a surface. Rendering never selects a solver by
 * inspecting a surface name, adapter's numeric kinds or current node count.
 */
export type CosmosLayoutSolver = Readonly<{
  diagnosticsName: string;
  /** A ready renderer also owns an accepted first CPU frame. */
  awaitInitialPositions?: true;
  initialPositions(columns: Pick<CosmosSceneColumns, "nodeKeys" | "nodeContainerIndices">, fallback: Float32Array): Float32Array;
  positions(current: Float32Array, readNative: () => Float32Array): Float32Array;
  knownPositions(current: Float32Array): Float32Array | undefined;
  uploadedRows(positions: Float32Array): Float32Array | null;
  permitsVisibility(hidden: boolean): boolean;
  cameraPolicy(count: number): CosmosCameraPolicy;
  hasUsablePositionRows(solved: boolean): boolean;
  shouldFitPublishedRows(restored: boolean): boolean;
  communityGeneration(): "none" | "native-relation";
  initialDiagnostics(): Record<string, string>;
  acceptsCommunityPositions(): boolean;
  layoutStatus<T>(status: T): T | null;
  createPositionLayout(input: CosmosPositionLayoutInput, options: CosmosPositionLayoutOptions): CosmosPositionLayout | null;
  synchronizePositionRows(layout: CosmosPositionLayout | null, positions: Float32Array, pins: readonly number[]): void;
  afterPositionEdit(host: () => void, native?: () => void): void;
  afterNativePositionEdit(native: () => void): void;
  attachPositionTracking(attach: () => void): void;
  attachLargePointPicker(count: number, attach: () => void): void;
  collisionRadii(sizes: Float32Array, shapes: Float32Array, basis: CosmosCollisionBasis,
    maximum: number, scale: number, count: number, collisionSizes?: Float32Array): Float32Array | null;
  collisionConfig(forceApplied: boolean, forceBasis: CosmosCollisionBasis,
    collisionBasis: CosmosCollisionBasis, scale: number, count: number): Partial<GraphConfig>;
  syncPhysics(input: CosmosPhysicsPermission, runtime: CosmosPhysicsRuntime): boolean;
}>;
