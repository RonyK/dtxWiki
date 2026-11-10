import type { GraphPoint } from "../rendererTypes";
import type { D3LayoutState, LocalLayout, LocalLayoutOptions } from "./d3Layout";
import type { LocalLayoutInput, LocalLayoutPositions } from "./layoutTypes";
import { d3PhyllotaxisPositions } from "./initialPositions";
import type { D3WorkerCommand, D3WorkerResponse } from "./d3WorkerProtocol";
import { packD3Positions, unpackD3Positions } from "./d3WorkerProtocol";

export type D3WorkerLayoutState = D3LayoutState;
export type D3WorkerLayoutOptions = LocalLayoutOptions & {
  onError?: (error: Error) => void;
  workerFactory?: () => Worker;
};
function finite(point: GraphPoint | undefined): point is GraphPoint {
  return !!point && Number.isFinite(Math.fround(point.x)) && Number.isFinite(Math.fround(point.y));
}
function initialRows(input: LocalLayoutInput, cached?: LocalLayoutPositions | null, fixed?: LocalLayoutPositions | null) {
  const nodeKeys = new Set<string>();
  const seeds = new Set<string>();
  for (const node of input.nodes) {
    if (typeof node.key !== "string" || !node.key) continue;
    nodeKeys.add(node.key); if (node.isSeed) seeds.add(node.key);
  }
  const keys = Object.freeze([...nodeKeys].sort());
  const seed = keys.find(key => seeds.has(key));
  const positions = d3PhyllotaxisPositions(keys.length);
  keys.forEach((key, index) => {
    const saved = finite(fixed?.[key]) ? fixed![key] : cached?.[key];
    if (key === seed) { positions[index * 2] = 0; positions[index * 2 + 1] = 0; }
    else if (finite(saved)) { positions[index * 2] = saved.x; positions[index * 2 + 1] = saved.y; }
  });
  return { keys, seed, positions };
}
/** Mirrors initialization without importing or running D3 on the UI thread. */
export function createInitialD3Positions(input: LocalLayoutInput, cached?: LocalLayoutPositions | null,
  fixed?: LocalLayoutPositions | null): LocalLayoutPositions {
  return unpackD3Positions(initialRows(input, cached, fixed));
}

/** Indexed frames are authoritative; the legacy object API materializes only on demand. */
export function createD3WorkerLayout(input: LocalLayoutInput, options: D3WorkerLayoutOptions = {}): LocalLayout {
  let { keys, seed, positions } = initialRows(input, options.cachedPositions, options.fixedPositions);
  let byKey = new Map(keys.map((key, index) => [key, index]));
  let sequence = 0, topology = 1;
  let catalogReady = false, published = false, destroyed = false, suppressed = false;
  let pendingStaticFrame = 0;
  let dragging: string | null = null;
  const pins = new Set(Object.keys(options.fixedPositions ?? {}));
  if (seed) pins.add(seed);
  const arrivals = new Map<string, number>();
  let state: D3LayoutState = { ready: false, running: false, phase: "initial" };
  const worker = options.workerFactory?.()
    ?? new Worker(new URL("./d3Layout.worker.ts", import.meta.url), { type: "module", name: "shared-d3-layout" });
  const getPositions = () => unpackD3Positions({ keys, positions });
  const send = (command: D3WorkerCommand): void => {
    if (destroyed) return;
    const transfer: Transferable[] = [];
    const pack = (points: LocalLayoutPositions) => {
      const visible = Object.fromEntries(Object.entries(points).filter(([key, point]) => byKey.has(key) && finite(point)));
      const rows = packD3Positions(visible); transfer.push(rows.positions.buffer); return rows;
    };
    let wire;
    switch (command.type) {
      case "init": wire = { ...command, options: { ...command.options,
        cachedPositions: command.options.cachedPositions ? pack(command.options.cachedPositions) : undefined,
        fixedPositions: command.options.fixedPositions ? pack(command.options.fixedPositions) : undefined } }; break;
      case "update": wire = { ...command, fixedPositions: command.fixedPositions ? pack(command.fixedPositions) : undefined }; break;
      case "setPositions": wire = { ...command, positions: pack(command.positions) }; break;
      case "pause": wire = { ...command, positions: command.positions ? pack(command.positions) : undefined }; break;
      default: wire = command;
    }
    worker.postMessage({ ...wire, sequence: ++sequence, topology }, transfer);
  };
  const fail = (error: Error): void => {
    if (destroyed) return;
    destroyed = true; arrivals.clear();
    state = { ...state, running: false, phase: "paused" };
    worker.removeEventListener("message", onMessage);
    worker.removeEventListener("error", onError); worker.removeEventListener("messageerror", onError);
    worker.terminate(); options.onState?.({ ...state }); options.onError?.(error);
  };
  const onMessage = (event: MessageEvent<D3WorkerResponse>): void => {
    const response = event.data;
    if (destroyed) return;
    // A fault belongs to this live Worker, not to its latest coordinate command.
    if (response.type === "error") { fail(new Error(response.message)); return; }
    if (response.type === "frame") worker.postMessage({ type: "ack" });
    if (response.topology !== topology) return;
    if (response.type === "catalog") {
      if (response.keys.length !== keys.length || response.keys.some((key, index) => key !== keys[index])) {
        fail(new Error("D3 Worker returned a different visible node catalog")); return;
      }
      catalogReady = true; return;
    }
    if (response.type === "settledPin") {
      const requested = arrivals.get(response.key);
      if (requested !== undefined && response.sequence >= requested && byKey.has(response.key) && finite(response.position)) {
        arrivals.delete(response.key); pins.add(response.key); options.onSettledPin?.(response.key, { ...response.position });
      }
      return;
    }
    if (response.sequence !== sequence) return;
    if (!catalogReady || !(response.positions instanceof Float32Array) || response.positions.length !== keys.length * 2
      || !response.positions.every(Number.isFinite)) {
      fail(new Error("D3 Worker returned invalid visible coordinate rows")); return;
    }
    const initial = !published && response.state.ready;
    const staticSettle = pendingStaticFrame !== 0 && response.sequence >= pendingStaticFrame;
    state = { ...response.state };
    if (!suppressed || initial || staticSettle) {
      positions = response.positions; published = true; pendingStaticFrame = 0;
      if (options.onIndexedTick) options.onIndexedTick(positions, keys);
      if (!destroyed && options.onTick) options.onTick(getPositions());
    }
    if (!destroyed && response.sequence === sequence) options.onState?.({ ...state });
  };
  const onError = (event: Event): void => {
    event.preventDefault();
    fail(new Error(event instanceof ErrorEvent ? event.message : "D3 Worker failed to deliver a layout frame"));
  };
  worker.addEventListener("message", onMessage); worker.addEventListener("error", onError); worker.addEventListener("messageerror", onError);
  send({ type: "init", input, options: { cachedPositions: options.cachedPositions, fixedPositions: options.fixedPositions,
    reducedMotion: options.reducedMotion, autoStart: options.autoStart, initialPositionsAreSolved: options.initialPositionsAreSolved } });
  const localPoint = (key: string, point?: GraphPoint): GraphPoint | null => {
    const index = byKey.get(key);
    if (destroyed || index === undefined || key !== seed && point !== undefined && !finite(point)) return null;
    const next = key === seed ? { x: 0, y: 0 } : point ?? { x: positions[index * 2], y: positions[index * 2 + 1] };
    positions = positions.slice(); positions[index * 2] = next.x; positions[index * 2 + 1] = next.y;
    return { ...next };
  };
  return {
    getPositions, getKeys: () => keys, getIndexedPositions: () => positions.slice(),
    getState: () => ({ ...state }), isRunning: () => !destroyed && state.running,
    update(next, fixedPositions, updateOptions = {}) {
      if (destroyed) return;
      const retained = getPositions();
      const nextRows = initialRows(next, retained);
      keys = nextRows.keys; seed = nextRows.seed; positions = nextRows.positions;
      byKey = new Map(keys.map((key, index) => [key, index]));
      for (const key of keys) if (!Object.hasOwn(retained, key) && finite(fixedPositions?.[key])) localPoint(key, fixedPositions![key]);
      for (const key of arrivals.keys()) if (!byKey.has(key)) arrivals.delete(key);
      for (const key of pins) if (!byKey.has(key)) pins.delete(key);
      topology++; catalogReady = false;
      if (updateOptions.settle || updateOptions.settleAtCurrentAlpha) pendingStaticFrame = sequence + 1;
      send({ type: "update", input: next, fixedPositions, ...updateOptions });
    },
    setPositions(next, nextPins, reconcile = {}) {
      if (destroyed) return;
      const pinsOnly = reconcile.pinsOnly ?? !published;
      const desired = nextPins === undefined ? new Set(pins) : new Set(nextPins);
      positions = positions.slice();
      keys.forEach((key, index) => {
        if ((!pinsOnly || desired.has(key)) && finite(next[key])) { positions[index * 2] = next[key].x; positions[index * 2 + 1] = next[key].y; }
      });
      if (seed) { const index = byKey.get(seed)!; positions[index * 2] = positions[index * 2 + 1] = 0; desired.add(seed); }
      pins.clear(); desired.forEach(key => pins.add(key));
      send({ type: "setPositions", positions: next, pins: nextPins,
        options: { ...reconcile, pinsOnly, reheat: reconcile.reheat ?? published } });
    },
    pause() {
      if (destroyed) return;
      suppressed = true; state = { ...state, running: false, phase: state.ready ? "paused" : "initial" };
      // Provisional phyllotaxis must never travel back over the Worker's cold solve.
      if (published) {
        const rows = positions.slice();
        worker.postMessage({ type: "pause", sequence: ++sequence, topology, positions: { keys, positions: rows } }, [rows.buffer]);
      } else send({ type: "pause" });
      options.onState?.({ ...state });
    },
    resume(reheat = false) { if (!destroyed) { suppressed = false; send({ type: "resume", reheat }); } },
    pin(key, position, reheat = published) {
      if (key !== seed && position !== undefined && !finite(position)) return null;
      const point = localPoint(key, key === dragging ? undefined : position);
      if (point) { arrivals.delete(key); pins.add(key); send({ type: "pin", key, position: point, reheat }); } return point;
    },
    pinWhenSettled(key) {
      if (destroyed || arrivals.has(key) || !byKey.has(key)) return;
      arrivals.set(key, sequence + 1); send({ type: "pinWhenSettled", key });
    },
    unpin(key, reheat = published) { arrivals.delete(key); pins.delete(key); send({ type: "unpin", key, reheat }); },
    clearPins(reheat = published) { arrivals.clear(); pins.clear(); if (seed) pins.add(seed); send({ type: "clearPins", reheat }); },
    beginDrag(key, position) {
      const point = localPoint(key, position); if (point) { if (key !== seed) dragging = key; send({ type: "beginDrag", key, position: point }); } return point;
    },
    moveDrag(key, position) { if (dragging !== key) return null; const point = localPoint(key, position); if (point) send({ type: "moveDrag", key, position: point }); return point; },
    endDrag(key) { if (dragging === key) { dragging = null; send({ type: "endDrag", key }); } },
    setReducedMotion(enabled) { send({ type: "setReducedMotion", enabled }); },
    destroy() {
      if (destroyed) return;
      send({ type: "destroy" }); destroyed = true; arrivals.clear();
      worker.removeEventListener("message", onMessage); worker.removeEventListener("error", onError); worker.removeEventListener("messageerror", onError); worker.terminate();
    },
  };
}
