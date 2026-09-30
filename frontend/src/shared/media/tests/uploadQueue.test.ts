import { describe, expect, it, vi } from "vitest";

import { UploadQueue } from "../uploadQueue";

describe("UploadQueue", () => {
  it("runs uploads sequentially and reports lifecycle transitions", async () => {
    const states: string[] = [];
    let active = 0;
    const queue = new UploadQueue<string>({
      async upload(value) {
        active += 1;
        expect(active).toBe(1);
        await Promise.resolve(value);
        active -= 1;
      },
      onChange: (entry) => states.push(`${entry.id}:${entry.status}`),
    });

    await Promise.all([queue.enqueue("a", "first"), queue.enqueue("b", "second")]);

    expect(states).toEqual([
      "a:queued", "a:uploading", "b:queued",
      "a:succeeded", "b:uploading", "b:succeeded",
    ]);
  });

  it("aborts an active upload and can retry the same domain value", async () => {
    const states: string[] = [];
    const upload = vi.fn(async (_value: string, signal: AbortSignal) => {
      if (upload.mock.calls.length === 1) {
        await new Promise<void>((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      }
    });
    const queue = new UploadQueue<string, true>({
      upload,
      onChange: (entry) => states.push(entry.status),
    }, { activeCancelOutcomeUnknown: true });

    const first = queue.enqueue("asset", "file");
    expect(queue.cancel("asset")).toBe(true);
    await first;
    const retry = queue.retry("asset");
    expect(retry).not.toBeNull();
    await retry;

    expect(upload).toHaveBeenCalledTimes(2);
    expect(upload.mock.calls[0][0]).toBe(upload.mock.calls[1][0]);
    expect(states).toEqual([
      "queued", "uploading", "cancelling", "outcome-unknown",
      "queued", "uploading", "succeeded",
    ]);
  });

  it("starts entries in marker order with bounded concurrency", async () => {
    const starts: string[] = [];
    const active = new Set<string>();
    const resolvers = new Map<string, () => void>();
    const queue = new UploadQueue<string>({
      upload: vi.fn(
        (value) => new Promise<void>((resolve) => {
          starts.push(value);
          active.add(value);
          resolvers.set(value, () => {
            active.delete(value);
            resolve();
          });
        }),
      ),
    }, { maxConcurrency: 2 });

    const completions = ["a", "b", "c", "d", "e"].map((id) =>
      queue.enqueue(id, id));
    expect(starts).toEqual(["a", "b"]);
    expect(active.size).toBe(2);

    resolvers.get("b")?.();
    await vi.waitFor(() => expect(starts).toEqual(["a", "b", "c"]));
    resolvers.get("a")?.();
    await vi.waitFor(() => expect(starts).toEqual(["a", "b", "c", "d"]));
    resolvers.get("d")?.();
    await vi.waitFor(() => expect(starts).toEqual(["a", "b", "c", "d", "e"]));
    resolvers.get("c")?.();
    resolvers.get("e")?.();
    await Promise.all(completions);

    expect(queue.snapshot().map((entry) => entry.id)).toEqual(["a", "b", "c", "d", "e"]);
  });

  it("cancels queued work definitively without invoking the adapter", async () => {
    let releaseActive!: () => void;
    const upload = vi.fn(
      () => new Promise<void>((resolve) => {
        releaseActive = resolve;
      }),
    );
    const queue = new UploadQueue<string>({ upload });
    const active = queue.enqueue("active", "active");
    const queued = queue.enqueue("queued", "queued");

    expect(queue.cancel("queued")).toBe(true);
    await queued;
    expect(queue.snapshot().find((entry) => entry.id === "queued")?.status)
      .toBe("cancelled");
    expect(upload).toHaveBeenCalledTimes(1);
    releaseActive();
    await active;
  });

  it("keeps failures retryable and exposes a safe message", async () => {
    const queue = new UploadQueue<string>({
      upload: vi.fn().mockRejectedValue(new Error("raw req-secret")),
    });
    await queue.enqueue("asset", "file");

    expect(queue.snapshot()[0]).toMatchObject({
      id: "asset",
      status: "failed",
      message: "Request failed. Please try again.",
    });
    expect(queue.snapshot()[0].message).not.toContain("raw req-secret");
    expect(queue.cancel("asset")).toBe(false);
  });

  it("removes only resolved failed or cancelled entries", async () => {
    const queue = new UploadQueue<string>({
      upload: vi.fn().mockRejectedValue(new Error("temporary")),
    });
    await queue.enqueue("failed", "file");

    expect(queue.remove("failed")).toBe(true);
    expect(queue.snapshot()).toEqual([]);
    expect(queue.retry("failed")).toBeNull();
    expect(queue.remove("missing")).toBe(false);
  });

  it("honors a retry-not-before delay and bounds scheduling outside the adapter", async () => {
    vi.useFakeTimers();
    try {
      const upload = vi.fn()
        .mockRejectedValueOnce(new Error("rate limited"))
        .mockResolvedValueOnce(undefined);
      const queue = new UploadQueue<string>({
        retryDelayMs: () => 5_000,
        upload,
      });
      await queue.enqueue("asset", "file");
      const failed = queue.snapshot()[0];
      expect(failed.retryAt).toBe(Date.now() + 5_000);

      const retry = queue.retry("asset");
      expect(retry).not.toBeNull();
      expect(upload).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(4_999);
      expect(upload).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      await retry;
      expect(upload).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("bulk-retries only failed entries, leaving unknown outcomes unresolved", async () => {
    const upload = vi.fn(async (value: string, signal: AbortSignal) => {
      if (value === "unknown") {
        await new Promise<void>((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      }
      if (value === "failed" && upload.mock.calls.filter(([entry]) => entry === value).length === 1) {
        throw new Error("temporary");
      }
    });
    const queue = new UploadQueue<string, true>({ upload }, {
      activeCancelOutcomeUnknown: true,
      maxConcurrency: 2,
    });
    const unknown = queue.enqueue("unknown", "unknown");
    await queue.enqueue("failed", "failed");
    queue.cancel("unknown");
    await unknown;

    await Promise.all(queue.retryFailed());

    expect(upload.mock.calls.filter(([value]) => value === "failed")).toHaveLength(2);
    expect(upload.mock.calls.filter(([value]) => value === "unknown")).toHaveLength(1);
    expect(queue.snapshot().find((entry) => entry.id === "unknown")?.status)
      .toBe("outcome-unknown");
  });

  it("clears active work and ignores its late completion", async () => {
    const states: string[] = [];
    let resolveUpload!: () => void;
    const queue = new UploadQueue<string, true>({
      upload: vi.fn(
        () => new Promise<void>((resolve) => {
          resolveUpload = resolve;
        }),
      ),
      onChange: (entry) => states.push(entry.status),
    }, { activeCancelOutcomeUnknown: true });
    const completion = queue.enqueue("asset", "file");

    queue.clear();
    resolveUpload();
    await completion;

    expect(queue.snapshot()).toEqual([]);
    expect(states).toEqual([
      "queued", "uploading", "cancelling", "outcome-unknown",
    ]);
  });
});
