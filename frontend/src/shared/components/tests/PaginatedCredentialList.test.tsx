import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PaginatedCredentialList } from "../PaginatedCredentialList";

describe("PaginatedCredentialList", () => {
  it("submits and clears optional server-side search through shared chrome", async () => {
    const user = userEvent.setup();
    const onSearchChange = vi.fn();
    const onSearchSubmit = vi.fn();
    const { rerender } = renderList({
      onSearchChange,
      onSearchSubmit,
      searchValue: "",
    });

    await user.type(screen.getByRole("searchbox", { name: "Search clients" }), "nova");
    expect(onSearchChange).toHaveBeenLastCalledWith("a");

    rerender(
      listElement({ onSearchChange, onSearchSubmit, searchValue: "nova" }),
    );
    const searchRegion = screen.getByRole("search");
    expect(searchRegion.tagName).toBe("FORM");
    expect(searchRegion.querySelector(":scope > .search-field")).not.toBeNull();
    const searchbox = screen.getByRole("searchbox", { name: "Search clients" });
    searchbox.focus();
    await user.keyboard("{Enter}");
    expect(onSearchSubmit).toHaveBeenLastCalledWith("nova");

    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(onSearchSubmit).toHaveBeenCalledWith("nova");
    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(onSearchChange).toHaveBeenLastCalledWith("");
    expect(onSearchSubmit).toHaveBeenLastCalledWith("");
  });

  it("offers an explicit retry when the current page fails", async () => {
    const onRetry = vi.fn();
    renderList({
      error: new Error("raw client-request secret"),
      onRetry,
      onSearchChange: vi.fn(),
      onSearchSubmit: vi.fn(),
      retryLabel: "Retry client list",
      searchValue: "",
    });

    expect(screen.getByText("Could not load clients")).toBeInTheDocument();
    expect(
      screen.queryByText("raw client-request secret"),
    ).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Retry client list" }),
    );
    expect(onRetry).toHaveBeenCalledOnce();
  });
});

type ListInput = {
  error?: unknown;
  onRetry?: () => void;
  onSearchChange: (value: string) => void;
  onSearchSubmit: (value: string) => void;
  retryLabel?: string;
  searchValue: string;
};

function renderList(input: ListInput) {
  return render(listElement(input));
}

function listElement(input: ListInput) {
  return (
    <PaginatedCredentialList
      emptyText="None"
      error={null}
      errorMessage="Could not load clients"
      filter="active"
      filterLabel="State"
      filterOptions={[{ count: 1, key: "active", label: "Active" }]}
      filteredCount={1}
      focusRevision={0}
      hasNext={false}
      hasPrevious={false}
      itemCount={1}
      loading={false}
      loadingText="Loading"
      nextLabel="Next"
      onFilterChange={vi.fn()}
      onNext={vi.fn()}
      onPrevious={vi.fn()}
      pageIndex={0}
      pageSize={20}
      previousLabel="Previous"
      rangeLabel={() => "1 of 1"}
      recoveredFromCursor={false}
      recoveredText="Reset"
      searchClearLabel="Clear search"
      searchLabel="Search clients"
      searchPlaceholder="Search"
      searchSubmitLabel="Search"
      {...input}
    >
      <div>row</div>
    </PaginatedCredentialList>
  );
}
