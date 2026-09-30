import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SettingsSectionPicker } from "../SettingsSectionPicker";

describe("SettingsSectionPicker", () => {
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
