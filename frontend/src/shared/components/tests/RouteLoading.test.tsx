import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RouteLoading } from "../RouteLoading";

describe("RouteLoading", () => {
  it("exposes loading progress to assistive technology", () => {
    render(<RouteLoading label="Loading Context" />);

    expect(screen.getByRole("status", { name: "Loading Context" })).toHaveTextContent(
      "Loading Context",
    );
  });
});
