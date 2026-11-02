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
      preferences: { ask: false, agent: false, knowledgeMap: false },
      setMenuVisible,
      ready: true,
    });
  });

  it("changes Knowledge Map, Ask and Agent independently without a save step", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<LabsSection labelledBy="labs-heading" />);

    const ask = screen.getByRole("checkbox", { name: "Show Ask menu" });
    const agent = screen.getByRole("checkbox", { name: "Show Agent menu" });
    const knowledgeMap = screen.getByRole("checkbox", { name: "Show Knowledge Map menu" });
    expect(ask).not.toBeChecked();
    expect(agent).not.toBeChecked();
    expect(knowledgeMap).not.toBeChecked();
    expect(knowledgeMap).toHaveAccessibleDescription(/workspace owner/);

    await user.click(knowledgeMap);
    expect(setMenuVisible).toHaveBeenLastCalledWith("knowledgeMap", true);

    await user.click(ask);
    expect(setMenuVisible).toHaveBeenLastCalledWith("ask", true);

    vi.mocked(useLabsPreferences).mockReturnValue({
      preferences: { ask: true, agent: false, knowledgeMap: true },
      setMenuVisible,
      ready: true,
    });
    rerender(<LabsSection labelledBy="labs-heading" />);
    expect(ask).toBeChecked();
    expect(knowledgeMap).toBeChecked();
    expect(agent).not.toBeChecked();

    await user.click(agent);
    expect(setMenuVisible).toHaveBeenLastCalledWith("agent", true);
    await user.click(ask);
    expect(setMenuVisible).toHaveBeenLastCalledWith("ask", false);
    await user.click(knowledgeMap);
    expect(setMenuVisible).toHaveBeenLastCalledWith("knowledgeMap", false);
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
    expect(screen.getByText(/saved for your account in this browser/)).toBeInTheDocument();
  });

  it("keeps all controls disabled until the account has resolved", async () => {
    const user = userEvent.setup();
    vi.mocked(useLabsPreferences).mockReturnValue({
      preferences: { ask: false, agent: false, knowledgeMap: false },
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
