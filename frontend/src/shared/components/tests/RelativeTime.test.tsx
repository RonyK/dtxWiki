import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RelativeTime } from "../RelativeTime";

const NOW = new Date("2026-06-13T12:00:00Z");

afterEach(() => {
  vi.useRealTimers();
});

describe("RelativeTime", () => {
  it("renders a localized relative label with the absolute time in the tooltip", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const oneMinuteAgo = new Date(NOW.getTime() - 60_000).toISOString();
    render(<RelativeTime value={oneMinuteAgo} locale="en" />);
    const el = screen.getByText("1 minute ago");
    expect(el.tagName).toBe("TIME");
    expect(el).toHaveAttribute("dateTime", oneMinuteAgo);
    // Tooltip carries the absolute long-form timestamp.
    expect(el.getAttribute("title")).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/);
  });

  it("re-renders as time passes via the shared tick", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const value = new Date(NOW.getTime() - 60_000).toISOString();
    render(<RelativeTime value={value} locale="en" />);
    expect(screen.getByText("1 minute ago")).toBeInTheDocument();

    // The label is fixed at render; advancing the clock past a tick must update
    // it without a prop change (the clock-refresh fix).
    act(() => {
      vi.advanceTimersByTime(5 * 60_000);
    });
    expect(screen.queryByText("1 minute ago")).not.toBeInTheDocument();
    expect(screen.getByText("6 minutes ago")).toBeInTheDocument();
  });

  it("shows the neutral marker for a missing value", () => {
    render(<RelativeTime value={null} locale="en" />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
