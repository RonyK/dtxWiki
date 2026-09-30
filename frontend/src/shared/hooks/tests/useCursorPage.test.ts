import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import type { CursorListPage } from "../../api/mcpAccess";
import { useCursorPage } from "../useCursorPage";

type Row = { id: string };
type Page = CursorListPage<Row, { active: number }>;

function page(ids: string[], nextCursor: string | null = null): Page {
  return {
    data: ids.map((id) => ({ id })),
    next_cursor: nextCursor,
    total_count: ids.length,
    filtered_count: ids.length,
    status_counts: { active: ids.length },
  };
}

describe("useCursorPage", () => {
  it("navigates with an opaque cursor stack and returns to the prior cursor", async () => {
    const load = vi.fn(async (cursor: string | null) =>
      cursor === "cursor-2" ? page(["row-2"]) : page(["row-1"], "cursor-2"),
    );
    const { result } = renderHook(() =>
      useCursorPage<Row, Page>({
        enabled: true,
        errorMessage: "Could not load rows",
        load,
        queryKey: "active",
      }),
    );

    await waitFor(() => expect(result.current.page?.data[0]?.id).toBe("row-1"));
    await act(async () => result.current.next());
    expect(result.current.page?.data[0]?.id).toBe("row-2");
    expect(result.current.pageIndex).toBe(1);
    expect(result.current.hasPrevious).toBe(true);

    await act(async () => result.current.previous());
    expect(result.current.page?.data[0]?.id).toBe("row-1");
    expect(result.current.pageIndex).toBe(0);
    expect(load.mock.calls.map(([cursor]) => cursor)).toEqual([
      null,
      "cursor-2",
      null,
    ]);
  });

  it("ignores a stale response after the workspace or filter query changes", async () => {
    const resolvers = new Map<string, (value: Page) => void>();
    const loadForQuery = vi.fn(
      (queryKey: string) =>
        new Promise<Page>((resolve) => {
          resolvers.set(queryKey, resolve);
        }),
    );
    const { result, rerender } = renderHook(
      ({ queryKey }: { queryKey: string }) =>
        useCursorPage<Row, Page>({
          enabled: true,
          errorMessage: "Could not load rows",
          load: () => loadForQuery(queryKey),
          queryKey,
        }),
      { initialProps: { queryKey: "workspace-a:active" } },
    );

    await waitFor(() => expect(resolvers.has("workspace-a:active")).toBe(true));
    rerender({ queryKey: "workspace-b:active" });
    await waitFor(() => expect(resolvers.has("workspace-b:active")).toBe(true));

    act(() => resolvers.get("workspace-b:active")?.(page(["fresh"])));
    await waitFor(() => expect(result.current.page?.data[0]?.id).toBe("fresh"));
    act(() => resolvers.get("workspace-a:active")?.(page(["stale"])));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.page?.data[0]?.id).toBe("fresh");
  });

  it("moves back when reloading an empty last page after its final row is deleted", async () => {
    let lastPageDeleted = false;
    const load = vi.fn(async (cursor: string | null) => {
      if (cursor === "cursor-2") {
        return lastPageDeleted ? page([]) : page(["row-2"]);
      }
      return page(["row-1"], "cursor-2");
    });
    const { result } = renderHook(() =>
      useCursorPage<Row, Page>({
        enabled: true,
        errorMessage: "Could not load rows",
        load,
        queryKey: "active",
      }),
    );

    await waitFor(() => expect(result.current.page?.data[0]?.id).toBe("row-1"));
    await act(async () => result.current.next());
    expect(result.current.page?.data[0]?.id).toBe("row-2");

    lastPageDeleted = true;
    await act(async () => result.current.reload());

    expect(result.current.page?.data[0]?.id).toBe("row-1");
    expect(result.current.pageIndex).toBe(0);
    expect(result.current.hasPrevious).toBe(false);
    expect(load.mock.calls.map(([cursor]) => cursor)).toEqual([
      null,
      "cursor-2",
      "cursor-2",
      null,
    ]);
  });

  it("lets a mutation reload supersede an in-flight cursor navigation", async () => {
    let navigationResolve: ((value: Page) => void) | null = null;
    let navigationSignal: AbortSignal | null = null;
    let mutationCommitted = false;
    const load = vi.fn((cursor: string | null, signal: AbortSignal) => {
      if (cursor === "cursor-2") {
        navigationSignal = signal;
        return new Promise<Page>((resolve) => {
          navigationResolve = resolve;
        });
      }
      return Promise.resolve(
        mutationCommitted
          ? page(["row-1-after-mutation"], "cursor-2")
          : page(["row-1"], "cursor-2"),
      );
    });
    const { result } = renderHook(() =>
      useCursorPage<Row, Page>({
        enabled: true,
        errorMessage: "Could not load rows",
        load,
        queryKey: "active",
      }),
    );

    await waitFor(() => expect(result.current.page?.data[0]?.id).toBe("row-1"));
    let navigationPromise: Promise<void> | undefined;
    act(() => {
      navigationPromise = result.current.next();
    });
    await waitFor(() => expect(result.current.loading).toBe(true));

    mutationCommitted = true;
    await act(async () => result.current.reload());
    expect((navigationSignal as AbortSignal | null)?.aborted).toBe(true);
    expect(result.current.page?.data[0]?.id).toBe("row-1-after-mutation");
    expect(result.current.pageIndex).toBe(0);

    act(() => navigationResolve?.(page(["stale-row-2"])));
    await act(async () => navigationPromise);
    expect(result.current.page?.data[0]?.id).toBe("row-1-after-mutation");
    expect(result.current.pageIndex).toBe(0);
  });

  it.each([
    "stale_cursor",
    "mcp_client_cursor_invalid",
    "invalid_access_key_cursor",
  ])("recovers backend cursor error %s at the first page", async (errorCode) => {
    let firstPageReads = 0;
    const load = vi.fn(async (cursor: string | null) => {
      if (cursor === "stale-cursor") {
        throw new ApiError(400, {
          error_code: errorCode,
          message: "The list changed",
          request_id: "request-1",
          warnings: [],
        });
      }
      firstPageReads += 1;
      return page([`first-${firstPageReads}`], "stale-cursor");
    });
    const { result } = renderHook(() =>
      useCursorPage<Row, Page>({
        enabled: true,
        errorMessage: "Could not load rows",
        load,
        queryKey: "active",
      }),
    );

    await waitFor(() => expect(result.current.page?.data[0]?.id).toBe("first-1"));
    await act(async () => result.current.next());

    expect(result.current.page?.data[0]?.id).toBe("first-2");
    expect(result.current.pageIndex).toBe(0);
    expect(result.current.recoveredFromCursor).toBe(true);
    expect(load.mock.calls.map(([cursor]) => cursor)).toEqual([
      null,
      "stale-cursor",
      null,
    ]);
  });
});
