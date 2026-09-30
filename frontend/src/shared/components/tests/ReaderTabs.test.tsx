import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ReaderTabs } from "../ReaderTabs";

describe("ReaderTabs", () => {
  it("moves focus with arrow, home, and end keys", async () => {
    function Harness() {
      const [active, setActive] = useState("view");
      return (
        <ReaderTabs
          active={active}
          ariaLabel="Reader views"
          idBase="reader"
          onChange={setActive}
          tabs={[
            { key: "view", label: "View" },
            { key: "history", label: "History", count: 0 },
            { key: "activity", label: "Activity", count: 2 },
          ]}
        />
      );
    }

    render(<Harness />);

    const view = screen.getByRole("tab", { name: "View" });
    view.focus();
    await userEvent.keyboard("{ArrowRight}");

    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "History" })).toHaveFocus(),
    );
    expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByText("0")).toBeNull();

    await userEvent.keyboard("{End}");
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Activity/ })).toHaveFocus(),
    );
    expect(screen.getByText("2")).toBeInTheDocument();
    await userEvent.keyboard("{Home}");
    await waitFor(() => expect(view).toHaveFocus());
  });

  it("skips disabled tabs", async () => {
    const onChange = vi.fn();
    render(
      <ReaderTabs
        active="view"
        ariaLabel="Reader views"
        idBase="reader"
        onChange={onChange}
        tabs={[
          { key: "view", label: "View" },
          {
            key: "draft",
            label: "Draft",
            disabled: true,
            title: "Not available yet",
          },
          { key: "history", label: "History" },
        ]}
      />,
    );

    screen.getByRole("tab", { name: "View" }).focus();
    await userEvent.keyboard("{ArrowRight}");

    expect(onChange).toHaveBeenCalledWith("history");
    // The reason a tab is disabled rides on `title`, so the label can stay the
    // plain view name instead of spelling the state out inline.
    expect(screen.getByRole("tab", { name: "Draft" })).toHaveAttribute(
      "title",
      "Not available yet",
    );
  });

  it("uses count labels for accessible tab names", () => {
    render(
      <ReaderTabs
        active="pending"
        ariaLabel="Review lifecycle"
        idBase="review-lifecycle"
        onChange={vi.fn()}
        tabs={[
          {
            key: "pending",
            label: "Pending",
            count: 3,
            countLabel: "3 review requests",
          },
        ]}
      />,
    );

    expect(
      screen.getByRole("tab", { name: "Pending, 3 review requests" }),
    ).toBeInTheDocument();
    expect(screen.getByText("3")).toHaveAttribute("aria-hidden", "true");
  });

  it("renders a tab badge in the accessible name with its explanatory title", () => {
    render(
      <ReaderTabs
        active="graph"
        ariaLabel="Reader views"
        idBase="reader"
        onChange={vi.fn()}
        tabs={[
          {
            key: "graph",
            label: "Graph",
            badge: {
              label: "Preview",
              title: "Preview: this area is still being refined.",
            },
          },
        ]}
      />,
    );

    const graphTab = screen.getByRole("tab", { name: "Graph, Preview" });
    expect(graphTab).toHaveAttribute(
      "title",
      "Preview: this area is still being refined.",
    );
    expect(within(graphTab).getByText("Preview")).toHaveClass(
      "nav-preview-badge",
    );
  });
});
