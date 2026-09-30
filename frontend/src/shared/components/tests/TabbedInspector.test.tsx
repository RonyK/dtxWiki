import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TabbedInspector } from "../TabbedInspector";

describe("TabbedInspector", () => {
  it("switches tabs with clicks and arrow keys", async () => {
    const onActiveKeyChange = vi.fn();
    render(
      <TabbedInspector
        ariaLabel="Page inspector"
        defaultActiveKey="properties"
        onActiveKeyChange={onActiveKeyChange}
        tabs={[
          { key: "properties", label: "Properties", content: <p>Page metadata</p> },
          { key: "history", label: "History", badge: 2, content: <p>Version timeline</p> },
          { key: "backlinks", label: "Backlinks", badge: 1, content: <p>Incoming links</p> },
        ]}
      />,
    );

    expect(screen.getByRole("tabpanel")).toHaveTextContent("Page metadata");

    await userEvent.click(screen.getByRole("tab", { name: /^History/ }));

    expect(onActiveKeyChange).toHaveBeenCalledWith("history");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Version timeline");

    await userEvent.keyboard("{ArrowRight}");

    expect(screen.getByRole("tab", { name: /^Backlinks/ })).toHaveFocus();
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Incoming links");
  });

  it("supports controlled active tab state", () => {
    render(
      <TabbedInspector
        activeKey="history"
        ariaLabel="Controlled inspector"
        tabs={[
          { key: "properties", label: "Properties", content: <p>Page metadata</p> },
          { key: "history", label: "History", content: <p>Version timeline</p> },
        ]}
      />,
    );

    const tablist = screen.getByRole("tablist", { name: "Controlled inspector tabs" });
    expect(within(tablist).getByRole("tab", { name: "History" }))
      .toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Version timeline");
  });
});
