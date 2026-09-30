import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBanner } from "../StatusBanner";

describe("StatusBanner", () => {
  it("uses status role for informational messages", () => {
    render(<StatusBanner tone="info">Saved</StatusBanner>);

    expect(screen.getByRole("status")).toHaveTextContent("Saved");
  });

  it("uses alert role for warnings", () => {
    render(<StatusBanner tone="warning">Save failed</StatusBanner>);

    expect(screen.getByRole("alert")).toHaveTextContent("Save failed");
  });
});
