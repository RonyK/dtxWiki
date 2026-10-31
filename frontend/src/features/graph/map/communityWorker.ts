import {
  computeWorkspaceMapCommunities,
  type WorkspaceMapCommunityInput,
} from "./communityLayout";

export type WorkspaceMapCommunityRequest = {
  type: "compute";
  requestId: number;
  identityKey: string;
  input: WorkspaceMapCommunityInput;
};

export type WorkspaceMapCommunityResponse =
  | {
    type: "complete";
    requestId: number;
    identityKey: string;
    communityIds: Uint32Array;
    communityCount: number;
    initialPositions: Float32Array;
    computeMs: number;
  }
  | {
    type: "error";
    requestId: number;
    identityKey: string;
  };

type WorkerScope = {
  onmessage: ((event: MessageEvent<WorkspaceMapCommunityRequest>) => void) | null;
  postMessage(message: WorkspaceMapCommunityResponse, transfer?: Transferable[]): void;
};

const scope = self as unknown as WorkerScope;

scope.onmessage = (event) => {
  const request = event.data;
  if (request?.type !== "compute") return;
  try {
    const startedAt = performance.now();
    const result = computeWorkspaceMapCommunities(request.input);
    scope.postMessage({
      type: "complete",
      requestId: request.requestId,
      identityKey: request.identityKey,
      communityIds: result.communityIds,
      communityCount: result.communityCount,
      initialPositions: result.initialPositions,
      computeMs: performance.now() - startedAt,
    }, [result.communityIds.buffer, result.initialPositions.buffer]);
  } catch {
    // Avoid echoing actor data or node keys in a cross-thread error message.
    scope.postMessage({
      type: "error",
      requestId: request.requestId,
      identityKey: request.identityKey,
    });
  }
};
