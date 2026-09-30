import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PanelVisibilityIcon } from "../PanelVisibilityIcon";

describe("PanelVisibilityIcon", () => {
  it("renders an aria-hidden box icon for every side/action combo", () => {
    for (const side of ["left", "right", "bottom"] as const) {
      for (const action of ["show", "hide"] as const) {
        const { container, unmount } = render(
          <PanelVisibilityIcon side={side} action={action} />,
        );
        const svg = container.querySelector("svg");
        expect(svg).not.toBeNull();
        expect(svg).toHaveAttribute("aria-hidden", "true");
        // panel box (rect) + divider + shaft + arrow
        expect(container.querySelectorAll("rect")).toHaveLength(1);
        expect(container.querySelectorAll("path")).toHaveLength(3);
        unmount();
      }
    }
  });

  it("honours the size prop", () => {
    const { container } = render(
      <PanelVisibilityIcon side="left" action="show" size={20} />,
    );
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("width", "20");
    expect(svg).toHaveAttribute("height", "20");
  });

  it("uses a bottom divider and vertical arrow for sheet controls", () => {
    const { container } = render(
      <PanelVisibilityIcon side="bottom" action="show" />,
    );
    expect(
      [...container.querySelectorAll("path")].map((path) => path.getAttribute("d")),
    ).toEqual(["M3 15h18", "M12 7v6", "m9 10 3-3 3 3"]);
  });
});
