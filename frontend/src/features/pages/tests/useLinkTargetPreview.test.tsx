import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { previewLinkTargets, type LinkTargetPreview } from "../../../shared/api/linkTargets";
import { useLinkTargetPreview } from "../useLinkTargetPreview";

vi.mock("../../../shared/api/linkTargets", () => ({ previewLinkTargets: vi.fn() }));

const input = {
  actorId: "reader",
  body: "[Target](dtxwiki://index/context/11111111-1111-4111-8111-111111111111)  \nNext",
  enabled: true,
  sourcePageId: "source",
  sourceVersionId: "version-1",
  workspaceId: "workspace-1",
};
const targets = [{ locator: "copied-ref", href: "/p/workspace-1/target", target_page_id: "target", state: "ok" as const }];

describe("useLinkTargetPreview", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(previewLinkTargets).mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("discards late responses for a different exact buffer and leaves the authored buffer untouched", async () => {
    const pending: Array<(preview: LinkTargetPreview) => void> = [];
    vi.mocked(previewLinkTargets).mockImplementation(() => new Promise(resolve => pending.push(resolve)));
    const { result, rerender } = renderHook(props => useLinkTargetPreview(props), { initialProps: input });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    const first = vi.mocked(previewLinkTargets).mock.calls[0];
    expect(first[1].body_md).toBe(input.body);

    // These bodies have equal normalized content_hash values, but only the
    // first has a Markdown hard break. Request identity must keep them apart.
    const next = { ...input, body: input.body.replace("  \n", "\n") };
    rerender(next);
    expect(first[2]?.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    const second = vi.mocked(previewLinkTargets).mock.calls[1];
    expect(second[1].body_md).toBe(next.body);
    expect(second[1].request_id).not.toBe(first[1].request_id);

    await act(async () => pending[1]({ ...second[1], workspace_id: next.workspaceId, link_targets: targets }));
    expect(result.current).toEqual(targets);
    await act(async () => pending[0]({ ...first[1], workspace_id: input.workspaceId, link_targets: [] }));
    expect(result.current).toEqual(targets);
    expect(next.body).toBe(input.body.replace("  \n", "\n"));
  });

  it("clears previous targets on source, workspace and reader changes and rejects mismatched source receipts", async () => {
    const pending: Array<(preview: LinkTargetPreview) => void> = [];
    vi.mocked(previewLinkTargets).mockImplementation(() => new Promise(resolve => pending.push(resolve)));
    const { result, rerender } = renderHook(props => useLinkTargetPreview(props), { initialProps: input });
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    const first = vi.mocked(previewLinkTargets).mock.calls[0];
    await act(async () => pending[0]({ ...first[1], workspace_id: input.workspaceId, link_targets: targets }));
    expect(result.current).toEqual(targets);

    const next = { ...input, actorId: "other-reader", sourceVersionId: "version-2", workspaceId: "workspace-2" };
    rerender(next);
    expect(result.current).toEqual([]);
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    const second = vi.mocked(previewLinkTargets).mock.calls[1];
    await act(async () => pending[1]({
      ...second[1], source_version_id: input.sourceVersionId,
      workspace_id: next.workspaceId, link_targets: targets,
    }));
    expect(result.current).toEqual([]);
    rerender({ ...next, enabled: false });
    expect(second[2]?.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(previewLinkTargets).toHaveBeenCalledTimes(2);
  });
});
