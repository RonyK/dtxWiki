import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearPersistedWorkspaceId,
  getCurrentWorkspaceId,
  getCurrentWorkspaceSlug,
  listWorkspaces,
} from "../workspace";

describe("workspace list request deadline", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    clearPersistedWorkspaceId();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    clearPersistedWorkspaceId();
  });

  it.each(["headers", "body"] as const)(
    "aborts a shared request stalled at %s after 30 seconds and permits retry",
    async (stalledAt) => {
      const fetchMock = vi
        .spyOn(globalThis, "fetch")
        .mockImplementationOnce((_input, init) =>
          stalledResponse(init?.signal, stalledAt),
        )
        .mockResolvedValueOnce(workspaceResponse("workspace-b", "beta"));

      // All consumers share one transport and its deadline, including the
      // helpers that resolve the current id or slug during initial hydration.
      const outcomes = Promise.allSettled([
        listWorkspaces(),
        listWorkspaces(),
        getCurrentWorkspaceId(),
        getCurrentWorkspaceSlug(),
      ]);
      const signal = fetchMock.mock.calls[0][1]?.signal;
      expect(signal).toBeInstanceOf(AbortSignal);
      expect(fetchMock).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(29_999);
      expect(signal?.aborted).toBe(false);
      await vi.advanceTimersByTimeAsync(1);

      expect(signal?.aborted).toBe(true);
      expect(await outcomes).toEqual(
        Array.from({ length: 4 }, () => ({
          status: "rejected",
          reason: expect.objectContaining({ name: "AbortError" }),
        })),
      );
      expect(
        window.localStorage.getItem("dtxwiki.selectedWorkspaceId"),
      ).toBeNull();

      // This is an explicit caller retry; a failed GET must not retain the
      // rejected shared promise or automatically start a second request.
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await expect(listWorkspaces()).resolves.toMatchObject({
        data: { current_workspace_id: "workspace-b" },
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-b");
      await expect(getCurrentWorkspaceSlug()).resolves.toBe("beta");
      expect(fetchMock).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(30_000);
      expect(fetchMock.mock.calls[1][1]?.signal?.aborted).toBe(false);
    },
  );

  it("clears the deadline after a successful response and retains the selection", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(workspaceResponse("workspace-a", "alpha"));

    await expect(listWorkspaces()).resolves.toMatchObject({
      data: { current_workspace_id: "workspace-a" },
    });

    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(false);
    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-a");
    await expect(getCurrentWorkspaceSlug()).resolves.toBe("alpha");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("releases a failed transport and its timer before an explicit retry", async () => {
    const error = new TypeError("Network connection failed");
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(error)
      .mockResolvedValueOnce(workspaceResponse("workspace-b", "beta"));

    await expect(listWorkspaces()).rejects.toBe(error);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await expect(listWorkspaces()).resolves.toMatchObject({
      data: { current_workspace_id: "workspace-b" },
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(false);
    expect(fetchMock.mock.calls[1][1]?.signal?.aborted).toBe(false);
  });

  it("does not cancel or evict a newer generation when an old request times out", async () => {
    let resolveCurrent!: (response: Response) => void;
    const currentResponse = new Promise<Response>((resolve) => {
      resolveCurrent = resolve;
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce((_input, init) =>
        stalledResponse(init?.signal, "headers"),
      )
      .mockReturnValueOnce(currentResponse);

    const previousOutcome = Promise.allSettled([listWorkspaces()]);
    await vi.advanceTimersByTimeAsync(20_000);
    clearPersistedWorkspaceId();
    const currentList = listWorkspaces();
    const currentSignal = fetchMock.mock.calls[1][1]?.signal;

    // Only the old generation's deadline has elapsed. Its finally handler
    // must leave the newer shared promise and that request's timer intact.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await previousOutcome).toEqual([
      {
        status: "rejected",
        reason: expect.objectContaining({ name: "AbortError" }),
      },
    ]);
    expect(currentSignal?.aborted).toBe(false);
    const concurrentCurrentList = listWorkspaces();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    resolveCurrent(workspaceResponse("workspace-b", "beta"));
    await expect(currentList).resolves.toMatchObject({
      data: { current_workspace_id: "workspace-b" },
    });
    await expect(concurrentCurrentList).resolves.toMatchObject({
      data: { current_workspace_id: "workspace-b" },
    });
    await expect(getCurrentWorkspaceId()).resolves.toBe("workspace-b");

    await vi.advanceTimersByTimeAsync(20_000);
    expect(currentSignal?.aborted).toBe(false);
  });
});

function stalledResponse(
  signal: AbortSignal | null | undefined,
  stalledAt: "headers" | "body",
): Promise<Response> {
  if (!signal) {
    throw new Error("Workspace list must supply its cancellation signal");
  }
  if (stalledAt === "headers") {
    return new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), {
        once: true,
      });
    });
  }
  // The headers arrive, but the server leaves its JSON body unfinished. Model
  // fetch's abort behavior on the actual Response stream so the deadline is
  // tested through apiRequest's response.json(), not just a mocked JSON call.
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('{"data":'));
      signal.addEventListener("abort", () => controller.error(signal.reason), {
        once: true,
      });
    },
  });
  return Promise.resolve(
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function workspaceResponse(id: string, slug: string) {
  return new Response(
    JSON.stringify({
      data: {
        current_workspace_id: id,
        workspaces: [
          {
            id,
            slug,
            title: slug,
            name: slug,
            goal: "",
            default_language: "en",
            visibility: "private",
          },
        ],
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}
