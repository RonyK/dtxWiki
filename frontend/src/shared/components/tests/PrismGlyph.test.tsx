import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PrismGlyph } from "../PrismGlyph";

describe("PrismGlyph", () => {
  it("renders a decorative span with the prism-glyph class", () => {
    const { container } = render(<PrismGlyph />);
    const glyph = container.querySelector(".prism-glyph");
    expect(glyph).not.toBeNull();
    expect(glyph?.tagName).toBe("SPAN");
  });

  it("is marked aria-hidden so screen readers ignore the decoration", () => {
    const { container } = render(<PrismGlyph />);
    const glyph = container.querySelector(".prism-glyph");
    expect(glyph).toHaveAttribute("aria-hidden", "true");
  });
});
