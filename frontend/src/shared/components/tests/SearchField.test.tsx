import { render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SearchField } from "../SearchField";

describe("SearchField", () => {
  it("clears a populated search value with an accessible button", async () => {
    const onChange = vi.fn();
    render(
      <SearchField
        ariaLabel="Search Chats"
        clearLabel="Clear Search Chats"
        value="policy"
        onChange={onChange}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Clear Search Chats" }));
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("applies the same clear policy on Escape", async () => {
    const onChange = vi.fn();
    const onClear = vi.fn();
    render(
      <SearchField
        ariaLabel="키 이름 검색"
        clearLabel="키 이름 검색 지우기"
        onChange={onChange}
        onClear={onClear}
        value="노트북"
      />,
    );

    const search = screen.getByRole("searchbox", { name: "키 이름 검색" });
    search.focus();
    await userEvent.keyboard("{Escape}");
    expect(onChange).toHaveBeenCalledWith("");
    expect(onClear).toHaveBeenCalledOnce();
  });

  it("returns focus to the controlled input after its clear button unmounts", async () => {
    function ControlledSearch() {
      const [value, setValue] = useState("policy");
      return (
        <SearchField
          ariaLabel="Search Chats"
          clearLabel="Clear Search Chats"
          onChange={setValue}
          value={value}
        />
      );
    }
    render(<ControlledSearch />);
    await userEvent.click(
      screen.getByRole("button", { name: "Clear Search Chats" }),
    );

    expect(screen.queryByRole("button", { name: "Clear Search Chats" }))
      .toBeNull();
    await waitFor(() =>
      expect(screen.getByRole("searchbox", { name: "Search Chats" }))
        .toHaveFocus(),
    );
  });

  it("exposes a localized manual submit action and loading state", () => {
    render(
      <form>
        <SearchField
          ariaLabel="Search key name"
          clearLabel="Clear key search"
          loading
          onChange={vi.fn()}
          submitLabel="Search keys"
          value=""
        />
      </form>,
    );

    expect(screen.getByRole("searchbox", { name: "Search key name" }))
      .toHaveAttribute("type", "search");
    expect(screen.getByRole("button", { name: "Search keys" })).toBeDisabled();
    expect(screen.getByRole("searchbox").closest(".search-field"))
      .toHaveAttribute("aria-busy", "true");
  });
});
