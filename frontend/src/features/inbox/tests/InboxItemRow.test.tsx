import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { InboxItem } from "../../../shared/api/inbox";
import { i18n } from "../../../shared/i18n";
import { InboxItemRow } from "../InboxScreen";
import { presentInboxItem } from "../inboxRenderers";

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
