import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FAB } from "../FAB";

describe("FAB", () => {
  it("runs the primary action from the accessible floating button", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();

    render(<FAB ariaLabel="Create item" label="New" onClick={onClick} />);

    await user.click(screen.getByRole("button", { name: "Create item" }));

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("does not run the action while disabled", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();

    render(<FAB ariaLabel="Create item" disabled label="New" onClick={onClick} />);

    await user.click(screen.getByRole("button", { name: "Create item" }));

    expect(onClick).not.toHaveBeenCalled();
  });
});
