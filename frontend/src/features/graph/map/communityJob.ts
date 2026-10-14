import type { WorkspaceMapColumns } from "./sceneColumns";
import type {
  WorkspaceMapCommunityRequest,
  WorkspaceMapCommunityResponse,
} from "./communityWorker";

export const WORKSPACE_MAP_COMMUNITY_TIMEOUT_MS = 10_000;

export type WorkspaceMapCommunityOutcome =
  | {
    kind: "complete";
    identityKey: string;
    communityIds: Uint32Array;
    communityCount: number;
    initialPositions: Float32Array;
    computeMs: number;
  }
  | {
    kind: "cancelled" | "timeout" | "error";
    identityKey: string;
  };

type CommunityWorker = {
  postMessage(message: WorkspaceMapCommunityRequest, transfer: Transferable[]): void;
  terminate(): void;
  onmessage: ((event: MessageEvent<WorkspaceMapCommunityResponse>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
};

type ActiveJob = {
  worker: CommunityWorker;
  requestId: number;
  identityKey: string;
  timeout: ReturnType<typeof setTimeout>;
  resolve: (outcome: WorkspaceMapCommunityOutcome) => void;
};

export type WorkspaceMapCommunityJob = {
  compute(columns: WorkspaceMapColumns): Promise<WorkspaceMapCommunityOutcome>;
  cancel(): void;
  destroy(): void;
};

export type WorkspaceMapCommunityJobOptions = {
  timeoutMs?: number;
  workerFactory?: () => CommunityWorker;
};

export function workspaceMapCommunityIdentityKey(columns: WorkspaceMapColumns): string {
  const identity = columns.identity;
  return JSON.stringify([
    identity.workspaceId,
    identity.actorId,
    identity.surface,
    identity.selectionKey,
    identity.visibleFingerprint,
  ]);
}

/**
 * One disposable worker per scene. Terminating the prior worker is necessary:
 * a synchronous Louvain run cannot handle an in-worker cancellation message.
 */
export function createWorkspaceMapCommunityJob(
  options: WorkspaceMapCommunityJobOptions = {},
): WorkspaceMapCommunityJob {
  const workerFactory = options.workerFactory
    ?? (() => new Worker(new URL("./communityWorker.ts", import.meta.url), { type: "module" }));
  const timeoutMs = options.timeoutMs ?? WORKSPACE_MAP_COMMUNITY_TIMEOUT_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new Error("Workspace Map community timeout must be positive.");
  }
  let active: ActiveJob | null = null;
  let nextRequestId = 0;
  let destroyed = false;

  function finish(job: ActiveJob, outcome: WorkspaceMapCommunityOutcome): void {
    if (active !== job) return;
    active = null;
    clearTimeout(job.timeout);
    job.worker.onmessage = null;
    job.worker.onerror = null;
    job.worker.terminate();
    job.resolve(outcome);
  }

  function cancel(): void {
    if (active) {
      finish(active, { kind: "cancelled", identityKey: active.identityKey });
    }
  }

  async function compute(columns: WorkspaceMapColumns): Promise<WorkspaceMapCommunityOutcome> {
    cancel();
    const identityKey = workspaceMapCommunityIdentityKey(columns);
    if (destroyed) return { kind: "cancelled", identityKey };

    let worker: CommunityWorker;
    try {
      worker = workerFactory();
    } catch {
      return { kind: "error", identityKey };
    }
    const requestId = ++nextRequestId;
    return new Promise<WorkspaceMapCommunityOutcome>((resolve) => {
      const job: ActiveJob = {
        worker,
        requestId,
        identityKey,
        timeout: setTimeout(() => finish(job, { kind: "timeout", identityKey }), timeoutMs),
        resolve,
      };
      active = job;
      worker.onmessage = (event) => {
        const response = event.data;
        if (active !== job || response?.requestId !== requestId
          || response.identityKey !== identityKey) return;
        if (response.type === "error") {
          finish(job, { kind: "error", identityKey });
          return;
        }
        if (response.type === "complete"
          && response.communityIds instanceof Uint32Array
          && response.communityIds.length === columns.nodeKeys.length
          && response.initialPositions instanceof Float32Array
          && response.initialPositions.length === columns.nodeKeys.length * 2
          && response.initialPositions.every(Number.isFinite)
          && Number.isInteger(response.communityCount)
          && response.communityCount >= 0
          && response.communityCount <= columns.nodeKeys.length
          && response.communityIds.every((id) => id < response.communityCount)
          // Cluster centers divide by member counts, so every declared group
          // must contain a member; range checks alone do not prevent NaN.
          && new Set(response.communityIds).size === response.communityCount
          && Number.isFinite(response.computeMs)
          && response.computeMs >= 0) {
          finish(job, {
            kind: "complete",
            identityKey,
            communityIds: response.communityIds,
            communityCount: response.communityCount,
            initialPositions: response.initialPositions,
            computeMs: response.computeMs,
          });
        } else {
          finish(job, { kind: "error", identityKey });
        }
      };
      worker.onerror = () => finish(job, { kind: "error", identityKey });
      const request: WorkspaceMapCommunityRequest = {
        type: "compute",
        requestId,
        identityKey,
        input: {
          workspaceId: columns.identity.workspaceId,
          nodeKeys: [...columns.nodeKeys],
          // Transfer copies: the Map's live GPU columns must stay attached.
          edgeSources: columns.edgeSources.slice(),
          edgeTargets: columns.edgeTargets.slice(),
          edgeWeights: columns.edgeWeights.slice(),
        },
      };
      try {
        worker.postMessage(request, [
          request.input.edgeSources.buffer,
          request.input.edgeTargets.buffer,
          request.input.edgeWeights.buffer,
        ]);
      } catch {
        finish(job, { kind: "error", identityKey });
      }
    });
  }

  return {
    compute,
    cancel,
    destroy() {
      destroyed = true;
      cancel();
    },
  };
}
