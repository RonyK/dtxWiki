import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import type { ActivityItem } from "../../api/operations";
import { ActivityEventCard } from "../ActivityEventCard";

function item(overrides: Partial<ActivityItem> = {}): ActivityItem {
  return {
    id: "event-1", lane: "history", event_key: "tree.node.rename", event_args: {},
    title: "tree.node.rename", body: "tree.node.rename", target_title: "New name",
    details: { page_id: "page-1", rename: { before: "Old name", after: "New name" } },
    actor: { actor_kind: "human", actor_id: null, display_name: "Actual author", is_current_user: true },
    target: { target_kind: "workspace_tree_node", target_id: "node-1" },
    created_at: "2026-10-28T10:00:00Z", review_item_id: null, affordances: ["open"],
    ...overrides,
  };
}

describe("ActivityEventCard", () => {
  it("shows the actual rename and preserves the author's name with keyboard-accessible details", async () => {
    render(<MemoryRouter><ActivityEventCard item={item()} workspaceId="workspace-1" /></MemoryRouter>);
    const article = screen.getByRole("article");
    expect(within(article).getByText("Renamed")).toHaveClass("context-event-chip");
    expect(within(article).getByText("Old name")).toHaveClass("context-event-before");
    expect(within(article).getByText("New name").tagName).toBe("STRONG");
    expect(within(article).queryByText("tree.node.rename")).toBeNull();
    const author = within(article).getByRole("button", { name: /Actual author/ });
    await userEvent.click(author);
    expect(within(screen.getByRole("dialog")).getByText("Actual author")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(author).toHaveFocus();
  });

  it("links an immutable publication to its actual comparison but never links a past mutable save as a snapshot", () => {
    const data = item({
      event_key: "context_page.draft.publish", body: "context_page.draft.publish",
      details: {
        page_id: "page-1", version: { id: "after", number: 8, immutable: true },
        previous_published_version: { id: "before", number: 3, immutable: true },
      },
    });
    const { rerender } = render(<MemoryRouter><ActivityEventCard item={data} workspaceId="workspace-1" /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Compare changes" })).toHaveAttribute("href", "/workspaces/workspace-1/context/pages/page-1?tab=history&compareFrom=before&compareTo=after");
    rerender(<MemoryRouter><ActivityEventCard item={{ ...data, event_key: "page.autosave.draft", body: "page.autosave.draft" }} workspaceId="workspace-1" /></MemoryRouter>);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Change summary not recorded")).toBeInTheDocument();
    rerender(<MemoryRouter><ActivityEventCard item={{ ...data, event_key: "page.submit_context_edit_for_review", body: "page.submit_context_edit_for_review", review_item_id: "review-1" }} workspaceId="workspace-1" /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "View review" })).toHaveAttribute("href", "/workspaces/workspace-1/review/review-1");
  });
});
