import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TreeNodePayload } from "../../api/workspace";
import { TreeRail } from "../TreeRail";

// Keep the real positioning and clipping middleware. jsdom has no layout or
// resize observers, so native test events supply autoUpdate's notifications.
vi.mock("@floating-ui/dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@floating-ui/dom")>();
  return {
    ...actual,
    autoUpdate: (owner: HTMLElement, _popup: HTMLElement, update: () => void) => {
      const rail = owner.closest(".tree-rail-body")!;
      rail.addEventListener("scroll", update);
      window.addEventListener("resize", update);
      update();
      return () => {
        rail.removeEventListener("scroll", update);
        window.removeEventListener("resize", update);
      };
    },
  };
});

function node(id = "first"): TreeNodePayload {
  return {
    id, title: `${id} full title`, section: "context", slug: null,
    node_kind: "page_ref", parent_id: null, sort_key: id,
    target_kind: "context_page", target_id: id, is_system_node: false,
    can_have_children: false, primary_placement: true, visibility_status: "visible",
    children_count: 0, breadcrumb: [],
  };
}

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.matches(".tree-row")) return new DOMRect(30, 150, 240, 36);
    if (this.matches(".tree-row-tooltip")) return new DOMRect(0, 0, 280, 70);
    if (this.matches(".tree-rail-body")) return new DOMRect(20, 100, 260, 240);
    return new DOMRect(0, 0, 1024, 768);
  });
  for (const property of ["offsetWidth", "clientWidth"] as const) {
    vi.spyOn(HTMLElement.prototype, property, "get").mockImplementation(function (this: HTMLElement) {
      return this.getBoundingClientRect().width;
    });
  }
  for (const property of ["offsetHeight", "clientHeight"] as const) {
    vi.spyOn(HTMLElement.prototype, property, "get").mockImplementation(function (this: HTMLElement) {
      return this.getBoundingClientRect().height;
    });
  }
});
afterEach(() => { vi.restoreAllMocks(); });

describe("TreeRail tooltip ownership", () => {
  it("dismisses an unfocused hover tooltip with document Escape and stays dismissed over that row", () => {
    render(<TreeRail title="Context" nodes={[node()]} />);
    const row = screen.getByRole("treeitem");
    fireEvent.mouseEnter(row);
    expect(document.body).toHaveFocus();
    expect(screen.getByRole("tooltip")).toHaveTextContent("first full title");
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.pointerMove(row);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("allows pointer handoff to the popup, then dismisses when its entire hover region is left", () => {
    render(<TreeRail title="Context" nodes={[node()]} />);
    const row = screen.getByRole("treeitem");
    fireEvent.mouseEnter(row);
    const popup = screen.getByRole("tooltip");
    fireEvent.mouseLeave(row, { relatedTarget: popup });
    fireEvent.mouseEnter(popup, { relatedTarget: row });
    fireEvent.pointerMove(popup);
    expect(screen.getByRole("tooltip")).toBe(popup);
    fireEvent.mouseLeave(popup, { relatedTarget: document.body });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("keeps Escape inside a popup from immediately reopening the row exposed underneath it", () => {
    render(<TreeRail title="Context" nodes={[node(), node("second")]} />);
    const [first, second] = screen.getAllByRole("treeitem");
    fireEvent.mouseEnter(first, { clientX: 20, clientY: 30 });
    const popup = screen.getByRole("tooltip");
    fireEvent.mouseLeave(first, { relatedTarget: popup });
    fireEvent.pointerMove(popup, { clientX: 20, clientY: 80 });
    fireEvent.keyDown(document.body, { key: "Escape" });
    fireEvent.mouseEnter(second, { clientX: 20, clientY: 80 });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.pointerMove(second, { clientX: 20, clientY: 80 });
    fireEvent.mouseEnter(second, { clientX: 20, clientY: 80 });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.pointerMove(second, { clientX: 21, clientY: 80 });
    fireEvent.mouseEnter(second, { clientX: 21, clientY: 80 });
    expect(screen.getByRole("tooltip")).toHaveTextContent("second full title");
  });

  it("allows a tooltip inside its own modal mobile drawer", async () => {
    const { rerender } = render(<div aria-modal="true" role="dialog"><span /><TreeRail title="Context" nodes={[node()]} /></div>);
    fireEvent.mouseEnter(screen.getByRole("treeitem"));
    expect(screen.getByRole("tooltip")).toHaveTextContent("first full title");
    // A drawer rerender must not make the overlay observer mistake the
    // containing mobile dialog for a newly covering modal.
    await act(async () => {
      rerender(<div aria-modal="true" role="dialog"><span>Drawer heading</span><TreeRail title="Context" nodes={[node()]} /></div>);
    });
    expect(screen.getByRole("tooltip")).toHaveTextContent("first full title");
  });

  it("keeps one active tooltip when hover and keyboard focus belong to different rails", () => {
    render(<><TreeRail title="Context" nodes={[node()]} /><TreeRail title="Other" nodes={[node("second")]} /></>);
    const [first, second] = screen.getAllByRole("treeitem");
    act(() => { first.focus(); });
    fireEvent.mouseEnter(second);
    expect(screen.getAllByRole("tooltip")).toHaveLength(1);
    expect(screen.getByRole("tooltip")).toHaveTextContent("second full title");
    fireEvent.mouseLeave(second, { relatedTarget: document.body });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("hands Escape to an opened menu and dismisses on drag or a newly opened modal", async () => {
    render(<TreeRail title="Context" nodes={[node()]} onRenameNode={vi.fn()} />);
    const row = screen.getByRole("treeitem");
    fireEvent.mouseEnter(row);
    fireEvent.click(within(row).getByRole("button", { name: "Open actions for first full title" }));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    fireEvent.mouseEnter(row);
    fireEvent.dragStart(row);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.mouseEnter(row);
    const modal = document.createElement("div");
    modal.setAttribute("aria-modal", "true");
    await act(async () => { document.body.append(modal); });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    modal.remove();
  });
});

describe("TreeRail tooltip live geometry", () => {
  function layout({ tooltipHeight = 70, viewportHeight = 768 } = {}) {
    let rowTop = 150;
    let rowLeft = 30;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.matches(".tree-row") && this.closest<HTMLElement>(".tree-rail-body")?.style.display === "none") return new DOMRect();
      if (this.matches(".tree-row")) return new DOMRect(rowLeft, rowTop, 240, 36);
      if (this.matches(".tree-row-tooltip")) {
        const contentMaxHeight = Number.parseFloat(this.querySelector<HTMLElement>(".tree-row-tooltip-content")?.style.maxHeight ?? "");
        return new DOMRect(0, 0, 280, Math.min(tooltipHeight, Number.isFinite(contentMaxHeight) ? contentMaxHeight + 2 : tooltipHeight));
      }
      if (this.matches(".tree-rail-body")) return new DOMRect(20, 100, 260, 240);
      return new DOMRect(0, 0, 1024, viewportHeight);
    });
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.getBoundingClientRect().width;
    });
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.getBoundingClientRect().height;
    });
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.getBoundingClientRect().width;
    });
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.getBoundingClientRect().height;
    });
    render(<TreeRail title="Context" nodes={[node()]} />);
    const row = screen.getByRole("treeitem");
    const rail = row.closest<HTMLElement>(".tree-rail-body")!;
    rail.style.overflowY = "auto";
    act(() => { row.focus(); });
    return { row, rail, move: (top: number, left = rowLeft) => { rowTop = top; rowLeft = left; } };
  }

  it("repositions a focused owner on scroll and viewport resize using its current box", async () => {
    const { rail, move } = layout();
    await waitFor(() => { expect(screen.getByRole("tooltip")).toHaveStyle({ top: "192px" }); });
    move(180);
    fireEvent.scroll(rail);
    await waitFor(() => { expect(screen.getByRole("tooltip")).toHaveStyle({ top: "222px" }); });
    move(210, 60);
    fireEvent.resize(window);
    await waitFor(() => { expect(screen.getByRole("tooltip")).toHaveStyle({ top: "252px", left: "60px" }); });
  });

  it("dismisses a still-focused owner once it scrolls out of the clipped rail", async () => {
    const { row, rail, move } = layout();
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    move(50);
    fireEvent.scroll(rail);
    await waitFor(() => { expect(screen.queryByRole("tooltip")).not.toBeInTheDocument(); });
    expect(row).toHaveFocus();
  });

  it("dismisses when responsive layout removes the owner's rendered box without unmounting it", async () => {
    const { rail, row } = layout();
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    rail.style.display = "none";
    fireEvent.resize(window);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(row).toHaveFocus();
  });

  it("fits a long title into the available side height without covering its owner or leaving the viewport", async () => {
    const { row } = layout({ tooltipHeight: 700, viewportHeight: 280 });
    await waitFor(() => {
      const popup = screen.getByRole("tooltip");
      const top = Number.parseFloat(popup.style.top);
      const height = popup.getBoundingClientRect().height;
      expect(top).toBeGreaterThanOrEqual(8);
      expect(top + height).toBeLessThanOrEqual(row.getBoundingClientRect().top - 6);
      expect(Number.parseFloat(popup.querySelector<HTMLElement>(".tree-row-tooltip-content")!.style.maxHeight)).toBeGreaterThan(0);
    });
  });
});
