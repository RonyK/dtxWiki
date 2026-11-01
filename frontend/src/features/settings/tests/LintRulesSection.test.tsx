import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../shared/api/operations", () => ({
  getLintRules: vi.fn(),
  getSettingsPolicies: vi.fn(),
  updateSettingsPolicy: vi.fn(),
}));
import {
  getLintRules,
  getSettingsPolicies,
  updateSettingsPolicy,
} from "../../../shared/api/operations";
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
      policy_locked: true,
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
const POLICY = { lint: { policy: { auto_fix: false, link_fix_mode: "auto", auto_approve_link_fixes: true } } };

const WORKSPACE_ID = "00000000-0000-4000-8000-000000000110";

// PR3: +4 collected tests, 11 new logical inputs across new/extended cases:
// locked safeguard; recovered policy tuple; both reads pending; policies ready
// before checks; incomplete policy; failed checks read; four failed save keys;
// and a late response from the previous workspace. Browser captures are separate.
describe("LintRulesSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getLintRules).mockResolvedValue(RULES as never);
    vi.mocked(getSettingsPolicies).mockResolvedValue(POLICY as never);
    vi.mocked(updateSettingsPolicy).mockResolvedValue({} as never);
  });

  it("groups rules by layer and toggles via the lint policy", async () => {
    const user = userEvent.setup();
    renderRouted(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />);

    const checks = screen.getByText("Checks");
    expect(checks.closest("details")).not.toHaveAttribute("open");
    await user.click(checks);
    expect(await screen.findByText("Format")).toBeInTheDocument();
    expect(screen.getByText("Knowledge")).toBeInTheDocument();
    expect(screen.getByText("Maintenance")).toBeInTheDocument();
    expect(screen.getByText("Code fence balance")).toBeInTheDocument();
    expect(screen.getByText("External source")).toBeInTheDocument();
    expect(screen.getByText("Required safeguard")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Code fence balance" })).not.toBeInTheDocument();
    expect(screen.getByText("client suggestion").closest("label")).toBeNull();

    // Queried by accessible name: the checkbox and its label are associated by
    // id rather than nesting, and that association is what makes the row usable.
    const checkbox = screen.getByRole("checkbox", { name: "Orphan page" });
    await user.click(checkbox);

    await waitFor(() =>
      expect(updateSettingsPolicy).toHaveBeenCalledWith(WORKSPACE_ID, {
        group: "lint",
        key: "kb.orphan_page",
        value: true,
      }),
    );
  });

  it("toggles auto-fix mode via the lint policy (F2b)", async () => {
    const user = userEvent.setup();
    renderRouted(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />);
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    await waitFor(() => expect(autofix).toBeEnabled());
    expect(autofix).not.toBeChecked();
    await user.click(autofix);
    await waitFor(() =>
      expect(updateSettingsPolicy).toHaveBeenCalledWith(WORKSPACE_ID, {
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
    expect(updateSettingsPolicy).toHaveBeenCalledWith(WORKSPACE_ID, { group: "lint", key: "link_fix_mode", value: "suggest" });
    await user.click(approval);
    expect(updateSettingsPolicy).toHaveBeenCalledWith(WORKSPACE_ID, { group: "lint", key: "auto_approve_link_fixes", value: false });
  });

  it("keeps checks editable on policy failure and retries the actual options", async () => {
    const user = userEvent.setup();
    vi.mocked(getSettingsPolicies).mockRejectedValue(new Error("boom"));
    renderRouted(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be loaded");
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    const mode = screen.getByRole("combobox", { name: /Fix internal links/ });
    const approval = screen.getByRole("checkbox", { name: /Automatically approve safe link fixes/ });
    expect(autofix).toBeDisabled();
    expect(mode).toBeDisabled();
    expect(approval).toBeDisabled();
    await user.click(autofix);
    expect(updateSettingsPolicy).not.toHaveBeenCalled();
    await user.click(screen.getByText("Checks"));
    expect(screen.getByText("Code fence balance")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Orphan page" })).toBeEnabled();
    vi.mocked(getSettingsPolicies).mockResolvedValue({ lint: { policy: {
      auto_fix: true, link_fix_mode: "suggest", auto_approve_link_fixes: false,
    } } } as never);
    await user.click(screen.getByRole("button", { name: "Retry options" }));
    await waitFor(() => expect(autofix).toBeEnabled());
    expect(autofix).toBeChecked();
    expect(mode).toHaveValue("suggest");
    expect(approval).not.toBeChecked();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(getLintRules).toHaveBeenCalledTimes(1);
    expect(updateSettingsPolicy).not.toHaveBeenCalled();
  });

  it("disables toggles when read-only", async () => {
    renderRouted(<LintRulesSection readOnly={true} workspaceId={WORKSPACE_ID} />);
    await userEvent.click(screen.getByText("Checks"));
    await screen.findByText("Code fence balance");
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes.length).toBeGreaterThan(0);
    for (const checkbox of checkboxes) {
      expect(checkbox).toBeDisabled();
    }
    expect(screen.getByRole("combobox", { name: /Fix internal links/ })).toBeDisabled();
    expect(updateSettingsPolicy).not.toHaveBeenCalled();
  });
  it("loads policies independently while checks are pending and filters visible checks", async () => {
    const user = userEvent.setup();
    let resolveRules!: (value: never) => void;
    let resolvePolicies!: (value: never) => void;
    vi.mocked(getLintRules).mockImplementation(() => new Promise(resolve => { resolveRules = resolve; }));
    vi.mocked(getSettingsPolicies).mockImplementation(() => new Promise(resolve => { resolvePolicies = resolve; }));
    renderRouted(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />);
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    expect(autofix).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
    await act(async () => resolvePolicies(POLICY as never));
    expect(autofix).toBeEnabled();
    await act(async () => resolveRules(RULES as never));
    await user.click(screen.getByText("Checks"));
    await user.type(screen.getByRole("searchbox", { name: "Search checks" }), "orphan");
    expect(screen.queryByText("Code fence balance")).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Orphan page" })).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Toggle shown Knowledge checks" }));
    expect(updateSettingsPolicy).toHaveBeenCalledWith(WORKSPACE_ID, { group: "lint", key: "kb.orphan_page", value: true });
  });

  it("rejects incomplete policy responses and retries checks without resetting saved options", async () => {
    const user = userEvent.setup();
    vi.mocked(getSettingsPolicies).mockResolvedValue({ lint: { policy: {} } } as never);
    vi.mocked(getLintRules).mockRejectedValue(new Error("checks offline"));
    renderRouted(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />);
    await screen.findByText(/could not be loaded/);
    expect(screen.getByRole("combobox")).toBeDisabled();
    vi.mocked(getSettingsPolicies).mockResolvedValue(POLICY as never);
    await user.click(screen.getByRole("button", { name: "Retry options" }));
    await waitFor(() => expect(screen.getByRole("combobox")).toBeEnabled());
    await user.selectOptions(screen.getByRole("combobox"), "suggest");
    await user.click(screen.getByText("Checks"));
    vi.mocked(getLintRules).mockResolvedValue(RULES as never);
    await user.click(screen.getByRole("button", { name: "Retry checks" }));
    await screen.findByText("Code fence balance");
    expect(screen.getByRole("combobox")).toHaveValue("suggest");
    expect(getSettingsPolicies).toHaveBeenCalledTimes(2);
  });

  it("preserves confirmed values when policy and rule saves fail", async () => {
    const user = userEvent.setup();
    vi.mocked(updateSettingsPolicy).mockRejectedValue(new Error("save failed"));
    renderRouted(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />);
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    await waitFor(() => expect(autofix).toBeEnabled());
    await user.click(autofix);
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to update");
    expect(autofix).not.toBeChecked();
    await user.selectOptions(screen.getByRole("combobox"), "suggest");
    expect(screen.getByRole("combobox")).toHaveValue("auto");
    const approval = screen.getByRole("checkbox", { name: /Automatically approve safe link fixes/ });
    await user.click(approval);
    expect(approval).toBeChecked();
    await user.click(screen.getByText("Checks"));
    const orphan = screen.getByRole("checkbox", { name: "Orphan page" });
    await user.click(orphan);
    await waitFor(() => expect(orphan).toBeEnabled());
    expect(orphan).not.toBeChecked();
  });

  it("discards pending options when the workspace changes", async () => {
    let resolveOld!: (value: never) => void;
    vi.mocked(getSettingsPolicies).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
    const view = renderRouted(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />);
    view.rerender(<MemoryRouter><LintRulesSection readOnly={false} workspaceId="00000000-0000-4000-8000-000000000120" /></MemoryRouter>);
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    await waitFor(() => expect(autofix).toBeEnabled());
    await act(async () => resolveOld({ lint: { policy: { auto_fix: true, link_fix_mode: "suggest", auto_approve_link_fixes: false } } } as never));
    expect(autofix).not.toBeChecked();
    expect(screen.getByRole("combobox")).toHaveValue("auto");
  });
});
