import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useAsyncLoad } from "../useAsyncLoad";

describe("useAsyncLoad", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("aborts the controller on unmount when signal-aware", () => {
    let receivedSignal: AbortSignal | undefined;
    const load = vi.fn((signal?: AbortSignal) => {
      receivedSignal = signal;
      return new Promise<string>(() => {});
    });

    const { unmount } = renderHook(() =>
      useAsyncLoad(load, [], {
        errorMessage: "load failed",
        signalAware: true,
      }),
    );

    expect(receivedSignal).toBeDefined();
    expect(receivedSignal?.aborted).toBe(false);

    act(() => unmount());

    expect(receivedSignal?.aborted).toBe(true);
  });

  it("aborts the previous controller when dependencies change", () => {
    const signals: AbortSignal[] = [];
    const load = vi.fn((signal?: AbortSignal) => {
      if (signal) {
        signals.push(signal);
      }
      return new Promise<string>(() => {});
    });

    const { rerender } = renderHook(
      ({ dep }: { dep: number }) =>
        useAsyncLoad(load, [dep], {
          errorMessage: "load failed",
          signalAware: true,
        }),
      { initialProps: { dep: 1 } },
    );

    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);

    act(() => rerender({ dep: 2 }));

    expect(signals).toHaveLength(2);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
  });

  it("keeps loaded data when a reload fails and only falls back before the first success", async () => {
    const resolvers: Array<{
      resolve: (value: string) => void;
      reject: (reason?: unknown) => void;
    }> = [];
    const load = vi.fn(
      () =>
        new Promise<string>((resolve, reject) => {
          resolvers.push({ resolve, reject });
        }),
    );

    const { result } = renderHook(() =>
      useAsyncLoad(load, [], { errorMessage: "load failed", fallbackData: "empty" }),
    );

    // First load fails: nothing is on screen yet, so the fallback applies.
    await act(async () => {
      resolvers[0].reject(new Error("first boom"));
      await Promise.resolve();
    });
    expect(result.current.data).toBe("empty");
    expect(result.current.error).toBe("load failed");

    await act(async () => {
      result.current.reload();
    });
    await act(async () => {
      resolvers[1].resolve("loaded");
      await Promise.resolve();
    });
    expect(result.current.data).toBe("loaded");

    // A failed RELOAD must not wipe the loaded data back to the empty shape.
    await act(async () => {
      result.current.reload();
    });
    await act(async () => {
      resolvers[2].reject(new Error("reload boom"));
      await Promise.resolve();
    });
    expect(result.current.data).toBe("loaded");
    expect(result.current.error).toBe("load failed");
  });

  it("falls back when a failed load belongs to a superseded query", async () => {
    const resolvers: Array<{
      resolve: (value: string) => void;
      reject: (reason?: unknown) => void;
    }> = [];
    const load = vi.fn(
      () =>
        new Promise<string>((resolve, reject) => {
          resolvers.push({ resolve, reject });
        }),
    );

    const { result, rerender } = renderHook(
      ({ dep }: { dep: number }) =>
        useAsyncLoad(load, [dep], { errorMessage: "load failed", fallbackData: "empty" }),
      { initialProps: { dep: 1 } },
    );

    await act(async () => {
      resolvers[0].resolve("page-1-of-query-1");
      await Promise.resolve();
    });
    expect(result.current.data).toBe("page-1-of-query-1");

    // Switching query and failing must not leave query 1's rows (and its keyset
    // cursor) on screen under query 2's filters.
    act(() => rerender({ dep: 2 }));
    await act(async () => {
      resolvers[1].reject(new Error("switch boom"));
      await Promise.resolve();
    });
    expect(result.current.data).toBe("empty");
    expect(result.current.error).toBe("load failed");
  });

  it("drops stale responses when deps change before earlier promises settle", async () => {
    const resolvers: Array<(value: string) => void> = [];
    const load = vi.fn((_signal?: AbortSignal) => {
      return new Promise<string>((resolve) => {
        resolvers.push(resolve);
      });
    });

    const { result, rerender } = renderHook(
      ({ dep }: { dep: number }) =>
        useAsyncLoad(load, [dep], {
          errorMessage: "load failed",
          fallbackData: null,
        }),
      { initialProps: { dep: 1 } },
    );

    expect(resolvers).toHaveLength(1);
    expect(result.current.data).toBeNull();

    act(() => rerender({ dep: 2 }));
    expect(resolvers).toHaveLength(2);

    // Resolve the stale (first) promise — its result must be dropped.
    await act(async () => {
      resolvers[0]("stale");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);

    // Resolve the latest promise — its result wins.
    await act(async () => {
      resolvers[1]("fresh");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.data).toBe("fresh");
    expect(result.current.loading).toBe(false);
  });

  it("does not surface stale errors after a newer load supersedes them", async () => {
    const resolvers: Array<{
      resolve: (value: string) => void;
      reject: (reason?: unknown) => void;
    }> = [];
    const load = vi.fn((_signal?: AbortSignal) => {
      return new Promise<string>((resolve, reject) => {
        resolvers.push({ resolve, reject });
      });
    });

    const { result, rerender } = renderHook(
      ({ dep }: { dep: number }) =>
        useAsyncLoad(load, [dep], {
          errorMessage: "fallback error",
          fallbackData: null,
        }),
      { initialProps: { dep: 1 } },
    );

    act(() => rerender({ dep: 2 }));
    expect(resolvers).toHaveLength(2);

    // Reject the stale promise — its error must not bubble to UI.
    await act(async () => {
      resolvers[0].reject(new Error("boom"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.error).toBeNull();

    await act(async () => {
      resolvers[1].resolve("ok");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.data).toBe("ok");
    expect(result.current.error).toBeNull();
  });

  it("keeps signalAware init-only after mount", () => {
    const receivedSignals: Array<AbortSignal | undefined> = [];
    const load = vi.fn((signal?: AbortSignal) => {
      receivedSignals.push(signal);
      return new Promise<string>(() => {});
    });

    const { rerender } = renderHook(
      ({ aware }: { aware: boolean }) =>
        useAsyncLoad(load, [aware], {
          errorMessage: "load failed",
          signalAware: aware,
        }),
      { initialProps: { aware: false } },
    );

    expect(receivedSignals).toEqual([undefined]);

    act(() => rerender({ aware: true }));

    expect(receivedSignals).toEqual([undefined, undefined]);
  });

  it("allows a caller to map a known error without exposing generic details", async () => {
    const load = vi.fn().mockRejectedValue(new Error("raw req-secret"));
    const formatError = vi.fn((_error: unknown, fallback: string) => `mapped: ${fallback}`);

    const { result } = renderHook(() =>
      useAsyncLoad(load, [], {
        errorMessage: "load failed",
        formatError,
      }),
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(formatError).toHaveBeenCalledWith(expect.any(Error), "load failed");
    expect(result.current.error).toBe("mapped: load failed");
    expect(result.current.error).not.toContain("req-secret");
  });
});
