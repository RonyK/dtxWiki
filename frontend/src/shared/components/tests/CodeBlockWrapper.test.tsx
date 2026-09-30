import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodeBlockWrapper } from "../CodeBlockWrapper";

describe("CodeBlockWrapper", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
  });

  it("renders a language badge and copies code to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(
      <CodeBlockWrapper code="const value = 1;" language="TypeScript">
        <pre>
          <code>const value = 1;</code>
        </pre>
      </CodeBlockWrapper>,
    );

    expect(screen.getByText("TypeScript")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("const value = 1;");
      expect(screen.getByRole("button", { name: "Code copied" })).toBeInTheDocument();
    });
  });

  it("disables copy when the block has no source text", () => {
    render(
      <CodeBlockWrapper code="" language="text">
        <pre>
          <code />
        </pre>
      </CodeBlockWrapper>,
    );

    expect(screen.getByRole("button", { name: "Copy code" })).toBeDisabled();
  });
});
