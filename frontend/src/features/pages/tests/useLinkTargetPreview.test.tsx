import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { previewLinkTargets, type LinkTargetPreview } from "../../../shared/api/linkTargets";
import { LINK_CHECK_IDLE_MS, useLinkTargetPreview } from "../useLinkTargetPreview";

vi.mock("../../../shared/api/linkTargets", () => ({ previewLinkTargets: vi.fn() }));

const ref = "dtxwiki://index/context/11111111-1111-4111-8111-111111111111";
const added = "dtxwiki://index/library/22222222-2222-4222-8222-222222222222";
const opened = { locator: "/p/workspace-1/opened", href: "/p/workspace-1/opened", state: "ok" as const };
const confirmed = { locator: ref, href: "/p/workspace-1/target", target_page_id: "target", state: "ok" as const };
const input = {
  actorId: "reader",
  body: `[Opened](${opened.locator}) [Copied](${ref})`,
  enabled: true,
  initialTargets: [opened],
  sourcePageId: "source",
  sourceVersionId: "version-1",
  workspaceId: "workspace-1",
};

function answer(request: Parameters<typeof previewLinkTargets>[1], link_targets: LinkTargetPreview["link_targets"]) {
  return { ...request, workspace_id: "workspace-1", link_targets } as LinkTargetPreview;
}

describe("useLinkTargetPreview", () => {
  it("checks an explicit citation once and returns its reader projection without sending the document", async () => {
    const onReferences = vi.fn();
    vi.mocked(previewLinkTargets).mockImplementation(async (_workspace, request) => ({
      ...answer(request, [confirmed]),
      references: [{ target_kind: "context_page", target_id: "11111111-1111-4111-8111-111111111111", number: 1,
        title: "Source title", href: confirmed.href, state: "available", inline_count: 1, metadata_present: false,
        occurrences: [{ href: ref, start: 0, end: ref.length + 7 }] }],
    }));
    const props = { ...input, citationLocators: [ref], onReferences };
    const { result, rerender } = renderHook((value) => useLinkTargetPreview(value), { initialProps: props });
    act(() => result.current.reportLinks([ref]));
    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    expect(vi.mocked(previewLinkTargets).mock.calls[0][1].body_md).toBe(`[x](${ref})\n@cite{${ref}}`);
    expect(onReferences).toHaveBeenCalledWith([expect.objectContaining({ title: "Source title" })]);
    rerender({ ...props, body: `${input.body}\nMore writing` });
    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    expect(previewLinkTargets).toHaveBeenCalledTimes(1);
  });

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(previewLinkTargets).mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("checks only unconfirmed links after an idle second and keeps confirmed ones stable", async () => {
    const pending: Array<(preview: LinkTargetPreview) => void> = [];
    vi.mocked(previewLinkTargets).mockImplementation(() => new Promise(resolve => pending.push(resolve)));
    const { result, rerender } = renderHook(props => useLinkTargetPreview(props), { initialProps: input });
    act(() => result.current.reportLinks([opened.locator, ref]));
    // The opened body's links come from the page read; only the copied ref waits.
    expect(result.current.targets).toEqual([opened]);
    expect([...result.current.checking]).toEqual([ref]);

    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS - 1); });
    expect(previewLinkTargets).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    const [, request] = vi.mocked(previewLinkTargets).mock.calls[0];
    // Only the unconfirmed link is sent, never the document.
    expect(request).toMatchObject({ body_md: `[x](${ref})`, source_version_id: "version-1" });

    await act(async () => pending[0](answer(request, [confirmed])));
    expect(result.current.targets).toEqual([opened, confirmed]);
    expect(result.current.checking.size).toBe(0);

    // Typing elsewhere keeps both results as they are and asks nothing again.
    const stable = result.current.targets;
    rerender({ ...input, body: `${input.body} more text` });
    act(() => result.current.reportLinks([opened.locator, ref]));
    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    expect(result.current.targets).toBe(stable);
    expect(previewLinkTargets).toHaveBeenCalledTimes(1);

    // A new source version abandons a pending check, whose pins belonged to
    // the old version, and asks again; confirmed links stay without flicker.
    const withAdded = { ...input, body: `${input.body} [Added](${added})` };
    rerender(withAdded);
    act(() => result.current.reportLinks([opened.locator, ref, added]));
    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    const [, oldVersionRequest, oldVersionSignal] = vi.mocked(previewLinkTargets).mock.calls[1];
    rerender({ ...withAdded, sourceVersionId: "version-2" });
    expect(oldVersionSignal?.aborted).toBe(true);
    await act(async () => pending[1](answer(oldVersionRequest, [{ locator: added, href: "/old-pin", state: "ok" }])));
    expect(result.current.targets).toBe(stable);
    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    expect(vi.mocked(previewLinkTargets).mock.calls[2][1]).toMatchObject({
      body_md: `[x](${added})`, source_version_id: "version-2",
    });
  });

  it("drops another scope's results and stops checking after failed requests", async () => {
    const pending: Array<{ resolve: (preview: LinkTargetPreview) => void; reject: (error: Error) => void }> = [];
    vi.mocked(previewLinkTargets).mockImplementation(() => new Promise((resolve, reject) => pending.push({ resolve, reject })));
    const { result, rerender } = renderHook(props => useLinkTargetPreview(props), { initialProps: input });
    act(() => result.current.reportLinks([opened.locator, ref]));
    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    const [, firstRequest, firstSignal] = vi.mocked(previewLinkTargets).mock.calls[0];

    // Another reader never sees the first reader's results, even a late answer.
    const other = { ...input, actorId: "other-reader", initialTargets: [] };
    rerender(other);
    expect(firstSignal?.aborted).toBe(true);
    expect(result.current.targets).toEqual([]);
    await act(async () => pending[0].resolve(answer(firstRequest, [confirmed])));
    expect(result.current.targets).toEqual([]);
    expect([...result.current.checking]).toEqual([opened.locator, ref]);

    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    // A disjoint batch starts while the first is pending. When both fail,
    // no link may keep spinning without a pending check.
    const edited = { ...other, body: `${other.body} more` };
    rerender(edited);
    act(() => result.current.reportLinks([opened.locator, ref, added]));
    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    expect(vi.mocked(previewLinkTargets).mock.calls[2][1].body_md).toBe(`[x](${added})`);
    await act(async () => pending[1].reject(new Error("offline")));
    await act(async () => pending[2].reject(new Error("offline")));
    expect(result.current.checking.size).toBe(0);
    // The next edit followed by a pause retries every failed link.
    rerender({ ...edited, body: `${edited.body}!` });
    await act(async () => { await vi.advanceTimersByTimeAsync(LINK_CHECK_IDLE_MS); });
    expect(vi.mocked(previewLinkTargets).mock.calls[3][1].body_md).toBe(
      `[x](${opened.locator})\n[x](${ref})\n[x](${added})`,
    );
  });
});
