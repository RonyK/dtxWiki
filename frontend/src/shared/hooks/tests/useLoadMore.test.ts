import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useLoadMore } from "../useLoadMore";
import type { LoadMorePage } from "../useLoadMore";

type Row = { id: string };

function page(ids: string[], next: string | null, total?: number): LoadMorePage<Row> {
  return { items: ids.map((id) => ({ id })), next_cursor: next, total };
}

describe("useLoadMore", () => {
  it("exposes hasMore and total from the initial page", () => {
    const { result } = renderHook(() =>
      useLoadMore<Row>({
        initial: page(["a", "b"], "cursor-1", 50),
        fetchPage: vi.fn(),
      }),
    );

    expect(result.current.items.map((row) => row.id)).toEqual(["a", "b"]);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.total).toBe(50);
    expect(result.current.nextCursor).toBe("cursor-1");
  });

  it("appends the next page and advances the cursor on loadMore", async () => {
    const fetchPage = vi.fn(async () => page(["c", "d"], null));
    const { result } = renderHook(() =>
      useLoadMore<Row>({
        initial: page(["a", "b"], "cursor-1"),
        fetchPage,
      }),
    );

    await act(async () => {
      await result.current.loadMore();
    });

    expect(fetchPage).toHaveBeenCalledWith("cursor-1");
    expect(result.current.items.map((row) => row.id)).toEqual([
      "a",
      "b",
      "c",
      "d",
    ]);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.nextCursor).toBeNull();
  });

  it("no-ops loadMore when the list is exhausted", async () => {
    const fetchPage = vi.fn();
    const { result } = renderHook(() =>
      useLoadMore<Row>({
        initial: page(["a"], null),
        fetchPage,
      }),
    );

    await act(async () => {
      await result.current.loadMore();
    });

    expect(fetchPage).not.toHaveBeenCalled();
  });

  it("re-seeds when the reset key changes", () => {
    const { result, rerender } = renderHook(
      ({ key, initial }: { key: string; initial: LoadMorePage<Row> }) =>
        useLoadMore<Row>({ initial, resetKey: key, fetchPage: vi.fn() }),
      { initialProps: { key: "k1", initial: page(["a"], "c1") } },
    );

    expect(result.current.items.map((row) => row.id)).toEqual(["a"]);

    act(() => rerender({ key: "k2", initial: page(["x", "y"], null) }));

    expect(result.current.items.map((row) => row.id)).toEqual(["x", "y"]);
    expect(result.current.hasMore).toBe(false);
  });

  it("skips a stale append when the base page was replaced mid-flight", async () => {
    let resolveFetch: ((value: LoadMorePage<Row>) => void) | null = null;
    const fetchPage = vi.fn(
      () =>
        new Promise<LoadMorePage<Row>>((resolve) => {
          resolveFetch = resolve;
        }),
    );

    const { result, rerender } = renderHook(
      ({ key, initial }: { key: string; initial: LoadMorePage<Row> }) =>
        useLoadMore<Row>({ initial, resetKey: key, fetchPage }),
      { initialProps: { key: "k1", initial: page(["a"], "c1") } },
    );

    // Start a load against cursor "c1".
    let pending: Promise<void>;
    act(() => {
      pending = result.current.loadMore();
    });
    expect(fetchPage).toHaveBeenCalledWith("c1");

    // A filter change re-seeds the accumulator while the request is in flight.
    act(() => rerender({ key: "k2", initial: page(["fresh"], null) }));
    expect(result.current.items.map((row) => row.id)).toEqual(["fresh"]);

    // The stale page resolves — it must NOT be appended to the new base.
    await act(async () => {
      resolveFetch?.(page(["stale"], null));
      await pending;
    });

    expect(result.current.items.map((row) => row.id)).toEqual(["fresh"]);
  });
});
