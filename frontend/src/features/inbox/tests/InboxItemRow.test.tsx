import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

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
  it("loads the route workspace inbox even when the ambient selection differs", async () => {
    workspaceContext.current = {
      selectedWorkspace: { id: "workspace-ambient", title: "Ambient" },
      workspaces: [
        { id: "workspace-ambient", title: "Ambient" },
        { id: "workspace-route", title: "Route workspace" },
      ],
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ data: { items: [], status_items: [], status_overflow: [], next_cursor: null } }),
    } as Response);
    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-route/inbox"]}>
        <InboxScreen />
      </MemoryRouter>,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      `${BASE}/api/v1/workspaces/workspace-route/inbox?state=all`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/users/me/inbox"),
      expect.anything(),
    );
    vi.restoreAllMocks();
  });

  it("links only what the server resolved, lets a kind's renderer title it, and shows an unknown kind without content", async () => {
    await i18n.changeLanguage("en");
    const resolved: InboxItem = {
      ...base,
      kind: "test.note",
      item_key: "note",
      display: { title: "Review ready", href: "/workspaces/ws-1/review", params: {} },
    };
    render(
      <MemoryRouter>
        <ul>
          <InboxItemRow busy={false} item={base} onMark={vi.fn()} />
          <InboxItemRow busy={false} item={resolved} onMark={vi.fn()} />
        </ul>
      </MemoryRouter>,
    );

    expect(screen.getByText("A message this version cannot show").closest("a")).toBeNull();
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Review ready" })).toHaveAttribute(
      "href",
      "/workspaces/ws-1/review",
    );
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
});
