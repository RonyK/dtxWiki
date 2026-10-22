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
  it("dismisses on chevron activation after pointer focus and allows the newly expanded child's first hover", () => {
    const parent = { ...node("parent"), node_kind: "folder" as const, target_kind: null, target_id: null, can_have_children: true, children_count: 1 };
    const child = { ...node("child"), parent_id: parent.id };
    const nodes = [parent, child];
    const onExpandedNodeIdsChange = vi.fn();
    const { rerender } = render(<TreeRail title="Context" nodes={nodes} expandedNodeIds={[]} onExpandedNodeIdsChange={onExpandedNodeIdsChange} />);
    const parentRow = screen.getByRole("treeitem", { name: parent.title });
    fireEvent.mouseEnter(parentRow, { clientX: 40, clientY: 165 });
    const chevron = within(parentRow).getByRole("button", { name: `Expand ${parent.title}` });
    fireEvent.pointerDown(chevron, { clientX: 40, clientY: 165, pointerType: "mouse" });
    act(() => { chevron.focus(); });
    expect(screen.getByRole("tooltip")).toHaveTextContent(parent.title);
    fireEvent.click(chevron, { clientX: 40, clientY: 165, detail: 1 });
    expect(onExpandedNodeIdsChange).toHaveBeenCalledWith([parent.id]);
    rerender(<TreeRail title="Context" nodes={nodes} expandedNodeIds={[parent.id]} onExpandedNodeIdsChange={onExpandedNodeIdsChange} />);
    expect(chevron).toHaveFocus();
    fireEvent.mouseEnter(parentRow, { clientX: 40, clientY: 165 });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    // Chromium/WebKit send mouseenter before the next pointermove. The first
    // actual entry into the newly revealed child must not be suppressed.
    const childRow = screen.getByRole("treeitem", { name: child.title });
    fireEvent.mouseEnter(childRow, { clientX: 80, clientY: 201 });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.pointerMove(childRow, { clientX: 80, clientY: 201 });
    expect(screen.getByRole("tooltip")).toHaveTextContent(child.title);
  });

  it("uses the actual click position after pointer movement while the popup was dismissed", () => {
    const parent = { ...node("parent"), node_kind: "folder" as const, target_kind: null, target_id: null, can_have_children: true, children_count: 1 };
    render(<TreeRail title="Context" nodes={[parent]} expandedNodeIds={[]} onExpandedNodeIdsChange={vi.fn()} />);
    const row = screen.getByRole("treeitem");
    const chevron = within(row).getByRole("button", { name: `Expand ${parent.title}` });
    fireEvent.mouseEnter(row, { clientX: 80, clientY: 165 });
    act(() => { chevron.focus(); });
    fireEvent.keyDown(chevron, { key: "Escape" });
    // No active popup is left to track this within-row pointer movement.
    fireEvent.pointerMove(chevron, { clientX: 40, clientY: 165 });
    fireEvent.click(chevron, { clientX: 40, clientY: 165, detail: 1 });
    fireEvent.mouseEnter(row, { clientX: 40, clientY: 165 });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("dismisses during keyboard expansion while preserving tree focus and subsequent child navigation", () => {
    const parent = { ...node("parent"), node_kind: "folder" as const, target_kind: null, target_id: null, can_have_children: true, children_count: 1 };
    const child = { ...node("child"), parent_id: parent.id };
    const nodes = [parent, child];
    const onExpandedNodeIdsChange = vi.fn();
    const { rerender } = render(<TreeRail title="Context" nodes={nodes} expandedNodeIds={[]} onExpandedNodeIdsChange={onExpandedNodeIdsChange} />);
    const parentRow = screen.getByRole("treeitem", { name: parent.title });
    act(() => { parentRow.focus(); });
    expect(screen.getByRole("tooltip")).toHaveTextContent(parent.title);
    fireEvent.keyDown(parentRow, { key: "ArrowRight" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(parentRow).toHaveFocus();
    rerender(<TreeRail title="Context" nodes={nodes} expandedNodeIds={[parent.id]} onExpandedNodeIdsChange={onExpandedNodeIdsChange} />);
    fireEvent.keyDown(parentRow, { key: "ArrowDown" });
    expect(screen.getByRole("treeitem", { name: child.title })).toHaveFocus();
    expect(screen.getByRole("tooltip")).toHaveTextContent(child.title);
  });

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

  it("ignores rounded stationary mouseenter after Escape and resumes on actual pointer movement", () => {
    render(<TreeRail title="Context" nodes={[node()]} />);
    const row = screen.getByRole("treeitem");
    fireEvent.mouseEnter(row, { clientX: 20, clientY: 80 });
    fireEvent.pointerMove(row, { clientX: 20, clientY: 80.75 });
    fireEvent.keyDown(document.body, { key: "Escape" });
    // Chromium truncates MouseEvent coordinates while PointerEvent retains
    // fractions; a layout change under a still pointer synthesizes enter.
    fireEvent.mouseEnter(row, { clientX: 20, clientY: 80 });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.pointerMove(row, { clientX: 20, clientY: 80.75 });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.pointerMove(row, { clientX: 20, clientY: 81 });
    expect(screen.getByRole("tooltip")).toHaveTextContent("first full title");
  });

  it("does not open a hover tooltip during touch movement", () => {
    render(<TreeRail title="Context" nodes={[node()]} />);
    const move = new MouseEvent("pointermove", { bubbles: true, clientX: 40, clientY: 165 });
    Object.defineProperty(move, "pointerType", { value: "touch" });
    fireEvent(screen.getByRole("treeitem"), move);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
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
  function layout({ titleLines = 2, chrome = 36, viewportHeight = 768 } = {}) {
    let rowTop = 150;
    let rowLeft = 30;
    // A title shows no more than the line count the hook clamps it to.
    const shownLines = (popup: HTMLElement | null) => Math.min(titleLines,
      Number(popup?.style.getPropertyValue("--tree-tooltip-title-lines")) || 4);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.matches(".tree-row") && this.closest<HTMLElement>(".tree-rail-body")?.style.display === "none") return new DOMRect();
      if (this.matches(".tree-row")) return new DOMRect(rowLeft, rowTop, 240, 36);
      // A deeply indented title starts past the rail's clip (right edge 280);
      // the popup's visibility must follow the whole row, not the title.
      if (this.matches(".tree-row-title")) return new DOMRect(rowLeft + 300, rowTop + 8, 150, 20);
      if (this.matches(".tree-row-tooltip")) return new DOMRect(0, 0, 280, chrome + shownLines(this) * 17);
      if (this.matches(".tree-row-tooltip-title")) return new DOMRect(0, 0, 262, shownLines(this.closest(".tree-row-tooltip")) * 17);
      if (this.matches(".tree-row-tooltip-arrow")) return new DOMRect(0, 0, 8, 8);
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
    // Beside the row, outside the rail, with the arrow at the row's middle.
    await waitFor(() => { expect(screen.getByRole("tooltip")).toHaveStyle({ top: "150px", left: "278px" }); });
    expect(screen.getByRole("tooltip").querySelector(".tree-row-tooltip-arrow")).toHaveStyle({ top: "14px" });
    move(180);
    fireEvent.scroll(rail);
    await waitFor(() => { expect(screen.getByRole("tooltip")).toHaveStyle({ top: "180px" }); });
    move(210, 60);
    fireEvent.resize(window);
    await waitFor(() => { expect(screen.getByRole("tooltip")).toHaveStyle({ top: "210px", left: "308px" }); });
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

  it("drops long-title lines to stay beside its owner inside the viewport", async () => {
    const { row } = layout({ titleLines: 12, chrome: 160, viewportHeight: 220 });
    const popup = screen.getByRole("tooltip");
    popup.querySelector<HTMLElement>(".tree-row-tooltip-title")!.style.lineHeight = "17px";
    fireEvent.resize(window);
    // Four lines (228px) exceed the 204px viewport height between paddings,
    // even shifted up; two lines fit.
    await waitFor(() => { expect(popup.style.getPropertyValue("--tree-tooltip-title-lines")).toBe("2"); });
    const top = Number.parseFloat(popup.style.top);
    expect(top).toBeGreaterThanOrEqual(8);
    expect(top + popup.getBoundingClientRect().height).toBeLessThanOrEqual(212);
    expect(Number.parseFloat(popup.style.left)).toBeGreaterThanOrEqual(row.getBoundingClientRect().right);
  });
});
