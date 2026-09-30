import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getContextReconciliation,
  reconcileContextBranch,
  type ContextReconciliationDetail,
} from "../../../shared/api/contextReconciliation";
import { discardAlternativeDraft } from "../../../shared/api/pages";
import { ContextReconciliationWorkbench } from "../ContextReconciliationWorkbench";

vi.mock("../../../shared/api/contextReconciliation", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getContextReconciliation: vi.fn(),
  reconcileContextBranch: vi.fn(),
}));
vi.mock("../../../shared/api/pages", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  discardAlternativeDraft: vi.fn(),
}));
vi.mock("../../pages/MarkdownView", () => ({
  MarkdownView: ({ body, renderPolicy }: { body: string; renderPolicy: string }) => (
    <div data-render-policy={renderPolicy}>{body}</div>
  ),
}));

const getDetailMock = vi.mocked(getContextReconciliation);
const reconcileMock = vi.mocked(reconcileContextBranch);
const discardMock = vi.mocked(discardAlternativeDraft);

describe("ContextReconciliationWorkbench", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getDetailMock.mockResolvedValue(reconciliationDetail());
    reconcileMock.mockResolvedValue({
      dry_run: true,
      preview: {
        head_version_id: "head-1",
        merged_body_markdown: "# Server merged preview",
        predicted_block_map: [],
        unresolved_count: 0,
      },
    });
  });

  it("shows base, HEAD and branch content and posts explicit block resolutions", async () => {
    const user = userEvent.setup();
    const onCompleted = vi.fn();
    renderWorkbench(onCompleted);

    expect(await screen.findByText("Current HEAD")).toBeInTheDocument();
    expect(screen.getByText("Base v1 → HEAD v3")).toBeInTheDocument();
    expect(screen.getByText("Branch v4")).toBeInTheDocument();
    expect(
      screen.getByText("Branch author: agent · Assistant"),
    ).toHaveAttribute("data-actor-kind", "agent");
    expect(screen.getByText("HEAD advanced 1 time")).toBeInTheDocument();
    expect(screen.getByText("HEAD intro")).toBeInTheDocument();
    expect(screen.getByText("Your intro")).toBeInTheDocument();
    expect(
      document.querySelectorAll(
        '.context-reconciliation-side-actor-kind[data-actor-kind="user"]',
      ),
    ).toHaveLength(3);
    expect(
      document.querySelectorAll(
        '.context-reconciliation-side-provenance time[datetime="2026-08-31T00:00:00Z"]',
      ),
    ).toHaveLength(3);
    await user.click(screen.getByText("Show common base"));
    expect(screen.getByText("Base intro")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Keep HEAD"));
    await user.click(screen.getByRole("button", { name: /Table row 2, conflict/i }));
    expect(screen.getByLabelText("Restore your branch")).toBeDisabled();
    await user.click(screen.getByLabelText("Write a new block"));
    expect(
      screen.getByRole("button", { name: "Create reconciled draft" }),
    ).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Start from HEAD" }));
    expect(screen.getByLabelText("Block Markdown")).toHaveValue("HEAD conclusion");
    expect(
      screen.getByRole("button", { name: "Start from your branch" }),
    ).toBeDisabled();
    await user.clear(screen.getByLabelText("Block Markdown"));
    await user.type(screen.getByLabelText("Block Markdown"), "Custom conclusion");
    await user.type(
      screen.getByLabelText("Reconciliation reason"),
      "Resolve both conflicts",
    );
    await user.click(screen.getByRole("button", { name: "Preview merged draft" }));

    await waitFor(() => expect(reconcileMock).toHaveBeenCalledTimes(1));
    expect(reconcileMock.mock.calls[0]?.[3]).toMatchObject({
      dry_run: true,
      expected_head_version_id: "head-1",
      reconciles: ["branch-1"],
      reason: "Resolve both conflicts",
      excluded_clean_change_ids: [],
      resolutions: [
        {
          block_id: "block-1",
          choice: "keep_head",
          expected_hash: "1".repeat(64),
          content: null,
        },
        {
          block_id: "block-2",
          choice: "custom",
          expected_hash: "2".repeat(64),
          content: "Custom conclusion",
        },
      ],
    });
    const preview = await screen.findByText("# Server merged preview");
    expect(preview).toHaveAttribute("data-render-policy", "untrusted-preview");

    reconcileMock.mockResolvedValueOnce({
      dry_run: false,
      commit: {
        draft_version_id: "draft-2",
        head_version_id: "head-1",
        branch_status: "superseded",
        review_item_id: null,
      },
    });
    await user.click(screen.getByRole("button", { name: "Create reconciled draft" }));
    await waitFor(() => expect(onCompleted).toHaveBeenCalledTimes(1));
    const previewKey = reconcileMock.mock.calls[0]?.[3].idempotency_key;
    const submitKey = reconcileMock.mock.calls[1]?.[3].idempotency_key;
    expect(previewKey).toMatch(/^[0-9a-f-]{36}$/);
    expect(submitKey).toMatch(/^[0-9a-f-]{36}$/);
    expect(submitKey).toBe(previewKey);
  });

  it("shows complete insert-after effects instead of only the first inserted block", async () => {
    const detail = reconciliationDetail();
    detail.conflicts[0].head_effects = [
      { op: "insert_after", content: "head gap one\n\nhead gap two" },
    ];
    detail.conflicts[0].yours_effects = [
      { op: "insert_after", content: "branch gap one\n\nbranch gap two" },
    ];
    getDetailMock.mockResolvedValue(detail);
    renderWorkbench();

    expect(
      await screen.findByText("Current blocks in this insertion gap"),
    ).toBeInTheDocument();
    expect(screen.getByText("Complete branch insertion")).toBeInTheDocument();
    expect(
      screen.getByText((_text, element) =>
        element?.tagName === "PRE"
        && element.textContent === "head gap one\n\nhead gap two"
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText((_text, element) =>
        element?.tagName === "PRE"
        && element.textContent === "branch gap one\n\nbranch gap two"
      ),
    ).toBeInTheDocument();
  });

  it("keeps one intent key across a failed preview transport retry", async () => {
    const user = userEvent.setup();
    const detail = reconciliationDetail();
    detail.conflicts = [];
    getDetailMock.mockResolvedValue(detail);
    reconcileMock
      .mockRejectedValueOnce(new Error("network unavailable"))
      .mockResolvedValueOnce({
        dry_run: true,
        preview: {
          head_version_id: "head-1",
          merged_body_markdown: "# Retried preview",
          predicted_block_map: [],
          unresolved_count: 0,
        },
      });
    renderWorkbench();

    await screen.findByText("Base v1 → HEAD v3");
    await user.type(screen.getByLabelText("Reconciliation reason"), "Clean merge");
    await user.click(screen.getByRole("button", { name: "Preview merged draft" }));
    await screen.findByText("Could not reconcile this branch.");
    await user.click(screen.getByRole("button", { name: "Preview merged draft" }));
    await screen.findByText("# Retried preview");

    expect(reconcileMock).toHaveBeenCalledTimes(2);
    expect(reconcileMock.mock.calls[1]?.[3].idempotency_key).toBe(
      reconcileMock.mock.calls[0]?.[3].idempotency_key,
    );
  });

  it("does not reapply choices when HEAD advances and requires an explicit refresh", async () => {
    const user = userEvent.setup();
    getDetailMock
      .mockResolvedValueOnce(reconciliationDetail())
      .mockResolvedValueOnce(reconciliationDetail("head-2"));
    renderWorkbench();

    await screen.findByText("Current HEAD");
    await user.click(screen.getByLabelText("Keep HEAD"));
    fireEvent.focus(window);

    expect(
      await screen.findByText(/HEAD advanced\. Refresh before choosing resolutions/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Create reconciled draft" }),
    ).toBeDisabled();
    expect(reconcileMock).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Refresh conflicts" }));
    await waitFor(() => expect(getDetailMock).toHaveBeenCalledTimes(3));
    expect(screen.getByText("0 of 2 conflicts resolved")).toBeInTheDocument();
  });

  it("preserves choices when a focus probe confirms the same HEAD", async () => {
    const user = userEvent.setup();
    renderWorkbench();

    await screen.findByText("Current HEAD");
    await user.click(screen.getByLabelText("Keep HEAD"));
    expect(screen.getByText("1 of 2 conflicts resolved")).toBeInTheDocument();

    fireEvent.focus(window);

    await waitFor(() => expect(getDetailMock).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("Keep HEAD")).toBeChecked();
    expect(screen.getByText("1 of 2 conflicts resolved")).toBeInTheDocument();
    expect(screen.queryByText(/HEAD advanced\. Refresh before choosing/i))
      .not.toBeInTheDocument();
  });

  it("allows excluding projected branch-only changes from a zero-conflict draft", async () => {
    const user = userEvent.setup();
    const detail = reconciliationDetail();
    detail.conflicts = [];
    detail.blocks = [
      {
        block_id: "branch-only",
        kind: "paragraph",
        granularity: "block",
        parent_block_id: null,
        row_index: null,
        state: "branch_only",
        label: "New note",
      },
      {
        block_id: "head-only",
        kind: "paragraph",
        granularity: "block",
        parent_block_id: null,
        row_index: null,
        state: "head_only",
        label: "HEAD note",
      },
    ];
    detail.clean_changes = [
      {
        block_id: "branch-only",
        resolution_target_id: null,
        kind: "paragraph",
        state: "branch_only",
        head: null,
        yours: {
          hash: "d".repeat(64),
          text: "New branch note",
          actor_kind: "agent",
          actor_display_name: "Assistant",
          at: "2026-08-31T00:00:00Z",
          version_id: "branch-1",
        },
      },
      {
        block_id: "head-only",
        resolution_target_id: "head-only",
        kind: "paragraph",
        state: "head_only",
        head: {
          hash: "e".repeat(64),
          text: "HEAD note",
          actor_kind: "user",
          actor_display_name: "Ada",
          at: "2026-08-31T00:00:00Z",
          version_id: "head-1",
        },
        yours: null,
      },
    ];
    getDetailMock.mockResolvedValue(detail);
    reconcileMock.mockResolvedValue({
      dry_run: false,
      commit: {
        draft_version_id: "draft-2",
        head_version_id: "head-1",
        branch_status: "superseded",
        review_item_id: null,
      },
    });
    const onCompleted = vi.fn();
    renderWorkbench(onCompleted);

    expect(await screen.findByText(/included in the reconciled draft by default/i))
      .toBeInTheDocument();
    const includeBranchChange = screen.getByRole("checkbox", {
      name: "Include New note",
    });
    expect(includeBranchChange).toBeChecked();
    expect(
      screen.queryByRole("checkbox", { name: "Include HEAD note" }),
    ).not.toBeInTheDocument();
    await user.click(includeBranchChange);
    expect(screen.getByLabelText("Reconciliation reason")).not.toBeRequired();
    expect(
      screen.getByRole("button", { name: "Create reconciled draft" }),
    ).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Create reconciled draft" }));

    await waitFor(() => expect(onCompleted).toHaveBeenCalledTimes(1));
    expect(reconcileMock.mock.calls[0]?.[3]).toMatchObject({
      dry_run: false,
      resolutions: [],
      excluded_clean_change_ids: ["branch-only"],
      reason: "Merge branch after verifying that no conflicts remain.",
    });
  });

  it("moves across the block strip by keyboard and has no basic accessibility violations", async () => {
    renderWorkbench();

    const first = await screen.findByRole("button", { name: /Intro, conflict/i });
    const second = screen.getByRole("button", { name: /Table row 2, conflict/i });
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowDown" });

    expect(second).toHaveFocus();
    expect(second).toHaveAttribute("aria-current", "true");
    expect((await axe(document.body)).violations).toEqual([]);
  });

  it("requires and sends an explicit discard reason", async () => {
    const user = userEvent.setup();
    discardMock.mockResolvedValue({ version_id: "branch-1" } as never);
    const onCompleted = vi.fn();
    renderWorkbench(onCompleted);

    await screen.findByText("Current HEAD");
    const discardSection = screen.getByRole("heading", { name: "Discard branch" })
      .closest("section");
    expect(discardSection).not.toBeNull();
    await user.click(
      within(discardSection as HTMLElement).getByRole("button", {
        name: "Discard branch",
      }),
    );
    const confirm = within(discardSection as HTMLElement).getByRole("button", {
      name: "Discard branch permanently",
    });
    expect(confirm).toBeDisabled();
    await user.type(
      within(discardSection as HTMLElement).getByLabelText("Discard reason"),
      "No longer needed",
    );
    await user.click(confirm);

    await waitFor(() =>
      expect(discardMock).toHaveBeenCalledWith("workspace-1", "page-1", "branch-1", {
        reason: "No longer needed",
      }),
    );
    expect(onCompleted).toHaveBeenCalledWith("discarded");
  });
  it("surfaces a failing onCompleted instead of dropping it (UI-02)", async () => {
    // onCompleted was called un-awaited, so the host's reload -- which also
    // rewrites the URL -- became an unhandled rejection the user never saw:
    // the workbench reported success while nothing had refreshed.
    const user = userEvent.setup();
    discardMock.mockResolvedValue({ version_id: "branch-1" } as never);
    const onCompleted = vi
      .fn()
      .mockRejectedValue(new Error("reload failed after discard"));
    renderWorkbench(onCompleted);

    await screen.findByText("Current HEAD");
    const discardSection = screen
      .getByRole("heading", { name: "Discard branch" })
      .closest("section");
    await user.click(
      within(discardSection as HTMLElement).getByRole("button", {
        name: "Discard branch",
      }),
    );
    await user.type(
      within(discardSection as HTMLElement).getByLabelText("Discard reason"),
      "No longer needed",
    );
    await user.click(
      within(discardSection as HTMLElement).getByRole("button", {
        name: "Discard branch permanently",
      }),
    );

    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("discarded"));
    expect(
      await screen.findByText("Could not discard this branch."),
    ).toBeInTheDocument();
  });
});

function renderWorkbench(onCompleted = vi.fn()) {
  return render(
    <ContextReconciliationWorkbench
      branchVersionId="branch-1"
      pageId="page-1"
      workspaceId="workspace-1"
      onClose={vi.fn()}
      onCompleted={onCompleted}
    />,
  );
}

function reconciliationDetail(
  headVersionId = "head-1",
): ContextReconciliationDetail {
  const side = (
    text: string | null,
    hash: string | null,
    versionId: string,
  ) => ({
    hash,
    text,
    actor_kind: "user",
    actor_display_name: "Ada",
    at: "2026-08-31T00:00:00Z",
    version_id: versionId,
  });
  const version = (versionId: string, versionNo: number) => ({
    version_id: versionId,
    version_no: versionNo,
    content_hash: `${versionId}-hash`,
    created_at: "2026-08-31T00:00:00Z",
  });
  return {
    page: { id: "page-1", slug: "decision", title: "Decision" },
    branch: {
      ...version("branch-1", 4),
      actor_kind: "agent",
      actor_display_name: "Assistant",
      base_version_id: "base-1",
    },
    base: version("base-1", 1),
    original_head: version("head-0", 2),
    head: version(headVersionId, 3),
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
      {
        block_id: "block-2",
        kind: "table_row",
        granularity: "table_row",
        parent_block_id: "table-1",
        row_index: 1,
        state: "conflict",
        label: "Table row 2",
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
        base: side("Base intro", "a".repeat(64), "base-1"),
        head: side("HEAD intro", "1".repeat(64), headVersionId),
        yours: side("Your intro", "b".repeat(64), "branch-1"),
        head_effects: [],
        yours_effects: [],
        identity_retired: false,
        restore_yours_available: true,
      },
      {
        block_id: "block-2",
        resolution_target_id: "block-2",
        kind: "table_row",
        granularity: "table_row",
        parent_block_id: "table-1",
        row_index: 1,
        line_span: [3, 4],
        base: side("Base conclusion", "c".repeat(64), "base-1"),
        head: side("HEAD conclusion", "2".repeat(64), headVersionId),
        yours: side(null, null, "branch-1"),
        head_effects: [],
        yours_effects: [],
        identity_retired: true,
        restore_yours_available: false,
      },
    ],
    clean_changes: [],
  };
}
