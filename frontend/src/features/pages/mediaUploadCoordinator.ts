import type { ResilientUploadQueueStatus } from "../../shared/media/uploadQueue";

export const LEGACY_MEDIA_UPLOAD_BATCH_LIMITS = Object.freeze({
  maxBatchBytes: 10 * 1024 * 1024,
  maxFilesPerBatch: 1,
  maxParallelUploads: 1,
});

export const MAX_MEDIA_UPLOAD_RETRY_DELAY_MS = 60_000;

export type MediaUploadBatchCapabilities = {
  max_batch_bytes?: number;
  max_files_per_batch?: number;
  max_parallel_uploads?: number;
  max_upload_bytes: number;
  policy_revision: string;
  uploadable_media_types: readonly string[];
};

export type MediaUploadBatchLimits = {
  maxBatchBytes: number;
  maxFileBytes: number;
  maxFilesPerBatch: number;
  maxParallelUploads: number;
};

export type MediaUploadCandidate = {
  file: File;
  id: string;
  validationError?: string | null;
};

export type MediaUploadRejectionCode =
  | "batch_bytes_exceeded"
  | "batch_file_count_exceeded"
  | "empty_file"
  | "file_too_large"
  | "unsupported_media_type"
  | "validation_failed";

export type MediaUploadPreflightItem = MediaUploadCandidate & {
  rejectionCode?: MediaUploadRejectionCode;
  rejectionMessage?: string;
};

export type MediaUploadPreflight = {
  accepted: MediaUploadPreflightItem[];
  items: MediaUploadPreflightItem[];
  limits: MediaUploadBatchLimits;
  policyRevision: string;
  rejected: MediaUploadPreflightItem[];
  totalBytes: number;
};

export type MediaUploadProgress =
  | { kind: "not-started" }
  | { kind: "indeterminate" }
  | { kind: "bytes"; loaded: number; total: number };

export type MediaUploadCoordinatorItem = MediaUploadPreflightItem & {
  message?: string;
  progress: MediaUploadProgress;
  status: ResilientUploadQueueStatus | "rejected";
};

export type MediaUploadCoordinatorState = {
  batchId: string;
  items: MediaUploadCoordinatorItem[];
  policyRejectionMessage: string | null;
  policyRefreshRequired: boolean;
  policyRevision: string;
  stalePolicyItemIds: string[];
  stalePolicyRevision: string | null;
};

export type MediaUploadCoordinatorAction =
  | { type: "item-status"; id: string; status: ResilientUploadQueueStatus; message?: string }
  | { type: "item-progress"; id: string; loaded: number; total: number }
  | { type: "remove-item"; id: string }
  | { type: "policy-refresh-required"; id: string; stalePolicyRevision: string }
  | { type: "policy-refreshed"; preflight: MediaUploadPreflight }
  | { type: "policy-refresh-failed"; message: string }
  | {
      type: "policy-repreflight-rejected";
      message: string;
      preflight: MediaUploadPreflight;
    };

export type MediaUploadSummary = {
  activeCount: number;
  aggregateProgress: MediaUploadProgress;
  cancelledCount: number;
  failedCount: number;
  outcomeUnknownCount: number;
  policyRefreshingBatchCount: number;
  policyRefreshFailedBatchCount: number;
  policyRejectedBatchCount: number;
  queuedCount: number;
  rejectedCount: number;
  succeededCount: number;
  totalBytes: number;
  totalCount: number;
  unresolvedCount: number;
};

export function resolveMediaUploadBatchLimits(
  capabilities: MediaUploadBatchCapabilities,
): MediaUploadBatchLimits {
  if (!capabilities.policy_revision.trim() ||
      !isPositiveInteger(capabilities.max_upload_bytes) ||
      !Array.isArray(capabilities.uploadable_media_types)) {
    throw new Error("Media upload capabilities are not current or valid.");
  }
  return {
    maxBatchBytes: optionalPositiveInteger(
      capabilities.max_batch_bytes,
      LEGACY_MEDIA_UPLOAD_BATCH_LIMITS.maxBatchBytes,
      "max_batch_bytes",
    ),
    maxFileBytes: capabilities.max_upload_bytes,
    maxFilesPerBatch: optionalPositiveInteger(
      capabilities.max_files_per_batch,
      LEGACY_MEDIA_UPLOAD_BATCH_LIMITS.maxFilesPerBatch,
      "max_files_per_batch",
    ),
    maxParallelUploads: optionalPositiveInteger(
      capabilities.max_parallel_uploads,
      LEGACY_MEDIA_UPLOAD_BATCH_LIMITS.maxParallelUploads,
      "max_parallel_uploads",
    ),
  };
}

export function preflightMediaUploadBatch(
  candidates: readonly MediaUploadCandidate[],
  capabilities: MediaUploadBatchCapabilities,
): MediaUploadPreflight {
  const limits = resolveMediaUploadBatchLimits(capabilities);
  const totalBytes = candidates.reduce((total, candidate) => total + candidate.file.size, 0);
  const batchRejection = candidates.length > limits.maxFilesPerBatch
    ? ["batch_file_count_exceeded", `Choose at most ${limits.maxFilesPerBatch} file(s).`] as const
    : totalBytes > limits.maxBatchBytes
      ? ["batch_bytes_exceeded", `The selected files exceed the ${limits.maxBatchBytes}-byte batch limit.`] as const
      : null;
  const allowedTypes = new Set(
    capabilities.uploadable_media_types.map((entry) => entry.trim().toLowerCase()),
  );
  const items = candidates.map<MediaUploadPreflightItem>((candidate) => {
    const rejection = batchRejection ?? rejectionForCandidate(candidate, limits, allowedTypes);
    return rejection
      ? { ...candidate, rejectionCode: rejection[0], rejectionMessage: rejection[1] }
      : { ...candidate };
  });
  return {
    accepted: items.filter((item) => !item.rejectionCode),
    items,
    limits,
    policyRevision: capabilities.policy_revision,
    rejected: items.filter((item) => Boolean(item.rejectionCode)),
    totalBytes,
  };
}

export function createMediaUploadCoordinatorState(
  batchId: string,
  preflight: MediaUploadPreflight,
): MediaUploadCoordinatorState {
  return {
    batchId,
    items: preflight.items.map((item) => ({
      ...item,
      progress: { kind: "not-started" },
      status: item.rejectionCode ? "rejected" : "queued",
    })),
    policyRejectionMessage: null,
    policyRefreshRequired: false,
    policyRevision: preflight.policyRevision,
    stalePolicyItemIds: [],
    stalePolicyRevision: null,
  };
}

export function reduceMediaUploadCoordinator(
  state: MediaUploadCoordinatorState,
  action: MediaUploadCoordinatorAction,
): MediaUploadCoordinatorState {
  if (action.type === "remove-item") {
    const stalePolicyItemIds = state.stalePolicyItemIds.filter(
      (id) => id !== action.id,
    );
    return {
      ...state,
      items: state.items.filter((item) => item.id !== action.id),
      policyRejectionMessage: stalePolicyItemIds.length > 0
        ? state.policyRejectionMessage
        : null,
      policyRefreshRequired: stalePolicyItemIds.length > 0
        ? state.policyRefreshRequired
        : false,
      stalePolicyItemIds,
      stalePolicyRevision: stalePolicyItemIds.length > 0
        ? state.stalePolicyRevision
        : null,
    };
  }
  if (action.type === "policy-refresh-required") {
    return {
      ...state,
      policyRefreshRequired: true,
      stalePolicyItemIds: state.stalePolicyItemIds.includes(action.id)
        ? state.stalePolicyItemIds
        : [...state.stalePolicyItemIds, action.id],
      stalePolicyRevision: action.stalePolicyRevision,
    };
  }
  if (action.type === "policy-refreshed") {
    return mergePolicyPreflight(state, action.preflight, null);
  }
  if (action.type === "policy-repreflight-rejected") {
    return mergePolicyPreflight(state, action.preflight, action.message);
  }
  if (action.type === "policy-refresh-failed") {
    return {
      ...state,
      policyRejectionMessage: action.message,
      policyRefreshRequired: false,
    };
  }
  return {
    ...state,
    items: state.items.map((item) => {
      if (item.id !== action.id || item.status === "rejected") return item;
      if (action.type === "item-progress") {
        return {
          ...item,
          progress: {
            kind: "bytes",
            loaded: boundedBytes(action.loaded, action.total),
            total: Math.max(0, action.total),
          },
        };
      }
      return {
        ...item,
        message: action.message,
        progress: progressForStatus(action.status, item),
        status: action.status,
      };
    }),
  };
}

export function summarizeMediaUploads(
  state: MediaUploadCoordinatorState,
): MediaUploadSummary {
  return summarizeMediaUploadBatches([state]);
}

export function summarizeMediaUploadBatches(
  states: readonly MediaUploadCoordinatorState[],
): MediaUploadSummary {
  const items = states.flatMap((state) => state.items);
  const accepted = items.filter((item) => item.status !== "rejected");
  const activeWithoutWireProgress = accepted.some((item) =>
    (item.status === "uploading" || item.status === "cancelling" ||
      item.status === "outcome-unknown") && item.progress.kind !== "bytes");
  const loaded = accepted.reduce((total, item) => {
    if (item.status === "succeeded") return total + item.file.size;
    return total + (item.progress.kind === "bytes" ? item.progress.loaded : 0);
  }, 0);
  const progressTotalBytes = accepted.reduce((total, item) => total + item.file.size, 0);
  const totalBytes = items.reduce((total, item) => total + item.file.size, 0);
  const aggregateProgress: MediaUploadProgress = activeWithoutWireProgress
    ? { kind: "indeterminate" }
    : loaded > 0
      ? { kind: "bytes", loaded, total: progressTotalBytes }
      : { kind: "not-started" };
  const unresolvedCount = accepted.filter((item) => item.status !== "succeeded").length;
  return {
    activeCount: accepted.filter((item) =>
      item.status === "uploading" || item.status === "cancelling").length,
    aggregateProgress,
    cancelledCount: accepted.filter((item) => item.status === "cancelled").length,
    failedCount: accepted.filter((item) => item.status === "failed").length,
    outcomeUnknownCount: accepted.filter((item) => item.status === "outcome-unknown").length,
    policyRefreshingBatchCount: states.filter((state) => state.policyRefreshRequired).length,
    policyRefreshFailedBatchCount: states.filter((state) =>
      state.policyRejectionMessage && state.policyRevision === state.stalePolicyRevision).length,
    policyRejectedBatchCount: states.filter((state) =>
      state.policyRejectionMessage && state.policyRevision !== state.stalePolicyRevision).length,
    queuedCount: accepted.filter((item) => item.status === "queued").length,
    rejectedCount: items.length - accepted.length,
    succeededCount: accepted.filter((item) => item.status === "succeeded").length,
    totalBytes,
    totalCount: items.length,
    unresolvedCount,
  };
}

export function decideMediaUploadPolicyRepreflight(
  state: MediaUploadCoordinatorState,
  preflight: MediaUploadPreflight,
): { replayItemIds: string[]; state: MediaUploadCoordinatorState } {
  const originalIds = state.items.map((item) => item.id);
  const refreshedIds = preflight.items.map((item) => item.id);
  if (originalIds.length !== refreshedIds.length ||
      originalIds.some((id, index) => id !== refreshedIds[index])) {
    throw new Error("Policy re-preflight must cover the original batch in marker order.");
  }
  if (preflight.rejected.length > 0) {
    const rejection = preflight.rejected[0];
    const message = rejection?.rejectionMessage ??
      "The original batch no longer meets the current upload policy.";
    return {
      replayItemIds: [],
      state: reduceMediaUploadCoordinator(state, {
        type: "policy-repreflight-rejected",
        message,
        preflight,
      }),
    };
  }
  const staleIds = new Set(state.stalePolicyItemIds);
  return {
    replayItemIds: state.items
      .filter((item) => staleIds.has(item.id) && item.status === "failed")
      .map((item) => item.id),
    state: reduceMediaUploadCoordinator(state, { type: "policy-refreshed", preflight }),
  };
}

function mergePolicyPreflight(
  state: MediaUploadCoordinatorState,
  preflight: MediaUploadPreflight,
  policyRejectionMessage: string | null,
): MediaUploadCoordinatorState {
  const currentById = new Map(state.items.map((item) => [item.id, item]));
  return {
    ...state,
    items: preflight.items.map((item) => {
      const current = currentById.get(item.id);
      return current
        ? {
            ...current,
            file: item.file,
            rejectionCode: item.rejectionCode,
            rejectionMessage: item.rejectionMessage,
            validationError: item.validationError,
          }
        : {
            ...item,
            progress: { kind: "not-started" },
            status: item.rejectionCode ? "rejected" : "queued",
          };
    }),
    policyRejectionMessage,
    policyRefreshRequired: false,
    policyRevision: preflight.policyRevision,
    stalePolicyItemIds: policyRejectionMessage ? state.stalePolicyItemIds : [],
    stalePolicyRevision: policyRejectionMessage ? state.stalePolicyRevision : null,
  };
}

export function boundedMediaUploadRetryDelayMs(
  retryAfterMs: number | null,
  attempt: number,
  maximumMs = MAX_MEDIA_UPLOAD_RETRY_DELAY_MS,
) {
  const exponential = 1_000 * (2 ** Math.max(0, Math.min(10, attempt - 1)));
  const requested = retryAfterMs === null ? exponential : retryAfterMs;
  return Math.min(Math.max(0, Math.trunc(requested)), maximumMs);
}

export function isMediaPolicyChangedError(error: unknown) {
  return typeof error === "object" && error !== null && "body" in error &&
    (error as { body?: { error_code?: unknown } }).body?.error_code === "media_policy_changed";
}

function rejectionForCandidate(
  candidate: MediaUploadCandidate,
  limits: MediaUploadBatchLimits,
  allowedTypes: ReadonlySet<string>,
): readonly [MediaUploadRejectionCode, string] | null {
  if (candidate.validationError) return ["validation_failed", candidate.validationError];
  if (candidate.file.size === 0) return ["empty_file", "Empty files cannot be uploaded."];
  if (candidate.file.size > limits.maxFileBytes) {
    return ["file_too_large", `This file exceeds the ${limits.maxFileBytes}-byte upload limit.`];
  }
  if (!allowedTypes.has(candidate.file.type.trim().toLowerCase())) {
    return ["unsupported_media_type", "This file type is not supported."];
  }
  return null;
}

function progressForStatus(
  status: ResilientUploadQueueStatus,
  item: MediaUploadCoordinatorItem,
): MediaUploadProgress {
  if (status === "succeeded") return { kind: "bytes", loaded: item.file.size, total: item.file.size };
  if (status === "uploading" || status === "cancelling" || status === "outcome-unknown") {
    return item.progress.kind === "bytes" ? item.progress : { kind: "indeterminate" };
  }
  return { kind: "not-started" };
}

function optionalPositiveInteger(value: number | undefined, fallback: number, name: string) {
  if (value === undefined) return fallback;
  if (!isPositiveInteger(value)) throw new Error(`${name} must be a positive integer.`);
  return value;
}

function isPositiveInteger(value: number) {
  return Number.isSafeInteger(value) && value > 0;
}

function boundedBytes(loaded: number, total: number) {
  return Math.min(Math.max(0, loaded), Math.max(0, total));
}
