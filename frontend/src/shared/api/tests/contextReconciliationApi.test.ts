import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getContextReconciliation,
  reconcileContextBranch,
  type ContextReconciliationDetail,
} from "../contextReconciliation";
import { discardReviewReconciliationsBulk } from "../operations";
import { discardAlternativeDraft } from "../pages";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

describe("context reconciliation api", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("reads the server-owned block projection", async () => {
    const detail = reconciliationDetail();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ data: detail }));

    await expect(
      getContextReconciliation(WORKSPACE_ID, "page-1", "branch-1"),
    ).resolves.toEqual(detail);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/page-1/versions/branch-1/reconciliation`,
    );
  });

  it.each([true, false])(
    "posts dry_run=%s through the same reconciliation route",
    async (dryRun) => {
      const fetchMock = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValue(
          jsonResponse({
            data: dryRun
              ? {
                  dry_run: true,
                  preview: {
                    head_version_id: "head-1",
                    merged_body_markdown: "# Preview",
                    predicted_block_map: [],
                    unresolved_count: 0,
                  },
                }
              : {
                  dry_run: false,
                  commit: {
                    draft_version_id: "draft-2",
                    head_version_id: "head-1",
                    branch_status: "superseded",
                    review_item_id: null,
                  },
                },
          }),
        );
      const request = {
        expected_head_version_id: "head-1",
        reconciles: ["branch-1"] as [string],
        resolutions: [
          {
            block_id: "block-1",
            choice: "keep_head" as const,
            expected_hash: "hash-head",
            content: null,
          },
        ],
        excluded_clean_change_ids: ["branch-only-1"],
        reason: "Resolve the conflict",
        idempotency_key: "00000000-0000-4000-8000-000000000001",
        dry_run: dryRun,
      };

      await reconcileContextBranch(WORKSPACE_ID, "page-1", "branch-1", request);

      expect(fetchMock).toHaveBeenCalledWith(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/page-1/versions/branch-1/reconciliation`,
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify(request),
        }),
      );
    },
  );

  it("requires a reason when discarding one alternative branch", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: { version_id: "branch-1" } }),
    );

    await discardAlternativeDraft(WORKSPACE_ID, "page-1", "branch-1", {
      reason: "Obsolete branch",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/context/pages/page-1/versions/branch-1/discard`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ reason: "Obsolete branch" }),
      }),
    );
  });

  it("posts bulk reconciliation discard IDs and reason", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        data: {
          discarded_review_item_ids: ["review-1"],
          skipped: [{ review_item_id: "review-2", error_code: "forbidden" }],
        },
      }),
    );

    const result = await discardReviewReconciliationsBulk("workspace-1", {
      review_item_ids: ["review-1", "review-2"],
      reason: "Superseded experiments",
    });

    expect(result.skipped[0]?.error_code).toBe("forbidden");
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/workspace-1/reviews/reconciliations/discard-bulk`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          review_item_ids: ["review-1", "review-2"],
          reason: "Superseded experiments",
        }),
      }),
    );
  });
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function reconciliationDetail(): ContextReconciliationDetail {
  const side = {
    hash: "hash",
    text: "text",
    actor_kind: "user",
    actor_display_name: "Ada",
    at: "2026-08-31T00:00:00Z",
    version_id: "version-1",
  };
  return {
    page: { id: "page-1", slug: "decision", title: "Decision" },
    branch: {
      version_id: "branch-1",
      version_no: 3,
      content_hash: "branch-hash",
      created_at: "2026-08-31T00:00:00Z",
      actor_kind: "agent",
      actor_display_name: "Assistant",
      base_version_id: "base-1",
    },
    base: {
      version_id: "base-1",
      version_no: 1,
      content_hash: "base-hash",
      created_at: "2026-08-30T00:00:00Z",
    },
    original_head: {
      version_id: "head-0",
      version_no: 1,
      content_hash: "old-hash",
      created_at: "2026-08-30T00:00:00Z",
    },
    head: {
      version_id: "head-1",
      version_no: 2,
      content_hash: "head-hash",
      created_at: "2026-08-31T00:00:00Z",
    },
    head_advance_count: 1,
    blocks: [
      {
        block_id: "block-1",
        kind: "paragraph",
        granularity: "block",
        parent_block_id: null,
        row_index: null,
        state: "conflict",
        label: "Intro",
      },
    ],
    conflicts: [
      {
        block_id: "block-1",
        resolution_target_id: "block-1",
        kind: "paragraph",
        granularity: "block",
        parent_block_id: null,
        row_index: null,
        line_span: [1, 2],
        base: side,
        head: { ...side, hash: "hash-head", text: "HEAD text" },
        yours: { ...side, hash: "hash-yours", text: "Your text" },
        head_effects: [],
        yours_effects: [],
        identity_retired: false,
        restore_yours_available: true,
      },
    ],
    clean_changes: [],
  };
}
