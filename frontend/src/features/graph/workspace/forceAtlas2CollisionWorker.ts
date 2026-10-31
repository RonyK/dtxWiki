import {
  snapshotWorkspaceForceAtlas2CollisionInput,
  snapshotWorkspaceForceAtlas2CollisionPositions,
  type WorkspaceForceAtlas2CollisionInput,
  type WorkspaceForceAtlas2CollisionResponse,
} from "./forceAtlas2Collision";

export interface WorkspaceForceAtlas2CollisionWorker {
  project(positions: Float32Array): Promise<Float32Array>;
  destroy(): void;
}

let nextGeneration = 0;

/** One scene's immutable collision worker and at most one transferable request. */
export function createWorkspaceForceAtlas2CollisionWorker(
  input: WorkspaceForceAtlas2CollisionInput,
): WorkspaceForceAtlas2CollisionWorker {
  const { radii, pins } = snapshotWorkspaceForceAtlas2CollisionInput(input);
  if (nextGeneration === Number.MAX_SAFE_INTEGER) throw new Error("Collision Worker generation exhausted");
  const generation = ++nextGeneration;
  const worker = new Worker(new URL("./forceAtlas2Collision.worker.ts", import.meta.url),
    { type: "module", name: "workspace-fa2-collision" });
  let nextRequestId = 0;
  let failure: Error | null = null;
  let pending: { requestId: number; positions: Float32Array;
    resolve(positions: Float32Array): void; reject(error: Error): void } | null = null;

  const close = (error: Error) => {
    if (failure) return;
    failure = error;
    worker.removeEventListener("message", onMessage);
    worker.removeEventListener("error", onError);
    worker.removeEventListener("messageerror", onError);
    worker.terminate();
    const request = pending;
    pending = null;
    request?.reject(error);
  };
  const onMessage = (event: MessageEvent<WorkspaceForceAtlas2CollisionResponse>) => {
    if (failure) return;
    const response = event.data;
    if (!response || response.generation !== generation) return;
    if (response.type === "error" && response.requestId === null) {
      close(new Error(response.message || "Collision Worker initialization failed"));
      return;
    }
    const request = pending;
    if (!request || response.requestId !== request.requestId) return;
    if (response.type === "error") { close(new Error(response.message || "Collision Worker projection failed")); return; }
    try {
      if (response.type !== "projected" || !(response.positions instanceof ArrayBuffer)
        || response.positions.byteLength !== radii.length * 2 * Float32Array.BYTES_PER_ELEMENT) {
        throw new Error("Collision Worker returned invalid indexed xy rows");
      }
      const positions = snapshotWorkspaceForceAtlas2CollisionPositions(new Float32Array(response.positions), radii.length);
      for (const pin of pins) {
        if (!Object.is(positions[pin * 2], request.positions[pin * 2])
          || !Object.is(positions[pin * 2 + 1], request.positions[pin * 2 + 1])) {
          throw new Error("Collision Worker moved an exact pinned row");
        }
      }
      pending = null;
      request.resolve(positions);
    } catch (error) { close(error instanceof Error ? error : new Error(String(error))); }
  };
  const onError = (event: Event) => {
    event.preventDefault();
    close(new Error(event instanceof ErrorEvent && event.message ? event.message : "Collision Worker failed to deliver rows"));
  };
  worker.addEventListener("message", onMessage);
  worker.addEventListener("error", onError);
  worker.addEventListener("messageerror", onError);
  const initialRadii = radii.slice();
  try { worker.postMessage({ type: "init", generation, radii: initialRadii.buffer, pins }, [initialRadii.buffer]); }
  catch (error) { close(error instanceof Error ? error : new Error(String(error))); throw failure; }

  return {
    project(positions) {
      if (failure) return Promise.reject(failure);
      if (pending) return Promise.reject(new Error("Collision Worker permits only one in-flight projection"));
      let rows: Float32Array;
      try { rows = snapshotWorkspaceForceAtlas2CollisionPositions(positions, radii.length); }
      catch (error) { return Promise.reject(error); }
      if (nextRequestId === Number.MAX_SAFE_INTEGER) return Promise.reject(new Error("Collision Worker request counter exhausted"));
      const requestId = ++nextRequestId, transferred = rows.slice();
      return new Promise<Float32Array>((resolve, reject) => {
        pending = { requestId, positions: rows, resolve, reject };
        try { worker.postMessage({ type: "project", generation, requestId, positions: transferred.buffer }, [transferred.buffer]); }
        catch (error) { close(error instanceof Error ? error : new Error(String(error))); }
      });
    },
    destroy() { close(new Error("Collision Worker has been destroyed")); },
  };
}
