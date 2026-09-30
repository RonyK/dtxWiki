import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { i18n } from "../../i18n";
import { BottomSheet } from "../BottomSheet";
import { MobileDrawer } from "../MobileDrawer";

describe("mobile workspace surfaces", () => {
  it("renders MobileDrawer with mobile dialog semantics", () => {
    render(
      <MobileDrawer
        id="drawer"
        label="Explorer"
        mobileLayout
        mobileOpen
        onPointerDown={vi.fn()}
      >
        <button type="button">Drawer action</button>
      </MobileDrawer>,
    );

    const drawer = screen.getByRole("dialog", { name: "Explorer drawer" });
    expect(drawer).toHaveClass("workspace-left-panel");
    expect(drawer).toHaveAttribute("aria-modal", "true");
    expect(drawer).toHaveAttribute("data-mobile-open", "true");
    expect(screen.getByRole("button", { name: "Drawer action" })).toBeInTheDocument();
  });

  it("marks closed MobileDrawer content inert on mobile", () => {
    const view = render(
      <MobileDrawer id="drawer" label="Explorer" mobileLayout mobileOpen={false}>
        <button type="button">Drawer action</button>
      </MobileDrawer>,
    );

    const drawer = view.container.querySelector("#drawer");
    expect(drawer).toHaveAttribute("aria-hidden", "true");
    expect(drawer).toHaveAttribute("inert");
    expect(drawer?.getAttribute("inert")).toBe("");
    expect(drawer).toHaveAttribute("data-mobile-open", "false");
  });

  it("renders BottomSheet with mobile dialog semantics", () => {
    render(
      <BottomSheet id="sheet" label="Details" mobileLayout mobileOpen>
        <button type="button">Sheet action</button>
      </BottomSheet>,
    );

    const sheet = screen.getByRole("dialog", { name: "Details sheet" });
    expect(sheet).toHaveClass("inspector-panel");
    expect(sheet).toHaveAttribute("aria-modal", "true");
    expect(sheet).toHaveAttribute("data-mobile-open", "true");
    expect(screen.getByRole("button", { name: "Sheet action" })).toBeInTheDocument();
  });

  it("marks closed BottomSheet content inert on mobile", () => {
    const view = render(
      <BottomSheet id="sheet" label="Details" mobileLayout mobileOpen={false}>
        <button type="button">Sheet action</button>
      </BottomSheet>,
    );

    const sheet = view.container.querySelector("#sheet");
    expect(sheet).toHaveAttribute("aria-hidden", "true");
    expect(sheet).toHaveAttribute("inert");
    expect(sheet?.getAttribute("inert")).toBe("");
    expect(sheet).toHaveAttribute("data-mobile-open", "false");
  });

  it("localizes the surface type in the dialog names", async () => {
    await i18n.changeLanguage("ko");

    render(
      <>
        <MobileDrawer id="drawer" label="탐색기" mobileLayout mobileOpen>
          <button type="button">Drawer action</button>
        </MobileDrawer>
        <BottomSheet id="sheet" label="세부 정보" mobileLayout mobileOpen>
          <button type="button">Sheet action</button>
        </BottomSheet>
      </>,
    );

    expect(screen.getByRole("dialog", { name: "탐색기 서랍" })).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "세부 정보 시트" })).toBeInTheDocument();
  });
});
