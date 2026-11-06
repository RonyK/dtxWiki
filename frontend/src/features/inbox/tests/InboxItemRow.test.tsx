import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InboxItem } from "../../../shared/api/inbox";
import { i18n } from "../../../shared/i18n";
import { InboxItemRow, InboxScreen } from "../InboxScreen";
import { presentInboxItem } from "../inboxRenderers";

const workspaceContext = vi.hoisted(() => ({ current: null as unknown }));
vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useOptionalWorkspaceContext: () => workspaceContext.current,
}));

const BASE = "http://127.0.0.1:21080";

const base: InboxItem = {
  kind: "test.retired",
  item_key: "old",
  revision: "v1",
  event_at: "2026-10-28T00:00:00+00:00",
  unread: true,
  archived: false,
  display: null,
};

describe("InboxItemRow", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    workspaceContext.current = null;
  });

  it("loads the route workspace inbox even when the ambient selection differs", async () => {
    workspaceContext.current = {
      selectedWorkspace: { id: "workspace-ambient", title: "Ambient" },
      workspaces: [
        { id: "workspace-ambient", title: "Ambient" },
        { id: "00000000-0000-4000-8000-000000000004", title: "Route workspace" },
      ],
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ data: { items: [], status_items: [], status_overflow: [], next_cursor: null } }),
    } as Response);
    render(
      <MemoryRouter initialEntries={["/workspaces/00000000-0000-4000-8000-000000000004/inbox"]}>
        <InboxScreen />
      </MemoryRouter>,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/00000000-0000-4000-8000-000000000004/inbox?state=all`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/users/me/inbox"),
      expect.anything(),
    );
    // The workspace segment names the route's workspace, not the ambient one.
    expect(screen.getByRole("radio", { name: /Route workspace/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText("Ambient")).not.toBeInTheDocument();
    vi.restoreAllMocks();
  });

  it("renders resolved and unknown items and marks their read state", async () => {
    await i18n.changeLanguage("en");
    const resolved: InboxItem = {
      ...base,
      kind: "test.note",
      item_key: "note",
      unread: false,
      display: { title: "Review ready", href: "/workspaces/00000000-0000-4000-8000-000000000008/review", params: {} },
    };
    const onMark = vi.fn();
    render(
      <MemoryRouter>
        <ul>
          <InboxItemRow busy={false} item={base} onMark={onMark} />
          <InboxItemRow busy={false} item={resolved} onMark={onMark} />
        </ul>
      </MemoryRouter>,
    );

    expect(screen.getByText("A message this version cannot show").closest("a")).toBeNull();
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Review ready" })).toHaveAttribute(
      "href",
      "/workspaces/00000000-0000-4000-8000-000000000008/review",
    );
    const markRead = screen.getByRole("button", { name: "Mark as read" });
    const markUnread = screen.getByRole("button", { name: "Mark as unread" });
    expect(markRead).toHaveAttribute("title", "Mark as read");
    expect(markUnread).toHaveAttribute("title", "Mark as unread");
    fireEvent.click(markRead);
    fireEvent.click(markUnread);
    expect(onMark).toHaveBeenNthCalledWith(1, base, { read: true });
    expect(onMark).toHaveBeenNthCalledWith(2, resolved, { read: false });
    const t = i18n.getFixedT("en", "inbox");
    const renderers = {
      "test.note": { title: () => "Rendered by its kind", summary: () => "Its summary" },
    };
    expect(presentInboxItem(resolved, t, renderers)).toMatchObject({
      title: "Rendered by its kind",
      summary: "Its summary",
    });
    expect(presentInboxItem(base, t, renderers)).toMatchObject({
      title: "A message this version cannot show",
      summary: null,
    });
  });

  it("archives status items and messages alike, explains removal, and restores from Archived", async () => {
    await i18n.changeLanguage("en");
    // A status item with a stated end and a message with a condition only.
    const until = new Date(Date.now() + 3 * 86_400_000 + 3_600_000).toISOString();
    let messages: InboxItem[] = [
      {
        ...base, kind: "access_key_expiry", item_key: "key",
        display: { title: "CI key", params: { stage: "expiring", expires_at: until, visible_until: until } },
      },
      {
        ...base, kind: "review_result", item_key: "result",
        display: { title: "Approved edit", params: { decision: "approve" } },
      },
    ];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const respond = (data: unknown) => ({ ok: true, status: 200, json: async () => ({ data }) }) as Response;
      if (init?.method === "POST") {
        const marks = JSON.parse(String(init.body));
        messages = messages.map((item) => item.item_key === marks.item_key ? { ...item, archived: marks.archived } : item);
        return respond({ applied: true, item: messages.find((item) => item.item_key === marks.item_key) });
      }
      const query = new URL(String(url)).searchParams;
      const archived = query.get("state") === "archived";
      // The first archive scan finds only entries whose source is gone.
      if (archived && !query.has("cursor")) {
        return respond({ status_items: [], items: [], status_overflow: [], next_cursor: "archive-next" });
      }
      const visible = messages.filter((item) => item.archived === archived);
      return respond({
        status_items: archived ? [] : visible.filter((item) => item.kind !== "review_result"),
        items: archived ? visible : visible.filter((item) => item.kind === "review_result"),
        status_overflow: [], next_cursor: null,
      });
    });
    const changed = vi.fn();
    window.addEventListener("dtxwiki:inbox-changed", changed);
    try {
      render(<MemoryRouter initialEntries={["/inbox"]}><InboxScreen /></MemoryRouter>);
      expect(await screen.findAllByRole("button", { name: "Archive" })).toHaveLength(2);

      // The dated end and the condition-only rule open their full text.
      fireEvent.click(screen.getByRole("button", { name: "Auto-removed in 3 days" }));
      expect(screen.getByText(/May disappear sooner if the key is revoked/)).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "When this is removed" }));
      expect(screen.getByText("Disappears if the decision is undone or replaced, or you lose access.")).toBeVisible();

      for (const title of ["CI key", "Approved edit"]) {
        const row = screen.getByText(title).closest("li")!;
        fireEvent.click(within(row).getByRole("button", { name: "Archive" }));
        await waitFor(() => expect(row).not.toBeInTheDocument());
      }
      expect(fetchMock).toHaveBeenCalledWith(`${BASE}/api/v1/users/me/inbox/marks`, expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ kind: "access_key_expiry", item_key: "key", revision: "v1", archived: true }),
      }));

      // An empty scan with older entries explains itself instead of "No archived messages".
      fireEvent.click(screen.getByRole("radio", { name: "Archived" }));
      expect(await screen.findByText("None of the recent items can be shown now.")).toBeVisible();
      expect(screen.queryByText("No archived messages.")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Check older items" }));
      expect(await screen.findAllByRole("button", { name: "Move back to Inbox" })).toHaveLength(2);
      const keyRow = screen.getByText("CI key").closest("li")!;
      fireEvent.click(within(keyRow).getByRole("button", { name: "Move back to Inbox" }));
      await waitFor(() => expect(keyRow).not.toBeInTheDocument());

      fireEvent.click(screen.getByRole("radio", { name: "Inbox" }));
      const restored = (await screen.findByText("CI key")).closest("li")!;
      expect(within(restored).getByRole("button", { name: "Archive" })).toBeVisible();
      expect(changed).toHaveBeenCalledTimes(3);
    } finally {
      window.removeEventListener("dtxwiki:inbox-changed", changed);
    }
  });

  it("refills the capped live list after archiving while keeping loaded message pages", async () => {
    let keys: InboxItem[] = Array.from({ length: 21 }, (_, index) => ({
      ...base, kind: "access_key_expiry", item_key: String(index + 1),
      display: { title: `Key ${index + 1}`, params: {} },
    }));
    const messages: InboxItem[] = [
      { ...base, item_key: "message-1", unread: false, display: { title: "Message 1", params: {} } },
      { ...base, item_key: "message-2", display: { title: "Message 2", params: {} } },
    ];
    let pauseRefresh = false;
    let releaseRefresh: (() => void) | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      let data: unknown;
      if (init?.method === "POST") {
        const marks = JSON.parse(String(init.body));
        keys = keys.map((item) => item.item_key === marks.item_key ? { ...item, archived: marks.archived } : item);
        data = { applied: true, item: keys.find((item) => item.item_key === marks.item_key) };
      } else {
        const query = new URL(String(url)).searchParams;
        const more = query.has("cursor");
        const live = keys.filter((item) => !item.archived);
        const visibleMessages = messages.filter((item) => query.get("state") !== "unread" || item.unread);
        data = {
          status_items: more ? [] : live.slice(0, 20),
          status_overflow: !more && live.length > 20 ? [{ kind: "access_key_expiry", total: live.length }] : [],
          items: [visibleMessages[more ? 1 : 0]],
          next_cursor: !more && visibleMessages.length > 1 ? "message-next" : null,
        };
        if (pauseRefresh && !more && query.get("state") === "all") {
          pauseRefresh = false;
          return new Promise<Response>((resolve) => {
            releaseRefresh = () => resolve({ ok: true, status: 200, json: async () => ({ data }) } as Response);
          });
        }
      }
      return { ok: true, status: 200, json: async () => ({ data }) } as Response;
    });
    render(<MemoryRouter initialEntries={["/inbox"]}><InboxScreen /></MemoryRouter>);
    await screen.findByText("Key 1");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Message 2");

    // The 21st source item must stay reachable after removing the initial 20.
    for (let index = 1; index <= 20; index++) {
      const row = screen.getByText(`Key ${index}`).closest("li")!;
      fireEvent.click(within(row).getByRole("button", { name: "Archive" }));
      await waitFor(() => expect(row).not.toBeInTheDocument());
      await waitFor(() => expect(screen.getAllByRole("button", { name: "Archive" }))
        .toHaveLength(Math.min(20, 21 - index) + 2));
    }
    expect(screen.getByText("Key 21")).toBeVisible();
    expect(screen.getByText("Message 1")).toBeVisible();
    expect(screen.getByText("Message 2")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();

    // A pending refill belongs to its original view, even after switching filters.
    pauseRefresh = true;
    fireEvent.click(within(screen.getByText("Key 21").closest("li")!).getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(releaseRefresh).toBeDefined());
    fireEvent.click(screen.getByRole("radio", { name: "Unread" }));
    await screen.findByText("Message 2");
    expect(screen.queryByText("Message 1")).not.toBeInTheDocument();
    await act(async () => { releaseRefresh!(); });
    expect(screen.queryByText("Message 1")).not.toBeInTheDocument();
  });

  it("updates the countdown and refreshes all loaded archive pages at the deadline", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2031-01-01T00:00:00Z"));
    const until = "2031-01-01T00:01:30Z";
    const expiring: InboxItem = {
      ...base, kind: "access_key_expiry", archived: true,
      display: { title: "Expiring notice", params: { stage: "expired", visible_until: until } },
    };
    const kept: InboxItem[] = [
      { ...base, item_key: "first", archived: true, display: { title: "First archive", params: {} } },
      { ...base, item_key: "last", archived: true, display: { title: "Last archive", params: {} } },
    ];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      const query = new URL(String(url)).searchParams;
      const archived = query.get("state") === "archived";
      const more = query.has("cursor");
      const items = !archived ? [] : !more ? [kept[0]]
        : Date.now() < new Date(until).getTime() ? [expiring, kept[1]] : [kept[1]];
      return {
        ok: true, status: 200,
        json: async () => ({ data: {
          status_items: [], status_overflow: [], items,
          next_cursor: archived && !more ? "archive-next" : null,
        } }),
      } as Response;
    });
    let unmount!: () => void;
    await act(async () => {
      ({ unmount } = render(<MemoryRouter initialEntries={["/inbox"]}><InboxScreen /></MemoryRouter>));
    });
    await act(async () => { fireEvent.click(screen.getByRole("radio", { name: "Archived" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Load more" })); });
    expect(screen.getByRole("button", { name: "Auto-removed in 2 minutes" })).toBeVisible();
    const initialRequests = fetchMock.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(screen.getByRole("button", { name: "Auto-removed in 1 minute" })).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(initialRequests);

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(screen.queryByText("Expiring notice")).not.toBeInTheDocument();
    expect(screen.getByText("First archive")).toBeVisible();
    expect(screen.getByText("Last archive")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(initialRequests + 2);

    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    expect(fetchMock).toHaveBeenCalledTimes(initialRequests + 2);
  });
});
