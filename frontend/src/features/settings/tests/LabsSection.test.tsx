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
      preferences: { chats: false, agent: false },
      setMenuVisible,
      ready: true,
    });
  });

  it("changes Chats and Agent independently without a save step", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<LabsSection labelledBy="labs-heading" />);

    const chats = screen.getByRole("checkbox", { name: "Show Chats menu" });
    const agent = screen.getByRole("checkbox", { name: "Show Agent menu" });
    expect(chats).not.toBeChecked();
    expect(agent).not.toBeChecked();

    await user.click(chats);
    expect(setMenuVisible).toHaveBeenLastCalledWith("chats", true);

    vi.mocked(useLabsPreferences).mockReturnValue({
      preferences: { chats: true, agent: false },
      setMenuVisible,
      ready: true,
    });
    rerender(<LabsSection labelledBy="labs-heading" />);
    expect(chats).toBeChecked();
    expect(agent).not.toBeChecked();

    await user.click(agent);
    expect(setMenuVisible).toHaveBeenLastCalledWith("agent", true);
    await user.click(chats);
    expect(setMenuVisible).toHaveBeenLastCalledWith("chats", false);
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
    expect(screen.getByText(/saved for your account in this browser/)).toBeInTheDocument();
  });

  it("keeps both controls disabled until the account has resolved", async () => {
    const user = userEvent.setup();
    vi.mocked(useLabsPreferences).mockReturnValue({
      preferences: { chats: false, agent: false },
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
