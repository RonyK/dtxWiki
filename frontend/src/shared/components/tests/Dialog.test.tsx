import { readAppStyles } from "../../../test-support/appStyles";

import { StrictMode, useRef, useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Dialog } from "../Dialog";

describe("Dialog", () => {
  it("renders dialog with aria-modal, role and labelledBy linkage", () => {
    render(
      <Dialog labelledBy="dialog-heading" onClose={() => {}}>
        <h2 id="dialog-heading">Confirm action</h2>
        <p>Body content</p>
      </Dialog>,
    );

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-labelledby", "dialog-heading");
    expect(screen.getByRole("heading", { name: "Confirm action" })).toHaveAttribute(
      "id",
      "dialog-heading",
    );
  });

  it("closes when Escape is pressed", () => {
    const onClose = vi.fn();
    render(
      <Dialog labelledBy="dialog-heading" onClose={onClose}>
        <h2 id="dialog-heading">Confirm</h2>
      </Dialog>,
    );

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not close from implicit dismissal while dismissible is false", () => {
    const onClose = vi.fn();
    render(
      <Dialog dismissible={false} labelledBy="dialog-heading" onClose={onClose}>
        <h2 id="dialog-heading">Saving</h2>
      </Dialog>,
    );

    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.mouseDown(document.querySelector(".dialog-backdrop") as Element);

    expect(onClose).not.toHaveBeenCalled();
  });

  it("respects Escape already handled by an inner popup", () => {
    const onClose = vi.fn();
    render(
      <Dialog labelledBy="dialog-heading" onClose={onClose}>
        <h2 id="dialog-heading">Move item</h2>
        <button
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
            }
          }}
          type="button"
        >
          Inner popup
        </button>
      </Dialog>,
    );

    fireEvent.keyDown(screen.getByRole("button", { name: "Inner popup" }), {
      key: "Escape",
    });

    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes when the backdrop is mousedown'd, but not when the panel is", () => {
    const onClose = vi.fn();
    render(
      <Dialog labelledBy="dialog-heading" onClose={onClose}>
        <h2 id="dialog-heading">Confirm</h2>
      </Dialog>,
    );

    const panel = screen.getByRole("dialog");
    fireEvent.mouseDown(panel);
    expect(onClose).not.toHaveBeenCalled();

    const backdrop = document.querySelector(".dialog-backdrop");
    expect(backdrop).not.toBeNull();
    fireEvent.mouseDown(backdrop as Element);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("applies an optional className to the panel", () => {
    render(
      <Dialog className="custom-panel" labelledBy="dialog-heading" onClose={() => {}}>
        <h2 id="dialog-heading">Hi</h2>
      </Dialog>,
    );

    expect(screen.getByRole("dialog")).toHaveClass("dialog-panel", "custom-panel");
  });

  it("renders a fixed footer outside the scroll region", () => {
    render(
      <Dialog
        footer={<button type="button">Save</button>}
        labelledBy="dialog-heading"
        onClose={() => {}}
      >
        <h2 id="dialog-heading">Edit</h2>
      </Dialog>,
    );

    const dialog = screen.getByRole("dialog");
    const footer = dialog.querySelector(".dialog-footer");
    const scrollRegion = dialog.querySelector(".dialog-scroll-region");
    expect(footer).not.toBeNull();
    expect(scrollRegion).not.toBeNull();
    expect(footer?.parentElement).toBe(dialog);
    expect(scrollRegion?.contains(screen.getByRole("button", { name: "Save" })))
      .toBe(false);
  });

  it("portals the backdrop to body instead of a transformed caller", () => {
    const { container } = render(
      <div style={{ transform: "translateY(0)" }}>
        <Dialog labelledBy="dialog-heading" onClose={() => {}}>
          <h2 id="dialog-heading">Nested modal</h2>
        </Dialog>
      </div>,
    );

    const backdrop = document.querySelector(".dialog-backdrop");
    expect(backdrop?.parentElement).toBe(document.body);
    expect(container.querySelector(".dialog-backdrop")).toBeNull();
  });

  it("keeps base dialog panels scrollable inside the viewport", () => {
    const styles = readAppStyles();

    expect(styles).toContain("--z-dialog: 100;");
    expect(styles).toContain("z-index: var(--z-dialog);");
    expect(styles).toContain("max-height: calc(100dvh - 40px);");
    expect(styles).toContain(".dialog-scroll-region");
    expect(styles).toContain("overflow: auto;");
  });

  it("traps Tab focus inside the panel when focus tries to leave", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">Outside before</button>
        <Dialog labelledBy="dialog-heading" onClose={() => {}}>
          <h2 id="dialog-heading">Confirm</h2>
          <button type="button">Inside</button>
        </Dialog>
        <button type="button">Outside after</button>
      </>,
    );

    const outsideBefore = screen.getByRole("button", { name: "Outside before" });
    // Move focus outside the dialog, then Tab — trap should pull focus back into the panel.
    outsideBefore.focus();
    expect(document.activeElement).toBe(outsideBefore);

    await user.tab();
    // Activated focus must end up inside the dialog (either the inside button or the panel itself).
    const panel = screen.getByRole("dialog");
    expect(panel.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(outsideBefore);
    expect(document.activeElement).not.toBe(
      screen.getByRole("button", { name: "Outside after" }),
    );
  });

  it("auto-focuses the panel when opened", () => {
    render(
      <Dialog labelledBy="dialog-heading" onClose={() => {}}>
        <h2 id="dialog-heading">Confirm</h2>
      </Dialog>,
    );
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });

  it("preserves a descendant autoFocus request during StrictMode effect replay", () => {
    render(
      <StrictMode>
        <Dialog labelledBy="dialog-heading" onClose={() => {}}>
          <h2 id="dialog-heading">Create item</h2>
          <input aria-label="Page title" autoFocus />
        </Dialog>
      </StrictMode>,
    );

    expect(screen.getByLabelText("Page title")).toHaveFocus();
  });

  it("keeps focus in place when onClose changes identity on a parent re-render", () => {
    function Harness({ onClose }: { onClose: () => void }) {
      return (
        <Dialog labelledBy="dialog-heading" onClose={onClose}>
          <h2 id="dialog-heading">Confirm</h2>
          <input aria-label="Name" />
        </Dialog>
      );
    }
    const { rerender } = render(<Harness onClose={() => {}} />);
    const input = screen.getByLabelText("Name");
    input.focus();
    expect(document.activeElement).toBe(input);

    // A background state flip in the parent passes a fresh inline arrow; the
    // focus side effects must not re-run and steal the caret from the input.
    rerender(<Harness onClose={() => {}} />);

    expect(document.activeElement).toBe(input);
  });

  it("invokes the latest onClose on Escape after a re-render", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(
      <Dialog labelledBy="dialog-heading" onClose={first}>
        <h2 id="dialog-heading">Confirm</h2>
      </Dialog>,
    );
    rerender(
      <Dialog labelledBy="dialog-heading" onClose={second}>
        <h2 id="dialog-heading">Confirm</h2>
      </Dialog>,
    );

    fireEvent.keyDown(window, { key: "Escape" });

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("restores focus to the previously focused element when closed", () => {
    function Harness({ open }: { open: boolean }) {
      return (
        <>
          <button type="button">Opener</button>
          {open ? (
            <Dialog labelledBy="dialog-heading" onClose={() => {}}>
              <h2 id="dialog-heading">Confirm</h2>
            </Dialog>
          ) : null}
        </>
      );
    }
    const { rerender } = render(<Harness open={false} />);
    const opener = screen.getByRole("button", { name: "Opener" });
    opener.focus();

    rerender(<Harness open />);
    expect(document.activeElement).toBe(screen.getByRole("dialog"));

    rerender(<Harness open={false} />);
    expect(document.activeElement).toBe(opener);
  });

  it("uses a connected fallback when closing removes the opener", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      const [openerPresent, setOpenerPresent] = useState(true);
      const fallbackRef = useRef<HTMLButtonElement | null>(null);
      return (
        <>
          {openerPresent ? (
            <button onClick={() => setOpen(true)} type="button">
              Opener removed by mutation
            </button>
          ) : null}
          <button ref={fallbackRef} type="button">Connected fallback</button>
          {open ? (
            <Dialog
              labelledBy="dialog-heading"
              onClose={() => {
                setOpenerPresent(false);
                setOpen(false);
              }}
              restoreFocusFallback={() => fallbackRef.current}
            >
              <h2 id="dialog-heading">Confirm mutation</h2>
            </Dialog>
          ) : null}
        </>
      );
    }

    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Opener removed by mutation" }));
    expect(screen.getByRole("dialog")).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Connected fallback" })).toHaveFocus(),
    );
  });

  it("does not restore focus into an inert opener subtree", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      const openerScopeRef = useRef<HTMLDivElement | null>(null);
      const fallbackRef = useRef<HTMLButtonElement | null>(null);
      return (
        <>
          <div ref={openerScopeRef}>
            <button onClick={() => setOpen(true)} type="button">Inert opener</button>
          </div>
          <button ref={fallbackRef} type="button">Non-inert fallback</button>
          {open ? (
            <Dialog
              labelledBy="dialog-heading"
              onClose={() => {
                openerScopeRef.current?.setAttribute("inert", "");
                setOpen(false);
              }}
              restoreFocusFallback={() => fallbackRef.current}
            >
              <h2 id="dialog-heading">Confirm mutation</h2>
            </Dialog>
          ) : null}
        </>
      );
    }

    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Inert opener" }));

    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Non-inert fallback" })).toHaveFocus(),
    );
  });

  it("uses the fallback when the connected opener becomes disabled", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      const [openerDisabled, setOpenerDisabled] = useState(false);
      const fallbackRef = useRef<HTMLButtonElement | null>(null);
      return (
        <>
          <button
            disabled={openerDisabled}
            onClick={() => setOpen(true)}
            type="button"
          >
            Disabled after close
          </button>
          <button ref={fallbackRef} type="button">Enabled fallback</button>
          {open ? (
            <Dialog
              labelledBy="disabled-opener-dialog-heading"
              onClose={() => {
                setOpenerDisabled(true);
                setOpen(false);
              }}
              restoreFocusFallback={() => fallbackRef.current}
            >
              <h2 id="disabled-opener-dialog-heading">Disable opener</h2>
            </Dialog>
          ) : null}
        </>
      );
    }

    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Disabled after close" }));
    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Enabled fallback" })).toHaveFocus(),
    );
  });
});
