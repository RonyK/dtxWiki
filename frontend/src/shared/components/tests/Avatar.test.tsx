import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  actorDisplayName,
  AvatarWithTimestamp,
  avatarInitials,
} from "../Avatar";

afterEach(() => {
  vi.useRealTimers();
});

describe("Avatar", () => {
  it("renders initials, actor name, and the shared relative time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-09T02:00:00Z"));

    render(
      <AvatarWithTimestamp
        actorType="human"
        name="Test User"
        timestamp="2026-05-09T00:00:00Z"
      />,
    );

    expect(screen.getByText("TU")).toBeInTheDocument();
    expect(screen.getByText("Test User")).toBeInTheDocument();
    // The shared helper routes through Intl.RelativeTimeFormat (localized
    // wording), not the old English-only "2h ago" shape.
    expect(screen.getByText("2 hours ago")).toBeInTheDocument();
  });

  it("normalizes actor fallback names and initials", () => {
    expect(actorDisplayName(null, "user")).toBe("Human editor");
    expect(actorDisplayName(null, "agent")).toBe("Agent worker");
    expect(actorDisplayName(null, "system_task")).toBe("System Task");
    expect(avatarInitials("Human editor")).toBe("HE");
  });
});
