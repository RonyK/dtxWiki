import { useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../i18n";
import { Dialog } from "../Dialog";
import { TreeRail } from "../TreeRail";
import { WorkspaceLayout } from "../WorkspaceLayout";

describe("WorkspaceLayout", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setMobileViewport(false);
  });

  it("closes and restores the explorer and details panels", async () => {
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="test"
        left={({ onClose }) => (
          <aside aria-label="Explorer">
            <button onClick={onClose} type="button">
              Hide Explorer
            </button>
          </aside>
        )}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveAttribute("data-left-collapsed", "false");
    expect(layout).toHaveAttribute("data-inspector-collapsed", "false");
    expect(layout).toHaveAttribute("data-mobile-left-open", "false");
    expect(layout).toHaveAttribute("data-mobile-inspector-open", "false");
    expect(layout).toHaveStyle({
      "--workspace-left-w": "280px",
      "--workspace-right-w": "300px",
    });

    await user.click(screen.getByRole("button", { name: "Hide Explorer" }));
    expect(layout).toHaveAttribute("data-left-collapsed", "true");
    expect(layout).toHaveStyle({ "--workspace-left-w": "44px" });
    expect(screen.getByRole("button", { name: "Show Explorer" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Hide Details" }));
    expect(layout).toHaveAttribute("data-inspector-collapsed", "true");
    expect(layout).toHaveStyle({ "--workspace-right-w": "44px" });
    expect(screen.getByRole("button", { name: "Show Details" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Show Explorer" }));
    await user.click(screen.getByRole("button", { name: "Show Details" }));

    expect(layout).toHaveAttribute("data-left-collapsed", "false");
    expect(layout).toHaveAttribute("data-inspector-collapsed", "false");
    expect(layout).toHaveStyle({
      "--workspace-left-w": "280px",
      "--workspace-right-w": "300px",
    });
  });

  it("supports a collapsed default for the details panel", () => {
    const view = render(
      <WorkspaceLayout
        defaultInspectorOpen={false}
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="collapsed"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveAttribute("data-inspector-collapsed", "true");
    expect(layout).toHaveStyle({ "--workspace-right-w": "44px" });
    expect(screen.getByRole("button", { name: "Show Details" })).toBeInTheDocument();
  });

  it("restores a right-only inspector without reserving an explorer rail", async () => {
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<div>Map details</div>}
        inspectorLabel="Details"
        layoutKey="map"
        main={<main>Map</main>}
      />,
    );
    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveAttribute("data-has-left", "false");
    expect(layout).toHaveStyle({ "--workspace-left-w": "0px" });
    expect(view.container.querySelector(".workspace-left-panel")).toBeNull();
    expect(view.container.querySelector(".workspace-panel-restore-left")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Hide Details" }));
    await user.click(screen.getByRole("button", { name: "Show Details" }));
    expect(screen.getByText("Map details")).toBeVisible();

    view.unmount();
    setMobileViewport(true);
    const mobileView = render(
      <WorkspaceLayout inspector={<div>Map details</div>} inspectorLabel="Details"
        layoutKey="map" main={<main>Map</main>} />,
    );
    expect(mobileView.container.querySelector(".workspace-mobile-drawer-toggle")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Open Details sheet" }));
    expect(screen.getByRole("dialog", { name: "Details sheet" })).toBeVisible();
  });

  it("supports screens without a details panel", () => {
    const view = render(
      <WorkspaceLayout
        inspector={null}
        layoutKey="no-inspector"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveAttribute("data-inspector-collapsed", "true");
    expect(layout).toHaveStyle({ "--workspace-right-w": "0px" });
    expect(screen.queryByRole("button", { name: "Show Details" })).toBeNull();
  });

  it("supports a collapsed default for the explorer panel", () => {
    const view = render(
      <WorkspaceLayout
        defaultLeftOpen={false}
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="left-collapsed"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveAttribute("data-left-collapsed", "true");
    expect(layout).toHaveStyle({ "--workspace-left-w": "44px" });
    expect(screen.getByRole("button", { name: "Show Explorer" })).toBeInTheDocument();
  });

  it("opens the Agent explorer by default when no preference is stored", () => {
    const view = render(
      <WorkspaceLayout
        defaultLeftOpen
        inspector={null}
        layoutKey="agent"
        left={() => <aside aria-label="Explorer">Agent tree</aside>}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    expect(view.container.querySelector(".workspace-screen"))
      .toHaveAttribute("data-left-collapsed", "false");
    expect(screen.getByLabelText("Explorer")).toHaveTextContent("Agent tree");
  });

  it("lets stored panel state override the collapsed default", () => {
    window.localStorage.setItem(
      "dtxwiki:workspace-layout:stored:inspector",
      "true",
    );

    const view = render(
      <WorkspaceLayout
        defaultInspectorOpen={false}
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="stored"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveAttribute("data-inspector-collapsed", "false");
  });

  it("lets stored explorer panel state override the collapsed default", () => {
    window.localStorage.setItem("dtxwiki:workspace-layout:stored-left:left", "true");

    const view = render(
      <WorkspaceLayout
        defaultLeftOpen={false}
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="stored-left"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveAttribute("data-left-collapsed", "false");
    expect(layout).toHaveStyle({ "--workspace-left-w": "280px" });
  });

  it("keeps the legacy v1 left preference for non-browse layouts (#17)", () => {
    // Browse layouts use v2 keys; other layouts still honor their unversioned
    // v1 preference rather than resetting unrelated panel choices.
    window.localStorage.setItem("dtxwiki:workspace-layout:ask:left", "true");

    const view = render(
      <WorkspaceLayout
        defaultLeftOpen={false}
        layoutKey="ask"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    expect(
      view.container.querySelector(".workspace-screen"),
    ).toHaveAttribute("data-left-collapsed", "false");
  });

  it("ignores the stale collapsed Agent preference after default-open change", () => {
    window.localStorage.setItem("dtxwiki:workspace-layout:agent:left", "false");

    const view = render(
      <WorkspaceLayout
        defaultLeftOpen
        layoutKey="agent"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    expect(
      view.container.querySelector(".workspace-screen"),
    ).toHaveAttribute("data-left-collapsed", "false");
    expect(window.localStorage.getItem("dtxwiki:workspace-layout:v2:agent:left"))
      .toBe("true");
  });

  it("honors a stored v2 left preference for browse screens", () => {
    // Proves the v2 read path (not just the default): a returning context user
    // who collapsed the rail under v2 keeps it collapsed despite the open default.
    window.localStorage.setItem("dtxwiki:workspace-layout:v2:context:left", "false");

    const view = render(
      <WorkspaceLayout
        layoutKey="context"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    expect(
      view.container.querySelector(".workspace-screen"),
    ).toHaveAttribute("data-left-collapsed", "true");
  });

  it("ignores a stale v1 left value for browse screens (#3 stays fixed)", () => {
    // A returning context user had v1 left="false"; the v2 key is absent, so it
    // must fall through to the new open default, not the stale collapsed value.
    window.localStorage.setItem("dtxwiki:workspace-layout:context:left", "false");

    const view = render(
      <WorkspaceLayout
        layoutKey="context"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    expect(
      view.container.querySelector(".workspace-screen"),
    ).toHaveAttribute("data-left-collapsed", "false");
  });

  it("opens details when an inspector auto-open key is provided", () => {
    const view = render(
      <WorkspaceLayout
        defaultInspectorOpen={false}
        inspector={<div>Inspector content</div>}
        inspectorAutoOpenKey={null}
        inspectorLabel="Details"
        layoutKey="auto"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveAttribute("data-inspector-collapsed", "true");

    view.rerender(
      <WorkspaceLayout
        defaultInspectorOpen={false}
        inspector={<div>Inspector content</div>}
        inspectorAutoOpenKey="turn-1"
        inspectorLabel="Details"
        layoutKey="auto"
        left={() => <aside aria-label="Explorer" />}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    expect(layout).toHaveAttribute("data-inspector-collapsed", "false");
  });

  it("cycles the explorer width from the left panel control", async () => {
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="width"
        left={({ expandWidthLabel, onExpandWidth }) => (
          <aside aria-label="Explorer">
            <button onClick={onExpandWidth} type="button">
              {expandWidthLabel}
            </button>
          </aside>
        )}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    expect(layout).toHaveStyle({ "--workspace-left-w": "280px" });

    await user.click(screen.getByRole("button", { name: "Widen Explorer" }));
    expect(layout).toHaveStyle({ "--workspace-left-w": "420px" });

    await user.click(screen.getByRole("button", { name: "Widen Explorer" }));
    expect(layout).toHaveStyle({ "--workspace-left-w": "560px" });

    await user.click(screen.getByRole("button", { name: "Reset Explorer width" }));
    expect(layout).toHaveStyle({ "--workspace-left-w": "280px" });
  });

  it("localizes the panel width and visibility controls", async () => {
    // The panel labels are composed from a caller-supplied rail name, so they
    // are the last shell strings that can silently stay English.
    const user = userEvent.setup();
    await i18n.changeLanguage("ko");
    render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="세부 정보"
        layoutKey="localized"
        left={({ expandWidthLabel, onExpandWidth }) => (
          <aside aria-label="탐색기">
            <button aria-label={expandWidthLabel} onClick={onExpandWidth} type="button" />
          </aside>
        )}
        leftLabel="탐색기"
        main={<main>Main content</main>}
      />,
    );

    expect(screen.getByRole("button", { name: "탐색기 넓히기" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "세부 정보 숨기기" }));

    expect(screen.getByRole("button", { name: "세부 정보 표시" })).toBeInTheDocument();
  });

  it("opens and closes the mobile explorer drawer without changing desktop width", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="mobile-drawer"
        left={({ onExpandWidth }) => (
          <aside aria-label="Explorer">
            Explorer content
            {onExpandWidth ? <button type="button">Widen Explorer</button> : null}
          </aside>
        )}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    const leftPanel = view.container.querySelector(".workspace-left-panel");
    expect(layout).toHaveAttribute("data-mobile-left-open", "false");
    expect(layout).toHaveStyle({ "--workspace-left-w": "280px" });
    expect(leftPanel).toHaveAttribute("data-mobile-open", "false");

    await user.click(screen.getByRole("button", { name: "Open Explorer drawer" }));

    expect(layout).toHaveAttribute("data-mobile-left-open", "true");
    expect(layout).toHaveStyle({ "--workspace-left-w": "280px" });
    expect(leftPanel).toHaveAttribute("data-mobile-open", "true");
    expect(screen.queryByRole("button", { name: "Widen Explorer" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Close Explorer drawer" }));

    expect(layout).toHaveAttribute("data-mobile-left-open", "false");
    expect(layout).toHaveAttribute("data-left-collapsed", "false");
    expect(leftPanel).toHaveAttribute("data-mobile-open", "false");
  });

  it("dismisses the mobile explorer drawer with a left swipe", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="mobile-drawer-swipe"
        left={() => <aside aria-label="Explorer">Explorer content</aside>}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    const leftPanel = view.container.querySelector(".workspace-left-panel");
    await user.click(screen.getByRole("button", { name: "Open Explorer drawer" }));
    expect(layout).toHaveAttribute("data-mobile-left-open", "true");

    fireEvent.pointerDown(leftPanel as Element, { clientX: 260 });
    fireEvent.pointerMove(leftPanel as Element, { clientX: 170 });
    fireEvent.pointerUp(leftPanel as Element, { clientX: 170 });

    expect(layout).toHaveAttribute("data-mobile-left-open", "false");
    expect(leftPanel).toHaveAttribute("data-mobile-open", "false");
  });

  it("traps keyboard focus in the mobile explorer drawer and restores the trigger", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="mobile-drawer-keyboard"
        left={() => (
          <aside aria-label="Explorer">
            <button type="button">First drawer action</button>
            <button type="button">Last drawer action</button>
          </aside>
        )}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    const trigger = screen.getByRole("button", { name: "Open Explorer drawer" });
    await user.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "Explorer drawer" });
    const firstAction = within(dialog).getByRole("button", { name: "First drawer action" });
    const lastAction = within(dialog).getByRole("button", { name: "Last drawer action" });
    await waitFor(() => expect(firstAction).toHaveFocus());

    fireEvent.keyDown(firstAction, { key: "Tab", shiftKey: true });
    expect(lastAction).toHaveFocus();
    fireEvent.keyDown(lastAction, { key: "Tab" });
    expect(firstAction).toHaveFocus();

    fireEvent.keyDown(firstAction, { key: "Escape" });

    expect(layout).toHaveAttribute("data-mobile-left-open", "false");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Open Explorer drawer" })).toHaveFocus(),
    );
  });

  it("lets an open tree row menu own the first Escape before closing the mobile drawer", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        layoutKey="mobile-drawer-nested-menu"
        left={() => (
          <TreeRail
            title="Context"
            nodes={[
              {
                id: "page-node",
                section: "context",
                title: "Page",
                slug: "page",
                node_kind: "page_ref",
                parent_id: null,
                sort_key: "0001",
                target_kind: "context_page",
                target_id: "page-id",
                is_system_node: false,
                can_have_children: false,
                primary_placement: true,
                visibility_status: "visible",
                children_count: 0,
                breadcrumb: [],
              },
            ]}
            onCopyNodeLink={() => undefined}
          />
        )}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    await user.click(screen.getByRole("button", { name: "Open Explorer drawer" }));
    const menuTrigger = screen.getByRole("button", {
      name: "Open actions for Page",
    });
    await user.click(menuTrigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(layout).toHaveAttribute("data-mobile-left-open", "true");
    await waitFor(() => expect(menuTrigger).toHaveFocus());

    await user.keyboard("{Escape}");

    expect(layout).toHaveAttribute("data-mobile-left-open", "false");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Open Explorer drawer" })).toHaveFocus(),
    );
  });

  it("lets a portaled dialog own the first Escape before closing the mobile drawer", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        layoutKey="mobile-drawer-nested-dialog"
        left={() => (
          <aside aria-label="Explorer">
            <NestedDialogLauncher />
          </aside>
        )}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    await user.click(screen.getByRole("button", { name: "Open Explorer drawer" }));
    const dialogOpener = screen.getByRole("button", { name: "Open nested dialog" });
    await waitFor(() => expect(dialogOpener).toHaveFocus());
    await user.click(dialogOpener);
    expect(screen.getByRole("dialog", { name: "Nested confirmation" })).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Nested confirmation" }))
      .not.toBeInTheDocument();
    expect(layout).toHaveAttribute("data-mobile-left-open", "true");
    await waitFor(() => expect(dialogOpener).toHaveFocus());

    await user.keyboard("{Escape}");

    expect(layout).toHaveAttribute("data-mobile-left-open", "false");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Open Explorer drawer" })).toHaveFocus(),
    );
  });

  it("opens and dismisses the mobile inspector sheet without changing desktop width", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="mobile-inspector"
        left={() => <aside aria-label="Explorer">Explorer content</aside>}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    const inspectorPanel = view.container.querySelector(".inspector-panel");
    expect(layout).toHaveAttribute("data-mobile-inspector-open", "false");
    expect(layout).toHaveStyle({ "--workspace-right-w": "300px" });
    expect(inspectorPanel).toHaveAttribute("data-mobile-open", "false");

    const sheetTrigger = screen.getByRole("button", { name: "Open Details sheet" });
    expect(
      [...sheetTrigger.querySelectorAll("path")].map((path) => path.getAttribute("d")),
    ).toEqual(["M3 15h18", "M12 7v6", "m9 10 3-3 3 3"]);

    await user.click(sheetTrigger);

    expect(layout).toHaveAttribute("data-mobile-inspector-open", "true");
    expect(layout).toHaveStyle({ "--workspace-right-w": "300px" });
    expect(inspectorPanel).toHaveAttribute("data-mobile-open", "true");
    const hideDetails = within(screen.getByRole("dialog", { name: "Details sheet" }))
      .getByRole("button", { name: "Hide Details" });
    expect(
      [...hideDetails.querySelectorAll("path")].map((path) => path.getAttribute("d")),
    ).toEqual(["M3 15h18", "M12 7v6", "m9 12 3 3 3-3"]);

    await user.click(screen.getByRole("button", { name: "Close Details sheet" }));

    expect(layout).toHaveAttribute("data-mobile-inspector-open", "false");
    expect(layout).toHaveAttribute("data-inspector-collapsed", "false");
    expect(inspectorPanel).toHaveAttribute("data-mobile-open", "false");
  });

  it("closes the mobile inspector sheet on Escape and restores the trigger", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<button type="button">Inspector action</button>}
        inspectorLabel="Details"
        layoutKey="mobile-inspector-keyboard"
        left={() => <aside aria-label="Explorer">Explorer content</aside>}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    const trigger = screen.getByRole("button", { name: "Open Details sheet" });
    await user.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "Details sheet" });
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "Hide Details" })).toHaveFocus(),
    );

    fireEvent.keyDown(
      within(dialog).getByRole("button", { name: "Hide Details" }),
      { key: "Escape" },
    );

    expect(layout).toHaveAttribute("data-mobile-inspector-open", "false");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Open Details sheet" })).toHaveFocus(),
    );
  });

  it("dismisses the mobile inspector sheet with a downward swipe", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const view = render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="mobile-inspector-swipe"
        left={() => <aside aria-label="Explorer">Explorer content</aside>}
        leftLabel="Explorer"
        main={<main>Main content</main>}
      />,
    );

    const layout = view.container.querySelector(".workspace-screen");
    const inspectorPanel = view.container.querySelector(".inspector-panel");
    await user.click(screen.getByRole("button", { name: "Open Details sheet" }));
    expect(layout).toHaveAttribute("data-mobile-inspector-open", "true");

    fireEvent.pointerDown(inspectorPanel as Element, { clientY: 120 });
    fireEvent.pointerMove(inspectorPanel as Element, { clientY: 210 });
    fireEvent.pointerUp(inspectorPanel as Element, { clientY: 210 });

    expect(layout).toHaveAttribute("data-mobile-inspector-open", "false");
    expect(layout).toHaveAttribute("data-inspector-collapsed", "false");
    expect(inspectorPanel).toHaveAttribute("data-mobile-open", "false");
  });

  it("runs the mobile floating action", async () => {
    setMobileViewport(true);
    const user = userEvent.setup();
    const onCreate = vi.fn();

    render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="mobile-action"
        left={() => <aside aria-label="Explorer">Explorer content</aside>}
        leftLabel="Explorer"
        main={<main>Main content</main>}
        mobileAction={{
          ariaLabel: "Create workspace item",
          label: "New",
          onClick: onCreate,
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Create workspace item" }));

    expect(onCreate).toHaveBeenCalledTimes(1);
  });

  it("marks Context mobile actions for the non-overlay responsive dock", () => {
    setMobileViewport(true);

    const view = render(
      <WorkspaceLayout
        inspector={<div>Inspector content</div>}
        inspectorLabel="Details"
        layoutKey="context-mobile-actions"
        left={() => <aside aria-label="Explorer">Explorer content</aside>}
        leftLabel="Explorer"
        main={<main className="feature-main">Wide diagram content</main>}
        mobileAction={{
          ariaLabel: "Create Context item",
          label: "New",
          onClick: () => undefined,
        }}
        variant="context"
      />,
    );

    expect(view.container.querySelector(".workspace-screen"))
      .toHaveClass("context-screen");
    expect(screen.getByRole("button", { name: "Create Context item" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Details sheet" }))
      .toBeInTheDocument();
  });
});

function setMobileViewport(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

function NestedDialogLauncher() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">Open nested dialog</button>
      {open ? (
        <Dialog labelledBy="nested-dialog-title" onClose={() => setOpen(false)}>
          <h2 id="nested-dialog-title">Nested confirmation</h2>
        </Dialog>
      ) : null}
    </>
  );
}
