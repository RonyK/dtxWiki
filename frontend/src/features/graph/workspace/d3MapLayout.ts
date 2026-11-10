import type { WorkspaceMapColumns } from "../map/sceneColumns";
import { createD3WorkerLayout, type D3WorkerLayoutState } from "../shared/physics/d3WorkerLayout";
import type { LocalLayoutInput, LocalLayoutPositions } from "../local/layoutTypes";
import { buildWorkspaceD3UnitBasis, isReusableWorkspaceD3UnitBasis, measureWorkspaceD3BodyOverlap,
  workspaceD3FitZoom, type WorkspaceD3UnitBasis } from "./d3MapUnits";

export type { WorkspaceD3UnitBasis } from "./d3MapUnits";
export type WorkspaceD3Layout = {
  start(reheat?: boolean): void;
  stop(): void;
  setPositions(positions: Float32Array, pins?: readonly number[], options?: { reheat?: boolean }): void;
  setReducedMotion(enabled: boolean): void;
  destroy(): void;
  isRunning(): boolean;
};

/** Indexed Map binding. Original relation rows enter D3; folders never do. */
export type WorkspaceD3LayoutInput = {
  columns: WorkspaceMapColumns;
  positions: Float32Array;
  diameters: Float32Array;
  shapes: Float32Array;
  pins: readonly number[];
  reducedMotion: boolean;
  restoredSolved?: boolean;
  unitBasis?: WorkspaceD3UnitBasis;
  viewport: { width: number; height: number };
  maxDiameterCss?: number;
};
export type WorkspaceD3LayoutOptions = {
  onPositions(positions: Float32Array): void;
  onState(state: D3WorkerLayoutState): void;
  onUnitBasis?(basis: WorkspaceD3UnitBasis): void;
  onError(error: Error): void;
};

// One D3 cooling run in two batches: the rows after the first batch give the
// real extent that converts CSS glyphs to graph units, and the same cooling
// finishes with those radii (ending below alphaMin). Short contact passes then
// resolve what is left. No camera/resize can rearm it.
const INITIAL_SOLVE_TICKS = 150;
const REMAINING_SOLVE_TICKS = 160;
const CONTACT_PASS_TICKS = 80;
const MAX_CONTACT_PASSES = 3;
const FIT_CLEARANCE_SLACK = .97;
const MATERIAL_OVERLAP_CSS = .5;

export function createWorkspaceD3Layout(input: WorkspaceD3LayoutInput, options: WorkspaceD3LayoutOptions): WorkspaceD3Layout {
  const keys = input.columns.nodeKeys;
  const toRecord = (positions: Float32Array): LocalLayoutPositions => Object.fromEntries(
    keys.map((key, index) => [key, { x: positions[index * 2], y: positions[index * 2 + 1] }]),
  );
  const reused = input.restoredSolved === true && isReusableWorkspaceD3UnitBasis(input.unitBasis, keys.length);
  let basis = reused ? input.unitBasis! : buildWorkspaceD3UnitBasis(input);
  let acceptedSolved = reused;
  let basisPublished = false;
  let destroyed = false;
  let explicitlyPaused = false;
  let requestedRunning = false;
  let requestedReheat = false;
  let reducedMotion = input.reducedMotion;
  let indexedOrder: readonly number[] | null = null;
  let directOrder = true;
  let fixedIndices = new Set(input.pins);
  let catalog: readonly string[] | null = null;
  let calibrationPasses = 0;
  const edges: LocalLayoutInput["edges"] = input.columns.edgeIds.map((_, index) => ({
    sourceKey: keys[input.columns.edgeSources[index]], targetKey: keys[input.columns.edgeTargets[index]],
    kind: input.columns.edgeKinds[index] === 1 ? "citation" : "link",
    count: input.columns.edgeCounts[index], occurrences: input.columns.edgeWeights[index],
  }));
  const scene = (): LocalLayoutInput => ({
    nodes: keys.map((key, index) => ({ key, isSeed: false, radiusPx: basis.radiusModel[index] })), edges,
  });
  let currentScene = scene();
  const cachedPositions = toRecord(input.positions);
  const fixedPositions = Object.fromEntries(input.pins.map(index => [keys[index], cachedPositions[keys[index]]]));
  const fail = (error: Error): void => {
    if (destroyed) return;
    destroyed = true;
    layout.destroy();
    try { options.onState({ ready: false, running: false, phase: "paused" }); }
    catch { /* A failed state consumer must not prevent the terminal error notification. */ }
    options.onError(error);
  };
  const hasBodyOverlap = (positions: Float32Array, fit: number): boolean => {
    const actualGlyphs = buildWorkspaceD3UnitBasis({ ...input, positions, viewport: basis.referenceViewport, referenceZoom: fit });
    return measureWorkspaceD3BodyOverlap(positions, actualGlyphs.nominalRadiusCss, fit, MATERIAL_OVERLAP_CSS, fixedIndices).overlappingNodes > 0;
  };
  const layout = createD3WorkerLayout(currentScene, {
    cachedPositions, fixedPositions, autoStart: false, reducedMotion,
    initialPositionsAreSolved: reused, initialTicks: INITIAL_SOLVE_TICKS,
    onIndexedTick(positions, workerKeys) {
      if (destroyed) return;
      try {
        if (catalog !== workerKeys) {
          catalog = workerKeys;
          const source = new Map(workerKeys.map((key, index) => [key, index]));
          indexedOrder = keys.map(key => source.get(key) ?? -1);
          if (indexedOrder.some(index => index < 0)) { fail(new Error("D3 Map received a different visible catalog")); return; }
          directOrder = indexedOrder.every((source, index) => source === index);
        }
        const ordered = directOrder ? positions : new Float32Array(positions.length);
        if (!directOrder) indexedOrder!.forEach((source, index) => {
          ordered[index * 2] = positions[source * 2]; ordered[index * 2 + 1] = positions[source * 2 + 1];
        });
        if (!acceptedSolved) {
          // A hidden/user-paused initial scene retains displayed rows. Resume
          // asks for another bounded Worker frame instead of copying phyllotaxis.
          if (explicitlyPaused) return;
          const fit = workspaceD3FitZoom(ordered, basis.referenceViewport);
          const coolingRemains = calibrationPasses === 0;
          // Glyphs keep their CSS size, so a dense scene may not separate at a
          // phone's Fit. Zooming in resolves that; it is never a Map failure.
          if (coolingRemains || calibrationPasses <= MAX_CONTACT_PASSES && hasBodyOverlap(ordered, fit)) {
            calibrationPasses++;
            basis = buildWorkspaceD3UnitBasis({ ...input, positions: ordered, viewport: basis.referenceViewport,
              referenceZoom: Math.max(.001, Math.min(basis.referenceZoom, fit) * FIT_CLEARANCE_SLACK),
              calibrationPasses });
            currentScene = scene();
            layout.update(currentScene, undefined, { settle: true, settleAtCurrentAlpha: true,
              settleTicks: coolingRemains ? REMAINING_SOLVE_TICKS : CONTACT_PASS_TICKS });
            return;
          }
          acceptedSolved = true;
        }
        if (!basisPublished) { basisPublished = true; options.onUnitBasis?.(basis); }
        // Forward even identical resumed rows: the host may have rejected a
        // previous publication while hidden. The host owns GPU-upload deduping.
        options.onPositions(ordered);
        if (requestedRunning && !reducedMotion) {
          const reheat = requestedReheat;
          requestedRunning = false; requestedReheat = false;
          layout.resume(reheat);
        }
      } catch (error) { fail(error instanceof Error ? error : new Error(String(error))); }
    },
    onState(state) {
      if (destroyed) return;
      try {
        options.onState(acceptedSolved ? state : { ready: false, running: false,
          phase: explicitlyPaused ? "paused" : "initial" });
      } catch (error) { fail(error instanceof Error ? error : new Error(String(error))); }
    },
    onError: fail,
  });
  return {
    start(reheat = false) {
      if (destroyed) return;
      const wasPaused = explicitlyPaused;
      explicitlyPaused = false;
      if (acceptedSolved) { layout.resume(reheat); return; }
      requestedRunning = true; requestedReheat ||= reheat;
      if (wasPaused) {
        // Explicit static frame works while the proxy suppresses old live
        // responses. It neither restores unpinned fallback rows nor reheats.
        layout.update(currentScene, undefined, { settle: true, settleTicks: 1, settleAtCurrentAlpha: true });
      }
    },
    stop() {
      if (destroyed) return;
      explicitlyPaused = true;
      requestedRunning = false; requestedReheat = false;
      layout.pause(!acceptedSolved);
    },
    destroy() { if (!destroyed) { destroyed = true; layout.destroy(); } },
    isRunning: () => !destroyed && acceptedSolved && layout.isRunning(),
    setReducedMotion(enabled) {
      if (destroyed || enabled === reducedMotion) return;
      reducedMotion = enabled;
      layout.setReducedMotion(enabled);
    },
    setPositions(positions, pins = [], reconcile = {}) {
      if (destroyed) return;
      fixedIndices = new Set(pins);
      layout.setPositions(toRecord(positions), pins.map(index => keys[index]),
        { pinsOnly: !acceptedSolved, reheat: acceptedSolved && reconcile.reheat !== false });
    },
  };
}
