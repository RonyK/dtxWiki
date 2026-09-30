import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { RecentItemsList } from "../RecentItemsList";
import type { RecentItem } from "../RecentItemsList";

type Row = RecentItem;

const ITEMS: Row[] = [
  { id: "a", title: "Alpha", subtitle: "alpha-slug" },
  { id: "b", title: "Beta", subtitle: "beta-slug" },
];

function renderList(overrides: Partial<Parameters<typeof RecentItemsList>[0]> = {}) {
  const props = {
    kind: "context" as const,
    items: ITEMS,
    total: 60,
    hasMore: true,
    loading: false,
    onLoadMore: vi.fn(),
    onSelect: vi.fn(),
    headingLabel: "Context",
    emptyHeading: "Nothing here yet",
    loadMoreLabel: "Load more",
    loadingMoreLabel: "Loading",
    noun: "items",
    ...overrides,
  };
  render(<RecentItemsList {...props} />);
  return props;
}

describe("RecentItemsList", () => {
  it("shows the exact authoritative total even when more rows remain", () => {
    // total is the server's exact count; hasMore just means more are unloaded.
    // An exact total renders as "60 items" (F-3), never a misleading "60+".
    renderList({ total: 60, hasMore: true });
    expect(screen.getByText("60 items")).toHaveTextContent(/^60\s*items$/);
    expect(screen.queryByText("60+")).not.toBeInTheDocument();
  });

  it("shows an 'N+' lower bound only when the total is unknown but more remain", () => {
    renderList({ total: null, hasMore: true });
    expect(screen.getByText(`${ITEMS.length}+ items`))
      .toHaveTextContent(`${ITEMS.length}+`);
  });

  it("shows an exact count and hides Load more when !hasMore", () => {
    renderList({ total: 2, hasMore: false });
    expect(screen.getByText("2 items")).toHaveTextContent(/^2\s*items$/);
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
  });

  it("falls back to items.length when total is null", () => {
    renderList({ total: null, hasMore: false });
    expect(screen.getByText(`${ITEMS.length} items`))
      .toHaveTextContent(`${ITEMS.length}`);
  });

  it("fires onLoadMore when the control is pressed", async () => {
    const props = renderList({ hasMore: true });
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(props.onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("disables Load more and swaps the label while loading", () => {
    renderList({ hasMore: true, loading: true });
    const button = screen.getByRole("button", { name: "Loading" });
    expect(button).toBeDisabled();
  });

  it("selects a row by id and marks the active row", async () => {
    const props = renderList({ activeId: "a" });
    expect(screen.getByRole("button", { name: /Alpha/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await userEvent.click(screen.getByRole("button", { name: /Beta/ }));
    expect(props.onSelect).toHaveBeenCalledWith("b");
  });

  it("renders the empty copy when there are no items", () => {
    renderList({ items: [], total: 0, hasMore: false });
    expect(screen.getByText("Nothing here yet")).toBeInTheDocument();
  });

  it("owns stable keys for custom rows without requiring caller keys", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    renderList({ renderRow: (item) => <div>{item.title}</div> });

    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Beta")).toBeInTheDocument();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});
