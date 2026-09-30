import { describe, expect, it } from "vitest";

import {
  boundedMediaUploadRetryDelayMs,
  createMediaUploadCoordinatorState,
  decideMediaUploadPolicyRepreflight,
  LEGACY_MEDIA_UPLOAD_BATCH_LIMITS,
  preflightMediaUploadBatch,
  reduceMediaUploadCoordinator,
  resolveMediaUploadBatchLimits,
  summarizeMediaUploads,
  summarizeMediaUploadBatches,
} from "../mediaUploadCoordinator";

const capabilities = {
  policy_revision: "policy-v1",
  uploadable_media_types: ["image/png"],
  max_upload_bytes: 8,
  max_files_per_batch: 2,
  max_batch_bytes: 12,
  max_parallel_uploads: 2,
};

function candidate(id: string, bytes: number, type = "image/png") {
  return { id, file: new File(["x".repeat(bytes)], `${id}.png`, { type }) };
}

describe("mediaUploadCoordinator", () => {
  it("uses legacy optional-field fallbacks only when fields are absent", () => {
    expect(resolveMediaUploadBatchLimits({
      policy_revision: "legacy-valid",
      uploadable_media_types: ["image/png"],
      max_upload_bytes: 20,
    })).toEqual({
      ...LEGACY_MEDIA_UPLOAD_BATCH_LIMITS,
      maxFileBytes: 20,
    });
    expect(() => resolveMediaUploadBatchLimits({
      ...capabilities,
      max_parallel_uploads: 0,
    })).toThrow("max_parallel_uploads");
  });

  it("preflights count, aggregate bytes, per-item size/type, and preserves order", () => {
    const count = preflightMediaUploadBatch(
      [candidate("a", 1), candidate("b", 1), candidate("c", 1)],
      capabilities,
    );
    expect(count.items.map((item) => [item.id, item.rejectionCode])).toEqual([
      ["a", "batch_file_count_exceeded"],
      ["b", "batch_file_count_exceeded"],
      ["c", "batch_file_count_exceeded"],
    ]);

    const mixed = preflightMediaUploadBatch(
      [candidate("too-big", 9), candidate("wrong-type", 1, "image/gif")],
      capabilities,
    );
    expect(mixed.items.map((item) => [item.id, item.rejectionCode])).toEqual([
      ["too-big", "file_too_large"],
      ["wrong-type", "unsupported_media_type"],
    ]);

    const bytes = preflightMediaUploadBatch(
      [candidate("a", 7), candidate("b", 6)],
      capabilities,
    );
    expect(bytes.rejected.every((item) => item.rejectionCode === "batch_bytes_exceeded")).toBe(true);
  });

  it("keeps progress indeterminate without wire bytes and derives exact counts", () => {
    const preflight = preflightMediaUploadBatch(
      [candidate("a", 3), candidate("b", 4)],
      capabilities,
    );
    let state = createMediaUploadCoordinatorState("batch-1", preflight);
    state = reduceMediaUploadCoordinator(state, {
      type: "item-status", id: "a", status: "uploading",
    });
    expect(summarizeMediaUploads(state).aggregateProgress).toEqual({ kind: "indeterminate" });
    state = reduceMediaUploadCoordinator(state, {
      type: "item-status", id: "a", status: "succeeded",
    });
    state = reduceMediaUploadCoordinator(state, {
      type: "item-status", id: "b", status: "outcome-unknown",
    });
    expect(summarizeMediaUploads(state)).toMatchObject({
      outcomeUnknownCount: 1,
      succeededCount: 1,
      unresolvedCount: 1,
    });
  });

  it("records policy refresh and replaces the batch only after re-preflight", () => {
    const first = preflightMediaUploadBatch([candidate("a", 2)], capabilities);
    let state = createMediaUploadCoordinatorState("batch-1", first);
    state = reduceMediaUploadCoordinator(state, {
      type: "policy-refresh-required", id: "a", stalePolicyRevision: "policy-v1",
    });
    expect(state).toMatchObject({ policyRefreshRequired: true, stalePolicyRevision: "policy-v1" });
    const refreshed = preflightMediaUploadBatch([candidate("a", 2)], {
      ...capabilities,
      policy_revision: "policy-v2",
    });
    state = reduceMediaUploadCoordinator(state, { type: "policy-refreshed", preflight: refreshed });
    expect(state).toMatchObject({ policyRefreshRequired: false, policyRevision: "policy-v2" });
  });

  it("replays only stale-policy failures after the original batch passes atomically", () => {
    const first = preflightMediaUploadBatch(
      [candidate("policy-failed", 2), candidate("other-failed", 2)],
      capabilities,
    );
    let state = createMediaUploadCoordinatorState("batch-1", first);
    state = reduceMediaUploadCoordinator(state, {
      type: "item-status", id: "policy-failed", status: "failed",
    });
    state = reduceMediaUploadCoordinator(state, {
      type: "item-status", id: "other-failed", status: "failed",
    });
    state = reduceMediaUploadCoordinator(state, {
      type: "policy-refresh-required",
      id: "policy-failed",
      stalePolicyRevision: "policy-v1",
    });
    const refreshed = preflightMediaUploadBatch(first.items, {
      ...capabilities,
      policy_revision: "policy-v2",
    });

    const decision = decideMediaUploadPolicyRepreflight(state, refreshed);

    expect(decision.replayItemIds).toEqual(["policy-failed"]);
    expect(decision.state).toMatchObject({
      policyRefreshRequired: false,
      policyRevision: "policy-v2",
      stalePolicyItemIds: [],
    });
    expect(decision.state.items.map((item) => item.status)).toEqual(["failed", "failed"]);
  });

  it("blocks every replay when fresh policy rejects any original-batch item", () => {
    const first = preflightMediaUploadBatch(
      [candidate("a", 2), candidate("b", 2)],
      capabilities,
    );
    let state = createMediaUploadCoordinatorState("batch-1", first);
    state = reduceMediaUploadCoordinator(state, {
      type: "item-status", id: "a", status: "failed",
    });
    state = reduceMediaUploadCoordinator(state, {
      type: "policy-refresh-required", id: "a", stalePolicyRevision: "policy-v1",
    });
    const rejected = preflightMediaUploadBatch(first.items, {
      ...capabilities,
      max_files_per_batch: 1,
      policy_revision: "policy-v2",
    });

    const decision = decideMediaUploadPolicyRepreflight(state, rejected);

    expect(decision.replayItemIds).toEqual([]);
    expect(decision.state.policyRejectionMessage).toContain("at most 1 file");
    expect(decision.state.stalePolicyItemIds).toEqual(["a"]);
  });

  it("aggregates count, bytes, and indeterminate progress across batches", () => {
    const first = createMediaUploadCoordinatorState(
      "batch-1",
      preflightMediaUploadBatch([candidate("a", 3)], capabilities),
    );
    let second = createMediaUploadCoordinatorState(
      "batch-2",
      preflightMediaUploadBatch([candidate("b", 4)], capabilities),
    );
    second = reduceMediaUploadCoordinator(second, {
      type: "item-status", id: "b", status: "uploading",
    });

    expect(summarizeMediaUploadBatches([first, second])).toMatchObject({
      activeCount: 1,
      aggregateProgress: { kind: "indeterminate" },
      totalBytes: 7,
      totalCount: 2,
      unresolvedCount: 2,
    });
  });

  it("bounds Retry-After and exponential fallback scheduling", () => {
    expect(boundedMediaUploadRetryDelayMs(120_000, 1)).toBe(60_000);
    expect(boundedMediaUploadRetryDelayMs(null, 3)).toBe(4_000);
  });
});
