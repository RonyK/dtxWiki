import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SearchScreen } from "./SearchScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

describe("SearchScreen race handling", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("does not apply an in-flight debounced search response after clear", async () => {
    vi.useFakeTimers();
    let resolveSearch: ((response: Response) => void) | undefined;
    let searchSignal: AbortSignal | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        searchSignal = init?.signal as AbortSignal | undefined;
        return new Promise<Response>((resolve) => {
          resolveSearch = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Query"), {
      target: { value: "stale" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
      await flushMicrotasks();
    });
    expect(resolveSearch).toBeDefined();
    expect(searchSignal?.aborted).toBe(false);

    fireEvent.change(screen.getByLabelText("Query"), {
      target: { value: "" },
    });
    await act(async () => {
      await flushMicrotasks();
    });
    expect(searchSignal?.aborted).toBe(true);

    await act(async () => {
      resolveSearch?.(
        jsonResponse({
          data: {
            ranked_results: [
              result("context-stale", "context_page", "Stale Result", "Late", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        }),
      );
    });

    expect(screen.queryByText("Stale Result")).not.toBeInTheDocument();
    expect(screen.getByText("Search your workspace")).toBeInTheDocument();
  });
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function flushMicrotasks() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

function result(
  id: string,
  type: string,
  title: string,
  snippet: string,
  score: number,
) {
  return {
    id,
    type,
    title,
    snippet,
    score,
    citation_available: true,
    stale: false,
    archived: false,
  };
}
