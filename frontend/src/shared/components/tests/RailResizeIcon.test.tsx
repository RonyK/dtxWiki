import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RailResizeIcon } from "../RailResizeIcon";

describe("RailResizeIcon", () => {
  it("renders an aria-hidden framed-box+arrow svg for each mode", () => {
    for (const mode of ["widen", "reset"] as const) {
      const { container, unmount } = render(<RailResizeIcon mode={mode} />);
      const svg = container.querySelector("svg");
      expect(svg).not.toBeNull();
      expect(svg).toHaveAttribute("aria-hidden", "true");
      // outer box + inner panel (2 rects) + directional arrow (1 path)
      expect(container.querySelectorAll("rect")).toHaveLength(2);
      expect(container.querySelectorAll("path")).toHaveLength(1);
      // exactly one rect carries the dashed "other state" outline
      const dashed = [...container.querySelectorAll("rect")].filter((r) =>
        r.getAttribute("stroke-dasharray"),
      );
      expect(dashed).toHaveLength(1);
      unmount();
    }
  });

  it("honours the size prop", () => {
    const { container } = render(<RailResizeIcon mode="widen" size={20} />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("width", "20");
    expect(svg).toHaveAttribute("height", "20");
  });
});
