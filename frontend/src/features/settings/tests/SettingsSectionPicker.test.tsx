import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SettingsSectionPicker } from "../SettingsSectionPicker";

describe("SettingsSectionPicker", () => {
  it("shows Labs with a workspace admin chip only after owner eligibility resolves", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const picker = (showLabs?: boolean) => (
      <SettingsSectionPicker
        activeSection="workspace"
        consentCount={0}
        onSelect={onSelect}
        showAttachments={false}
        showDeployment={false}
        showUsers={false}
        showLabs={showLabs}
        tokenCount={0}
      />
    );
    const view = render(picker());

    await user.click(screen.getByRole("button", { expanded: false }));
    const dialog = screen.getByRole("dialog");
    await user.type(
      within(dialog).getByRole("searchbox", { name: "Filter settings sections" }),
      "experimental",
    );
    expect(within(dialog).queryByRole("button", { name: /^Labs/ })).not.toBeInTheDocument();
    view.rerender(picker(true));
    const group = within(dialog).getByRole("group", { name: "This workspace" });
    const labs = within(group).getByRole("button", { name: /^Labs Workspace admin/ });
    expect(within(labs).getByText("Workspace admin")).toHaveClass("nav-admin-badge");
    expect(within(labs).queryByText("System admin")).not.toBeInTheDocument();
    await user.click(labs);

    expect(onSelect).toHaveBeenCalledWith("labs", undefined);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows an up-to-date badge while open, without retyping the filter", async () => {
    // 260912 review, P6d: `matches` used to depend on `[query]` alone while
    // `sections`/`policyTabs` were rebuilt fresh every render from `t` and
    // the count props -- so with the picker open and the query untouched, a
    // count change (tokenCount/consentCount) or a language switch left the
    // stale row cached until the next keystroke re-ran the filter. Rerendering
    // with a changed prop and NOT touching the query is what would have
    // passed on the old code (the memo's only dependency never changed).
    const user = userEvent.setup();
    const onSelect = () => {};
    const { rerender } = render(
      <SettingsSectionPicker
        activeSection="workspace"
        consentCount={0}
        onSelect={onSelect}
        showAttachments={false}
        showDeployment={false}
        showUsers={false}
        tokenCount={0}
      />,
    );

    await user.click(
      screen.getByRole("button", { expanded: false, name: /workspace/i }),
    );
    expect(screen.queryByTitle(/personal connections/i)).not.toBeInTheDocument();

    rerender(
      <SettingsSectionPicker
        activeSection="workspace"
        consentCount={0}
        onSelect={onSelect}
        showAttachments={false}
        showDeployment={false}
        showUsers={false}
        tokenCount={5}
      />,
    );

    expect(await screen.findByTitle(/personal connections/i)).toHaveTextContent("5");
  });
});
