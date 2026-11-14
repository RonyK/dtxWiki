import { createD3RowOrder } from "../shared/physics/d3RowOrder";
import { createD3WorkerLayout, createInitialD3Positions } from "../shared/physics/d3WorkerLayout";
import type { LocalLayoutInput, LocalLayoutPositions } from "../shared/physics/layoutTypes";
import type { CosmosLayoutSolver, CosmosPositionLayout, CosmosPositionLayoutInput, CosmosPositionLayoutOptions } from "../shared/cosmos/layoutTypes";
import type { CosmosSceneColumns } from "../shared/cosmos/sceneTypes";

function localInput(columns: CosmosSceneColumns): LocalLayoutInput {
  if (!columns.layoutInput) throw new Error("Context renderer requires its visible Local force input");
  return columns.layoutInput;
}

function record(keys: readonly string[], positions: Float32Array): LocalLayoutPositions {
  return Object.fromEntries(keys.map((key, index) => [key, { x: positions[index * 2], y: positions[index * 2 + 1] }]));
}

/** The Context binding retains Local force units and gains; Map calibration is separate. */
function createLocalPositionLayout(input: CosmosPositionLayoutInput, options: CosmosPositionLayoutOptions): CosmosPositionLayout {
  let current = input;
  let keys = input.columns.nodeKeys;
  let destroyed = false;
  let paused = true;
  let reducedMotion = input.reducedMotion;
  let keyIndices = new Map(keys.map((key, index) => [key, index]));
  const rowOrder = createD3RowOrder();
  const points = record(keys, input.positions);
  const fixed = Object.fromEntries(input.pins.map(index => [keys[index], points[keys[index]]]));
  const fail = (error: unknown) => {
    if (destroyed) return;
    destroyed = true;
    layout.destroy();
    options.onError(error instanceof Error ? error : new Error(String(error)));
  };
  const layout = createD3WorkerLayout(localInput(input.columns), {
    cachedPositions: points, fixedPositions: fixed, autoStart: false,
    reducedMotion, initialPositionsAreSolved: input.restoredSolved,
    onIndexedTick(rows, workerKeys) {
      if (destroyed) return;
      try { options.onPositions(rowOrder(rows, workerKeys, keys)); } catch (error) { fail(error); }
    },
    onState: state => {
      if (!destroyed) { try { options.onState(state); } catch (error) { fail(error); } }
    },
    onSettledPin(key, position) {
      const index = keyIndices.get(key);
      if (!destroyed && index !== undefined) options.onSettledPin?.(index, position);
    },
    onError: fail,
  });
  const key = (index: number) => keys[index];
  return {
    start(reheat = false) { if (!destroyed) { paused = false; layout.resume(reheat); } },
    // The Worker owns its unpinned rows, so a pause sends none back; stopping
    // a stopped layout does nothing.
    stop() { if (!destroyed && !paused) { paused = true; layout.pause(true); } },
    update(next) {
      if (destroyed) return;
      current = next; keys = next.columns.nodeKeys;
      keyIndices = new Map(keys.map((value, index) => [value, index]));
      const nextPoints = record(keys, next.positions);
      const nextFixed = Object.fromEntries(next.pins.map(index => [keys[index], nextPoints[keys[index]]]));
      // A paused frame adopts new membership without relaxing retained rows.
      // Reduced motion uses the existing bounded static topology solve.
      layout.update(localInput(next.columns), nextFixed,
        paused ? { settle: true, settleTicks: 0, settleAtCurrentAlpha: true }
          : reducedMotion ? { settle: true } : {});
    },
    // The Worker owns every unpinned row; pins and drags reach it as commands.
    // A row sync therefore declares pins only, never overwriting its rows.
    setPositions(rows, pins = current.pins, reconcile = {}) {
      if (!destroyed) layout.setPositions(record(keys, rows), pins.map(index => key(index)),
        { reheat: reconcile.reheat ?? !paused, pinsOnly: true });
    },
    setReducedMotion(enabled) {
      if (!destroyed && reducedMotion !== enabled) { reducedMotion = enabled; layout.setReducedMotion(enabled); }
    },
    pinWhenSettled(index) { if (!destroyed && key(index)) layout.pinWhenSettled(key(index)); },
    pin(index, position, reheat = !paused) { return destroyed || !key(index) ? null : layout.pin(key(index), position, reheat); },
    unpin(index, reheat = !paused) { if (!destroyed && key(index)) layout.unpin(key(index), reheat); },
    beginDrag(index, position) { return destroyed || !key(index) ? null : layout.beginDrag(key(index), position); },
    moveDrag(index, position) { return destroyed || !key(index) ? null : layout.moveDrag(key(index), position); },
    endDrag(index) { if (!destroyed && key(index)) layout.endDrag(key(index)); },
    isRunning: () => !destroyed && layout.isRunning(),
    destroy() { if (!destroyed) { destroyed = true; layout.destroy(); } },
  };
}

/** Same D3 engine/protocol as 2K; surface differences remain visible force inputs. */
export function createLocalCosmosLayoutSolver(): CosmosLayoutSolver {
  return Object.freeze({
    diagnosticsName: "d3-worker",
    awaitInitialPositions: true,
    initialPositions(columns, fallback) {
      const input = localInput(columns as CosmosSceneColumns);
      const positions = createInitialD3Positions(input);
      return Float32Array.from(fallback, (_, index) => {
        const point = positions[columns.nodeKeys[Math.floor(index / 2)]];
        return point ? index % 2 ? point.y : point.x : 0;
      });
    },
    positions: (current: Float32Array) => current,
    knownPositions: (current: Float32Array) => current,
    uploadedRows: () => null,
    permitsVisibility: (hidden: boolean) => !hidden,
    cameraPolicy: () => ({ enabled: false, framingPolicy: undefined, terminalFit: undefined }),
    hasUsablePositionRows: (solved: boolean) => solved,
    shouldFitPublishedRows: (restored: boolean) => !restored,
    communityGeneration: () => "none",
    initialDiagnostics: () => ({ localPhysics: "d3-worker", localLayoutPhase: "arranging", localLayoutRunning: "false", localLayoutTicks: "0" }),
    acceptsCommunityPositions: () => false,
    layoutStatus: <T>(status: T) => status,
    createPositionLayout: createLocalPositionLayout,
    synchronizePositionRows: (layout: CosmosPositionLayout | null, rows: Float32Array, pins: readonly number[]) => layout?.setPositions(rows, pins),
    afterPositionEdit: (host: () => void) => host(),
    afterNativePositionEdit: () => {},
    attachPositionTracking: () => {},
    attachLargePointPicker: () => {},
    collisionRadii: () => null,
    collisionConfig: () => ({ simulationCollision: 0 }),
    syncPhysics(input, runtime) {
      runtime.enableNative(false); runtime.pauseNative();
      if (!input.canInitialize) { runtime.stopLayout(); return false; }
      // A cold static frame must be solved even while the reader is paused.
      const layout = runtime.ensureLayout();
      layout?.setReducedMotion(input.reducedMotion);
      if (input.paused || input.hidden) { layout?.stop(); return false; }
      if (layout && (input.reheat || !layout.isRunning())) layout.start(input.reheat && !input.reducedMotion);
      return layout?.isRunning() ?? false;
    },
  } satisfies CosmosLayoutSolver);
}
