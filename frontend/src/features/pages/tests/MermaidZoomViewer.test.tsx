import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MermaidZoomViewer } from "../MermaidZoomViewer";

describe("MermaidZoomViewer", () => {
  it("renders zoom controls and sanitized SVG content", () => {
    render(
      <MermaidZoomViewer
        svg='<svg xmlns="http://www.w3.org/2000/svg"><text>Diagram</text></svg>'
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("toolbar", { name: "Mermaid zoom controls" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Zoom out" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reset zoom" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close Mermaid zoom" }))
      .toBeInTheDocument();
    expect(screen.getByText("Diagram")).toBeInTheDocument();
  });

  it("zooms with controls and resets to 100 percent", async () => {
    render(<MermaidZoomViewer svg="<svg/>" onClose={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByRole("button", { name: "Current zoom 120%" }))
      .toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Reset zoom" }));
    expect(screen.getByRole("button", { name: "Current zoom 100%" }))
      .toBeInTheDocument();
  });

  it("supports keyboard zoom shortcuts while mounted", async () => {
    render(<MermaidZoomViewer svg="<svg/>" onClose={vi.fn()} />);

    await userEvent.keyboard("+");
    expect(screen.getByRole("button", { name: "Current zoom 120%" }))
      .toBeInTheDocument();

    await userEvent.keyboard("0");
    expect(screen.getByRole("button", { name: "Current zoom 100%" }))
      .toBeInTheDocument();
  });

  it("supports touch pinch zoom", () => {
    render(<MermaidZoomViewer svg="<svg/>" onClose={vi.fn()} />);

    const viewport = screen.getByRole("img", { name: "Mermaid diagram" });
    fireEvent.touchStart(viewport, {
      touches: [
        { clientX: 0, clientY: 0 },
        { clientX: 100, clientY: 0 },
      ],
    });
    fireEvent.touchMove(viewport, {
      touches: [
        { clientX: 0, clientY: 0 },
        { clientX: 200, clientY: 0 },
      ],
    });

    expect(screen.getByRole("button", { name: "Current zoom 200%" }))
      .toBeInTheDocument();
  });

  it("calls onClose from the close button", async () => {
    const onClose = vi.fn();
    render(<MermaidZoomViewer svg="<svg/>" onClose={onClose} />);

    await userEvent.click(screen.getByRole("button", { name: "Close Mermaid zoom" }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
