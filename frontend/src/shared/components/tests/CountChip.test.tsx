import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CountChip } from "../CountChip";

describe("CountChip", () => {
  it("renders the value and label as plain text without a status tone", () => {
    render(<CountChip value={3} label="calls" />);
    const chip = screen.getByText("3 calls");
    expect(chip).toHaveClass("count-chip", "count-chip-neutral");
    // It must NOT be a StatusPill (no status dot / info-warn-danger tone).
    expect(chip.className).not.toContain("status-pill");
  });

  it("applies the danger tone and compact size when requested", () => {
    render(<CountChip value={2} label="errors" tone="danger" compact />);
    const chip = screen.getByText("2 errors");
    expect(chip).toHaveClass("count-chip-danger", "compact");
  });

  it("renders the backend total when it exceeds the loaded value", () => {
    render(<CountChip value={50} label="events" total={312} />);
    expect(screen.getByText("312 events")).toBeInTheDocument();
  });

  it("renders an 'N+' lower bound when capped and no total is known", () => {
    render(<CountChip value={50} label="events" approxMore />);
    expect(screen.getByText("50+ events")).toBeInTheDocument();
  });

  it("prefers a known total over the 'N+' lower bound", () => {
    render(<CountChip value={50} label="events" total={120} approxMore />);
    expect(screen.getByText("120 events")).toBeInTheDocument();
  });

  it("leaves existing callers unaffected (no total, not approx)", () => {
    render(<CountChip value={4} label="sessions" />);
    expect(screen.getByText("4 sessions")).toBeInTheDocument();
  });

  it("renders a filtered subset as 'value/outOf'", () => {
    render(<CountChip value={1} outOf={2} compact />);
    expect(screen.getByText("1/2")).toHaveClass("count-chip");
  });

  it("does not render the total when it does not exceed the loaded value", () => {
    render(<CountChip value={8} label="events" total={8} />);
    expect(screen.getByText("8 events")).toBeInTheDocument();
  });
});
