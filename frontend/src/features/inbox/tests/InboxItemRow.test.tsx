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

  it("archives every kind, shows its disappearance rule, and restores it from Archived", async () => {
    const until = "2031-02-03T15:45:00+00:00";
    let messages: InboxItem[] = [
      {
        ...base, kind: "review_attention", item_key: "review",
        display: { title: "Review me", params: { state: "pending" } },
      },
      {
        ...base, kind: "workspace_join", item_key: "join",
        display: { title: "Team", params: { status: "rejected", visible_until: until } },
      },
      {
        ...base, kind: "access_key_expiry", item_key: "key",
        display: {
          title: "CI key",
          params: { stage: "expired", expires_at: "2031-01-30T15:45:00+00:00", visible_until: until },
        },
      },
      {
        ...base, kind: "review_result", item_key: "result",
        display: { title: "Approved edit", params: { decision: "approve" } },
      },
    ];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (init?.method === "POST") {
        const marks = JSON.parse(String(init.body));
        const index = messages.findIndex((item) => item.item_key === marks.item_key);
        messages = messages.map((item, i) => i === index ? { ...item, archived: marks.archived } : item);
        return {
          ok: true, status: 200,
          json: async () => ({ data: { applied: true, item: messages[index] } }),
        } as Response;
      }
      const query = new URL(String(url)).searchParams;
      const archived = query.get("state") === "archived";
      // An empty archive scan still offers its next page, not an empty state.
      if (archived && !query.has("cursor")) {
        return {
          ok: true, status: 200,
          json: async () => ({ data: {
            status_items: [], items: [], status_overflow: [], next_cursor: "archive-next",
          } }),
        } as Response;
      }
      const visible = messages.filter((item) => item.archived === archived);
      return {
        ok: true, status: 200,
        json: async () => ({ data: {
          status_items: archived ? [] : visible.filter((item) => item.kind !== "review_result"),
          items: archived ? visible : visible.filter((item) => item.kind === "review_result"),
          status_overflow: [], next_cursor: null,
        } }),
      } as Response;
    });
    const changed = vi.fn();
    window.addEventListener("dtxwiki:inbox-changed", changed);
    try {
      render(
        <MemoryRouter initialEntries={["/inbox"]}><InboxScreen /></MemoryRouter>,
      );
      expect(await screen.findAllByRole("button", { name: "Archive" })).toHaveLength(4);
      const t = i18n.getFixedT("en", "inbox");
      expect(screen.getByText(t("kinds.review_attention.removal"))).toBeVisible();
      expect(screen.getByText(t("kinds.review_result.removal"))).toBeVisible();
      expect(screen.getByText(t("kinds.workspace_join.resultRemoval", { date: new Date(until) }))).toBeVisible();
      expect(screen.getByText(t("kinds.access_key_expiry.removal", { date: new Date(until) }))).toBeVisible();

      // Use the real screen, so hiding archive actions on status_items fails.
      for (const item of messages) {
        const row = screen.getByText(presentInboxItem(item, t).title).closest("li")!;
        fireEvent.click(within(row).getByRole("button", { name: "Archive" }));
        await waitFor(() => expect(row).not.toBeInTheDocument());
        expect(fetchMock).toHaveBeenCalledWith(`${BASE}/api/v1/users/me/inbox/marks`, expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ kind: item.kind, item_key: item.item_key, revision: item.revision, archived: true }),
        }));
      }
      expect(changed).toHaveBeenCalledTimes(4);
      fireEvent.click(screen.getByRole("radio", { name: "Archived" }));
      fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
      expect(screen.queryByText("No archived messages.")).not.toBeInTheDocument();
      expect(await screen.findAllByRole("button", { name: "Move back to Inbox" })).toHaveLength(4);
      expect(screen.getByText(t("kinds.access_key_expiry.removal", { date: new Date(until) }))).toBeVisible();
      const keyRow = screen.getByText("CI key").closest("li")!;
      fireEvent.click(within(keyRow).getByRole("button", { name: "Move back to Inbox" }));
      await waitFor(() => expect(keyRow).not.toBeInTheDocument());
      fireEvent.click(screen.getByRole("radio", { name: "Inbox" }));
      const restored = await screen.findByText("CI key");
      expect(within(restored.closest("li")!).getByRole("button", { name: "Archive" })).toBeVisible();
      expect(within(restored.closest("li")!).getByRole("button", { name: "Mark as read" })).toBeVisible();
      expect(changed).toHaveBeenCalledTimes(5);
    } finally {
      window.removeEventListener("dtxwiki:inbox-changed", changed);
    }
  });
});
