import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../shared/api/operations", () => ({
  getLintRules: vi.fn(),
  getSettingsPolicies: vi.fn(),
  updateSettingsPolicy: vi.fn(),
}));
vi.mock("../../../shared/api/workspace", () => ({
  getCurrentWorkspaceId: vi.fn(),
}));

import {
  getLintRules,
  getSettingsPolicies,
  updateSettingsPolicy,
} from "../../../shared/api/operations";
import { getCurrentWorkspaceId } from "../../../shared/api/workspace";
import { LintRulesSection } from "../LintRulesSection";

// The card links out to the lint queue, so it needs a router context.
function renderRouted(ui: ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

const RULES = {
  rules: [
    {
      rule_id: "md.code_fence_balance",
      layer: "format",
      default_enabled: true,
      enabled: true,
      runtime_checkable: true,
    },
    {
      rule_id: "kb.orphan_page",
      layer: "knowledge",
      default_enabled: false,
      enabled: false,
      runtime_checkable: true,
    },
    {
      rule_id: "maintenance.client_suggestion",
      layer: "maintenance",
      default_enabled: true,
      enabled: true,
      runtime_checkable: false,
    },
  ],
};

describe("LintRulesSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCurrentWorkspaceId).mockResolvedValue("ws-1");
    vi.mocked(getLintRules).mockResolvedValue(RULES as never);
    vi.mocked(getSettingsPolicies).mockResolvedValue({
      lint: { policy: { auto_fix: false } },
    } as never);
    vi.mocked(updateSettingsPolicy).mockResolvedValue({} as never);
  });

  it("groups rules by layer and toggles via the lint policy", async () => {
    const user = userEvent.setup();
    renderRouted(<LintRulesSection readOnly={false} />);

    expect(await screen.findByText("Format")).toBeInTheDocument();
    expect(screen.getByText("Knowledge")).toBeInTheDocument();
    expect(screen.getByText("Maintenance")).toBeInTheDocument();
    expect(screen.getByText("Code fence balance")).toBeInTheDocument();
    expect(screen.getByText("External source")).toBeInTheDocument();
    expect(screen.getByText("client suggestion").closest("label")).toBeNull();

    // Queried by accessible name: the checkbox and its label are associated by
    // id rather than nesting, and that association is what makes the row usable.
    const checkbox = screen.getByRole("checkbox", { name: "Orphan page" });
    await user.click(checkbox);

    await waitFor(() =>
      expect(updateSettingsPolicy).toHaveBeenCalledWith("ws-1", {
        group: "lint",
        key: "kb.orphan_page",
        value: true,
      }),
    );
  });

  it("toggles auto-fix mode via the lint policy (F2b)", async () => {
    const user = userEvent.setup();
    renderRouted(<LintRulesSection readOnly={false} />);
    const autofix = await screen.findByRole("checkbox", { name: /auto-fix/i });
    expect(autofix).not.toBeChecked();
    await user.click(autofix);
    await waitFor(() =>
      expect(updateSettingsPolicy).toHaveBeenCalledWith("ws-1", {
        group: "lint",
        key: "auto_fix",
        value: true,
      }),
    );
    // Link mode and approval are independent settings with different value
    // types; extending this policy control case avoids another UI fixture.
    const mode = screen.getByRole("combobox", { name: /Fix internal links/ });
    expect(mode).toHaveValue("auto");
    const approval = screen.getByRole("checkbox", { name: /Automatically approve safe link fixes/ });
    expect(approval).toBeChecked();
    await user.selectOptions(mode, "suggest");
    expect(updateSettingsPolicy).toHaveBeenCalledWith("ws-1", { group: "lint", key: "link_fix_mode", value: "suggest" });
    await user.click(approval);
    expect(updateSettingsPolicy).toHaveBeenCalledWith("ws-1", { group: "lint", key: "auto_approve_link_fixes", value: false });
  });

  it("still renders the rules list when the policies fetch fails (I10)", async () => {
    vi.mocked(getSettingsPolicies).mockRejectedValue(new Error("boom"));
    renderRouted(<LintRulesSection readOnly={false} />);
    // the rule list must still appear despite the auto-fix policies failure
    expect(await screen.findByText("Code fence balance")).toBeInTheDocument();
    // auto-fix degrades to off rather than breaking the whole section
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    expect(autofix).not.toBeChecked();
  });

  it("disables toggles when read-only", async () => {
    renderRouted(<LintRulesSection readOnly={true} />);
    const checkboxes = await screen.findAllByRole("checkbox");
    expect(checkboxes.length).toBeGreaterThan(0);
    for (const checkbox of checkboxes) {
      expect(checkbox).toBeDisabled();
    }
    expect(updateSettingsPolicy).not.toHaveBeenCalled();
  });
});
