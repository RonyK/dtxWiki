import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useLabsPreferences } from "../../../shared/hooks/useLabsPreferences";
import { setLocale } from "../../../shared/i18n";
import { LabsSection } from "../LabsSection";

vi.mock("../../../shared/hooks/useLabsPreferences", () => ({
  useLabsPreferences: vi.fn(),
}));

describe("LabsSection", () => {
  const setMenuVisible = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    setLocale("en", { persist: false });
    vi.mocked(useLabsPreferences).mockReturnValue({
      preferences: { ask: false, agent: false },
      setMenuVisible,
      ready: true,
    });
  });

  it("changes Ask and Agent independently without a save step", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<LabsSection labelledBy="labs-heading" />);

    const ask = screen.getByRole("checkbox", { name: "Show Ask menu" });
    const agent = screen.getByRole("checkbox", { name: "Show Agent menu" });
    expect(ask).not.toBeChecked();
    expect(agent).not.toBeChecked();

    await user.click(ask);
    expect(setMenuVisible).toHaveBeenLastCalledWith("ask", true);

    vi.mocked(useLabsPreferences).mockReturnValue({
      preferences: { ask: true, agent: false },
      setMenuVisible,
      ready: true,
    });
    rerender(<LabsSection labelledBy="labs-heading" />);
    expect(ask).toBeChecked();
    expect(agent).not.toBeChecked();

    await user.click(agent);
    expect(setMenuVisible).toHaveBeenLastCalledWith("agent", true);
    await user.click(ask);
    expect(setMenuVisible).toHaveBeenLastCalledWith("ask", false);
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
    expect(screen.getByText(/saved for your account in this browser/)).toBeInTheDocument();
  });

  it("keeps both controls disabled until the account has resolved", async () => {
    const user = userEvent.setup();
    vi.mocked(useLabsPreferences).mockReturnValue({
      preferences: { ask: false, agent: false },
      setMenuVisible,
      ready: false,
    });
    render(<LabsSection labelledBy="labs-heading" />);

    for (const checkbox of screen.getAllByRole("checkbox")) {
      expect(checkbox).toBeDisabled();
      await user.click(checkbox);
    }
    expect(setMenuVisible).not.toHaveBeenCalled();
  });
});
