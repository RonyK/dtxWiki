import { render, screen } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SegmentedControl } from "../SegmentedControl";

describe("SegmentedControl", () => {
  it("renders as a radio group and reports selected changes", async () => {
    const onChange = vi.fn();
    render(
      <SegmentedControl
        label="Period"
        options={[
          { key: "today", label: "today" },
          { key: "week", label: "week" },
        ]}
        value="today"
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("radiogroup", { name: "Period" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "today" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await userEvent.click(screen.getByRole("radio", { name: "week" }));
    expect(onChange).toHaveBeenCalledWith("week");
  });

  it("moves selection with arrow keys using roving tabindex", async () => {
    const user = userEvent.setup();
    function Example() {
      const [value, setValue] = useState<"today" | "week" | "month">("today");
      return (
        <SegmentedControl
          label="Period"
          options={[
            { key: "today", label: "today" },
            { key: "week", label: "week" },
            { key: "month", label: "month" },
          ]}
          value={value}
          onChange={setValue}
        />
      );
    }
    render(<Example />);

    const today = screen.getByRole("radio", { name: "today" });
    const week = screen.getByRole("radio", { name: "week" });
    today.focus();
    await user.keyboard("{ArrowRight}");

    expect(week).toHaveAttribute("aria-checked", "true");
    expect(week).toHaveAttribute("tabindex", "0");
  });
});
