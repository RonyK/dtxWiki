import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
});
