/** Ephemeral calculation state. Never a saved graph, camera or authority token. */
export interface WorkspaceForceAtlas2Progress {
  readonly profile: "gephi-lite-2k-v1";
  readonly phase: "structure" | "contact";
  readonly iterations: number;
  readonly structureIterations: number;
  readonly transitionReason: "stable" | "budget" | null;
}

const PROFILE = "gephi-lite-2k-v1";
const CHECKPOINT_INTERVAL = 8;
const MIN_STRUCTURE_ITERATIONS = 32;
const MAX_STRUCTURE_ITERATIONS = 256;
const STABLE_INTERVALS = 4;
const MAX_SCENE_SAMPLE = 256;
const MAX_EDGE_SAMPLE = 512;

/** Validate both the profile and reachable phase/counter combinations on remount. */
export function isWorkspaceForceAtlas2Progress(value: unknown): value is WorkspaceForceAtlas2Progress {
  if (!value || typeof value !== "object") return false;
  const progress = value as Partial<WorkspaceForceAtlas2Progress>;
  if (progress.profile !== PROFILE || !Number.isSafeInteger(progress.iterations)
    || !Number.isSafeInteger(progress.structureIterations)) return false;
  const iterations = progress.iterations!, structure = progress.structureIterations!;
  if (iterations < 0 || structure < 0 || structure > iterations) return false;
  if (progress.phase === "structure") {
    return progress.transitionReason === null && structure === iterations && structure < MAX_STRUCTURE_ITERATIONS;
  }
  if (progress.phase !== "contact" || structure < MIN_STRUCTURE_ITERATIONS || structure > MAX_STRUCTURE_ITERATIONS) {
    return false;
  }
  return progress.transitionReason === "stable"
    || (progress.transitionReason === "budget" && structure === MAX_STRUCTURE_ITERATIONS);
}

export function snapshotWorkspaceForceAtlas2Progress(
  value?: WorkspaceForceAtlas2Progress,
): WorkspaceForceAtlas2Progress {
  if (value === undefined) return Object.freeze({ profile: PROFILE, phase: "structure",
    iterations: 0, structureIterations: 0, transitionReason: null });
  if (!isWorkspaceForceAtlas2Progress(value)) throw new Error("Invalid ephemeral ForceAtlas2 phase progress");
  return Object.freeze({ profile: value.profile, phase: value.phase, iterations: value.iterations,
    structureIterations: value.structureIterations, transitionReason: value.transitionReason });
}

export interface WorkspaceForceAtlas2PhaseInput {
  positions: Float32Array;
  radii: Float32Array;
  sources: Uint32Array;
  targets: Uint32Array;
  pins?: readonly number[];
  progress?: WorkspaceForceAtlas2Progress;
}

export interface WorkspaceForceAtlas2PhaseGate {
  /** One accepted, complete official Worker update, never an RAF or elapsed time. */
  accept(positions: Float32Array): WorkspaceForceAtlas2Progress;
}

function median(values: number[]): number {
  if (!values.length) return 0;
  values.sort((a, b) => a - b);
  const middle = Math.floor(values.length / 2);
  return values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

/** Deterministic row-uniform samples, preserving original occurrence ordering. */
function rowSample(count: number, maximum: number): number[] {
  const size = Math.min(count, maximum);
  if (size <= 1) return size ? [0] : [];
  return Array.from({ length: size }, (_, index) => Math.floor(index * (count - 1) / (size - 1)));
}

function edgeSample(sources: Uint32Array, targets: Uint32Array): number[] {
  let eligible = 0;
  for (let edge = 0; edge < sources.length; edge++) if (sources[edge] !== targets[edge]) eligible++;
  const ordinals = rowSample(eligible, MAX_EDGE_SAMPLE);
  const rows: number[] = [];
  let ordinal = 0, sample = 0;
  for (let edge = 0; edge < sources.length && sample < ordinals.length; edge++) {
    if (sources[edge] === targets[edge]) continue;
    if (ordinal === ordinals[sample]) { rows.push(edge); sample++; }
    ordinal++;
  }
  return rows;
}

function snapshotRows(positions: Float32Array, count: number): Float32Array {
  if (!(positions instanceof Float32Array) || positions.length !== count * 2
    || !positions.every(Number.isFinite)) throw new Error("Invalid ForceAtlas2 phase observation rows");
  return positions.slice();
}

/**
 * Pure, renderer-independent handoff policy, not a mathematical convergence
 * certificate. Every eight complete updates, all unpinned rows contribute to
 * RMS displacement O(N). Scene R95 and original-edge median are APPROXIMATE:
 * deterministic samples cap their sorts at 256 nodes and 512 nonself edge
 * occurrences, independent of corpus size. Original topology is never changed.
 * The immutable radius median is sorted once at construction, not each update.
 */
export function createWorkspaceForceAtlas2PhaseGate(input: WorkspaceForceAtlas2PhaseInput): WorkspaceForceAtlas2PhaseGate {
  const count = input.radii.length;
  if (!(input.radii instanceof Float32Array) || count < 1 || count > 2000
    || !input.radii.every(radius => Number.isFinite(radius) && radius > 0)
    || !(input.sources instanceof Uint32Array) || !(input.targets instanceof Uint32Array)
    || input.sources.length !== input.targets.length
    || !input.sources.every(source => source < count) || !input.targets.every(target => target < count)
    || (input.pins !== undefined && (!Array.isArray(input.pins)
      || !input.pins.every(pin => Number.isInteger(pin) && pin >= 0 && pin < count)))) {
    throw new Error("Invalid immutable ForceAtlas2 phase columns");
  }
  let progress = snapshotWorkspaceForceAtlas2Progress(input.progress);
  let previous = snapshotRows(input.positions, count);
  const pins = new Set(input.pins ?? []);
  const nodes = rowSample(count, MAX_SCENE_SAMPLE);
  // Snapshot endpoints only for sampled original occurrences, not graph topology.
  const sampledEdges = edgeSample(input.sources, input.targets).map(edge => [input.sources[edge], input.targets[edge]] as const);
  const radius = median(Array.from(input.radii));
  const movementTolerance = radius / 4;
  const distributionFloor = radius * 2;

  const distributions = (positions: Float32Array) => {
    let x = 0, y = 0;
    for (const node of nodes) { x += positions[node * 2]; y += positions[node * 2 + 1]; }
    x /= nodes.length; y /= nodes.length;
    const distances = nodes.map(node => Math.hypot(positions[node * 2] - x, positions[node * 2 + 1] - y));
    distances.sort((a, b) => a - b);
    const r95 = distances[Math.floor((distances.length - 1) * .95)];
    const edgeMedian = median(sampledEdges.map(([source, target]) => Math.hypot(
      positions[source * 2] - positions[target * 2], positions[source * 2 + 1] - positions[target * 2 + 1],
    )));
    return { r95, edgeMedian };
  };
  let previousDistributions = distributions(previous);
  let observed = 0, stableIntervals = 0;
  const stableDistribution = (before: number, after: number) => Math.abs(after - before)
    <= .01 * Math.max(Math.abs(before), distributionFloor);

  return {
    accept(positions) {
      const rows = snapshotRows(positions, count);
      if (progress.iterations === Number.MAX_SAFE_INTEGER) throw new Error("ForceAtlas2 iteration counter exhausted");
      const iterations = progress.iterations + 1;
      if (progress.phase === "contact") {
        progress = Object.freeze({ ...progress, iterations });
        return progress;
      }
      const structureIterations = progress.structureIterations + 1;
      observed++;
      let transitionReason: WorkspaceForceAtlas2Progress["transitionReason"] = null;
      if (observed % CHECKPOINT_INTERVAL === 0) {
        let squaredMovement = 0, moving = 0;
        for (let node = 0; node < count; node++) {
          if (pins.has(node)) continue;
          const dx = rows[node * 2] - previous[node * 2], dy = rows[node * 2 + 1] - previous[node * 2 + 1];
          squaredMovement += dx * dx + dy * dy; moving++;
        }
        const rms = moving ? Math.sqrt(squaredMovement / moving) : 0;
        const nextDistributions = distributions(rows);
        const stable = rms <= movementTolerance
          && stableDistribution(previousDistributions.r95, nextDistributions.r95)
          && stableDistribution(previousDistributions.edgeMedian, nextDistributions.edgeMedian);
        stableIntervals = stable ? stableIntervals + 1 : 0;
        previous = rows;
        previousDistributions = nextDistributions;
        if (structureIterations >= MIN_STRUCTURE_ITERATIONS && stableIntervals >= STABLE_INTERVALS) transitionReason = "stable";
      }
      if (transitionReason === null && structureIterations >= MAX_STRUCTURE_ITERATIONS) transitionReason = "budget";
      progress = Object.freeze({ profile: PROFILE, phase: transitionReason ? "contact" : "structure",
        iterations, structureIterations, transitionReason });
      return progress;
    },
  };
}
