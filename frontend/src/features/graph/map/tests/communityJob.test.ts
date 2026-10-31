import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceMapColumns } from "../sceneColumns";
import {
  createWorkspaceMapCommunityJob,
  workspaceMapCommunityIdentityKey,
} from "../communityJob";
import type {
  WorkspaceMapCommunityRequest,
  WorkspaceMapCommunityResponse,
} from "../communityWorker";

function columns(nodeCount = 3, edgeCount = 2): WorkspaceMapColumns {
  const nodeKeys = Array.from({ length: nodeCount }, (_unused, index) => `node-${String(index).padStart(5, "0")}`);
  return {
    identity: {
      workspaceId: "workspace-a",
      actorId: "actor-a",
      surface: "workspace",
      selectionKey: "links",
      visibleFingerprint: "fingerprint-a",
    },
    nodeIds: [...nodeKeys],
    nodeKeys,
    nodeLabels: [...nodeKeys],
    nodeFolderKeys: nodeKeys.map(() => null),
    nodeKinds: new Uint8Array(nodeCount),
    nodeSizes: new Float32Array(nodeCount),
    edgeIds: Array.from({ length: edgeCount }, (_unused, index) => `edge-${index}`),
    edgeSources: Uint32Array.from({ length: edgeCount }, (_unused, index) => index % nodeCount),
    edgeTargets: Uint32Array.from({ length: edgeCount }, (_unused, index) => (index + 1) % nodeCount),
    edgeCounts: new Uint32Array(edgeCount).fill(1),
    edgeWeights: new Float64Array(edgeCount).fill(1),
    edgeKinds: new Uint8Array(edgeCount),
    edgeCrossFolder: new Uint8Array(edgeCount),
  };
}

class FakeWorker {
  onmessage: ((event: MessageEvent<WorkspaceMapCommunityResponse>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  request: WorkspaceMapCommunityRequest | null = null;
  transfer: Transferable[] = [];
  terminated = false;

  postMessage(request: WorkspaceMapCommunityRequest, transfer?: Transferable[]) {
    this.request = request;
    this.transfer = transfer ?? [];
  }

  terminate() {
    this.terminated = true;
  }

  emit(response: WorkspaceMapCommunityResponse) {
    this.onmessage?.({ data: response } as MessageEvent<WorkspaceMapCommunityResponse>);
  }
}

afterEach(() => vi.useRealTimers());

describe("Workspace Map community worker lifecycle", () => {
  it.each([
    { name: "NaN position", changes: { initialPositions: Float32Array.from([NaN, 0, 1, 0, 1, 1]) } },
    { name: "infinite position", changes: { initialPositions: Float32Array.from([0, 0, Infinity, 0, 1, 1]) } },
    { name: "zero communities for nonempty scene", changes: { communityCount: 0 } },
    { name: "more communities than nodes", changes: { communityCount: 4 } },
    { name: "out-of-range community", changes: { communityIds: Uint32Array.from([0, 1, 2]) } },
    { name: "empty declared community", changes: { communityIds: Uint32Array.from([0, 0, 0]) } },
    { name: "invalid compute time", changes: { computeMs: NaN } },
    { name: "negative compute time", changes: { computeMs: -1 } },
  ])("rejects $name instead of installing invalid GPU data", async ({ changes }) => {
    const worker = new FakeWorker();
    const job = createWorkspaceMapCommunityJob({ workerFactory: () => worker });
    const input = columns();
    const pending = job.compute(input);
    worker.emit({
      type: "complete",
      requestId: worker.request!.requestId,
      identityKey: worker.request!.identityKey,
      communityIds: Uint32Array.from([0, 1, 1]),
      communityCount: 2,
      initialPositions: Float32Array.from([0, 0, 1, 0, 1, 1]),
      computeMs: 1,
      ...changes,
    });
    expect(await pending).toEqual({ kind: "error", identityKey: workspaceMapCommunityIdentityKey(input) });
    expect(worker.terminated).toBe(true);
    expect(input.nodeKeys).toHaveLength(3);
    job.destroy();
  });

  it("accepts a valid empty scene with no communities", async () => {
    const worker = new FakeWorker();
    const job = createWorkspaceMapCommunityJob({ workerFactory: () => worker });
    const pending = job.compute(columns(0, 0));
    worker.emit({
      type: "complete", requestId: worker.request!.requestId,
      identityKey: worker.request!.identityKey, communityIds: new Uint32Array(),
      communityCount: 0, initialPositions: new Float32Array(), computeMs: 0,
    });
    expect(await pending).toMatchObject({ kind: "complete", communityCount: 0 });
    job.destroy();
  });

  it("transfers precise occurrence weights rather than relation counts without detaching live data", async () => {
    const worker = new FakeWorker();
    const job = createWorkspaceMapCommunityJob({ workerFactory: () => worker });
    const input = columns();
    input.edgeWeights = Float64Array.from([0x1_0000_0001, 19]);
    const pending = job.compute(input);
    expect(worker.request?.input.edgeWeights).toEqual(input.edgeWeights);
    expect(worker.request?.input.edgeWeights).not.toBe(input.edgeWeights);
    expect(worker.request?.input).not.toHaveProperty("edgeCounts");
    expect(worker.transfer).toContain(worker.request?.input.edgeWeights.buffer);
    expect(input.edgeCounts).toEqual(Uint32Array.from([1, 1]));
    expect(input.edgeWeights).toEqual(Float64Array.from([0x1_0000_0001, 19]));
    job.destroy();
    expect(await pending).toMatchObject({ kind: "cancelled" });
  });

  it("terminates old computation and discards stale or mismatched responses", async () => {
    const workers: FakeWorker[] = [];
    const job = createWorkspaceMapCommunityJob({ workerFactory: () => {
      const worker = new FakeWorker();
      workers.push(worker);
      return worker;
    } });
    const firstColumns = columns();
    const first = job.compute(firstColumns);
    const secondColumns = columns();
    secondColumns.identity = { ...secondColumns.identity, visibleFingerprint: "fingerprint-b" };
    const second = job.compute(secondColumns);

    expect(await first).toEqual({
      kind: "cancelled",
      identityKey: workspaceMapCommunityIdentityKey(firstColumns),
    });
    expect(workers[0].terminated).toBe(true);
    workers[0].emit({
      type: "complete",
      requestId: workers[0].request!.requestId,
      identityKey: workers[0].request!.identityKey,
      communityIds: Uint32Array.from([0, 0, 0]),
      communityCount: 1,
      initialPositions: new Float32Array(6),
      computeMs: 1,
    });
    workers[1].emit({
      type: "complete",
      requestId: workers[1].request!.requestId,
      identityKey: "wrong-actor-or-scene",
      communityIds: Uint32Array.from([0, 0, 0]),
      communityCount: 1,
      initialPositions: new Float32Array(6),
      computeMs: 1,
    });
    expect(workers[1].terminated).toBe(false);
    workers[1].emit({
      type: "complete",
      requestId: workers[1].request!.requestId,
      identityKey: workers[1].request!.identityKey,
      communityIds: Uint32Array.from([0, 1, 1]),
      communityCount: 2,
      initialPositions: Float32Array.from([0, 0, 1, 0, 1, 1]),
      computeMs: 4,
    });
    expect(await second).toMatchObject({
      kind: "complete",
      identityKey: workspaceMapCommunityIdentityKey(secondColumns),
      communityIds: Uint32Array.from([0, 1, 1]),
      communityCount: 2,
      initialPositions: Float32Array.from([0, 0, 1, 0, 1, 1]),
    });
    expect(workers[1].terminated).toBe(true);
    job.destroy();
  });

  it("times out at 10 seconds and leaves original 20k/47k GPU columns attached", async () => {
    vi.useFakeTimers();
    const worker = new FakeWorker();
    const job = createWorkspaceMapCommunityJob({ workerFactory: () => worker });
    const input = columns(20_000, 47_000);
    const result = job.compute(input);
    expect(worker.request?.input.nodeKeys).toHaveLength(20_000);
    expect(worker.request?.input.edgeSources).toHaveLength(47_000);
    expect(worker.request?.input.edgeSources).not.toBe(input.edgeSources);
    expect(worker.transfer).toEqual([
      worker.request?.input.edgeSources.buffer,
      worker.request?.input.edgeTargets.buffer,
      worker.request?.input.edgeWeights.buffer,
    ]);
    expect(input.edgeSources.byteLength).toBe(47_000 * Uint32Array.BYTES_PER_ELEMENT);
    expect(input.edgeWeights.byteLength).toBe(47_000 * Float64Array.BYTES_PER_ELEMENT);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await result).toEqual({
      kind: "timeout",
      identityKey: workspaceMapCommunityIdentityKey(input),
    });
    expect(worker.terminated).toBe(true);
    job.destroy();
  });

  it("cancels on destroy and refuses future work", async () => {
    const worker = new FakeWorker();
    const job = createWorkspaceMapCommunityJob({ workerFactory: () => worker });
    const input = columns();
    const pending = job.compute(input);
    job.destroy();
    expect(await pending).toMatchObject({ kind: "cancelled" });
    expect(worker.terminated).toBe(true);
    expect(await job.compute(input)).toMatchObject({ kind: "cancelled" });
  });
});
