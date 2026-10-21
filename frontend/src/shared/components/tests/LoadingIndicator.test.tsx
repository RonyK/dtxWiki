import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LoadingIndicator } from "../LoadingIndicator";
import { Spinner } from "../Spinner";

describe("LoadingIndicator", () => {
  it("announces one visible message without a nested spinner status", () => {
    render(<LoadingIndicator>워크스페이스 불러오는 중…</LoadingIndicator>);

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-atomic", "true");
    expect(status).toHaveTextContent("워크스페이스 불러오는 중…");
    expect(within(status).queryByRole("status")).not.toBeInTheDocument();
    expect(status.querySelector(".spinner")).toHaveAttribute("aria-hidden", "true");
  });

  it("supports labelled inline and paragraph contexts with the same glyph", () => {
    const { rerender } = render(
      <LoadingIndicator as="span" aria-label="Loading memberships" size={14}>
        Checking membership…
      </LoadingIndicator>,
    );
    const inline = screen.getByRole("status", { name: "Loading memberships" });
    expect(inline.tagName).toBe("SPAN");
    expect(inline.querySelector(".spinner-glyph")).toHaveStyle({ width: "14px", height: "14px" });

    rerender(<LoadingIndicator as="p" className="settings-loading-row">Loading settings…</LoadingIndicator>);
    expect(screen.getByRole("status").tagName).toBe("P");
    expect(screen.getByRole("status")).toHaveClass("settings-loading-row");
  });
});

describe("Spinner", () => {
  it("announces its label when rendered without a surrounding status", () => {
    render(<Spinner aria-label="검색 결과 불러오는 중" />);
    const status = screen.getByRole("status", { name: "검색 결과 불러오는 중" });
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status.querySelector(".spinner-glyph")).toHaveAttribute("aria-hidden", "true");
  });
});
