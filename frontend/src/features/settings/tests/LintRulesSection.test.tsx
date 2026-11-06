import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../shared/api/operations", () => ({
  getLintRules: vi.fn(),
  getSettingsPolicies: vi.fn(),
  updateSettingsPolicyBatch: vi.fn(),
}));
import { ApiError } from "../../../shared/api/client";
import {
  getLintRules,
  getSettingsPolicies,
  updateSettingsPolicyBatch,
} from "../../../shared/api/operations";
import { LintRulesSection } from "../LintRulesSection";
import { SettingsDirtyProvider } from "../SettingsDirtyRegistry";

// The card links to the lint queue and participates in Settings' dirty guard.
function routed(ui: ReactElement) {
  return <MemoryRouter><SettingsDirtyProvider>{ui}</SettingsDirtyProvider></MemoryRouter>;
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
      rule_id: "kb.missing_backlink",
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
const REVISION = "a".repeat(64);
function policy(overrides: Record<string, boolean | string> = {}, revision = REVISION) {
  return { lint: { name: "lint", title: "Lint rules", revision, policy: {
    auto_fix: false,
    link_fix_mode: "auto",
    auto_approve_link_fixes: true,
    ...overrides,
  } } };
}
const POLICY = policy();
const WORKSPACE_ID = "00000000-0000-4000-8000-000000000110";
const OTHER_WORKSPACE_ID = "00000000-0000-4000-8000-000000000120";
const saveButton = () => screen.getByRole("button", { name: /^(Save changes|Saving…)$/ });
const discardButton = () => screen.getByRole("button", { name: /Discard/ });

async function openChecks() {
  await userEvent.click(screen.getByText("Checks"));
  await screen.findByText("Code fence balance");
}

function beforeUnloadIsBlocked() {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

describe("LintRulesSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getLintRules).mockResolvedValue(RULES as never);
    vi.mocked(getSettingsPolicies).mockResolvedValue(POLICY as never);
    vi.mocked(updateSettingsPolicyBatch).mockImplementation(async (_id, request) =>
      policy(request.changes as Record<string, boolean | string>, "b".repeat(64)) as never,
    );
  });

  it("groups rules and saves edited checks only when Save changes is clicked", async () => {
    const user = userEvent.setup();
    render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    expect(screen.getByText("Checks").closest("details")).not.toHaveAttribute("open");
    await openChecks();
    expect(screen.getByText("Format")).toBeInTheDocument();
    expect(screen.getByText("Knowledge")).toBeInTheDocument();
    expect(screen.getByText("Maintenance")).toBeInTheDocument();
    expect(screen.getByText("External source")).toBeInTheDocument();
    expect(screen.getByText("Required safeguard")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Code fence balance" })).not.toBeInTheDocument();
    expect(screen.getByText("client suggestion").closest("label")).toBeNull();

    const orphan = screen.getByRole("checkbox", { name: "Orphan page" });
    const backlink = screen.getByRole("checkbox", { name: "Missing backlink" });
    expect(orphan).not.toBeChecked();
    expect(backlink).not.toBeChecked();
    expect(saveButton()).toBeDisabled();
    expect(beforeUnloadIsBlocked()).toBe(false);
    await user.click(orphan);
    expect(orphan).toBeChecked();
    expect(updateSettingsPolicyBatch).not.toHaveBeenCalled();
    expect(beforeUnloadIsBlocked()).toBe(true);
    const leave = vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      const queue = screen.getByRole("link", { name: "Open lint queue" });
      await user.click(queue);
      expect(leave).toHaveBeenCalledWith("You have unsaved changes here. Leave without saving them?");
      leave.mockClear();
      // A new-tab click loses nothing, so it must not ask or be blocked. The
      // window listener runs after React's and stops jsdom's navigation.
      let blocked: boolean | undefined;
      const probe = (event: Event) => { blocked = event.defaultPrevented; event.preventDefault(); };
      window.addEventListener("click", probe, { once: true });
      fireEvent.click(queue, { ctrlKey: true });
      expect(blocked).toBe(false);
      expect(leave).not.toHaveBeenCalled();
    } finally {
      leave.mockRestore();
    }
    await user.click(saveButton());
    expect(updateSettingsPolicyBatch).toHaveBeenCalledExactlyOnceWith(WORKSPACE_ID, {
      group: "lint", expected_revision: REVISION, changes: { "kb.orphan_page": true },
    });
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(orphan).toBeChecked();
    expect(backlink).not.toBeChecked();
    expect(beforeUnloadIsBlocked()).toBe(false);
  });

  it("discards option edits and atomically saves all changed options with busy controls", async () => {
    const user = userEvent.setup();
    let resolveSave!: (value: never) => void;
    vi.mocked(updateSettingsPolicyBatch).mockImplementationOnce(() => new Promise(resolve => { resolveSave = resolve; }));
    render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    await waitFor(() => expect(autofix).toBeEnabled());
    const mode = screen.getByRole("combobox", { name: /Fix internal links/ });
    const approval = screen.getByRole("checkbox", { name: /Automatically approve safe link fixes/ });
    await user.click(autofix);
    await user.selectOptions(mode, "suggest");
    await user.click(approval);
    expect(beforeUnloadIsBlocked()).toBe(true);
    await user.click(discardButton());
    expect(autofix).not.toBeChecked();
    expect(mode).toHaveValue("auto");
    expect(approval).toBeChecked();
    expect(saveButton()).toBeDisabled();
    expect(beforeUnloadIsBlocked()).toBe(false);
    expect(updateSettingsPolicyBatch).not.toHaveBeenCalled();

    await user.click(autofix);
    await user.selectOptions(mode, "suggest");
    await user.click(approval);
    await user.click(saveButton());
    expect(updateSettingsPolicyBatch).toHaveBeenCalledExactlyOnceWith(WORKSPACE_ID, {
      group: "lint", expected_revision: REVISION,
      changes: { auto_fix: true, link_fix_mode: "suggest", auto_approve_link_fixes: false },
    });
    expect(autofix).toBeDisabled();
    expect(mode).toBeDisabled();
    expect(approval).toBeDisabled();
    expect(saveButton()).toBeDisabled();
    expect(discardButton()).toBeDisabled();
    await act(async () => resolveSave(policy({ auto_fix: true, link_fix_mode: "suggest", auto_approve_link_fixes: false }) as never));
    expect(autofix).toBeChecked();
    expect(mode).toHaveValue("suggest");
    expect(approval).not.toBeChecked();
    expect(saveButton()).toBeDisabled();
    expect(beforeUnloadIsBlocked()).toBe(false);
  });

  it("blocks all edits when policy loading fails and retries authoritative options", async () => {
    const user = userEvent.setup();
    vi.mocked(getSettingsPolicies).mockRejectedValue(new Error("boom"));
    // This workspace opted in to Orphan page, so the server's catalog reports it on.
    vi.mocked(getLintRules).mockResolvedValue({ rules: RULES.rules.map((rule) =>
      rule.rule_id === "kb.orphan_page" ? { ...rule, enabled: true } : rule) } as never);
    render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be loaded");
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    const mode = screen.getByRole("combobox", { name: /Fix internal links/ });
    const approval = screen.getByRole("checkbox", { name: /Automatically approve safe link fixes/ });
    expect(autofix).toBeDisabled();
    expect(mode).toBeDisabled();
    expect(approval).toBeDisabled();
    expect(autofix).toHaveProperty("indeterminate", true);
    expect(approval).toHaveProperty("indeterminate", true);
    expect(mode).toHaveValue("");
    await openChecks();
    expect(screen.getByRole("checkbox", { name: "Orphan page" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Orphan page" })).toBeChecked();
    expect(saveButton()).toBeDisabled();
    vi.mocked(getSettingsPolicies).mockResolvedValue(policy({
      auto_fix: true, link_fix_mode: "suggest", auto_approve_link_fixes: false, "kb.orphan_page": true,
    }) as never);
    await user.click(screen.getByRole("button", { name: "Retry options" }));
    await waitFor(() => expect(autofix).toBeEnabled());
    expect(autofix).toBeChecked();
    expect(autofix).toHaveProperty("indeterminate", false);
    expect(approval).toHaveProperty("indeterminate", false);
    expect(mode).toHaveValue("suggest");
    expect(approval).not.toBeChecked();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(updateSettingsPolicyBatch).not.toHaveBeenCalled();
  });

  it("allows non-owners to inspect settings but disables every edit and omits save actions", async () => {
    const user = userEvent.setup();
    const view = render(routed(<LintRulesSection readOnly={true} workspaceId={WORKSPACE_ID} />));
    await openChecks();
    for (const checkbox of screen.getAllByRole("checkbox")) expect(checkbox).toBeDisabled();
    expect(screen.getByRole("combobox", { name: /Fix internal links/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Discard/ })).not.toBeInTheDocument();
    expect(updateSettingsPolicyBatch).not.toHaveBeenCalled();

    view.rerender(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /auto-fix/i })).toBeEnabled());
    await user.click(screen.getByRole("checkbox", { name: /auto-fix/i }));
    expect(beforeUnloadIsBlocked()).toBe(true);
    view.rerender(routed(<LintRulesSection readOnly={true} workspaceId={WORKSPACE_ID} />));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /auto-fix/i })).not.toHaveProperty("indeterminate", true));
    expect(screen.getByRole("checkbox", { name: /auto-fix/i })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /auto-fix/i })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
    expect(beforeUnloadIsBlocked()).toBe(false);
    expect(updateSettingsPolicyBatch).not.toHaveBeenCalled();
    expect(getLintRules).toHaveBeenCalledTimes(1);
    expect(getSettingsPolicies).toHaveBeenCalledTimes(1);
  });

  it("waits for both reads before editing and applies group toggles only to filtered checks", async () => {
    const user = userEvent.setup();
    let resolveRules!: (value: never) => void;
    let resolvePolicies!: (value: never) => void;
    vi.mocked(getLintRules).mockImplementation(() => new Promise(resolve => { resolveRules = resolve; }));
    vi.mocked(getSettingsPolicies).mockImplementation(() => new Promise(resolve => { resolvePolicies = resolve; }));
    render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    expect(autofix).toBeDisabled();
    expect(autofix).toHaveProperty("indeterminate", true);
    await act(async () => resolvePolicies(POLICY as never));
    expect(autofix).toBeDisabled();
    await act(async () => resolveRules(RULES as never));
    expect(autofix).toBeEnabled();
    await openChecks();
    await user.type(screen.getByRole("searchbox", { name: "Search checks" }), "orphan");
    expect(screen.queryByText("Code fence balance")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Missing backlink" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Toggle shown Knowledge checks" }));
    expect(updateSettingsPolicyBatch).not.toHaveBeenCalled();
    await user.clear(screen.getByRole("searchbox", { name: "Search checks" }));
    expect(screen.getByRole("checkbox", { name: "Orphan page" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Missing backlink" })).not.toBeChecked();
    await user.click(saveButton());
    expect(updateSettingsPolicyBatch).toHaveBeenCalledWith(WORKSPACE_ID, {
      group: "lint", expected_revision: REVISION, changes: { "kb.orphan_page": true },
    });
  });

  it("waits for failed checks to be retried before editing", async () => {
    const user = userEvent.setup();
    vi.mocked(getLintRules).mockRejectedValue(new Error("checks offline"));
    render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    expect(await screen.findByRole("button", { name: "Retry checks" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("auto"));
    expect(screen.getByRole("combobox")).toBeDisabled();
    vi.mocked(getLintRules).mockResolvedValue(RULES as never);
    await user.click(screen.getByRole("button", { name: "Retry checks" }));
    await waitFor(() => expect(screen.getByRole("combobox")).toBeEnabled());
    expect(screen.getByText("Checks").closest("details")).not.toHaveAttribute("open");
    expect(updateSettingsPolicyBatch).not.toHaveBeenCalled();
  });

  it("preserves unsaved values when saving fails and supports retry with the same revision", async () => {
    const user = userEvent.setup();
    vi.mocked(updateSettingsPolicyBatch).mockRejectedValueOnce(new Error("save failed"));
    render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    await waitFor(() => expect(autofix).toBeEnabled());
    await user.click(autofix);
    await openChecks();
    const orphan = screen.getByRole("checkbox", { name: "Orphan page" });
    await user.click(orphan);
    await user.click(saveButton());
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(autofix).toBeChecked();
    expect(orphan).toBeChecked();
    expect(saveButton()).toBeEnabled();
    expect(beforeUnloadIsBlocked()).toBe(true);
    // The retry commits but its response card is unusable: reload, never report failure.
    vi.mocked(updateSettingsPolicyBatch).mockResolvedValueOnce({ lint: { policy: {} } } as never);
    vi.mocked(getSettingsPolicies).mockResolvedValue(policy({ auto_fix: true, "kb.orphan_page": true }, "e".repeat(64)) as never);
    await user.click(saveButton());
    expect(updateSettingsPolicyBatch).toHaveBeenLastCalledWith(WORKSPACE_ID, {
      group: "lint", expected_revision: REVISION, changes: { auto_fix: true, "kb.orphan_page": true },
    });
    await waitFor(() => expect(getSettingsPolicies).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(autofix).toBeEnabled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(autofix).toBeChecked();
    expect(orphan).toBeChecked();
    expect(saveButton()).toBeDisabled();
    expect(beforeUnloadIsBlocked()).toBe(false);
  });

  it("reloads a conflicting revision without overwriting draft edits and retries explicitly", async () => {
    const user = userEvent.setup();
    const nextRevision = "c".repeat(64);
    vi.mocked(updateSettingsPolicyBatch).mockRejectedValueOnce(new ApiError(409, {
      error_code: "settings_revision_conflict", message: "Changed elsewhere", request_id: "test", warnings: [],
    }));
    render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    await waitFor(() => expect(autofix).toBeEnabled());
    await user.click(autofix);
    let resolveReload!: (value: never) => void;
    vi.mocked(getSettingsPolicies).mockImplementationOnce(() => new Promise(resolve => { resolveReload = resolve; }));
    await user.click(saveButton());
    await waitFor(() => expect(getSettingsPolicies).toHaveBeenCalledTimes(2));
    // Nothing claims the latest settings are loaded until they are.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => resolveReload(policy({ link_fix_mode: "suggest" }, nextRevision) as never));
    await waitFor(() => expect(saveButton()).toBeEnabled());
    expect(screen.getByRole("alert")).toHaveTextContent("changed elsewhere");
    expect(getLintRules).toHaveBeenCalledTimes(1);
    expect(autofix).toBeChecked();
    // Only this actor's edited key is retried; the other owner's link mode survives.
    await user.click(saveButton());
    expect(updateSettingsPolicyBatch).toHaveBeenLastCalledWith(WORKSPACE_ID, {
      group: "lint", expected_revision: nextRevision, changes: { auto_fix: true },
    });
  });

  it("ignores old load responses after switching workspaces", async () => {
    let resolveOld!: (value: never) => void;
    vi.mocked(getSettingsPolicies).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
    const view = render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    view.rerender(routed(<LintRulesSection readOnly={false} workspaceId={OTHER_WORKSPACE_ID} />));
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    await waitFor(() => expect(autofix).toBeEnabled());
    await act(async () => resolveOld(policy({ auto_fix: true, link_fix_mode: "suggest", auto_approve_link_fixes: false }) as never));
    expect(autofix).not.toBeChecked();
    expect(screen.getByRole("combobox")).toHaveValue("auto");
    expect(saveButton()).toBeDisabled();
  });

  it("isolates a pending save from the newly selected workspace", async () => {
    const user = userEvent.setup();
    let resolveOldSave!: (value: never) => void;
    vi.mocked(updateSettingsPolicyBatch).mockImplementationOnce(() => new Promise(resolve => { resolveOldSave = resolve; }));
    const view = render(routed(<LintRulesSection readOnly={false} workspaceId={WORKSPACE_ID} />));
    const autofix = screen.getByRole("checkbox", { name: /auto-fix/i });
    await waitFor(() => expect(autofix).toBeEnabled());
    await user.click(autofix);
    await user.click(saveButton());
    view.rerender(routed(<LintRulesSection readOnly={false} workspaceId={OTHER_WORKSPACE_ID} />));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /auto-fix/i })).toBeEnabled());
    await act(async () => resolveOldSave(policy({ auto_fix: true }, "d".repeat(64)) as never));
    expect(screen.getByRole("checkbox", { name: /auto-fix/i })).not.toBeChecked();
    expect(saveButton()).toBeDisabled();
    expect(updateSettingsPolicyBatch).toHaveBeenCalledExactlyOnceWith(WORKSPACE_ID, {
      group: "lint", expected_revision: REVISION, changes: { auto_fix: true },
    });
    expect(beforeUnloadIsBlocked()).toBe(false);
  });
});
