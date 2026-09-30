import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { WorkspacePanelChrome } from "../WorkspacePanelChrome";

describe("WorkspacePanelChrome", () => {
  it("renders nothing when no controls are supplied", () => {
    const { container } = render(<WorkspacePanelChrome />);
    expect(container.querySelectorAll("button")).toHaveLength(0);
  });

  it("renders refresh, widen, and close in a fixed order and wires handlers", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    const onExpandWidth = vi.fn();
    const onClose = vi.fn();
    render(
      <WorkspacePanelChrome
        refresh={{ onRefresh, label: "Refresh Context tree" }}
        expandWidth={{ onExpandWidth, label: "Widen Context explorer" }}
        close={{ onClose, label: "Hide Context explorer" }}
      />,
    );

    const buttons = screen.getAllByRole("button");
    expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Refresh Context tree",
      "Widen Context explorer",
      "Hide Context explorer",
    ]);

    await user.click(screen.getByRole("button", { name: "Refresh Context tree" }));
    await user.click(screen.getByRole("button", { name: "Widen Context explorer" }));
    await user.click(screen.getByRole("button", { name: "Hide Context explorer" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onExpandWidth).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("marks refresh busy with the busy label and disables it while refreshing", () => {
    render(
      <WorkspacePanelChrome
        refresh={{
          onRefresh: vi.fn(),
          refreshing: true,
          label: "Refresh Context tree",
          busyLabel: "Refreshing Context tree…",
        }}
      />,
    );
    const button = screen.getByRole("button", { name: "Refreshing Context tree…" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
  });

  it("flips the widen toggle to pressed at max width", () => {
    render(
      <WorkspacePanelChrome
        expandWidth={{
          onExpandWidth: vi.fn(),
          atMax: true,
          label: "Reset Context width",
        }}
      />,
    );
    const button = screen.getByRole("button", { name: "Reset Context width" });
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveAttribute("data-expand-width-at-max", "true");
  });

  it("keeps the widen toggle unpressed and emits the accent hook when not at max", () => {
    // data-expand-width-at-max="false" must always be present: the tree rail's
    // accent CSS (`.tree-rail-header-actions [data-expand-width-at-max="true"]`)
    // relies on this attribute existing on the expand button (T13 parity).
    render(
      <WorkspacePanelChrome
        expandWidth={{ onExpandWidth: vi.fn(), label: "Widen Context explorer" }}
      />,
    );
    const button = screen.getByRole("button", { name: "Widen Context explorer" });
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveAttribute("data-expand-width-at-max", "false");
  });

  it("omits the overflow trigger when no overflow items are given", () => {
    render(<WorkspacePanelChrome overflow={[]} overflowLabel="More actions" />);
    expect(
      screen.queryByRole("button", { name: "More actions" }),
    ).toBeNull();
  });

  it("opens the overflow menu and runs an item's action, then closes", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <WorkspacePanelChrome
        overflowLabel="More actions"
        overflow={[
          { key: "refresh", icon: null, label: "Refresh", onSelect },
        ]}
      />,
    );

    const trigger = screen.getByRole("button", { name: "More actions" });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const menu = screen.getByRole("menu", { name: "More actions" });

    await user.click(within(menu).getByRole("menuitem", { name: "Refresh" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    // Choosing an item closes the menu and returns focus to the trigger.
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("closes the overflow menu on Escape and returns focus to the trigger", async () => {
    const user = userEvent.setup();
    render(
      <WorkspacePanelChrome
        overflowLabel="More actions"
        overflow={[{ key: "a", icon: null, label: "Widen", onSelect: vi.fn() }]}
      />,
    );

    const trigger = screen.getByRole("button", { name: "More actions" });
    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("closes the overflow menu on Tab and returns focus to the trigger (not lost to body)", async () => {
    const user = userEvent.setup();
    render(
      <>
        <WorkspacePanelChrome
          overflowLabel="More actions"
          overflow={[{ key: "a", icon: null, label: "Widen", onSelect: vi.fn() }]}
        />
        <button type="button">After</button>
      </>,
    );

    const trigger = screen.getByRole("button", { name: "More actions" });
    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    // ARIA menu-button: Tab dismisses the menu. Focus must NOT be swallowed/lost
    // to <body>; it returns to the trigger, a stable control to Tab onward from.
    await user.keyboard("{Tab}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger).toHaveFocus();
    expect(document.body).not.toHaveFocus();
  });

  it("closes the overflow menu on an outside click without restoring focus", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">Outside</button>
        <WorkspacePanelChrome
          overflowLabel="More actions"
          overflow={[{ key: "a", icon: null, label: "Widen", onSelect: vi.fn() }]}
        />
      </>,
    );

    await user.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Outside" }));
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("renders an overflow item as disabled/busy and pressed when active", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <WorkspacePanelChrome
        overflowLabel="More actions"
        overflow={[
          {
            key: "refresh",
            icon: null,
            label: "Refreshing…",
            onSelect,
            disabled: true,
            busy: true,
          },
          {
            key: "widen",
            icon: null,
            label: "Reset width",
            onSelect: vi.fn(),
            active: true,
          },
        ]}
      />,
    );

    await user.click(screen.getByRole("button", { name: "More actions" }));
    const refreshItem = screen.getByRole("menuitem", { name: "Refreshing…" });
    expect(refreshItem).toBeDisabled();
    expect(refreshItem).toHaveAttribute("aria-busy", "true");
    // A toggle item is a menuitemcheckbox with aria-checked (menuitem cannot
    // carry a checked/pressed state).
    const widenItem = screen.getByRole("menuitemcheckbox", { name: "Reset width" });
    expect(widenItem).toHaveAttribute("aria-checked", "true");
    expect(widenItem).toHaveClass("is-active");

    // A disabled row does not fire its action.
    await user.click(refreshItem);
    expect(onSelect).not.toHaveBeenCalled();
  });
});
