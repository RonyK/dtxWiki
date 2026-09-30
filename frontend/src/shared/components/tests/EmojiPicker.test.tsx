import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { EmojiPicker } from "../EmojiPicker";

describe("EmojiPicker", () => {
  it("filters emoji options and returns the selected Unicode character", async () => {
    const onSelect = vi.fn();
    render(<EmojiPicker onClose={vi.fn()} onSelect={onSelect} />);

    await userEvent.type(screen.getByLabelText("Search emoji"), "rocket");
    const options = screen.getByRole("listbox", { name: "Emoji options" });
    await userEvent.click(within(options).getByRole("option", { name: "Insert emoji Rocket" }));

    expect(onSelect).toHaveBeenCalledWith("🚀");
  });

  it("closes from Escape", async () => {
    const onClose = vi.fn();
    render(<EmojiPicker onClose={onClose} onSelect={vi.fn()} />);

    await userEvent.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
