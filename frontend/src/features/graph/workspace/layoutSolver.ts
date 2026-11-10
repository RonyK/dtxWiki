import type { GraphConfig } from "./cosmosNative";
import { WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget } from "../shared/workspaceMapBudget";
import { d3PhyllotaxisPositions } from "../shared/physics/initialPositions";
import { workspaceInitialCameraPolicy } from "./initialCameraPolicy";
import { buildWorkspaceNativeCollisionRadii } from "./nativeCollisionRadii";
import { buildWorkspaceNativeCanonicalForceConfig } from "./nativeForceProfile";
import { buildWorkspaceRelationForceConfig, type WorkspaceRelationCollisionBasis } from "./relationPhysics";
import { createWorkspaceD3Layout, type WorkspaceD3Layout, type WorkspaceD3LayoutInput, type WorkspaceD3LayoutOptions, type WorkspaceD3UnitBasis } from "./d3MapLayout";

export type WorkspacePositionLayout = WorkspaceD3Layout;
export type WorkspacePositionUnitBasis = WorkspaceD3UnitBasis;

/**
 * Solver boundary chosen once from the admitted budget. Render, camera, label,
 * picking and gesture code ask for operations, never inspect the solver kind.
 */
export function createWorkspaceLayoutSolver(budget: WorkspaceMapBudget) {
  const ownsPositionRows = budget.documents <= WORKSPACE_MAP_DEFAULT_BUDGET.documents;
  return Object.freeze({
    diagnosticsName: ownsPositionRows ? "d3-worker" : "cosmos-native",
    initialPositions(keys: readonly string[], fallback: Float32Array): Float32Array {
      if (!ownsPositionRows) return fallback;
      const ordered = [...keys].sort();
      const rows = d3PhyllotaxisPositions(keys.length);
      const byKey = new Map(ordered.map((key, index) => [key, index]));
      const result = new Float32Array(rows.length);
      keys.forEach((key, index) => {
        const source = byKey.get(key)!;
        result[index * 2] = rows[source * 2]; result[index * 2 + 1] = rows[source * 2 + 1];
      });
      return result;
    },
    positions(current: Float32Array, readNative: () => Float32Array): Float32Array {
      return ownsPositionRows ? current : readNative();
    },
    knownPositions(current: Float32Array): Float32Array | undefined {
      return ownsPositionRows ? current : undefined;
    },
    uploadedRows(positions: Float32Array): Float32Array | null {
      return ownsPositionRows ? null : positions.slice();
    },
    permitsVisibility(hidden: boolean): boolean { return !ownsPositionRows || !hidden; },
    cameraPolicy(count: number) {
      const policy = workspaceInitialCameraPolicy(count, budget, ownsPositionRows);
      // Published D3 rows get one canonical Fit, independent of live motion.
      return ownsPositionRows ? { ...policy, enabled: false } : policy;
    },
    hasUsablePositionRows(solved: boolean): boolean { return !ownsPositionRows || solved; },
    shouldFitPublishedRows(restored: boolean): boolean { return ownsPositionRows && !restored; },
    communityGeneration(): "none" | "native-relation" { return ownsPositionRows ? "none" : "native-relation"; },
    initialDiagnostics(): Record<string, string> {
      return ownsPositionRows ? { mapPhysics: "d3-worker", mapLayoutPhase: "arranging", mapLayoutRunning: "false", mapLayoutTicks: "0" }
        : { mapPhysics: "cosmos-native" };
    },
    acceptsCommunityPositions(): boolean { return !ownsPositionRows; },
    layoutStatus<T>(status: T): T | null { return ownsPositionRows ? status : null; },
    createPositionLayout(input: WorkspaceD3LayoutInput, options: WorkspaceD3LayoutOptions): WorkspaceD3Layout | null {
      return ownsPositionRows ? createWorkspaceD3Layout(input, options) : null;
    },
    synchronizePositionRows(layout: WorkspaceD3Layout | null, positions: Float32Array, pins: readonly number[]): void {
      if (ownsPositionRows) layout?.setPositions(positions, pins);
    },
    afterPositionEdit(host: () => void, native: () => void = () => {}): void {
      if (ownsPositionRows) host(); else native();
    },
    afterNativePositionEdit(native: () => void): void { if (!ownsPositionRows) native(); },
    attachPositionTracking(attach: () => void): void { if (!ownsPositionRows) attach(); },
    attachLargePointPicker(count: number, attach: () => void): void {
      if (!ownsPositionRows && count > WORKSPACE_MAP_DEFAULT_BUDGET.documents) attach();
    },
    collisionRadii(sizes: Float32Array, shapes: Float32Array, basis: WorkspaceRelationCollisionBasis,
      maximum: number, scale: number, count: number): Float32Array | null {
      if (ownsPositionRows || count <= WORKSPACE_MAP_DEFAULT_BUDGET.documents) return null;
      return Float32Array.from(buildWorkspaceNativeCollisionRadii(sizes, shapes, basis, maximum), radius => radius * scale);
    },
    collisionConfig(forceApplied: boolean, forceBasis: WorkspaceRelationCollisionBasis,
      collisionBasis: WorkspaceRelationCollisionBasis, scale: number, count: number): Partial<GraphConfig> {
      const large = !ownsPositionRows && count > WORKSPACE_MAP_DEFAULT_BUDGET.documents;
      return { ...(forceApplied || ownsPositionRows ? {}
        : large ? buildWorkspaceNativeCanonicalForceConfig(scale) : buildWorkspaceRelationForceConfig(forceBasis)),
      simulationCollision: ownsPositionRows ? 0 : 1,
      simulationCollisionRadius: large ? 0 : collisionBasis.radius,
      simulationCollisionPadding: collisionBasis.padding };
    },
    syncPhysics(input: { permitted: boolean; paused: boolean; reducedMotion: boolean; hidden: boolean;
      dragging: boolean; alpha: number; canInitialize: boolean; reheat: boolean }, runtime: {
      enableNative(enabled: boolean): void;
      pauseNative(): void;
      startNative(alpha: number): void;
      ensureLayout(): WorkspaceD3Layout | null;
      stopLayout(): void;
    }): boolean {
      if (!input.canInitialize) {
        runtime.enableNative(false); runtime.pauseNative(); runtime.stopLayout(); return false;
      }
      runtime.enableNative(input.permitted && !ownsPositionRows);
      if (!ownsPositionRows) {
        if (input.permitted) runtime.startNative(input.alpha); else runtime.pauseNative();
        return false;
      }
      runtime.pauseNative();
      if ((!input.permitted && !input.reducedMotion) || input.dragging || input.paused || input.hidden) {
        runtime.stopLayout(); return false;
      }
      const layout = runtime.ensureLayout();
      layout?.setReducedMotion(input.reducedMotion);
      if (layout && (input.reheat || !layout.isRunning())) layout.start(input.reheat && !input.reducedMotion);
      return layout?.isRunning() ?? false;
    },
  });
}
