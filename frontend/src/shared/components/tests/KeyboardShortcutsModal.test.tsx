import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { KeyboardShortcutsModal } from "../KeyboardShortcutsModal";

describe("KeyboardShortcutsModal", () => {
  it("renders shortcut groups and closes from the chrome", () => {
    const onClose = vi.fn();

    render(
      <KeyboardShortcutsModal
        groups={[
          {
            label: "Navigation",
            items: [{ action: "Show this panel", keys: ["?"] }],
          },
        ]}
        onClose={onClose}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    expect(dialog).toHaveTextContent("Navigation");
    expect(dialog).toHaveTextContent("Show this panel");
    expect(screen.getByText("?").tagName).toBe("KBD");

    fireEvent.click(screen.getByRole("button", { name: "Close keyboard shortcuts" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("uses modal focus and Escape handling from the shared dialog", () => {
    const onClose = vi.fn();

    render(
      <KeyboardShortcutsModal
        groups={[
          {
            label: "Editing",
            items: [{ action: "Save now", keys: ["Ctrl/Cmd", "S"] }],
          },
        ]}
        onClose={onClose}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-labelledby", "keyboard-shortcuts-title");
    expect(document.activeElement).toBe(dialog);

    fireEvent.keyDown(window, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on backdrop mousedown but keeps panel interaction inside", () => {
    const onClose = vi.fn();
    render(
      <KeyboardShortcutsModal
        groups={[
          {
            label: "Navigation",
            items: [{ action: "Show this panel", keys: ["?"] }],
          },
        ]}
        onClose={onClose}
      />,
    );

    fireEvent.mouseDown(screen.getByRole("dialog", { name: "Keyboard shortcuts" }));
    expect(onClose).not.toHaveBeenCalled();

    const backdrop = document.querySelector(".dialog-backdrop");
    expect(backdrop).not.toBeNull();
    fireEvent.mouseDown(backdrop as Element);

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
