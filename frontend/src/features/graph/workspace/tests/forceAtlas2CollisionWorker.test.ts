import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type WorkspaceForceAtlas2CollisionRequest,
  type WorkspaceForceAtlas2CollisionResponse,
} from "../forceAtlas2Collision";
import { createWorkspaceForceAtlas2CollisionWorker } from "../forceAtlas2CollisionWorker";

const instances: FakeWorker[] = [];
class FakeWorker extends EventTarget {
  requests: WorkspaceForceAtlas2CollisionRequest[] = [];
  terminated = false;
  failPost = false;
  constructor(public url: URL, public options: WorkerOptions) { super(); instances.push(this); }
  postMessage(request: WorkspaceForceAtlas2CollisionRequest, transfer: Transferable[] = []) {
    if (this.failPost) throw new Error("transfer failed");
    this.requests.push(structuredClone(request, { transfer }));
  }
  terminate() { this.terminated = true; }
  reply(response: WorkspaceForceAtlas2CollisionResponse) {
    this.dispatchEvent(new MessageEvent("message", { data: response }));
  }
}

function projection(worker: FakeWorker) {
  const request = worker.requests.at(-1)!;
  if (request.type !== "project") throw new Error("Expected projection request");
  return request;
}

function complete(worker: FakeWorker, request = projection(worker), rows?: Float32Array) {
  worker.reply({ type: "projected", generation: request.generation, requestId: request.requestId,
    positions: (rows ?? new Float32Array(request.positions).slice()).buffer as ArrayBuffer, durationMs: 1 });
}

beforeEach(() => { instances.length = 0; vi.stubGlobal("Worker", FakeWorker); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("collision Worker transferable ownership", () => {
  it("creates a self-hosted module Worker and snapshots immutable init columns", () => {
    const radii = Float32Array.from([3, 4]), pins = [0];
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii, pins });
    radii.fill(999); pins.push(1);
    const worker = instances[0], init = worker.requests[0];
    expect(worker.url.pathname).toMatch(/forceAtlas2Collision\.worker\.ts$/);
    expect(worker.options).toEqual({ type: "module", name: "workspace-fa2-collision" });
    expect(init.type).toBe("init");
    if (init.type !== "init") throw new Error("Expected init");
    expect(new Float32Array(init.radii)).toEqual(Float32Array.from([3, 4]));
    expect(init.pins).toEqual([0]);
    layout.destroy();
  });

  it("transfers cloned xy while preserving the caller's buffer and original order", async () => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([3, 3]) });
    const rows = Float32Array.from([0, 0, 1, 0]), original = rows.slice();
    const promise = layout.project(rows), worker = instances[0];
    expect(rows.byteLength).toBe(16); expect(rows).toEqual(original);
    expect(new Float32Array(projection(worker).positions)).toEqual(original);
    complete(worker, projection(worker), Float32Array.from([-2.5, 0, 3.5, 0]));
    await expect(promise).resolves.toEqual(Float32Array.from([-2.5, 0, 3.5, 0]));
    expect(rows).toEqual(original);
    layout.destroy();
  });

  it("rejects a concurrent request without cancelling the first", async () => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([1]) });
    const first = layout.project(new Float32Array(2));
    await expect(layout.project(new Float32Array(2))).rejects.toThrow("one in-flight");
    complete(instances[0]); await expect(first).resolves.toEqual(new Float32Array(2));
    layout.destroy();
  });

  it("ignores foreign generations and duplicate/late request responses", async () => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([1]) });
    const first = layout.project(Float32Array.from([1, 2])), worker = instances[0], firstRequest = projection(worker);
    worker.reply({ type: "projected", generation: firstRequest.generation + 100, requestId: firstRequest.requestId,
      positions: new Float32Array(2).buffer, durationMs: 1 });
    let resolved = false; void first.then(() => { resolved = true; });
    await Promise.resolve(); expect(resolved).toBe(false);
    complete(worker); await expect(first).resolves.toEqual(Float32Array.from([1, 2]));
    const second = layout.project(Float32Array.from([3, 4]));
    complete(worker, firstRequest, Float32Array.from([999, 999]));
    complete(worker); await expect(second).resolves.toEqual(Float32Array.from([3, 4]));
    layout.destroy();
  });

  it("rejects pending projection on destroy and fences later responses", async () => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([1]) });
    const pending = layout.project(new Float32Array(2)), rejected = expect(pending).rejects.toThrow("destroyed");
    const worker = instances[0], request = projection(worker);
    layout.destroy(); layout.destroy();
    await rejected; expect(worker.terminated).toBe(true);
    complete(worker, request);
    await expect(layout.project(new Float32Array(2))).rejects.toThrow("destroyed");
  });

  it.each(["size", "finite", "pin"] as const)("rejects malformed returned rows: %s", async kind => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([1]), pins: [0] });
    const pending = layout.project(Float32Array.from([-0, 2])), rejected = expect(pending).rejects.toThrow();
    complete(instances[0], projection(instances[0]), kind === "size" ? new Float32Array(1)
      : kind === "finite" ? Float32Array.from([NaN, 2]) : Float32Array.from([0, 2]));
    await rejected; expect(instances[0].terminated).toBe(true);
  });

  it.each(["error", "messageerror"])("rejects errors and terminates the Worker: %s", async type => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([1]) });
    const rejected = expect(layout.project(new Float32Array(2))).rejects.toThrow("failed to deliver");
    instances[0].dispatchEvent(new Event(type, { cancelable: true }));
    await rejected; expect(instances[0].terminated).toBe(true);
  });

  it("rejects initialization failure even when a projection is already pending", async () => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([1]) });
    const rejected = expect(layout.project(new Float32Array(2))).rejects.toThrow("init failure");
    instances[0].reply({ type: "error", generation: projection(instances[0]).generation, requestId: null, message: "init failure" });
    await rejected; expect(instances[0].terminated).toBe(true);
  });

  it("rejects bad input without posting or retiring the healthy Worker", async () => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([1]) });
    await expect(layout.project(Float32Array.from([NaN, 0]))).rejects.toThrow("finite indexed");
    expect(instances[0].requests).toHaveLength(1); expect(instances[0].terminated).toBe(false);
    const pending = layout.project(new Float32Array(2)); complete(instances[0]); await pending;
    layout.destroy();
  });

  it("rejects transfer failures and closes pending ownership", async () => {
    const layout = createWorkspaceForceAtlas2CollisionWorker({ radii: Float32Array.from([1]) });
    instances[0].failPost = true;
    await expect(layout.project(new Float32Array(2))).rejects.toThrow("transfer failed");
    expect(instances[0].terminated).toBe(true);
  });

  it("validates immutable columns before allocating a Worker", () => {
    expect(() => createWorkspaceForceAtlas2CollisionWorker({ radii: new Float32Array() })).toThrow();
    expect(instances).toHaveLength(0);
  });

  it("executes the module entry protocol and reports bounded off-thread duration", async () => {
    const responses: WorkspaceForceAtlas2CollisionResponse[] = [];
    const scope: { onmessage: ((event: MessageEvent<WorkspaceForceAtlas2CollisionRequest>) => void) | null;
      postMessage(response: WorkspaceForceAtlas2CollisionResponse, transfer?: Transferable[]): void } = {
      onmessage: null, postMessage(response, transfer = []) { responses.push(structuredClone(response, { transfer })); },
    };
    vi.stubGlobal("self", scope);
    await import("../forceAtlas2Collision.worker");
    const send = (request: WorkspaceForceAtlas2CollisionRequest) => scope.onmessage!(new MessageEvent("message", { data: request }));
    send({ type: "init", generation: 17, radii: Float32Array.from([3, 3]).buffer, pins: [0] });
    send({ type: "project", generation: 18, requestId: 1, positions: new Float32Array(4).buffer });
    expect(responses).toHaveLength(0);
    send({ type: "project", generation: 17, requestId: 1, positions: Float32Array.from([0, 0, 1, 0]).buffer });
    expect(responses).toHaveLength(1);
    const result = responses[0]; expect(result.type).toBe("projected");
    if (result.type !== "projected") throw new Error("Expected projected rows");
    expect(result.durationMs).toBeGreaterThanOrEqual(0);
    expect(new Float32Array(result.positions)[0]).toBe(0);
    expect(new Float32Array(result.positions)[2]).toBeGreaterThan(1);
    send({ type: "project", generation: 17, requestId: 1, positions: new Float32Array(4).buffer });
    expect(responses).toHaveLength(1);
    send({ type: "project", generation: 17, requestId: 2, positions: new Float32Array(1).buffer });
    expect(responses[1]).toMatchObject({ type: "error", requestId: 2 });
  });
});
