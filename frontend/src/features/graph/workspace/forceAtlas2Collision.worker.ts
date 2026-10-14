import {
  projectWorkspaceForceAtlas2Collision,
  snapshotWorkspaceForceAtlas2CollisionInput,
  type WorkspaceForceAtlas2CollisionInput,
  type WorkspaceForceAtlas2CollisionRequest,
  type WorkspaceForceAtlas2CollisionResponse,
} from "./forceAtlas2Collision";

type WorkerScope = {
  onmessage: ((event: MessageEvent<WorkspaceForceAtlas2CollisionRequest>) => void) | null;
  postMessage(message: WorkspaceForceAtlas2CollisionResponse, transfer?: Transferable[]): void;
};
const scope = self as unknown as WorkerScope;
let input: WorkspaceForceAtlas2CollisionInput | null = null;
let generation: number | null = null;
let lastRequestId = 0;

scope.onmessage = event => {
  const request = event.data;
  if (!request || !Number.isSafeInteger(request.generation) || request.generation <= 0) return;
  if (generation !== null && request.generation !== generation) return;
  const requestId = request.type === "project" && Number.isSafeInteger(request.requestId) && request.requestId > 0
    ? request.requestId : null;
  try {
    if (request.type === "init") {
      if (input || !(request.radii instanceof ArrayBuffer)) throw new Error("Collision Worker initialization is immutable");
      input = snapshotWorkspaceForceAtlas2CollisionInput({ radii: new Float32Array(request.radii), pins: request.pins });
      generation = request.generation;
      return;
    }
    if (request.type !== "project" || requestId === null || !input || !(request.positions instanceof ArrayBuffer)) {
      throw new Error("Collision Worker requires initialized, indexed projection requests");
    }
    if (requestId <= lastRequestId) return;
    lastRequestId = requestId;
    const startedAt = performance.now();
    const positions = projectWorkspaceForceAtlas2Collision(new Float32Array(request.positions), input);
    scope.postMessage({ type: "projected", generation: request.generation, requestId,
      positions: positions.buffer as ArrayBuffer, durationMs: Math.max(0, performance.now() - startedAt) },
    [positions.buffer]);
  } catch (error) {
    scope.postMessage({ type: "error", generation: request.generation, requestId,
      message: error instanceof Error ? error.message : String(error) });
  }
};
