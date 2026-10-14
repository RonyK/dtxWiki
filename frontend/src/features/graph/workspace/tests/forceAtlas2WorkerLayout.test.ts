import type { MultiGraph } from "graphology";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createWorkspaceForceAtlas2WorkerLayout,
  type WorkspaceForceAtlas2WorkerInput,
  type WorkspaceForceAtlas2WorkerLayout,
} from "../forceAtlas2WorkerLayout";
import { buildWorkspaceForceAtlas2ContactUnits, buildWorkspaceForceAtlas2Units, type WorkspaceForceAtlas2Units } from "../forceAtlas2Units";

type NodeAttributes = { x: number; y: number; size: number; fixed: boolean };
type FakeSupervisor = {
  graph: MultiGraph<NodeAttributes, { weight: number }>;
  params: { settings: Record<string, unknown>; outputReducer(key: string, attributes: NodeAttributes): NodeAttributes };
  worker: EventTarget & { terminate: ReturnType<typeof vi.fn> };
  upstreamMessage: ReturnType<typeof vi.fn>;
  handleMessage: ReturnType<typeof vi.fn>;
  started: boolean;
  killed: boolean;
  startRows: NodeAttributes[];
  emit(positions: readonly number[]): void;
};

const fake = vi.hoisted(() => ({
  instances: [] as FakeSupervisor[],
  constructorError: null as Error | null,
  startError: null as Error | null,
}));

type CollisionRequest = { positions: Float32Array; resolve(value: Float32Array): void; reject(error: Error): void };
type FakeCollision = { radii: Float32Array; pins: readonly number[]; requests: CollisionRequest[];
  project: ReturnType<typeof vi.fn>; destroy: ReturnType<typeof vi.fn> };
const collision = vi.hoisted(() => ({ instances: [] as FakeCollision[], constructorError: null as Error | null }));

vi.mock("../forceAtlas2CollisionWorker", () => ({
  createWorkspaceForceAtlas2CollisionWorker(columns: { radii: Float32Array; pins?: readonly number[] }) {
    if (collision.constructorError) throw collision.constructorError;
    const requests: CollisionRequest[] = [];
    const instance = { radii: columns.radii.slice(), pins: [...(columns.pins ?? [])], requests,
      project: vi.fn((positions: Float32Array) => new Promise<Float32Array>((resolve, reject) => {
        requests.push({ positions: positions.slice(), resolve, reject });
      })),
      // Deliberately permit a late resolution even after destroy to test the
      // bridge's generation fence, independent of the factory's own fence.
      destroy: vi.fn(),
    };
    collision.instances.push(instance);
    return instance;
  },
}));

vi.mock("graphology-layout-forceatlas2/worker", () => ({
  default: class {
    worker = Object.assign(new EventTarget(), { terminate: vi.fn() });
    upstreamMessage = vi.fn();
    handleMessage = this.upstreamMessage;
    started = false;
    killed = false;
    startRows: NodeAttributes[] = [];
    constructor(
      public graph: FakeSupervisor["graph"],
      public params: FakeSupervisor["params"],
    ) {
      fake.instances.push(this);
      if (fake.constructorError) {
        // The official constructor subscribes before Blob Worker creation.
        graph.on("nodeAdded", () => undefined);
        throw fake.constructorError;
      }
      // Official supervisor subscribes before the bridge sees its Worker.
      this.worker.addEventListener("message", this.handleMessage);
      this.upstreamMessage.mockImplementation((event: MessageEvent<{ nodes: ArrayBuffer }>) => {
        const rows = new Float32Array(event.data.nodes);
        this.graph.updateEachNodeAttributes((key, attributes) => {
          const node = Number(key);
          return this.params.outputReducer(key, { ...attributes, x: rows[node * 10], y: rows[node * 10 + 1] });
        });
        // Official handleMessage reposts immediately AFTER the graph update.
        structuredClone(event.data.nodes, { transfer: [event.data.nodes] });
      });
    }
    start() {
      if (fake.startError) throw fake.startError;
      this.started = true;
      this.startRows = this.graph.mapNodes((_key, attributes) => ({ ...attributes }));
    }
    kill() { this.killed = true; this.started = false; this.worker.terminate(); }
    emit(positions: readonly number[]) {
      const rows = new Float32Array(this.graph.order * 10).fill(1);
      for (let node = 0; node < this.graph.order; node++) {
        rows[node * 10] = positions[node * 2]; rows[node * 10 + 1] = positions[node * 2 + 1];
      }
      this.worker.dispatchEvent(new MessageEvent("message", { data: { nodes: rows.buffer } }));
    }
  },
}));

const layouts: WorkspaceForceAtlas2WorkerLayout[] = [];

function input(overrides: Partial<WorkspaceForceAtlas2WorkerInput> = {}): WorkspaceForceAtlas2WorkerInput {
  return {
    positions: Float32Array.from([2049, 2050, 2051, 2052]),
    radii: Float32Array.from([3, 4]),
    sources: Uint32Array.from([0, 0, 1]),
    targets: Uint32Array.from([1, 1, 1]),
    weights: Float64Array.from([1, 2, 3]),
    origin: [2048, 2048],
    ...overrides,
  };
}

function glyphInput(overrides: Partial<WorkspaceForceAtlas2WorkerInput> = {}): WorkspaceForceAtlas2WorkerInput {
  const columns = input();
  const units = buildWorkspaceForceAtlas2Units({ positions: columns.positions, origin: [2048, 2048],
    diametersCss: Float32Array.from([10, 10]), pointShapes: Uint8Array.from([0, 1]), width: 200, height: 100 });
  return { ...columns, units, radii: Float32Array.from(units.collisionRadiusModel), ...overrides };
}

function contactInput(overrides: Partial<WorkspaceForceAtlas2WorkerInput> = {}): WorkspaceForceAtlas2WorkerInput {
  const columns = glyphInput();
  return { ...columns, progress: { profile: "gephi-lite-2k-v1", phase: "contact", iterations: 40,
    structureIterations: 32, transitionReason: "stable" },
  contactUnits: buildWorkspaceForceAtlas2ContactUnits(columns.units!, columns.positions), ...overrides };
}

async function settleProjection() { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); }

function setup(columns = input()) {
  const frames = new Map<number, FrameRequestCallback>();
  let nextFrame = 1;
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
    const id = nextFrame++;
    frames.set(id, callback);
    return id;
  }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => { frames.delete(id); }));
  const options = { onPositions: vi.fn<(positions: Float32Array) => void>(), onError: vi.fn<(error: Error) => void>(),
    onProgress: vi.fn<(progress: ReturnType<WorkspaceForceAtlas2WorkerLayout["getProgress"]>) => void>(),
    onContactUnits: vi.fn<(units: WorkspaceForceAtlas2Units) => void>() };
  const layout = createWorkspaceForceAtlas2WorkerLayout(columns, options);
  layouts.push(layout);
  return { layout, options, frames,
    latest: () => fake.instances.at(-1)!,
    flush() {
      const queued = [...frames.values()];
      frames.clear();
      queued.forEach(callback => callback(0));
    },
  };
}

beforeEach(() => {
  fake.instances.length = 0;
  fake.constructorError = null;
  fake.startError = null;
  collision.instances.length = 0;
  collision.constructorError = null;
});
afterEach(() => {
  layouts.splice(0).forEach(layout => layout.destroy());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("official ForceAtlas2 Worker lifecycle bridge", () => {
  it("starts lazily with one explicit structure profile and exact original parallel/loop rows", () => {
    const h = setup();
    expect(fake.instances).toHaveLength(0);
    expect(h.layout.isRunning()).toBe(false);
    h.layout.start();
    const worker = h.latest();
    expect(worker.params.settings).toEqual({ linLogMode: true, scalingRatio: 30, gravity: 1,
      strongGravityMode: false, outboundAttractionDistribution: false,
      edgeWeightInfluence: 1, slowDown: 1, barnesHutOptimize: false, adjustSizes: false });
    expect(worker.startRows).toEqual([
      { x: 1, y: 2, size: 3, fixed: false }, { x: 3, y: 4, size: 4, fixed: false },
    ]);
    expect(worker.graph.order).toBe(2);
    expect(worker.graph.size).toBe(3);
    expect(worker.graph.mapEdges((key, attributes, source, target) => [key, source, target, attributes.weight]))
      .toEqual([["0", "0", "1", 1], ["1", "0", "1", 2], ["2", "1", "1", 3]]);
    h.layout.start();
    expect(fake.instances).toHaveLength(1);
    expect(h.layout.isRunning()).toBe(true);
  });

  it("snapshots input columns, origin and pins before any caller mutations", () => {
    const origin: [number, number] = [2048, 2048];
    const pins = [0];
    const columns = input({ origin, pins });
    const h = setup(columns);
    columns.positions.fill(0); columns.radii.fill(1); columns.sources.fill(1);
    columns.targets.fill(0); columns.weights.fill(9); origin.fill(0); pins.length = 0;
    h.layout.start();
    expect(h.latest().startRows).toEqual([
      { x: 1, y: 2, size: 3, fixed: true }, { x: 3, y: 4, size: 4, fixed: false },
    ]);
    expect(h.latest().graph.getEdgeAttribute("0", "weight")).toBe(1);
    expect(h.latest().graph.source("0")).toBe("0");
    expect(h.latest().graph.target("0")).toBe("1");
  });

  it("coalesces complete upstream updates at one animation frame without recalibrating units", () => {
    const h = setup(); h.layout.start();
    h.latest().emit([10, 20, 30, 40]);
    h.latest().emit([-10000, 20000, 30000, -40000]);
    expect(h.frames.size).toBe(1);
    expect(h.options.onPositions).not.toHaveBeenCalled();
    h.flush();
    expect(h.options.onPositions).toHaveBeenCalledExactlyOnceWith(Float32Array.from([-7952, 22048, 32048, -37952]));
    expect(h.frames.size).toBe(0);
  });

  it("preserves exact canonical pin rows and native fixed flags across every worker update", () => {
    const original = Float32Array.from([2048.000244140625, -2048.000244140625, 10, 20]);
    const h = setup(input({ positions: original, pins: [0] })); h.layout.start();
    expect(h.latest().startRows[0].fixed).toBe(true);
    h.latest().emit([999, 999, 40, 50]); h.flush();
    expect(h.options.onPositions.mock.calls[0][0].slice(0, 2)).toEqual(original.slice(0, 2));
    expect(h.latest().graph.getNodeAttribute("0", "fixed")).toBe(true);
    expect(h.latest().graph.getNodeAttribute("0", "x")).toBe(Math.fround(original[0] - 2048));
  });

  it("stops at the last published frame, kills pending matrix work, and resumes fresh", () => {
    const h = setup(); h.layout.start();
    const first = h.latest();
    first.emit([10, 20, 30, 40]); h.flush();
    first.emit([100, 200, 300, 400]);
    const cancelledFrame = [...h.frames.values()][0];
    h.layout.stop();
    expect(first.killed).toBe(true);
    expect(first.worker.terminate).toHaveBeenCalledOnce();
    expect(h.layout.isRunning()).toBe(false);
    expect(h.frames.size).toBe(0);
    cancelledFrame(0);
    first.emit([1000, 2000, 3000, 4000]);
    expect(h.options.onPositions).toHaveBeenCalledTimes(1);
    h.layout.start();
    expect(fake.instances).toHaveLength(2);
    expect(h.latest().startRows.map(row => [row.x, row.y])).toEqual([[10, 20], [30, 40]]);
  });

  it("fences both stale frame and old matrix when active coordinates/pins are replaced", () => {
    const h = setup(); h.layout.start();
    const first = h.latest(); first.emit([10, 20, 30, 40]);
    const oldFrame = [...h.frames.values()][0];
    const next = Float32Array.from([100, 200, 300, 400]);
    h.layout.setPositions(next, [1]); next.fill(0);
    expect(first.killed).toBe(true);
    expect(fake.instances).toHaveLength(2);
    expect(h.latest().startRows).toEqual([
      { x: -1948, y: -1848, size: 3, fixed: false }, { x: -1748, y: -1648, size: 4, fixed: true },
    ]);
    oldFrame(0); first.emit([9000, 9000, 9000, 9000]);
    h.flush(); expect(h.options.onPositions).not.toHaveBeenCalled();
    h.latest().emit([10, 20, 999, 999]); h.flush();
    expect(h.options.onPositions).toHaveBeenCalledExactlyOnceWith(Float32Array.from([2058, 2068, 300, 400]));
  });

  it("updates paused coordinates/pins without constructing a Worker and clears obsolete fixed flags", () => {
    const h = setup(input({ pins: [0] }));
    h.layout.setPositions(Float32Array.from([1, 2, 3, 4]), [1]);
    expect(fake.instances).toHaveLength(0);
    h.layout.start();
    expect(h.latest().startRows.map(row => row.fixed)).toEqual([false, true]);
    h.layout.stop();
    h.layout.setPositions(Float32Array.from([5, 6, 7, 8]));
    h.layout.start();
    expect(h.latest().startRows.map(row => row.fixed)).toEqual([false, false]);
  });

  it("does not retain aliases of arrays handed to the renderer", () => {
    const h = setup(); h.layout.start();
    h.latest().emit([10, 20, 30, 40]); h.flush();
    h.options.onPositions.mock.calls[0][0].fill(0);
    h.layout.stop(); h.layout.start();
    expect(h.latest().startRows.map(row => [row.x, row.y])).toEqual([[10, 20], [30, 40]]);
  });

  it("invalid replacements preserve the current generation and its pending publication", () => {
    const h = setup(); h.layout.start();
    const worker = h.latest(); worker.emit([10, 20, 30, 40]);
    expect(() => h.layout.setPositions(Float32Array.from([1, NaN, 2, 3]))).toThrow();
    expect(() => h.layout.setPositions(Float32Array.from([1, 2, 3, 4]), [2])).toThrow();
    expect(worker.killed).toBe(false);
    expect(fake.instances).toHaveLength(1);
    h.flush(); expect(h.options.onPositions).toHaveBeenCalledOnce();
  });

  it("destroy invalidates actor/workspace state and cannot be resurrected by queued work", () => {
    const h = setup(); h.layout.start();
    const old = h.latest(); old.emit([10, 20, 30, 40]);
    const lateFrame = [...h.frames.values()][0];
    h.layout.destroy(); h.layout.destroy();
    h.layout.start(); h.layout.setPositions(Float32Array.from([100, 200, 300, 400]), [0]);
    lateFrame(0); old.emit([999, 999, 999, 999]); h.flush();
    expect(old.worker.terminate).toHaveBeenCalledOnce();
    expect(fake.instances).toHaveLength(1);
    expect(h.layout.isRunning()).toBe(false);
    expect(h.options.onPositions).not.toHaveBeenCalled();
    expect(h.options.onError).not.toHaveBeenCalled();
  });

  it("cannot deliver old authority's worker updates into a new independent layout", () => {
    const old = setup(); old.layout.start(); const oldWorker = old.latest();
    old.layout.destroy();
    const next = setup(input({ positions: Float32Array.from([100, 200, 300, 400]) })); next.layout.start();
    oldWorker.emit([9999, 9999, 9999, 9999]); old.flush(); next.flush();
    expect(next.options.onPositions).not.toHaveBeenCalled();
    expect(old.options.onPositions).not.toHaveBeenCalled();
    expect(next.latest().startRows.map(row => [row.x, row.y])).toEqual([[-1948, -1848], [-1748, -1648]]);
  });

  it.each([NaN, Infinity, -Infinity])("rejects %s returned coordinates without publishing and tears down after upstream returns", async value => {
    const h = setup(); h.layout.start(); const worker = h.latest();
    worker.emit([value, 20, 30, 40]);
    expect(worker.killed).toBe(false);
    await Promise.resolve(); h.flush();
    expect(worker.killed).toBe(true);
    expect(h.layout.isRunning()).toBe(false);
    expect(h.options.onPositions).not.toHaveBeenCalled();
    expect(h.options.onError).toHaveBeenCalledOnce();
  });

  it("does not turn a fenced old invalid result into an error in the resumed generation", async () => {
    const h = setup(); h.layout.start(); const first = h.latest();
    first.emit([NaN, 20, 30, 40]);
    h.layout.stop(); h.layout.start();
    await Promise.resolve();
    expect(h.options.onError).not.toHaveBeenCalled();
    expect(h.layout.isRunning()).toBe(true);
  });

  it.each([
    null,
    {},
    { nodes: new Float32Array(20) },
    { nodes: new Float32Array(19).buffer },
    { nodes: new Float32Array(21).buffer },
    { nodes: Float32Array.from([NaN, ...Array(19).fill(0)]).buffer },
    { nodes: Float32Array.from([Infinity, ...Array(19).fill(0)]).buffer },
  ])("rejects malformed transfer before the upstream message handler: %s", async data => {
    const h = setup(); h.layout.start(); const worker = h.latest();
    const upstream = vi.fn();
    worker.worker.addEventListener("message", upstream);
    worker.worker.dispatchEvent(new MessageEvent("message", { data }));
    expect(upstream).not.toHaveBeenCalled();
    expect(worker.upstreamMessage).not.toHaveBeenCalled();
    await Promise.resolve();
    expect(worker.killed).toBe(true);
    expect(h.options.onPositions).not.toHaveBeenCalled();
    expect(h.options.onError).toHaveBeenCalledOnce();
  });

  it("permits exactly shaped finite upstream transfers without changing their buffer", () => {
    const h = setup(); h.layout.start(); const worker = h.latest();
    const matrix = Float32Array.from(Array.from({ length: 20 }, (_value, index) => index));
    const upstream = vi.fn();
    worker.worker.addEventListener("message", upstream);
    const event = new MessageEvent("message", { data: { nodes: matrix.buffer } });
    worker.worker.dispatchEvent(event);
    expect(upstream).toHaveBeenCalledExactlyOnceWith(event);
    expect(worker.upstreamMessage).toHaveBeenCalledExactlyOnceWith(event);
    expect(event.data.nodes).toBe(matrix.buffer);
    expect(h.options.onError).not.toHaveBeenCalled();
  });

  it("validates before the unchanged upstream handler transfers and detaches its buffer", async () => {
    const h = setup(); h.layout.start(); const worker = h.latest();
    const matrix = new Float32Array(20).fill(1);
    const transferredSizes: number[] = [];
    worker.upstreamMessage.mockImplementation((event: MessageEvent<{ nodes: ArrayBuffer }>) => {
      transferredSizes.push(event.data.nodes.byteLength);
      structuredClone(event.data.nodes, { transfer: [event.data.nodes] });
    });
    worker.worker.dispatchEvent(new MessageEvent("message", { data: { nodes: matrix.buffer } }));
    await Promise.resolve();
    expect(transferredSizes).toEqual([80]);
    expect(matrix.buffer.byteLength).toBe(0);
    expect(worker.upstreamMessage).toHaveBeenCalledOnce();
    expect(h.options.onError).not.toHaveBeenCalled();
    expect(h.layout.isRunning()).toBe(true);
  });

  it("reports an exception from the upstream message handler without continuing its generation", async () => {
    const h = setup(); h.layout.start(); const worker = h.latest();
    const failure = new Error("upstream transfer failed");
    worker.upstreamMessage.mockImplementation(() => { throw failure; });
    worker.worker.dispatchEvent(new MessageEvent("message", { data: { nodes: new Float32Array(20).buffer } }));
    await Promise.resolve();
    expect(worker.killed).toBe(true);
    expect(h.options.onError).toHaveBeenCalledExactlyOnceWith(failure);
    expect(h.layout.isRunning()).toBe(false);
  });

  it.each(["error", "messageerror"])("terminates once for a runtime %s without global unhandled errors", async type => {
    const h = setup(); h.layout.start(); const worker = h.latest();
    const event = type === "error" ? new ErrorEvent(type, { message: "worker failed", cancelable: true })
      : new Event(type, { cancelable: true });
    worker.worker.dispatchEvent(event);
    await Promise.resolve();
    expect(event.defaultPrevented).toBe(true);
    expect(worker.killed).toBe(true);
    expect(h.options.onError).toHaveBeenCalledOnce();
    worker.worker.dispatchEvent(event); await Promise.resolve();
    expect(h.options.onError).toHaveBeenCalledOnce();
  });

  it("reports Worker constructor failures and removes partial upstream graph listeners", () => {
    const h = setup(); fake.constructorError = new Error("Blob Worker denied");
    h.layout.start();
    expect(h.options.onError).toHaveBeenCalledExactlyOnceWith(fake.constructorError);
    expect(fake.instances[0].graph.listenerCount("nodeAdded")).toBe(0);
    expect(h.layout.isRunning()).toBe(false);
  });

  it("reports synchronous upstream start failures and kills the created handle", () => {
    const h = setup(); fake.startError = new Error("postMessage failed");
    h.layout.start();
    expect(h.latest().killed).toBe(true);
    expect(h.options.onError).toHaveBeenCalledExactlyOnceWith(fake.startError);
    expect(h.layout.isRunning()).toBe(false);
  });

  it("fails closed if publishing to the renderer throws", () => {
    const h = setup(); h.options.onPositions.mockImplementation(() => { throw new Error("renderer unavailable"); });
    h.layout.start(); h.latest().emit([10, 20, 30, 40]); h.flush();
    expect(h.layout.isRunning()).toBe(false);
    expect(h.options.onError).toHaveBeenCalledOnce();
  });

  it("counts complete accepted official messages rather than coalesced RAF publications", () => {
    const h = setup(); h.layout.start();
    h.latest().emit([10, 20, 30, 40]); h.latest().emit([11, 21, 31, 41]);
    expect(h.layout.getProgress().iterations).toBe(0);
    expect(h.options.onProgress).not.toHaveBeenCalled();
    h.flush();
    expect(h.layout.getProgress()).toMatchObject({ phase: "structure", iterations: 2, structureIterations: 2 });
    expect(h.options.onProgress).toHaveBeenCalledExactlyOnceWith(h.layout.getProgress());
  });

  it("does not count unrelated graph attribute publications as Worker iterations", () => {
    const h = setup(); h.layout.start();
    h.latest().graph.updateEachNodeAttributes((_key, attributes) => ({ ...attributes, x: 99, y: 100 }));
    h.flush();
    expect(h.layout.getProgress().iterations).toBe(0);
    expect(h.options.onPositions).not.toHaveBeenCalled();
  });

  it("freezes an eligible frame before delegation of later messages and restarts contact at exactly published rows", () => {
    const h = setup(); h.layout.start(); const structure = h.latest();
    for (let iteration = 0; iteration < 32; iteration++) structure.emit([1, 2, 3, 4]);
    expect(structure.upstreamMessage).toHaveBeenCalledTimes(32);
    expect(h.layout.getProgress().phase).toBe("structure");
    structure.emit([999, 999, 999, 999]);
    expect(structure.upstreamMessage).toHaveBeenCalledTimes(32);
    expect(fake.instances).toHaveLength(1);
    h.flush();
    expect(h.options.onPositions).toHaveBeenCalledExactlyOnceWith(input().positions);
    expect(h.layout.getProgress()).toEqual({ profile: "gephi-lite-2k-v1", phase: "contact",
      iterations: 32, structureIterations: 32, transitionReason: "stable" });
    expect(structure.killed).toBe(true);
    expect(fake.instances).toHaveLength(2);
    expect(h.latest().params.settings.adjustSizes).toBe(true);
    expect(h.latest().startRows).toEqual(structure.startRows);
    expect(h.latest().graph.size).toBe(3);
  });

  it("publishes the budget reason at exactly 256 accepted updates, not a convergence claim", () => {
    const h = setup(); h.layout.start(); const structure = h.latest();
    for (let iteration = 1; iteration <= 256; iteration++) structure.emit([iteration * 10, 2, iteration * 10 + 2, 4]);
    structure.emit([99999, 99999, 99999, 99999]);
    expect(structure.upstreamMessage).toHaveBeenCalledTimes(256);
    h.flush();
    expect(h.layout.getProgress()).toMatchObject({ phase: "contact", iterations: 256,
      structureIterations: 256, transitionReason: "budget" });
    expect(h.latest().startRows.map(row => [row.x, row.y])).toEqual([[2560, 2], [2562, 4]]);
  });

  it("rolls unpublished phase/counters back on Pause and cannot restart from a cancelled transition RAF", () => {
    const h = setup(); h.layout.start(); const old = h.latest();
    for (let iteration = 0; iteration < 32; iteration++) old.emit([1, 2, 3, 4]);
    const frame = [...h.frames.values()][0];
    h.layout.stop(); frame(0);
    expect(h.layout.getProgress()).toMatchObject({ phase: "structure", iterations: 0, transitionReason: null });
    expect(fake.instances).toHaveLength(1);
    expect(h.options.onProgress).not.toHaveBeenCalled();
    h.layout.start();
    expect(h.latest().params.settings.adjustSizes).toBe(false);
    expect(h.latest().startRows).toEqual(old.startRows);
  });

  it("resets the stability history on Resume but retains the last displayed structure count", () => {
    const h = setup(); h.layout.start();
    for (let iteration = 0; iteration < 24; iteration++) h.latest().emit([1, 2, 3, 4]);
    h.flush(); h.layout.stop(); h.layout.start();
    const resumed = h.latest();
    for (let iteration = 0; iteration < 8; iteration++) resumed.emit([1, 2, 3, 4]);
    h.flush();
    expect(h.layout.getProgress()).toMatchObject({ phase: "structure", iterations: 32 });
    expect(fake.instances).toHaveLength(2);
    for (let iteration = 0; iteration < 24; iteration++) resumed.emit([1, 2, 3, 4]);
    h.flush();
    expect(h.layout.getProgress()).toMatchObject({ phase: "contact", iterations: 56, transitionReason: "stable" });
    expect(fake.instances).toHaveLength(3);
  });

  it.each(["stop", "destroy"] as const)("does not restart or notify stale progress when onPositions invokes %s", action => {
    const h = setup(); h.layout.start(); const old = h.latest();
    h.options.onPositions.mockImplementation(() => h.layout[action]());
    for (let iteration = 0; iteration < 32; iteration++) old.emit([1, 2, 3, 4]);
    h.flush();
    expect(h.layout.getProgress()).toMatchObject({ phase: "contact", iterations: 32 });
    expect(h.layout.isRunning()).toBe(false);
    expect(fake.instances).toHaveLength(1);
    expect(h.options.onProgress).not.toHaveBeenCalled();
    if (action === "stop") {
      h.layout.start();
      expect(h.latest().params.settings.adjustSizes).toBe(true);
    }
  });

  it("preserves a reentrant reseed/pin change instead of overwriting it with the old transition snapshot", () => {
    const h = setup(); h.layout.start(); const old = h.latest();
    const reseed = Float32Array.from([100, 200, 300, 400]);
    h.options.onPositions.mockImplementationOnce(() => h.layout.setPositions(reseed, [1]));
    for (let iteration = 0; iteration < 32; iteration++) old.emit([1, 2, 3, 4]);
    h.flush();
    expect(fake.instances).toHaveLength(2);
    expect(h.options.onProgress).not.toHaveBeenCalled();
    expect(h.latest().params.settings.adjustSizes).toBe(true);
    expect(h.latest().startRows).toEqual([
      { x: -1948, y: -1848, size: 3, fixed: false }, { x: -1748, y: -1648, size: 4, fixed: true },
    ]);
    expect(h.layout.getProgress()).toMatchObject({ phase: "contact", iterations: 32 });
  });

  it("rechecks generation after onProgress and leaves callback Pause contact state resumable", () => {
    const h = setup(); h.layout.start();
    h.options.onProgress.mockImplementation(() => h.layout.stop());
    for (let iteration = 0; iteration < 32; iteration++) h.latest().emit([1, 2, 3, 4]);
    h.flush();
    expect(h.options.onProgress).toHaveBeenCalledOnce();
    expect(fake.instances).toHaveLength(1);
    expect(h.layout.getProgress().phase).toBe("contact");
    expect(h.layout.isRunning()).toBe(false);
  });

  it("fails closed when the progress callback throws", () => {
    const h = setup(); h.layout.start();
    h.options.onProgress.mockImplementation(() => { throw new Error("progress observer unavailable"); });
    h.latest().emit([1, 2, 3, 4]); h.flush();
    expect(h.layout.isRunning()).toBe(false);
    expect(h.options.onError).toHaveBeenCalledOnce();
  });

  it("keeps contact on Pause, reseed/pin change, and a fresh ephemeral remount", () => {
    const initial = { profile: "gephi-lite-2k-v1", phase: "contact", iterations: 40,
      structureIterations: 32, transitionReason: "stable" } as const;
    const h = setup(input({ progress: initial })); h.layout.start();
    expect(h.latest().params.settings.adjustSizes).toBe(true);
    h.latest().emit([1, 2, 3, 4]); h.flush();
    h.layout.stop(); h.layout.setPositions(Float32Array.from([100, 200, 300, 400]), [0]); h.layout.start();
    expect(h.latest().params.settings.adjustSizes).toBe(true);
    expect(h.layout.getProgress()).toEqual({ ...initial, iterations: 41 });
    const remount = setup(input({ progress: h.layout.getProgress() })); remount.layout.start();
    expect(remount.latest().params.settings.adjustSizes).toBe(true);
    expect(remount.layout.getProgress()).toEqual(h.layout.getProgress());
  });

  it("snapshots progress and rejects foreign/unreachable remount state before constructing a Worker", () => {
    const mutable = { ...setup().layout.getProgress(), iterations: 8, structureIterations: 8 };
    const h = setup(input({ progress: mutable })); mutable.iterations = 99;
    expect(h.layout.getProgress().iterations).toBe(8);
    expect(Object.isFrozen(h.layout.getProgress())).toBe(true);
    expect(() => setup(input({ progress: { ...mutable, profile: "other" } as never }))).toThrow();
    expect(fake.instances).toHaveLength(0);
  });

  it("calibrates separate contact metadata once at the published structural frame without altering seed units", () => {
    const columns = glyphInput(), seed = columns.units!, before = [...seed.collisionRadiusModel];
    const h = setup(columns); h.layout.start(); const structure = h.latest();
    for (let iteration = 0; iteration < 40; iteration++) structure.emit([1, 2, 2001, 1002]);
    expect(h.layout.getContactUnits()).toBeUndefined();
    expect(collision.instances).toHaveLength(0);
    h.flush();
    const published = h.options.onPositions.mock.calls[0][0];
    const contact = h.layout.getContactUnits()!;
    expect(contact).toEqual(buildWorkspaceForceAtlas2ContactUnits(seed, published));
    expect(contact.referenceZoom).toBeLessThan(seed.referenceZoom);
    expect(seed.collisionRadiusModel).toEqual(before);
    expect(seed).toBe(columns.units);
    expect(h.options.onContactUnits).toHaveBeenCalledExactlyOnceWith(contact);
    expect(collision.instances).toHaveLength(1);
    expect(collision.instances[0].radii).toEqual(Float32Array.from(contact.collisionRadiusModel));
    expect(h.latest().startRows.map(row => row.size)).toEqual(contact.collisionRadiusModel);
    expect(h.latest().graph.size).toBe(3);
    h.layout.stop(); h.layout.start();
    expect(h.layout.getContactUnits()).toBe(contact);
    expect(h.options.onContactUnits).toHaveBeenCalledOnce();
    expect(collision.instances[0].destroy).toHaveBeenCalledOnce();
    expect(collision.instances[1].radii).toEqual(collision.instances[0].radii);
  });

  it("does not latch contact metadata when Pause discards an unpublished transition", () => {
    const h = setup(glyphInput()); h.layout.start();
    for (let iteration = 0; iteration < 32; iteration++) h.latest().emit([1, 2, 3, 4]);
    const late = [...h.frames.values()][0];
    h.layout.stop(); late(0);
    expect(h.layout.getContactUnits()).toBeUndefined();
    expect(h.options.onContactUnits).not.toHaveBeenCalled();
    expect(collision.instances).toHaveLength(0);
  });

  it("keeps contact metadata published but fences its notification if onPositions pauses the boundary", () => {
    const h = setup(glyphInput()); h.layout.start();
    h.options.onPositions.mockImplementation(() => h.layout.stop());
    for (let iteration = 0; iteration < 32; iteration++) h.latest().emit([1, 2, 3, 4]);
    h.flush();
    expect(h.layout.getContactUnits()).toBeDefined();
    expect(h.layout.getProgress().phase).toBe("contact");
    expect(h.options.onContactUnits).not.toHaveBeenCalled();
    expect(collision.instances).toHaveLength(0);
    h.layout.start();
    expect(collision.instances).toHaveLength(1);
  });

  it.each(["stop", "destroy"] as const)("fences progress/restart if onContactUnits invokes %s", action => {
    const h = setup(glyphInput()); h.layout.start();
    h.options.onContactUnits.mockImplementation(() => h.layout[action]());
    for (let iteration = 0; iteration < 32; iteration++) h.latest().emit([1, 2, 3, 4]);
    h.flush();
    expect(h.options.onContactUnits).toHaveBeenCalledOnce();
    expect(h.options.onProgress).not.toHaveBeenCalled();
    expect(h.layout.getContactUnits()).toBeDefined();
    expect(h.layout.isRunning()).toBe(false);
    expect(collision.instances).toHaveLength(0);
  });

  it("preserves reentrant contact callback reseed/pins and does not start another obsolete contact worker", () => {
    const h = setup(glyphInput()); h.layout.start();
    h.options.onContactUnits.mockImplementation(() => h.layout.setPositions(Float32Array.from([100, 200, 300, 400]), [1]));
    for (let iteration = 0; iteration < 32; iteration++) h.latest().emit([1, 2, 3, 4]);
    h.flush();
    expect(fake.instances).toHaveLength(2);
    expect(collision.instances).toHaveLength(1);
    expect(collision.instances[0].pins).toEqual([1]);
    expect(h.latest().startRows.map(row => [row.x, row.y, row.fixed])).toEqual([
      [-1948, -1848, false], [-1748, -1648, true],
    ]);
    expect(h.options.onProgress).not.toHaveBeenCalled();
  });

  it("fails closed when publishing contact metadata throws", () => {
    const h = setup(glyphInput()); h.layout.start();
    h.options.onContactUnits.mockImplementation(() => { throw new Error("metadata observer unavailable"); });
    for (let iteration = 0; iteration < 32; iteration++) h.latest().emit([1, 2, 3, 4]);
    h.flush();
    expect(h.layout.isRunning()).toBe(false);
    expect(h.options.onError).toHaveBeenCalledOnce();
    expect(h.layout.getContactUnits()).toBeDefined();
    expect(h.options.onProgress).not.toHaveBeenCalled();
  });

  it("preserves the same official buffer and all non-position columns through asynchronous collision feedback", async () => {
    const h = setup(contactInput()); h.layout.start(); const upstream = h.latest();
    const matrix = Float32Array.from(Array.from({ length: 20 }, (_, index) => index + 1));
    const before = matrix.slice(), buffer = matrix.buffer;
    const implementation = upstream.upstreamMessage.getMockImplementation()! as (event: MessageEvent<{ nodes: ArrayBuffer }>) => void;
    let received: Float32Array | undefined;
    upstream.upstreamMessage.mockImplementation((event: MessageEvent<{ nodes: ArrayBuffer }>) => {
      expect(event.data.nodes).toBe(buffer);
      received = new Float32Array(event.data.nodes).slice();
      implementation(event);
    });
    upstream.worker.dispatchEvent(new MessageEvent("message", { data: { nodes: buffer } }));
    expect(upstream.upstreamMessage).not.toHaveBeenCalled();
    expect(matrix.buffer.byteLength).toBe(80);
    const constraint = collision.instances[0];
    expect(constraint.requests[0].positions).toEqual(Float32Array.from([1, 2, 11, 12]));
    constraint.requests[0].resolve(Float32Array.from([20, 30, 40, 50]));
    await settleProjection();
    expect(upstream.upstreamMessage).toHaveBeenCalledOnce();
    expect(received).toBeDefined();
    expect([received![0], received![1], received![10], received![11]]).toEqual([20, 30, 40, 50]);
    for (let index = 0; index < before.length; index++) if (index % 10 >= 2) expect(received![index]).toBe(before[index]);
    expect(buffer.byteLength).toBe(0);
    h.flush();
    expect(h.options.onPositions).toHaveBeenCalledExactlyOnceWith(Float32Array.from([2068, 2078, 2088, 2098]));
    expect(fake.instances).toHaveLength(1);
    expect(h.layout.getProgress().iterations).toBe(41);
  });

  it("never asks for a second official iteration while collision feedback owns the current buffer", async () => {
    const h = setup(contactInput()); h.layout.start(); const upstream = h.latest();
    upstream.emit([10, 20, 30, 40]); upstream.emit([100, 200, 300, 400]);
    expect(collision.instances[0].project).toHaveBeenCalledOnce();
    expect(upstream.upstreamMessage).not.toHaveBeenCalled();
    expect(h.layout.getProgress().iterations).toBe(40);
    collision.instances[0].requests[0].resolve(Float32Array.from([10, 20, 30, 40]));
    await settleProjection(); h.flush();
    expect(upstream.upstreamMessage).toHaveBeenCalledOnce();
    expect(h.layout.getProgress().iterations).toBe(41);
    expect(fake.instances).toHaveLength(1);
  });

  it("feeds exact fixed model rows into collision and restores canonical pins even if a faulty constraint moves them", async () => {
    const columns = contactInput({ pins: [0] });
    const h = setup(columns); h.layout.start(); h.latest().emit([999, 999, 30, 40]);
    expect(collision.instances[0].requests[0].positions).toEqual(Float32Array.from([1, 2, 30, 40]));
    expect(collision.instances[0].pins).toEqual([0]);
    collision.instances[0].requests[0].resolve(Float32Array.from([900, 900, 35, 45]));
    await settleProjection(); h.flush();
    expect(h.options.onPositions).toHaveBeenCalledExactlyOnceWith(Float32Array.from([2049, 2050, 2083, 2093]));
    expect(h.latest().graph.getNodeAttribute("0", "fixed")).toBe(true);
    expect(h.latest().graph.getNodeAttribute("0", "x")).toBe(1);
  });

  it.each(["stop", "destroy"] as const)("discards a late collision result after %s without matrix writes or upstream repost", async action => {
    const h = setup(contactInput()); h.layout.start(); const upstream = h.latest();
    const matrix = new Float32Array(20).fill(1), before = matrix.slice();
    upstream.worker.dispatchEvent(new MessageEvent("message", { data: { nodes: matrix.buffer } }));
    h.layout[action]();
    expect(collision.instances[0].destroy).toHaveBeenCalledOnce();
    collision.instances[0].requests[0].resolve(Float32Array.from([100, 200, 300, 400]));
    await settleProjection(); h.flush();
    expect(matrix).toEqual(before);
    expect(upstream.upstreamMessage).not.toHaveBeenCalled();
    expect(h.options.onPositions).not.toHaveBeenCalled();
    expect(h.options.onError).not.toHaveBeenCalled();
    expect(h.layout.getContactUnits()).toBeDefined();
  });

  it("fences old collision feedback while a live reseed/pin change preserves the published contact calibration", async () => {
    const columns = contactInput(), h = setup(columns); h.layout.start(); const old = h.latest();
    old.emit([10, 20, 30, 40]);
    h.layout.setPositions(Float32Array.from([100, 200, 300, 400]), [1]);
    expect(h.layout.getContactUnits()).toBe(columns.contactUnits);
    expect(collision.instances).toHaveLength(2);
    expect(collision.instances[1].radii).toEqual(collision.instances[0].radii);
    expect(collision.instances[1].pins).toEqual([1]);
    collision.instances[0].requests[0].resolve(Float32Array.from([9000, 9000, 9000, 9000]));
    await settleProjection(); h.flush();
    expect(old.upstreamMessage).not.toHaveBeenCalled();
    expect(h.options.onPositions).not.toHaveBeenCalled();
    expect(h.latest().startRows.map(row => [row.x, row.y])).toEqual([[-1948, -1848], [-1748, -1648]]);
  });

  it("ignores an old collision rejection after actor/layout destruction", async () => {
    const h = setup(contactInput()); h.layout.start(); h.latest().emit([1, 2, 3, 4]);
    h.layout.destroy();
    collision.instances[0].requests[0].reject(new Error("late old authority failure"));
    await settleProjection();
    expect(h.options.onError).not.toHaveBeenCalled();
  });

  it("fails closed on a current collision rejection and retains the last published contact checkpoint", async () => {
    const columns = contactInput(), h = setup(columns); h.layout.start(); const old = h.latest();
    old.emit([1, 2, 3, 4]);
    const error = new Error("collision worker failed");
    collision.instances[0].requests[0].reject(error);
    await settleProjection(); h.flush();
    expect(h.options.onError).toHaveBeenCalledExactlyOnceWith(error);
    expect(h.layout.isRunning()).toBe(false);
    expect(old.upstreamMessage).not.toHaveBeenCalled();
    expect(h.options.onPositions).not.toHaveBeenCalled();
    expect(h.layout.getProgress().iterations).toBe(40);
    expect(h.layout.getContactUnits()).toBe(columns.contactUnits);
  });

  it.each([new Float32Array(2), Float32Array.from([NaN, 0, 1, 2]), Float32Array.from([Infinity, 0, 1, 2])])(
    "rejects invalid collision output %s before changing or transferring the official matrix", async projected => {
      const h = setup(contactInput()); h.layout.start(); const upstream = h.latest();
      const matrix = new Float32Array(20).fill(1), before = matrix.slice();
      upstream.worker.dispatchEvent(new MessageEvent("message", { data: { nodes: matrix.buffer } }));
      collision.instances[0].requests[0].resolve(projected);
      await settleProjection(); h.flush();
      expect(matrix).toEqual(before);
      expect(upstream.upstreamMessage).not.toHaveBeenCalled();
      expect(h.options.onError).toHaveBeenCalledOnce();
      expect(h.layout.isRunning()).toBe(false);
    },
  );

  it("cleans up a collision Worker if upstream construction fails", () => {
    const h = setup(contactInput()); fake.constructorError = new Error("FA2 constructor failed");
    h.layout.start();
    expect(collision.instances[0].destroy).toHaveBeenCalledOnce();
    expect(h.options.onError).toHaveBeenCalledExactlyOnceWith(fake.constructorError);
    expect(h.layout.isRunning()).toBe(false);
  });

  it("reports collision construction failure without constructing or starting upstream", () => {
    const h = setup(contactInput()); collision.constructorError = new Error("collision module worker denied");
    h.layout.start();
    expect(fake.instances).toHaveLength(0);
    expect(h.options.onError).toHaveBeenCalledExactlyOnceWith(collision.constructorError);
    expect(h.layout.isRunning()).toBe(false);
  });

  it("rejects inconsistent or incomplete restored units before creating any Worker", () => {
    const columns = contactInput();
    const cases: Partial<WorkspaceForceAtlas2WorkerInput>[] = [
      { units: { ...columns.units! } }, { origin: [0, 0] }, { radii: Float32Array.from([999, 999]) },
      { units: undefined }, { contactUnits: undefined },
      { progress: { profile: "gephi-lite-2k-v1", phase: "structure", iterations: 0, structureIterations: 0, transitionReason: null } },
      { contactUnits: buildWorkspaceForceAtlas2Units({ positions: [0, 0, 10, 20], diametersCss: [10, 10],
        pointShapes: [0, 1], origin: [2048, 2048], width: 300, height: 100 }) },
    ];
    for (const overrides of cases) expect(() => setup({ ...columns, ...overrides })).toThrow();
    expect(fake.instances).toHaveLength(0);
    expect(collision.instances).toHaveLength(0);
  });

  it("restores contact calibration on a fresh ephemeral remount without changing the seed", () => {
    const columns = contactInput(), h = setup(columns); h.layout.start();
    const remount = setup({ ...columns, contactUnits: h.layout.getContactUnits(), progress: h.layout.getProgress() });
    h.layout.destroy(); remount.layout.start();
    expect(remount.layout.getContactUnits()).toBe(columns.contactUnits);
    expect(collision.instances[1].radii).toEqual(collision.instances[0].radii);
    expect(remount.latest().startRows.map(row => row.size)).toEqual(columns.contactUnits!.collisionRadiusModel);
    expect(remount.options.onContactUnits).not.toHaveBeenCalled();
  });

  it("accepts exactly 2000 isolated original nodes without adding artificial folder edges", () => {
    const h = setup(input({ positions: new Float32Array(4000), radii: new Float32Array(2000).fill(1),
      sources: new Uint32Array(), targets: new Uint32Array(), weights: new Float64Array() }));
    h.layout.start();
    expect(h.latest().graph.order).toBe(2000);
    expect(h.latest().graph.size).toBe(0);
  });

  it.each([
    { positions: Float32Array.from([1, 2]) },
    { positions: Float32Array.from([NaN, 2, 3, 4]) },
    { positions: [1, 2, 3, 4] as unknown as Float32Array },
    { radii: Float32Array.from([0, 1]) },
    { radii: Float32Array.from([-1, 1]) },
    { radii: Float32Array.from([Infinity, 1]) },
    { radii: new Float32Array() },
    { radii: new Float32Array(2001).fill(1), positions: new Float32Array(4002) },
    { sources: Uint32Array.from([0]) },
    { targets: Uint32Array.from([0]) },
    { sources: Uint32Array.from([0, 0, 2]) },
    { targets: Uint32Array.from([0, 0, 2]) },
    { weights: Float64Array.from([0, 1, 1]) },
    { weights: Float64Array.from([-1, 1, 1]) },
    { weights: Float64Array.from([NaN, 1, 1]) },
    { weights: Float64Array.from([Infinity, 1, 1]) },
    { weights: Float64Array.from([1e100, 1, 1]) },
    { weights: Float64Array.from([1e-100, 1, 1]) },
    { pins: [-1] }, { pins: [2] }, { pins: [.5] }, { pins: [NaN] },
    { origin: [Infinity, 0] as const },
    { origin: [1e100, 0] as const },
  ])("rejects malformed/over-budget columns before Worker creation: %s", overrides => {
    expect(() => setup(input(overrides))).toThrow();
    expect(fake.instances).toHaveLength(0);
  });
});
