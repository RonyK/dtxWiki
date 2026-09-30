import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AdvancedSection } from "../AdvancedSection";

describe("AdvancedSection", () => {
  it("renders a collapsed advanced section by default", () => {
    render(
      <AdvancedSection label="Advanced settings">
        <label htmlFor="slug">Slug</label>
        <input id="slug" />
      </AdvancedSection>,
    );

    expect(
      screen.getByRole("button", { name: "Advanced settings" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Slug")).not.toBeInTheDocument();
  });

  it("opens and notifies controlled callers", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const { rerender } = render(
      <AdvancedSection
        label="Advanced slug"
        open={false}
        onOpenChange={onOpenChange}
      >
        <label htmlFor="slug">Slug</label>
        <input id="slug" />
      </AdvancedSection>,
    );

    await user.click(screen.getByRole("button", { name: "Advanced slug" }));

    expect(onOpenChange).toHaveBeenCalledWith(true);

    rerender(
      <AdvancedSection
        label="Advanced slug"
        open
        onOpenChange={onOpenChange}
      >
        <label htmlFor="slug">Slug</label>
        <input id="slug" />
      </AdvancedSection>,
    );

    expect(screen.getByRole("button", { name: "Advanced slug" }))
      .toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Slug")).toBeInTheDocument();
  });

  it("ignores internal state when controlled by parent open prop", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const { rerender } = render(
      <AdvancedSection
        label="Advanced slug"
        open
        onOpenChange={onOpenChange}
      >
        <label htmlFor="slug">Slug</label>
        <input id="slug" />
      </AdvancedSection>,
    );

    expect(screen.getByLabelText("Slug")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Advanced slug" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);

    // Parent ignored the request and kept open=true. Internal state must
    // not have flipped to "open" silently; switching to uncontrolled later
    // should still render as open.
    rerender(
      <AdvancedSection label="Advanced slug" onOpenChange={onOpenChange}>
        <label htmlFor="slug">Slug</label>
        <input id="slug" />
      </AdvancedSection>,
    );

    expect(screen.getByRole("button", { name: "Advanced slug" }))
      .toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Slug")).not.toBeInTheDocument();
  });
});
