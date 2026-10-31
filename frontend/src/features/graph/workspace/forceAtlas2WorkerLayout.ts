import { MultiGraph } from "graphology";
import FA2LayoutSupervisor from "graphology-layout-forceatlas2/worker";

import { createWorkspaceForceAtlas2CollisionWorker } from "./forceAtlas2CollisionWorker";

import {
  createWorkspaceForceAtlas2PhaseGate,
  snapshotWorkspaceForceAtlas2Progress,
  type WorkspaceForceAtlas2PhaseGate,
  type WorkspaceForceAtlas2Progress,
} from "./forceAtlas2Phases";
import {
  buildWorkspaceForceAtlas2ContactUnits,
  isReusableWorkspaceForceAtlas2ContactUnits,
  isReusableWorkspaceForceAtlas2Units,
  type WorkspaceForceAtlas2Units,
} from "./forceAtlas2Units";

export interface WorkspaceForceAtlas2WorkerInput {
  positions: Float32Array;
  /** Radii in the immutable scene's canonical units, not current camera pixels. */
  radii: Float32Array;
  sources: Uint32Array;
  targets: Uint32Array;
  weights: Float64Array;
  pins?: readonly number[];
  origin?: readonly [number, number];
  progress?: WorkspaceForceAtlas2Progress;
  units?: WorkspaceForceAtlas2Units;
  contactUnits?: WorkspaceForceAtlas2Units;
}

export interface WorkspaceForceAtlas2WorkerOptions {
  onPositions(positions: Float32Array): void;
  onError(error: Error): void;
  onProgress?(progress: WorkspaceForceAtlas2Progress): void;
  onContactUnits?(units: WorkspaceForceAtlas2Units): void;
}

export interface WorkspaceForceAtlas2WorkerLayout {
  start(): void;
  /** Fence old messages and retain the last frame actually published to the renderer. */
  stop(): void;
  /** Atomic reseed and pin replacement; a running layout restarts with a fresh matrix. */
  setPositions(positions: Float32Array, pins?: readonly number[]): void;
  destroy(): void;
  isRunning(): boolean;
  getProgress(): WorkspaceForceAtlas2Progress;
  getContactUnits(): WorkspaceForceAtlas2Units | undefined;
}

type NodeAttributes = { x: number; y: number; size: number; fixed: boolean };
type EdgeAttributes = { weight: number };
type Supervisor = FA2LayoutSupervisor<NodeAttributes, EdgeAttributes>;
// Pinned upstream 0.10.1 exposes these runtime members but omits them from its
// declarations. Own message delivery ordering without changing its algorithm.
type OwnedSupervisor = Supervisor & {
  worker: Worker;
  handleMessage(event: MessageEvent<unknown>): void;
};
type Run = {
  generation: number;
  supervisor: Supervisor;
  worker: Worker;
  listener: () => void;
  messageListener: (event: MessageEvent<unknown>) => void;
  errorListener: (event: Event) => void;
  gate: WorkspaceForceAtlas2PhaseGate;
  handlingMessage: boolean;
  transitionLatched: boolean;
  collisionWorker: ReturnType<typeof createWorkspaceForceAtlas2CollisionWorker> | null;
  collisionPending: boolean;
  frame: number | null;
  pending: { positions: Float32Array; progress: WorkspaceForceAtlas2Progress; transition: boolean } | null;
};

const MAX_NODES = 2000;
// One fixed browser profile; no sweep, inferSettings, or camera recalibration.
// Gephi Lite quickstart supplies scaling30, not a corpus-optimality claim.
// Contact prevention follows structural balance, as recommended by the FA2
// paper; the complete official Worker supplies both numerical stages. Model
// scale stays exactly 1 because log(1+d) is not scale-covariant.
const SETTINGS = Object.freeze({ linLogMode: true, scalingRatio: 30, gravity: 1,
  strongGravityMode: false, outboundAttractionDistribution: false,
  edgeWeightInfluence: 1, slowDown: 1, barnesHutOptimize: false });

function finiteFloat32(value: number): boolean {
  return Number.isFinite(value) && Number.isFinite(Math.fround(value));
}

function snapshotPositions(positions: Float32Array, count: number, origin: readonly number[]): Float32Array {
  if (!(positions instanceof Float32Array) || positions.length !== count * 2
    || !positions.every((value, index) => finiteFloat32(value)
      && finiteFloat32(value - origin[index % 2]))) {
    throw new Error("ForceAtlas2 Worker requires finite canonical Float32 position rows");
  }
  return positions.slice();
}

function snapshotPins(pins: readonly number[], count: number): ReadonlySet<number> {
  if (!Array.isArray(pins) || !pins.every(index => Number.isInteger(index) && index >= 0 && index < count)) {
    throw new Error("ForceAtlas2 Worker pin index is outside the original node rows");
  }
  return new Set(pins);
}

/**
 * Owns one visible, immutable 2K graph and the complete upstream CPU Worker.
 * No topology is regrouped or omitted: parallel original relation rows and
 * loops retain independent keys/weights. Folder membership is never an input.
 * Pausing/reseeding kills the supervisor, rather than reusing its outstanding
 * transferable buffer or stale fixed-node flags. Each new run starts at the
 * last published canonical coordinates with a fresh upstream adaptive history.
 */
export function createWorkspaceForceAtlas2WorkerLayout(
  input: WorkspaceForceAtlas2WorkerInput,
  options: WorkspaceForceAtlas2WorkerOptions,
): WorkspaceForceAtlas2WorkerLayout {
  if (!(input.radii instanceof Float32Array) || !(input.sources instanceof Uint32Array)
    || !(input.targets instanceof Uint32Array) || !(input.weights instanceof Float64Array)
    || input.radii.length < 1 || input.radii.length > MAX_NODES
    || input.sources.length !== input.weights.length || input.targets.length !== input.weights.length
    || !input.radii.every(value => finiteFloat32(value) && value > 0)) {
    throw new Error("ForceAtlas2 Worker requires valid indexed columns for 1–2000 original nodes");
  }
  const count = input.radii.length;
  const units = input.units;
  if (units !== undefined && !isReusableWorkspaceForceAtlas2Units(units, count)) {
    throw new Error("ForceAtlas2 Worker requires valid immutable seed units");
  }
  const origin = [...(input.origin ?? units?.origin ?? [0, 0])];
  if (origin.length !== 2 || !origin.every(finiteFloat32)) throw new Error("Finite ForceAtlas2 Worker origin required");
  let positions = snapshotPositions(input.positions, count, origin);
  let pins = snapshotPins(input.pins ?? [], count);
  let progress = snapshotWorkspaceForceAtlas2Progress(input.progress);
  const radii = input.radii.slice(), sources = input.sources.slice(), targets = input.targets.slice();
  if (units && (!origin.every((value, index) => value === units.origin[index])
    || !radii.every((value, index) => value === units.collisionRadiusModel[index]))) {
    throw new Error("ForceAtlas2 Worker seed units disagree with indexed physics rows");
  }
  let contactUnits = input.contactUnits;
  if (contactUnits !== undefined && (!units || progress.phase !== "contact"
    || !isReusableWorkspaceForceAtlas2ContactUnits(contactUnits, units))) {
    throw new Error("ForceAtlas2 Worker contact units disagree with the published seed/phase");
  }
  if (units && progress.phase === "contact" && !contactUnits) {
    throw new Error("Published contact progress requires its one-time contact units");
  }
  const masses = new Float32Array(count).fill(1);
  for (let edge = 0; edge < input.weights.length; edge++) {
    const source = input.sources[edge], target = input.targets[edge], weight = input.weights[edge];
    if (source >= count || target >= count || !finiteFloat32(weight) || Math.fround(weight) <= 0) {
      throw new Error("ForceAtlas2 Worker requires original endpoints and positive finite Float32 edge weights");
    }
    masses[source] += weight; masses[target] += weight;
    if (!Number.isFinite(masses[source]) || !Number.isFinite(masses[target])) {
      throw new Error("ForceAtlas2 Worker weighted mass exceeds Float32 range");
    }
  }
  const graph = new MultiGraph<NodeAttributes, EdgeAttributes>({ allowSelfLoops: true });
  for (let node = 0; node < count; node++) graph.addNode(String(node), {
    x: Math.fround(positions[node * 2] - origin[0]),
    y: Math.fround(positions[node * 2 + 1] - origin[1]),
    size: input.radii[node], fixed: pins.has(node),
  });
  for (let edge = 0; edge < input.weights.length; edge++) {
    graph.addUndirectedEdgeWithKey(String(edge), String(input.sources[edge]), String(input.targets[edge]), {
      weight: input.weights[edge],
    });
  }
  let generation = 0;
  let updatingGeneration = 0;
  let run: Run | null = null;
  let destroyed = false;

  const restorePublishedState = () => {
    for (let node = 0; node < count; node++) graph.mergeNodeAttributes(String(node), {
      x: Math.fround(positions[node * 2] - origin[0]),
      y: Math.fround(positions[node * 2 + 1] - origin[1]), fixed: pins.has(node),
      size: contactUnits?.collisionRadiusModel[node] ?? radii[node],
    });
  };
  const isCurrent = (token: number) => !destroyed && run?.generation === token && generation === token;
  const invalidate = () => {
    generation++;
    const old = run;
    run = null;
    if (!old) return;
    if (old.frame !== null) cancelAnimationFrame(old.frame);
    old.pending = null;
    graph.removeListener("eachNodeAttributesUpdated", old.listener);
    old.worker.removeEventListener("message", old.messageListener);
    old.worker.removeEventListener("error", old.errorListener);
    old.worker.removeEventListener("messageerror", old.errorListener);
    old.collisionWorker?.destroy();
    old.supervisor.kill();
  };
  const fail = (token: number, error: Error) => {
    if (!isCurrent(token)) return;
    invalidate();
    restorePublishedState();
    options.onError(error);
  };

  function start(): void {
    if (destroyed || run) return;
    restorePublishedState();
    const token = ++generation;
    let fault: Error | null = null;
    let supervisor: Supervisor;
    let collisionWorker: ReturnType<typeof createWorkspaceForceAtlas2CollisionWorker> | null = null;
    const gate = createWorkspaceForceAtlas2PhaseGate({ positions, radii, sources, targets, pins: [...pins], progress });
    try {
      if (contactUnits) collisionWorker = createWorkspaceForceAtlas2CollisionWorker({
        radii: Float32Array.from(contactUnits.collisionRadiusModel), pins: [...pins],
      });
      supervisor = new FA2LayoutSupervisor<NodeAttributes, EdgeAttributes>(graph, {
        settings: { ...SETTINGS, adjustSizes: progress.phase === "contact" },
        outputReducer(key: string, attributes: NodeAttributes): NodeAttributes {
          updatingGeneration = token;
          const node = Number(key);
          // A killed generation cannot import any returned coordinates.
          if (!isCurrent(token) || !finiteFloat32(attributes.x) || !finiteFloat32(attributes.y)
            || !finiteFloat32(attributes.x + origin[0]) || !finiteFloat32(attributes.y + origin[1])) {
            if (isCurrent(token)) fault ??= new Error("ForceAtlas2 Worker returned invalid coordinate rows");
            return { ...attributes, x: Math.fround(positions[node * 2] - origin[0]),
              y: Math.fround(positions[node * 2 + 1] - origin[1]), fixed: pins.has(node) };
          }
          return pins.has(node) ? { ...attributes, x: Math.fround(positions[node * 2] - origin[0]),
            y: Math.fround(positions[node * 2 + 1] - origin[1]), fixed: true } : attributes;
        },
      });
    } catch (error) {
      // Upstream attaches graph listeners before allocating its Blob Worker.
      // This graph is exclusively ours, so discard partial-constructor hooks.
      collisionWorker?.destroy();
      graph.removeAllListeners();
      options.onError(error instanceof Error ? error : new Error(String(error)));
      return;
    }
    const owned = supervisor as OwnedSupervisor;
    const worker = owned.worker;
    const messageListener = (event: MessageEvent<unknown>) => {
      if (!isCurrent(token) || run!.transitionLatched || run!.collisionPending) { event.stopImmediatePropagation(); return; }
      const data = event.data;
      const nodes = data && typeof data === "object" ? (data as { nodes?: unknown }).nodes : undefined;
      // Pinned upstream 0.10.1 uses ten Float32 attributes per original node.
      // The original handler immediately transfers this buffer back, detaching
      // it. Worker EventTarget does not give capture observers priority: validate
      // here BEFORE delegating, not in a separately registered capture guard.
      const expectedBytes = count * 10 * Float32Array.BYTES_PER_ELEMENT;
      const actualBytes = nodes instanceof ArrayBuffer ? nodes.byteLength : null;
      const invalidIndex = nodes instanceof ArrayBuffer && actualBytes === expectedBytes
        ? new Float32Array(nodes).findIndex(value => !Number.isFinite(value)) : -1;
      if (actualBytes !== expectedBytes || invalidIndex !== -1) {
        event.stopImmediatePropagation();
        queueMicrotask(() => fail(token, new Error("ForceAtlas2 Worker returned an invalid node matrix"
          + ` (type=${Object.prototype.toString.call(nodes)}, bytes=${actualBytes}, expected=${expectedBytes}, nonfinite=${invalidIndex})`)));
        return;
      }
      const active = run!;
      const delegate = () => {
        if (!isCurrent(token) || active.transitionLatched) return;
        active.handlingMessage = true;
        try { owned.handleMessage(event); }
        catch (error) { queueMicrotask(() => fail(token, error instanceof Error ? error : new Error(String(error)))); }
        finally { active.handlingMessage = false; }
      };
      if (!active.collisionWorker) { delegate(); return; }
      // Own this complete official buffer while the mature collision constraint
      // runs off-main. No next FA2 request is made until the SAME buffer returns
      // through upstream handleMessage. Adaptive force/history columns remain
      // untouched; only x/y are projected, never topology or per-frame reseeds.
      const matrix = new Float32Array(nodes as ArrayBuffer);
      const modelRows = new Float32Array(count * 2);
      for (let node = 0; node < count; node++) {
        modelRows[node * 2] = pins.has(node) ? Math.fround(positions[node * 2] - origin[0]) : matrix[node * 10];
        modelRows[node * 2 + 1] = pins.has(node) ? Math.fround(positions[node * 2 + 1] - origin[1]) : matrix[node * 10 + 1];
      }
      active.collisionPending = true;
      let projection: Promise<Float32Array>;
      try { projection = active.collisionWorker.project(modelRows); }
      catch (error) { queueMicrotask(() => fail(token, error instanceof Error ? error : new Error(String(error)))); return; }
      void projection.then(projected => {
        if (!isCurrent(token) || active.transitionLatched) return;
        if (!(projected instanceof Float32Array) || projected.length !== count * 2
          || !projected.every((value, index) => finiteFloat32(value) && finiteFloat32(value + origin[index % 2]))
          || matrix.byteLength !== expectedBytes) {
          throw new Error("Collision Worker returned invalid model position rows");
        }
        for (let node = 0; node < count; node++) {
          matrix[node * 10] = pins.has(node) ? Math.fround(positions[node * 2] - origin[0]) : projected[node * 2];
          matrix[node * 10 + 1] = pins.has(node) ? Math.fround(positions[node * 2 + 1] - origin[1]) : projected[node * 2 + 1];
        }
        active.collisionPending = false;
        delegate();
      }).catch(error => fail(token, error instanceof Error ? error : new Error(String(error))));
    };
    const errorListener = (event: Event) => {
      event.preventDefault();
      const error = event instanceof ErrorEvent && event.message ? new Error(event.message)
        : new Error("ForceAtlas2 Worker failed to deliver a layout frame");
      // Defer teardown until the upstream message handler has returned.
      queueMicrotask(() => fail(token, error));
    };
    const listener = () => {
      if (!isCurrent(token) || updatingGeneration !== token || !run!.handlingMessage || run!.transitionLatched) return;
      if (fault) {
        const error = fault;
        queueMicrotask(() => fail(token, error));
        return;
      }
      const active = run!;
      const next = new Float32Array(count * 2);
      for (let node = 0; node < count; node++) {
        const attributes = graph.getNodeAttributes(String(node));
        next[node * 2] = pins.has(node) ? positions[node * 2] : attributes.x + origin[0];
        next[node * 2 + 1] = pins.has(node) ? positions[node * 2 + 1] : attributes.y + origin[1];
      }
      let nextProgress: WorkspaceForceAtlas2Progress;
      try { nextProgress = active.gate.accept(next); }
      catch (error) { queueMicrotask(() => fail(token, error instanceof Error ? error : new Error(String(error)))); return; }
      const transition = progress.phase === "structure" && nextProgress.phase === "contact";
      active.pending = { positions: next, progress: nextProgress, transition };
      // Freeze this exact eligible frame. Upstream reposts immediately after its
      // graph update; reject its next returned matrix BEFORE delegating, until
      // this frame is published or invalidated by Pause/reseed/destruction.
      active.transitionLatched = transition;
      if (active.frame !== null) return;
      active.frame = requestAnimationFrame(() => {
        if (!isCurrent(token)) return;
        active.frame = null;
        const pending = active.pending;
        active.pending = null;
        if (!pending) return;
        let nextContactUnits: WorkspaceForceAtlas2Units | undefined;
        if (pending.transition && units) {
          try { nextContactUnits = buildWorkspaceForceAtlas2ContactUnits(units, pending.positions); }
          catch (error) { fail(token, error instanceof Error ? error : new Error(String(error))); return; }
        }
        positions = pending.positions;
        progress = pending.progress;
        if (nextContactUnits) contactUnits = nextContactUnits;
        try { options.onPositions(positions.slice()); }
        catch (error) { fail(token, error instanceof Error ? error : new Error(String(error))); return; }
        // Callbacks may synchronously Pause, reseed, or destroy this layout.
        if (!isCurrent(token)) return;
        if (nextContactUnits) {
          try { options.onContactUnits?.(nextContactUnits); }
          catch (error) { fail(token, error instanceof Error ? error : new Error(String(error))); return; }
          if (!isCurrent(token)) return;
        }
        try { options.onProgress?.(progress); }
        catch (error) { fail(token, error instanceof Error ? error : new Error(String(error))); return; }
        if (!isCurrent(token) || !pending.transition) return;
        // Fresh contact Worker at published rows, with its one-time scene radii.
        invalidate();
        start();
      });
    };
    run = { generation: token, supervisor, worker, listener, messageListener, errorListener,
      gate, handlingMessage: false, transitionLatched: false, collisionWorker,
      collisionPending: false, frame: null, pending: null };
    graph.on("eachNodeAttributesUpdated", listener);
    // One guarded delegate makes transfer ownership explicit. The supervisor's
    // bound handler still applies its complete upstream iteration and reposts
    // the same buffer using its unchanged protocol; no numerical code is copied.
    worker.removeEventListener("message", owned.handleMessage);
    worker.addEventListener("message", messageListener);
    worker.addEventListener("error", errorListener);
    worker.addEventListener("messageerror", errorListener);
    try { supervisor.start(); }
    catch (error) { fail(token, error instanceof Error ? error : new Error(String(error))); }
  }

  return {
    start,
    stop() {
      invalidate();
      if (!destroyed) restorePublishedState();
    },
    setPositions(next, nextPins = []) {
      if (destroyed) return;
      // Invalid replacements must leave the live generation intact.
      const snapshot = snapshotPositions(next, count, origin);
      const replacementPins = snapshotPins(nextPins, count);
      const wasRunning = run !== null;
      invalidate();
      positions = snapshot;
      pins = replacementPins;
      restorePublishedState();
      if (wasRunning) start();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      invalidate();
      graph.removeAllListeners();
    },
    isRunning: () => !destroyed && run !== null,
    getProgress: () => progress,
    getContactUnits: () => contactUnits,
  };
}
