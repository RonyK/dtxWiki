import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { i18n } from "../../i18n";
import { StatusPill } from "../StatusPill";

// Tone is the real contract here; the label now routes through i18n, so locate
// the pill by class instead of by its (translated) text.
function pill() {
  const el = document.querySelector(".status-pill");
  if (!el) {
    throw new Error("no status pill rendered");
  }
  return el;
}

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("StatusPill", () => {
  it("applies tones by status independent of the rendered label", () => {
    const { rerender } = render(<StatusPill status="active" />);
    expect(pill()).toHaveClass("status-pill", "status-pill-ok");
    rerender(<StatusPill status="draft" />);
    expect(pill()).toHaveClass("status-pill-warn");
    rerender(<StatusPill status="created" />);
    expect(pill()).toHaveClass("status-pill-ok");
    rerender(<StatusPill status="edited" />);
    expect(pill()).toHaveClass("status-pill-warn");
    rerender(<StatusPill status="referenced" />);
    expect(pill()).toHaveClass("status-pill-info");
    rerender(<StatusPill status="failed" />);
    expect(pill()).toHaveClass("status-pill-danger");
    rerender(<StatusPill status="stale" />);
    expect(pill()).toHaveClass("status-pill-muted");
  });

  it("distinguishes review risk levels by tone", () => {
    const { rerender } = render(<StatusPill status="high" />);
    expect(pill()).toHaveClass("status-pill-danger");
    rerender(<StatusPill status="medium" />);
    expect(pill()).toHaveClass("status-pill-warn");
    rerender(<StatusPill status="low" />);
    expect(pill()).toHaveClass("status-pill-muted");
  });

  it("distinguishes lint severity and lifecycle tones", () => {
    const { rerender } = render(<StatusPill status="error" />);
    expect(pill()).toHaveClass("status-pill-danger");
    rerender(<StatusPill status="warning" />);
    expect(pill()).toHaveClass("status-pill-warn");
    rerender(<StatusPill status="info" />);
    expect(pill()).toHaveClass("status-pill-info");
    rerender(<StatusPill status="open" />);
    expect(pill()).toHaveClass("status-pill-warn");
    rerender(<StatusPill status="acknowledged" />);
    expect(pill()).toHaveClass("status-pill-info");
    rerender(<StatusPill status="fixed" />);
    expect(pill()).toHaveClass("status-pill-ok");
    rerender(<StatusPill status="dismissed" />);
    expect(pill()).toHaveClass("status-pill-muted");
  });

  it("matches tone patterns case-insensitively", () => {
    render(<StatusPill status="REJECTED" />);
    expect(pill()).toHaveClass("status-pill-danger");
  });

  it("falls back to info tone for unknown statuses", () => {
    render(<StatusPill status="brand_new_state" />);
    expect(pill()).toHaveClass("status-pill-info");
  });

  it("self-translates a known enum via the shared status map, en and ko (A3)", async () => {
    const { rerender } = render(<StatusPill status="extract_pending" />);
    expect(screen.getByText("Extraction pending")).toBeInTheDocument();

    await i18n.changeLanguage("ko");
    rerender(<StatusPill status="extract_pending" />);
    expect(screen.getByText("추출 대기 중")).toBeInTheDocument();
    // The raw enum no longer leaks English into a ko surface.
    expect(screen.queryByText("extract pending")).not.toBeInTheDocument();
  });

  it("falls back to the de-underscored value for an unmapped status", () => {
    render(<StatusPill status="brand_new_state" />);
    expect(screen.getByText("brand new state")).toBeInTheDocument();
  });

  it("prefers an explicit label over the status map", () => {
    render(<StatusPill status="published" label="Custom label" />);
    expect(screen.getByText("Custom label")).toBeInTheDocument();
    expect(screen.queryByText("Published")).not.toBeInTheDocument();
  });

  it("toggles the compact modifier class", () => {
    const { rerender } = render(<StatusPill status="active" compact />);
    expect(pill()).toHaveClass("compact");
    rerender(<StatusPill status="active" />);
    expect(pill()).not.toHaveClass("compact");
  });
});
