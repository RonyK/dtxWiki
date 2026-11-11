import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InboxItem, InboxPage } from "../../../shared/api/inbox";
import { InboxScreen } from "../InboxScreen";

vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => null,
}));

const BASE = "http://127.0.0.1:21080/api/v1/users/me/inbox";

function item(key: string, kind = "test.status", eventAt = "2026-10-28T00:00:00+00:00"): InboxItem {
  return {
    kind,
    item_key: key,
    revision: "v1",
    event_at: eventAt,
    unread: true,
    archived: false,
    display: { title: key, href: null, params: {} },
  };
}

function page(
  status_items: InboxItem[],
  items: InboxItem[],
  next_cursor: string | null = null,
): InboxPage {
  return { status_items, items, next_cursor };
}

function response(data: unknown) {
  return new Response(JSON.stringify({ data }), {
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

function CurrentRoute() {
  return <output aria-label="Current route">{useLocation().pathname}</output>;
}

function show() {
  return render(
    <MemoryRouter initialEntries={["/inbox"]}>
      <CurrentRoute />
      <InboxScreen />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("InboxScreen pagination", () => {
  it("merges twenty-item pages into one chronological list with precise time and stable ties", async () => {
    const entries = Array.from({ length: 40 }, (_, index) => item(
      `Item ${index + 1}`,
      index % 2 ? "test.message" : "test.status",
      `2026-10-28T00:00:00.${String(999999 - index).padStart(6, "0")}Z`,
    ));
    entries[38].event_at = entries[36].event_at;
    entries[39].event_at = entries[36].event_at;
    const continuation = deferred<Response>();
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(page(
        entries.slice(0, 20).filter((_, index) => index % 2 === 0),
        entries.slice(0, 20).filter((_, index) => index % 2 === 1),
        "mixed-page",
      )))
      .mockImplementationOnce(() => continuation.promise);
    show();

    await screen.findByText("Item 20");
    expect(screen.getAllByRole("listitem")).toHaveLength(20);
    expect(screen.getAllByRole("region")).toHaveLength(1);
    const more = screen.getByRole("button", { name: "Load more" });
    act(() => {
      more.click();
      more.click();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Loading Inbox" })).toBeDisabled();
    expect(fetchMock).toHaveBeenNthCalledWith(1, `${BASE}?state=all&item_type=all&limit=20`, expect.anything());
    expect(fetchMock).toHaveBeenNthCalledWith(2, `${BASE}?state=all&item_type=all&cursor=mixed-page&limit=20`, expect.anything());

    await act(async () => continuation.resolve(response(page(
      entries.slice(20).filter((_, index) => index % 2 === 0),
      entries.slice(20).filter((_, index) => index % 2 === 1),
    ))));

    const list = screen.getByRole("region", { name: "All" });
    expect(within(list).getAllByRole("listitem").map((row) => row.querySelector(".inbox-row-title")?.textContent))
      .toEqual([...entries.slice(0, 36), entries[38], entries[36], entries[39], entries[37]]
        .map((entry) => entry.display?.title));
    expect(within(list).getAllByRole("list")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("retries the same cursor after failure and follows an empty continuation", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(page([], [], "first-cursor")))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(response(page([], [], "second-cursor")))
      .mockResolvedValueOnce(response(page([], [item("Recovered message", "test.stored")])));
    show();

    fireEvent.click(await screen.findByRole("button", { name: "Check older items" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("No messages.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check older items" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Check older items" })).toBeEnabled());
    expect(screen.queryByText("No messages.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check older items" }));

    expect(await screen.findByText("Recovered message")).toBeInTheDocument();
    expect(fetchMock.mock.calls.slice(1).map(([url]) => String(url))).toEqual([
      `${BASE}?state=all&item_type=all&cursor=first-cursor&limit=20`,
      `${BASE}?state=all&item_type=all&cursor=first-cursor&limit=20`,
      `${BASE}?state=all&item_type=all&cursor=second-cursor&limit=20`,
    ]);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("resets the type filter query, names the list after it, and ignores an old continuation", async () => {
    const oldFilter = deferred<Response>();
    const current = deferred<Response>();
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(page([item("Original status")], [], "old-filter")))
      .mockImplementationOnce(() => oldFilter.promise)
      .mockResolvedValueOnce(response(page([], [item("Filtered message", "test.message")], "current")))
      .mockImplementationOnce(() => current.promise)
      .mockResolvedValueOnce(response(page([item("Only current status")], [])));
    show();

    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    fireEvent.click(screen.getByRole("radio", { name: "Stored messages" }));
    await screen.findByText("Filtered message");
    expect(screen.queryByText("Original status")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Stored messages" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await act(async () => oldFilter.resolve(response(page([item("Stale status")], []))));
    expect(screen.queryByText("Stale status")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Loading Inbox" })).toBeDisabled();
    await act(async () => current.resolve(response(page([item("Current continuation")], []))));
    expect(screen.getByText("Current continuation")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(3, `${BASE}?state=all&item_type=message&limit=20`, expect.anything());

    fireEvent.click(screen.getByRole("radio", { name: "Current status" }));
    await screen.findByText("Only current status");
    expect(screen.queryByText("Filtered message")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(5, `${BASE}?state=all&item_type=status&limit=20`, expect.anything());
  });

  it("refreshes the loaded depth across both arrays and preserves concurrent marks when deduplicating", async () => {
    const firstStatus = item("Marked status", "test:status");
    const firstMessage = item("Marked message", "test.stored");
    const statusMark = deferred<Response>();
    let currentStatus = firstStatus;
    let currentMessage = firstMessage;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (init?.method === "POST") {
        const marks = JSON.parse(String(init.body));
        if (marks.item_key === firstStatus.item_key) {
          await statusMark.promise;
          currentStatus = { ...firstStatus, unread: false };
          return response({ applied: true, item: currentStatus });
        }
        currentMessage = { ...firstMessage, unread: false };
        return response({ applied: true, item: currentMessage });
      }
      if (new URL(String(input)).searchParams.has("cursor")) {
        return response(page(
          // A ':' in a kind must not collide with the same text in an item key.
          [firstStatus, item("status:Marked status", "test")],
          [firstMessage, item("New message", "test.stored")],
        ));
      }
      return response(page([currentStatus], [currentMessage], "more"));
    });
    show();

    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("New message");
    const statusRow = screen.getByText("Marked status").closest("li")!;
    const messageRow = screen.getByText("Marked message").closest("li")!;
    fireEvent.click(within(statusRow).getByRole("button", { name: "Mark as read" }));
    fireEvent.click(within(messageRow).getByRole("button", { name: "Mark as read" }));
    await waitFor(() => expect(within(messageRow).getByRole("button", { name: "Mark as unread" })).toBeEnabled());
    expect(within(statusRow).getByRole("button", { name: "Mark as read" })).toBeDisabled();
    await act(async () => statusMark.resolve(response(null)));

    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.getAllByText("Marked status")).toHaveLength(1);
    expect(screen.getAllByText("Marked message")).toHaveLength(1);
    expect(screen.getByText("status:Marked status")).toBeInTheDocument();
    expect(within(statusRow).getByRole("button", { name: "Mark as unread" })).toBeEnabled();
    expect(within(messageRow).getByRole("button", { name: "Mark as unread" })).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledTimes(8);
  });

  it("folds the filters on a compact screen behind a summary of the current choices", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: true, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    }));
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => response(page([item("A status")], [])));
    show();

    await screen.findByText("A status");
    const toggle = screen.getByRole("button", { name: "Filters Account · Inbox · All" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("radio", { name: "Unread" })).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("radio", { name: "Unread" }));
    fireEvent.click(toggle);
    expect(await screen.findByRole("button", { name: "Filters Account · Unread · All" })).toBe(toggle);
  });

  it("marks an opened title while loading more and ignores the superseded continuation", async () => {
    const linked = { ...item("Open this status"), display: { title: "Open this status", href: "/opened", params: {} } };
    const continuation = deferred<Response>();
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(page([linked], [], "more")))
      .mockImplementationOnce(() => continuation.promise)
      .mockResolvedValueOnce(response({ applied: true, item: { ...linked, unread: false } }))
      .mockResolvedValueOnce(response(page([{ ...linked, unread: false }], [], "more")));
    show();

    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    fireEvent.click(screen.getByRole("link", { name: "Open this status" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Mark as unread" })).toBeEnabled());
    expect(screen.getByRole("status", { name: "Current route" })).toHaveTextContent("/opened");
    expect(fetchMock).toHaveBeenNthCalledWith(3, `${BASE}/marks`, expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ kind: linked.kind, item_key: linked.item_key, revision: linked.revision, read: true }),
    }));
    await act(async () => continuation.resolve(response(page([item("Stale continuation")], []))));
    expect(screen.queryByText("Stale continuation")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
  });
});
