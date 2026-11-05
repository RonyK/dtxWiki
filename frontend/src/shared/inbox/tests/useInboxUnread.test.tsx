import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as inboxApi from "../../api/inbox";
import type { InboxPage } from "../../api/inbox";
import { useInboxUnread } from "../useInboxUnread";

function UnreadProbe() {
  const unread = useInboxUnread(null, true);
  return <output>{unread ? "Unread messages" : "No unread messages"}</output>;
}

function page(nextCursor: string | null): InboxPage {
  return { items: [], status_items: [], status_overflow: [], next_cursor: nextCursor };
}

describe("useInboxUnread", () => {
  afterEach(() => vi.restoreAllMocks());

  it("follows empty continuation pages to a visible unread item", async () => {
    const visible = page(null);
    visible.items.push({
      kind: "test.note",
      item_key: "visible-old",
      revision: "v1",
      event_at: null,
      unread: true,
      archived: false,
      display: { title: "Still unread", href: null, params: {} },
    });
    const list = vi.spyOn(inboxApi, "listInbox")
      .mockResolvedValueOnce(page("hidden-page"))
      .mockResolvedValueOnce(page("visible-page"))
      .mockResolvedValueOnce(visible);

    render(<UnreadProbe />);

    expect(await screen.findByText("Unread messages")).toBeInTheDocument();
    expect(list).toHaveBeenNthCalledWith(2, inboxApi.ACCOUNT_INBOX_SCOPE, {
      state: "unread", limit: 1, cursor: "hidden-page",
    });
    expect(list).toHaveBeenNthCalledWith(3, inboxApi.ACCOUNT_INBOX_SCOPE, {
      state: "unread", limit: 1, cursor: "visible-page",
    });
  });
});
