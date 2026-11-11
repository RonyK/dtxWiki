import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import * as graphApi from "../../../shared/api/graph";
import * as operations from "../../../shared/api/operations";
import * as authContext from "../../../shared/auth/AuthContext";
import type {
  GraphHistoryMode,
  GraphNode,
  GraphQueryPayload,
} from "../../../shared/api/graph";
import { GraphView } from "../GraphView";
import { buildLocalScene } from "../local/sceneInput";
import type { GraphRenderScene } from "../shared/rendererTypes";
import { graphFixture } from "./graphFixture";
import type { Ref } from "react";
import type { GraphCanvasHandle } from "../GraphCanvas";

const { canvasHandle } = vi.hoisted(() => ({ canvasHandle: {
  centerOn: vi.fn(), fit: vi.fn(), locate: vi.fn(), pauseLayout: vi.fn(), resumeLayout: vi.fn(),
  pin: vi.fn(() => null), reset: vi.fn(), setViewport: vi.fn(), zoomIn: vi.fn(), zoomOut: vi.fn(),
} }));

vi.mock("../GraphCanvas", async () => {
  const { useImperativeHandle } = await import("react");
  return {
  GraphCanvas: ({
    active,
    focusedId,
    onHover,
    onPin,
    onRendererStatusChange,
    onSelect,
    layoutPaused,
    pinnedIds,
    scene,
    selectedId,
    showLabels,
    ref,
  }: {
    active: boolean;
    focusedId: string | null;
    onHover: (id: string | null) => void;
    onPin: (id: string, position: { x: number; y: number }) => void;
    onRendererStatusChange: (status: "loading" | "ready" | "error") => void;
    onSelect: (id: string | null) => void;
    layoutPaused?: boolean;
    pinnedIds: string[];
    scene: GraphRenderScene;
    selectedId: string | null;
    showLabels?: boolean;
    ref?: Ref<GraphCanvasHandle>;
  }) => {
    useImperativeHandle(ref, () => canvasHandle);
    return (
    <div
      data-edge-labels={scene.edges.filter((edge) => !edge.hidden).map((edge) => edge.label).join("|")}
      data-edge-counts={scene.edges.filter((edge) => !edge.hidden).map((edge) => edge.count ?? 1).join("|")}
      data-rendered-edge-ids={buildLocalScene({ ...scene, edges: scene.edges.filter((edge) => !edge.hidden) }).edges.map((edge) => edge.graphId).sort().join("|")}
      data-edges={scene.edges.filter((edge) => !edge.hidden && !edge.removed).length}
      data-removed-edges={scene.edges.filter((edge) => !edge.hidden && edge.removed).length}
      data-active={String(active)}
      data-current={scene.currentId ?? ""}
      data-focused={focusedId ?? ""}
      data-nodes={scene.nodes.filter((node) => !node.hidden).length}
      data-pinned={pinnedIds.join("|")}
      data-selected={selectedId ?? ""}
      data-show-labels={String(showLabels)}
      data-layout-paused={String(layoutPaused)}
      data-testid="mock-graph-canvas"
    >
      <button onClick={() => onSelect("node:ask")} type="button">
        Select Ask from canvas
      </button>
      <button onClick={() => onSelect("edge:wikilink")} type="button">
        Select relationship from canvas
      </button>
      <button onClick={() => onSelect(null)} type="button">
        Clear from canvas stage
      </button>
      <button onClick={() => onHover("edge:wikilink")} type="button">
        Hover relationship from canvas
      </button>
      <button onClick={() => onHover("node:related")} type="button">
        Hover node from canvas
      </button>
      <button onClick={() => onRendererStatusChange("error")} type="button">
        Simulate renderer error
      </button>
      <button onClick={() => onPin("node:ask", { x: 1, y: 2 })} type="button">
        Report Ask pin from canvas
      </button>
      <button onClick={() => onRendererStatusChange("ready")} type="button">
        Simulate renderer ready
      </button>
    </div>
    );
  },
  };
});

vi.mock("../../pages/MarkdownView", () => ({
  MermaidDiagram: ({ source }: { source: string }) => <pre data-testid="mock-mermaid-diagram">{source}</pre>,
}));

describe("GraphView", () => {
  it("restores the chosen canvas ratio when opening a different Context page", () => {
    const key = "dtxwiki.contextGraph.aspectRatio";
    localStorage.setItem(key, "unsupported-ratio");
    try {
      const first = renderGraph({ openContents: false });
      expect(first.container.querySelector(".graph-height-picker > summary")).toHaveTextContent("16:11");
      fireEvent.click(first.container.querySelector(".graph-height-picker > summary")!);
      fireEvent.click(screen.getByRole("button", { name: "4:3" }));
      first.unmount();

      const payload = graphFixture();
      payload.query.seeds[0].id = payload.nodes[1].entity_id;
      payload.nodes = payload.nodes.map((node, index) => ({ ...node, is_seed: index === 1 }));
      const second = renderGraph({ openContents: false, payload });
      expect(second.container.querySelector(".graph-height-picker > summary")).toHaveTextContent("4:3");
      fireEvent.click(second.container.querySelector(".graph-height-picker > summary")!);
      expect(screen.getByRole("button", { name: "4:3" })).toHaveAttribute("aria-pressed", "true");
    } finally {
      localStorage.removeItem(key);
    }
  });

  it("loads the graph namespace without Context-owned graph strings", () => {
    const getFixedT = vi.spyOn(i18n, "getFixedT");
    try {
      renderGraph();

      expect(getFixedT.mock.calls.some(([, namespace]) => namespace === "graph"))
        .toBe(true);
      expect(i18n.hasResourceBundle("en", "graph")).toBe(true);
      expect(i18n.exists("graph.heading", { lng: "en", ns: "graph" })).toBe(true);
      expect(i18n.exists("graph.heading", { lng: "en", ns: "context" })).toBe(false);
      expect(i18n.exists("workspaceGraph.ui.title", { lng: "ko", ns: "graph" }))
        .toBe(true);
      expect(i18n.exists("workspaceGraph.ui.title", { lng: "ko", ns: "context" }))
        .toBe(false);
    } finally {
      getFixedT.mockRestore();
    }
  });

  it("separates type and scope, keeps camera controls inside the canvas, and collapses secondary content", async () => {
    const user = userEvent.setup();
    renderGraph({ openContents: false });
    const graph = screen.getByTestId("graph-view");
    const frame = graph.querySelector(".graph-frame")!;
    const stage = frame.querySelector(".graph-local-stage")!;
    expect(graph.querySelector(".graph-type-section")!.compareDocumentPosition(frame) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(frame.querySelector(".graph-frame-header")!.compareDocumentPosition(stage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(stage.nextElementSibling).toHaveClass("local-history");
    expect(stage).toContainElement(screen.getByRole("button", { name: "Fit graph" }));
    // The Map's control order and names; the Context graph pauses its layout too.
    expect([...stage.querySelectorAll(".graph-camera-controls > button")].map((button) => button.getAttribute("aria-label")))
      .toEqual(["Zoom in", "Zoom out", "Fit graph", "Pause layout", "Reset viewport, pins, and selection", "Export filtered graph"]);
    expect(screen.getByRole("button", { name: "Pause layout" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Simulate renderer ready" }));
    await user.click(screen.getByRole("button", { name: "Pause layout" }));
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-layout-paused", "true");
    await user.click(screen.getByRole("button", { name: "Resume layout" }));
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-layout-paused", "false");
    expect(stage).toContainElement(screen.getByRole("button", { name: "Refresh graph" }));
    expect(screen.queryByLabelText("History mode")).not.toBeInTheDocument();
    expect(graph.querySelector(".graph-list-layer")).toBeNull();
    expect(graph.querySelector(".graph-frame-filters")).not.toHaveAttribute("open");
    await user.click(screen.getByText("Refine"));
    expect(screen.getByLabelText("Ask turn")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "List" }));
    expect(screen.getByRole("button", { name: "List" })).toHaveAttribute("aria-pressed", "true");
    // The list replaces the canvas area inside the frame; the canvas stays mounted underneath.
    expect(stage.querySelector(".graph-list-layer")).toContainElement(screen.getByTestId("graph-list-view"));
    expect(stage).toContainElement(screen.getByTestId("mock-graph-canvas"));
  });

  it("names and locates the current document, not an expanded seed the payload lists first", async () => {
    const user = userEvent.setup();
    const base = graphFixture();
    // "Architecture Notes" was expanded: a seed after the primary one, listed first by the payload.
    const expanded = { ...base.nodes[1], is_seed: true };
    // The expanded seed also links to the primary seed's neighbour, a relation
    // off the path from the primary seed and never on a list row.
    const offPath = { ...base.edges[0], id: "edge:expanded-ask", target: "node:ask" };
    renderGraph({ openContents: false, payload: {
      ...base, edges: [...base.edges, offPath], nodes: [expanded, base.nodes[0], ...base.nodes.slice(2)],
      query: { ...base.query, seeds: [...base.query.seeds, { kind: expanded.entity_kind!, id: expanded.entity_id! }] },
    } });
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-current", "node:seed");
    const locate = screen.getByRole("button", { name: "Show Decision Log on the graph" });
    expect(locate).toHaveTextContent("Seed Decision Log+1");
    await user.click(locate);
    expect(canvasHandle.locate).toHaveBeenCalledExactlyOnceWith("node:seed");
    // The expanded document's path from the current document is the list's way
    // to its relation with the current document, which seed rows never select.
    await user.click(screen.getByRole("button", { name: "List" }));
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Architecture Notes"));
    const details = screen.getByRole("complementary", { name: "Details" });
    await user.click(seedPathRelation(details, /^Architecture Notes to Decision Log/));
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "edge:wikilink");
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Why was this chosen?"));
    await user.click(within(within(details).getByRole("list", { name: "Direct relationships with other seeds" }))
      .getByRole("button", { name: /^Architecture Notes to Why was this chosen\?/ }));
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "edge:expanded-ask");
  });

  it("keeps unselected Details hidden from navigation and does not open it for canvas hover", () => {
    renderGraph();
    const graph = screen.getByTestId("graph-view");
    const stage = graph.querySelector(".graph-local-stage")!;
    const canvas = screen.getByTestId("mock-graph-canvas");
    const details = screen.getByRole("heading", { name: "Details", hidden: true }).closest("aside")!;

    expect(details).toHaveAttribute("hidden");
    expect(details).not.toBeVisible();
    expect(screen.queryByRole("complementary", { name: "Details" })).not.toBeInTheDocument();
    expect(within(details).getByText("Select a node or relationship to inspect it."))
      .toBeInTheDocument();
    expect(details.querySelector("h5,button,a")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Hover node from canvas" }));
    fireEvent.click(screen.getByRole("button", { name: "Hover relationship from canvas" }));
    expect(graph.querySelector(".graph-local-stage")).toBe(stage);
    expect(screen.getByTestId("mock-graph-canvas")).toBe(canvas);
    expect(canvas).toHaveAttribute("data-selected", "");
    expect(details).toHaveAttribute("hidden");
    expect(screen.queryByRole("heading", { name: "Details" })).not.toBeInTheDocument();
  });

  it("hides external-view controls in fullscreen while keeping node and relationship Details inside the frame", () => {
    renderGraph({ openContents: false });
    const graph = screen.getByTestId("graph-view");
    const frame = graph.querySelector<HTMLElement>(".graph-frame")!;
    fireEvent.click(frame.querySelector(".graph-canvas-more > summary")!);
    expect(within(frame).getByRole("button", { name: "List" })).toBeVisible();
    expect(within(frame).getByRole("button", { name: "Open static graph view" })).toBeVisible();
    const descriptor = Object.getOwnPropertyDescriptor(document, "fullscreenElement");
    let fullscreenElement: Element | null = frame;
    Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => fullscreenElement });
    try {
      act(() => document.dispatchEvent(new Event("fullscreenchange")));
      // The list is a view of the frame, so it stays available in fullscreen.
      expect(within(frame).getByRole("button", { name: "List" })).toBeInTheDocument();
      expect(within(frame).queryByRole("button", { name: "Open static graph view" })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
      const details = within(frame).getByRole("complementary", { name: "Details" });
      expect(within(details).getByRole("heading", { name: "Why was this chosen?" })).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Select relationship from canvas" }));
      expect(within(details).getByRole("heading", { level: 5, name: "Wiki link" })).toBeVisible();
      expect(details.querySelector(".relation-list-ends")).toHaveTextContent(/Architecture Notes.*Link.*Decision Log/);
      expect(details).toBeVisible();
      expect(graph.querySelector(".graph-static-fallback")!.parentElement).toBe(graph);
      fullscreenElement = null;
      act(() => document.dispatchEvent(new Event("fullscreenchange")));
      expect(within(frame).getByRole("button", { name: "List" })).toBeVisible();
      expect(within(frame).getByRole("button", { name: "Open static graph view" })).toBeVisible();
    } finally {
      if (descriptor) Object.defineProperty(document, "fullscreenElement", descriptor);
      else Reflect.deleteProperty(document, "fullscreenElement");
    }
  });

  it("pages a 250-node, 750-relationship list by group with one Tab entry per list, highlight-only arrows, and keyboard Details handoff", async () => {
    const user = userEvent.setup();
    const payload = localListBudgetFixture();
    const onCollapse = vi.fn((_node: GraphNode) => Promise.resolve());
    const onExpand = vi.fn((_node: GraphNode) => Promise.resolve());
    const onDepthChange = vi.fn((_depth: 1 | 2) => Promise.resolve());
    const onHistoryModeChange = vi.fn((_mode: GraphHistoryMode) => Promise.resolve());
    renderGraph({ payload, onCollapse, onDepthChange, onExpand, onHistoryModeChange });
    const list = screen.getByTestId("graph-list-view");
    const canvas = screen.getByTestId("mock-graph-canvas");
    // The seed's own relations are open: three parallel relations each way around the ring.
    expect(within(within(list).getByRole("list", { name: "Incoming" })).getAllByRole("button")).toHaveLength(3);
    expect(within(within(list).getByRole("list", { name: "Outgoing" })).getAllByRole("button")).toHaveLength(3);
    // What the scope added is folded with its count, then shown ten rows at a time.
    const ringHead = within(list).getByRole("button", { name: /^Two or more steps away\s*247/ });
    expect(ringHead).toHaveAttribute("aria-expanded", "false");
    expect(within(list).queryByRole("list", { name: "Two or more steps away" })).not.toBeInTheDocument();
    await user.click(ringHead);
    const ring = within(list).getByRole("list", { name: "Two or more steps away" });
    const rows = within(ring).getAllByRole("button");
    expect(rows).toHaveLength(10);
    expect(ring.querySelector("a[tabindex='0'],a:not([tabindex])")).toBeNull();
    expect(within(list).getByRole("button", { name: "Show 10 more (237 left)" })).toBeInTheDocument();
    for (const entries of list.querySelectorAll("ul")) {
      const buttons = [...entries.querySelectorAll<HTMLButtonElement>(":scope > li > button")];
      expect(buttons.filter((button) => button.tabIndex === 0)).toHaveLength(1);
    }
    for (const mock of Object.values(canvasHandle)) mock.mockClear();
    act(() => rows[0].focus());
    await user.keyboard("{ArrowDown}{End}{Home}{End}");
    expect(rows[9]).toHaveFocus();
    const lastId = rows[9].getAttribute("data-local-graph-id")!;
    expect(canvas).toHaveAttribute("data-focused", lastId);
    expect(canvas).toHaveAttribute("data-selected", "");
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
    for (const action of [onCollapse, onDepthChange, onExpand, onHistoryModeChange]) expect(action).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("complementary", { name: "Details" })).toHaveFocus();
    expect(canvas).toHaveAttribute("data-selected", lastId);
    expect(canvas).toHaveAttribute("data-nodes", "250");
    expect(canvas).toHaveAttribute("data-edges", "750");
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
    for (const action of [onCollapse, onDepthChange, onExpand, onHistoryModeChange]) expect(action).not.toHaveBeenCalled();
  });

  it("keeps a seed's pages, selected row and focus across the bow-tie, the flow and the canvas", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    const inbound = Array.from({ length: 12 }, (_, index) => ({
      ...payload.nodes[1], id: `node:in:${index}`, entity_id: `in:${index}`, label: `Inbound ${index}`,
    }));
    payload.nodes = [...payload.nodes, ...inbound];
    payload.edges = [...payload.edges, ...inbound.map((node) => ({ ...payload.edges[0], id: `edge:${node.id}`, source: node.id }))];
    // Opening Details beside the list narrows it below the bow-tie width.
    let listWidth = 900;
    const resizes: (() => void)[] = [];
    class TestResizeObserver {
      callback: ResizeObserverCallback;
      constructor(callback: ResizeObserverCallback) {
        this.callback = callback;
      }
      observe(target: Element) {
        if (target.matches("[data-testid='graph-list-view']")) resizes.push(() => this.callback([], this));
      }
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.testid === "graph-list-view" ? listWidth : 0;
    });
    try {
      renderGraph({ payload });
      const list = screen.getByTestId("graph-list-view");
      expect(list).toHaveAttribute("data-layout", "bow-tie");
      const incoming = () => list.querySelectorAll<HTMLButtonElement>(
        ".graph-list-bowtie-column[data-direction='incoming'] button[data-local-graph-id]");
      // The last page removes its button: focus moves to the first row it revealed.
      await user.click(within(list).getByRole("button", { name: /^Show 3 more/ }));
      expect(incoming()).toHaveLength(13);
      expect(incoming()[10]).toHaveFocus();
      const chosen = incoming()[12];
      const id = chosen.dataset.localGraphId!;
      await user.click(chosen);
      // Details narrows the list to the flow: the page, the selected row and focus follow.
      listWidth = 500;
      act(() => resizes.forEach((resize) => resize()));
      expect(list).toHaveAttribute("data-layout", "flow");
      expect(list.querySelector("[data-relation-direction='incoming']")).toHaveAttribute("data-shown-count", "13");
      const pressed = list.querySelector<HTMLElement>(`[aria-pressed="true"][data-local-graph-id="${id}"]`);
      expect(pressed).toHaveFocus();
      // The canvas and back: the list keeps its pages.
      await user.click(screen.getByRole("button", { name: "Graph" }));
      await user.click(screen.getByRole("button", { name: "List" }));
      expect(screen.getByTestId("graph-list-view").querySelector("[data-relation-direction='incoming']"))
        .toHaveAttribute("data-shown-count", "13");
    } finally {
      width.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("shows and hides Details without remounting the stage or canvas, changing pins, or invoking graph actions", async () => {
    const user = userEvent.setup();
    const onCollapse = vi.fn((_node: GraphNode) => Promise.resolve());
    const onExpand = vi.fn((_node: GraphNode) => Promise.resolve());
    const onDepthChange = vi.fn((_depth: 1 | 2) => Promise.resolve());
    const onHistoryModeChange = vi.fn((_mode: GraphHistoryMode) => Promise.resolve());
    renderGraph({ onCollapse, onDepthChange, onExpand, onHistoryModeChange });
    const graph = screen.getByTestId("graph-view");
    const stage = graph.querySelector(".graph-local-stage")!;
    const canvas = screen.getByTestId("mock-graph-canvas");
    const details = screen.getByRole("heading", { name: "Details", hidden: true }).closest("aside")!;
    await pinAskFromDetails(user);
    for (const mock of Object.values(canvasHandle)) mock.mockClear();

    await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    expect(screen.getByRole("complementary", { name: "Details" })).toBe(details);
    expect(details).not.toHaveAttribute("hidden");
    expect(details).toBeVisible();
    expect(within(details).getByRole("heading", { level: 5, name: "Why was this chosen?" }))
      .toBeInTheDocument();
    const title = within(details).getByRole("heading", { level: 5, name: "Why was this chosen?" });
    expect(title.compareDocumentPosition(details.querySelector(".graph-detail-actions")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.click(within(details).getByRole("button", { name: "Clear selection" }));
    expect(details).toHaveAttribute("hidden");
    expect(details).not.toBeVisible();
    expect(screen.queryByRole("complementary", { name: "Details" })).not.toBeInTheDocument();
    expect(details.querySelector("h5,button,a")).toBeNull();
    expect(details).not.toHaveTextContent("Why was this chosen?");
    expect(graph.querySelector(".graph-local-stage")).toBe(stage);
    expect(screen.getByTestId("mock-graph-canvas")).toBe(canvas);
    expect(stage.querySelector(".graph-details")).toBe(details);
    expect(canvas).toHaveAttribute("data-nodes", "3");
    expect(canvas).toHaveAttribute("data-edges", "2");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
    expect(canvas).toHaveAttribute("data-selected", "");
    expect(graph).toHaveFocus();
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
    for (const action of [onCollapse, onDepthChange, onExpand, onHistoryModeChange]) {
      expect(action).not.toHaveBeenCalled();
    }
  });

  it("hides Details immediately when a filter removes its selection without retaining stale contents or remounting the canvas", async () => {
    const user = userEvent.setup();
    const onCollapse = vi.fn((_node: GraphNode) => Promise.resolve());
    const onExpand = vi.fn((_node: GraphNode) => Promise.resolve());
    const onDepthChange = vi.fn((_depth: 1 | 2) => Promise.resolve());
    const onHistoryModeChange = vi.fn((_mode: GraphHistoryMode) => Promise.resolve());
    renderGraph({ onCollapse, onDepthChange, onExpand, onHistoryModeChange });
    const graph = screen.getByTestId("graph-view");
    const stage = graph.querySelector(".graph-local-stage")!;
    const canvas = screen.getByTestId("mock-graph-canvas");
    await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    const details = screen.getByRole("complementary", { name: "Details" });
    expect(details).toHaveTextContent("Why was this chosen?");
    for (const mock of Object.values(canvasHandle)) mock.mockClear();

    await user.click(screen.getByText("Refine"));
    await user.click(screen.getByLabelText("Ask turn"));
    expect(details).toHaveAttribute("hidden");
    expect(screen.queryByRole("complementary", { name: "Details" })).not.toBeInTheDocument();
    expect(details).not.toHaveTextContent("Why was this chosen?");
    expect(details.querySelector("h5,button,a")).toBeNull();
    expect(graph.querySelector(".graph-local-stage")).toBe(stage);
    expect(screen.getByTestId("mock-graph-canvas")).toBe(canvas);
    expect(canvas).toHaveAttribute("data-nodes", "2");
    expect(canvas).toHaveAttribute("data-edges", "1");
    expect(canvas).toHaveAttribute("data-selected", "");
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
    for (const action of [onCollapse, onDepthChange, onExpand, onHistoryModeChange]) {
      expect(action).not.toHaveBeenCalled();
    }
  });

  it("lists every glyph and line of the mode in the shared shape and colour vocabulary", () => {
    const payload = graphFixture();
    payload.nodes.push(
      { ...payload.nodes[1], id: "node:library", entity_kind: "library_item_version", entity_id: "library", label: "Source" },
      { ...payload.nodes[1], id: "node:agent", entity_kind: "agent_page", entity_id: "agent", label: "Agent" },
    );
    const history = renderGraph({ payload });
    let legend = screen.getByTestId("graph-view").querySelector(".graph-legend")!;
    const kinds = (selector: string, attribute: string) =>
      [...legend.querySelectorAll(selector)].map((item) => item.getAttribute(attribute));
    // A fixed list per mode, not only what this scene happens to contain.
    expect(kinds("li[data-node-visual-kind]", "data-node-visual-kind"))
      .toEqual(["seed", "context", "version", "agent", "library", "conversation", "folder"]);
    // History splits activity by verb: writes and citations point at what
    // they changed or cited; reads and structure-only looks do not.
    expect(kinds("li[data-edge-visual-kind]", "data-edge-visual-kind"))
      .toEqual(["link", "citation", "coAccess", "membership", "actionWrite", "actionCite",
        "actionRead", "actionReference", "action", "removed"]);
    expect([...legend.querySelectorAll('svg[data-edge-arrow="target"]')]
      .map((cue) => cue.getAttribute("data-edge-visual-kind"))).toEqual(["actionWrite", "actionCite"]);
    expect(legend.querySelector('[data-legend-mark="event-target"] svg path[fill="none"]')).toHaveAttribute("stroke", "#7c3aed");
    expect(legend.querySelector('svg[data-node-visual-kind="version"] path')).toHaveAttribute("fill", "#7c3aed");
    expect(legend.querySelector('svg[data-node-visual-kind="context"] path')).toHaveAttribute("fill", "#7c3aed");
    expect(legend.querySelector('svg[data-node-visual-kind="agent"] path')).toHaveAttribute("fill", "#d97706");
    expect(legend.querySelector('svg[data-node-visual-kind="library"] path')).toHaveAttribute("fill", "#0f9f8f");
    expect(legend.querySelector('svg[data-node-visual-kind="conversation"] path')).toHaveAttribute("fill", "#0284c7");
    expect(legend.querySelector('svg[data-node-visual-kind="folder"] circle')).toHaveAttribute("fill", "none");
    expect(legend.querySelector('li[data-edge-visual-kind="action"] line')).toHaveAttribute("stroke-dasharray", "10 3");
    expect(legend.querySelector('li[data-edge-visual-kind="link"] line')).not.toHaveAttribute("stroke-dasharray");
    expect(legend.querySelector(".graph-action-cue,.graph-kind-cue")).toBeNull();
    history.unmount();

    renderGraph({ payload: graphRowCueFixture() });
    legend = screen.getByTestId("graph-view").querySelector(".graph-legend")!;
    expect(kinds("li[data-node-visual-kind]", "data-node-visual-kind"))
      .toEqual(["seed", "context", "agent", "library", "conversation", "folder"]);
    expect(kinds("li[data-edge-visual-kind]", "data-edge-visual-kind"))
      .toEqual(["link", "citation", "coAccess", "membership", "action"]);
  });

  it.each([false, true])("uses canonical actual-API node cues in main rows with forced colors=%s without changing labels or scene counts", (forcedColors) => {
    const restoreMedia = setGraphForcedColorsPreference(forcedColors);
    let view: ReturnType<typeof renderGraph> | null = null;
    try {
      const payload = graphRowCueFixture();
      view = renderGraph({ payload });
      const list = screen.getByTestId("graph-list-view");
      expect(screen.getByText("8 nodes · 11 edges")).toBeInTheDocument();
      // The seed is the hub; every other node is the far end of one of its relations.
      for (const node of payload.nodes) {
        const button = rowByLabel(list, node.label);
        expect(button).toHaveTextContent(node.label);
        expect(button).toHaveAccessibleName(new RegExp(node.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
        expect(button).toHaveAttribute("aria-pressed", "false");
        expectRowCue(payload, button, node, forcedColors);
      }
      const canvas = screen.getByTestId("mock-graph-canvas");
      expect(canvas).toHaveAttribute("data-nodes", "8");
      expect(canvas).toHaveAttribute("data-edges", "11");
      expect(canvas).toHaveAttribute("data-selected", "");
    } finally {
      view?.unmount();
      restoreMedia();
    }
  });

  it.each([false, true])("uses canonical actual-API node cues in neighbor and member rows with forced colors=%s", async (forcedColors) => {
    const restoreMedia = setGraphForcedColorsPreference(forcedColors);
    let view: ReturnType<typeof renderGraph> | null = null;
    try {
      const user = userEvent.setup();
      const payload = graphRowCueFixture();
      view = renderGraph({ payload });
      const list = screen.getByTestId("graph-list-view");
      const checkRows = (details: HTMLElement, expectedLabels: string[]) => {
        const buttons = detailsRelationRows(details);
        expect(buttons).toHaveLength(expectedLabels.length);
        expect(buttons.map((button) => button.querySelector("strong")!.textContent).sort())
          .toEqual([...expectedLabels].sort());
        for (const button of buttons) {
          const node = payload.nodes.find((candidate) => candidate.label === button.querySelector("strong")!.textContent)!;
          expectRowCue(payload, button, node, forcedColors);
        }
      };
      const memberLabels = ["Decision Log", "Source Version", "Source Artifact", "Published Snapshot", "Chat Session"];
      await user.click(rowByLabel(list, "Visible Source Folder"));
      const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
      checkRows(details, memberLabels);

      await user.click(rowByLabel(list, "Decision Log"));
      checkRows(details, payload.nodes.filter((node) => node.id !== "node:seed").map((node) => node.label));
      expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-nodes", "8");
      expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-edges", "11");
      expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:seed");
    } finally {
      view?.unmount();
      restoreMedia();
    }
  });

  it("shares scoped tokens and theme updates through one observer for rows, navigation, and legend, then disconnects", async () => {
    const NativeObserver = globalThis.MutationObserver;
    const observers: MutationObserver[] = [];
    const observerFactory = vi.spyOn(globalThis, "MutationObserver").mockImplementation(function (callback) {
      const observer = new NativeObserver(callback);
      vi.spyOn(observer, "disconnect");
      observers.push(observer);
      return observer;
    });
    const originalTheme = document.documentElement.getAttribute("data-theme");
    let view: ReturnType<typeof renderGraph> | null = null;
    try {
      view = renderGraph({ payload: graphRowCueFixture() });
      expect(observerFactory).toHaveBeenCalledTimes(1);
      const graph = screen.getByTestId("graph-view");
      const list = screen.getByTestId("graph-list-view");
      const rowFor = (label: string) => rowByLabel(list, label);
      await act(async () => {
        graph.style.setProperty("--graph-node-library", "#123456");
        graph.style.setProperty("--graph-surface", "#abcdef");
      });
      const libraryRow = rowFor("Source Version");
      expect(libraryRow.querySelector('[data-node-visual-kind="library"] path'))
        .toHaveAttribute("fill", "#123456");
      const legend = graph.querySelector(".graph-legend")!;
      expect(legend.querySelector('svg[data-node-visual-kind="library"] path'))
        .toHaveAttribute("fill", "#123456");
      const user = userEvent.setup();
      await user.click(rowFor("Decision Log"));
      const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
      expect(detailsRelationRows(details).find((row) => row.textContent?.includes("Source Version"))!
        .querySelector('[data-node-visual-kind="library"] path')).toHaveAttribute("fill", "#123456");
      await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
      // The current document's row shows the "This document" glyph in the Context colour.
      expect(rowFor("Decision Log").querySelector('[data-node-visual-kind="seed"] circle'))
        .toHaveAttribute("fill", "#8b5cf6");
      expect(legend.querySelector('svg[data-node-visual-kind="context"] path'))
        .toHaveAttribute("fill", "#8b5cf6");
      expect(libraryRow.querySelector('[data-node-visual-kind="library"] path'))
        .toHaveAttribute("fill", "#123456");
      expect(observerFactory).toHaveBeenCalledTimes(1);
      view.unmount();
      view = null;
      expect(observers[0].disconnect).toHaveBeenCalledTimes(1);
    } finally {
      view?.unmount();
      observerFactory.mockRestore();
      if (originalTheme === null) document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", originalTheme);
    }
  });

  it("keeps filters, canvas, DOM parity, active chips, and clear-all on one physical scene", async () => {
    const user = userEvent.setup();
    renderGraph({ openContents: false });

    expect(screen.getByText("3 nodes · 2 edges")).toBeInTheDocument();
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-nodes", "3");
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-edges", "2");
    // Camera commands belong to the canvas; List mode leaves them out of the menu.
    expect(screen.getByRole("button", { name: "Reset viewport, pins, and selection" }))
      .toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "List" }));
    expect(screen.queryByRole("button", { name: "Reset viewport, pins, and selection" })).not.toBeInTheDocument();
    await user.click(screen.getByText("Refine"));

    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Why was this chosen?"));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside");
    expect(within(details!).getByRole("heading", { level: 5, name: "Why was this chosen?" })).toBeInTheDocument();
    expect(within(details!).queryByRole("link", { name: "Open" })).not.toBeInTheDocument();
    expect(within(details!).getByRole("button", { name: "Center" })).toBeInTheDocument();
    expect(within(details!).getByRole("button", { name: "Pin" })).toBeInTheDocument();
    await user.click(screen.getByLabelText("Ask turn"));
    expect(screen.getByText("2 nodes · 1 edges")).toBeInTheDocument();
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-nodes", "2");
    expect(within(screen.getByTestId("graph-list-view")).queryByText("Why was this chosen?")).not.toBeInTheDocument();
    expect(screen.queryByText("included as evidence")).not.toBeInTheDocument();
    expect(screen.getByText("No visible change under the current filters."))
      .toBeInTheDocument();
    expect(within(details!).getByText("Select a node or relationship to inspect it."))
      .toBeInTheDocument();
    expect(details).toHaveAttribute("hidden");
    expect(screen.queryByRole("complementary", { name: "Details" })).not.toBeInTheDocument();
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "");
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-focused", "");
    expect(screen.getByRole("button", { name: "Remove filter: without Ask turn" }))
      .toBeInTheDocument();
    expect(screen.getByText("1 active filter")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear all" }));
    expect(screen.getByText("3 nodes · 2 edges")).toBeInTheDocument();
    expect(screen.getByText("0 active filters")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Inbound"));
    expect(screen.getByText("3 nodes · 1 edges")).toBeInTheDocument();
    expect(screen.queryByText("linked from")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear all" }));
    await user.click(screen.getByLabelText("Wiki link"));
    expect(screen.getByText("3 nodes · 1 edges")).toBeInTheDocument();
    expect(screen.queryByText("linked from")).not.toBeInTheDocument();

    // Filters hiding every relation: the list (not only the canvas) says so and offers the way back.
    await user.click(screen.getByLabelText("Inbound"));
    await user.click(screen.getByLabelText("Outbound"));
    const empty = screen.getByTestId("graph-list-view").querySelector<HTMLElement>(".graph-list-empty")!;
    expect(empty).toHaveTextContent("All relationships are hidden by the current filters.");
    await user.click(within(empty).getByRole("button", { name: "Clear all" }));
    expect(screen.getByText("3 nodes · 2 edges")).toBeInTheDocument();
  });

  it("opens the new Local renderer with relation chips and a live label toggle", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    payload.history.mode = "current";
    renderGraph({ openContents: false, payload });

    const canvas = screen.getByTestId("mock-graph-canvas");
    expect(canvas).toHaveAttribute("data-show-labels", "true");
    fireEvent.click(screen.getByTestId("graph-view").querySelector(".graph-view-options > summary")!);
    const labels = screen.getByRole("checkbox", { name: "Show labels" });
    await user.click(labels);
    expect(labels).not.toBeChecked();
    expect(canvas).toHaveAttribute("data-show-labels", "false");

    const links = screen.getByRole("button", { name: "Link 1" });
    expect(links).toHaveAttribute("aria-pressed", "true");
    await user.click(links);
    expect(links).toHaveAttribute("aria-pressed", "false");
    expect(canvas).toHaveAttribute("data-edges", "1");
    expect(screen.queryByRole("button", { name: "Often read together 0" })).not.toBeInTheDocument();
  });

  it("keeps backend created/edited/published actions on the canvas and in the list through both filter paths", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    const actionKinds = ["created", "edited", "published"];
    payload.history.mode = "action";
    payload.query.time = { mode: "action" };
    payload.nodes = [payload.nodes[0], ...actionKinds.map((kind, index): GraphNode => ({
      ...payload.nodes[1], id: `node:${kind}`, entity_id: `event-${kind}`,
      entity_kind: kind === "published" ? "audit_event" : "context_page_version",
      family: kind === "published" ? "event" : "document", label: `Event ${index + 1}`,
    }))];
    payload.edges = actionKinds.map((kind) => ({
      ...payload.edges[0], id: `action:${kind}`, edge_kind: kind, action_kind: kind,
      source: `node:${kind}`, target: payload.nodes[0].id,
      label: kind, layer: "action", provenance: "domain", sequence: null,
    }));
    payload.timeline = [];
    payload.facets.edge_kinds = actionKinds;
    payload.facets.action_kinds = actionKinds;
    payload.facets.layers = ["action"];
    renderGraph({ payload });

    const canvas = screen.getByTestId("mock-graph-canvas");
    const actions = screen.getByRole("button", { name: /Historical relationships.*3/ });
    const listRows = () => screen.getByTestId("graph-list-view").querySelectorAll(".graph-list-bowtie-row");
    expect(actions).toHaveAttribute("aria-pressed", "true");
    expect(canvas).toHaveAttribute("data-rendered-edge-ids", "action:created|action:edited|action:published");
    expect(listRows()).toHaveLength(3);
    expect(rowByLabel(screen.getByTestId("graph-list-view"), "Event 1").closest("li")?.querySelector('[data-visual="action"]')).toBeInTheDocument();
    await user.click(actions);
    expect(canvas).toHaveAttribute("data-rendered-edge-ids", "");
    expect(listRows()).toHaveLength(0);
    await user.click(actions);
    await user.click(screen.getByText("Refine"));
    const actionFilter = screen.getByRole("group", { name: "Action" });
    await user.click(within(actionFilter).getByLabelText("Edited"));
    expect(canvas).toHaveAttribute("data-rendered-edge-ids", "action:created|action:published");
    expect(listRows()).toHaveLength(2);
    expect(screen.getByText("4 nodes · 2 edges")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Legend" }));
    // Each activity line names the verbs it draws; the plain one the rest.
    const legend = screen.getByTestId("graph-view").querySelector(".graph-legend")!;
    expect(legend.querySelector('li[data-edge-visual-kind="actionWrite"]')).toHaveTextContent("Created · Edited");
    expect(screen.getByText("Review, publish, search and other records")).toBeVisible();
  });

  it("requests depth changes from the main toolbar without optimistically relabeling the scene", async () => {
    const user = userEvent.setup();
    let finishRequest!: () => void;
    const onDepthChange = vi.fn(() => new Promise<void>((resolve) => {
      finishRequest = resolve;
    }));
    const payload = graphFixture();
    payload.history.mode = "current";
    payload.query.time = { mode: "current" };
    payload.query.depth = 2;
    const { rerender } = renderGraph({ onDepthChange, payload });
    const toolbar = within(screen.getByRole("radiogroup", { name: "Relationship scope" }));
    expect(toolbar.getByRole("radio", { name: "Two-step expansion" })).toHaveAttribute("aria-checked", "true");
    toolbar.getByRole("radio", { name: "Two-step expansion" }).focus();
    await user.keyboard("{ArrowLeft}");
    expect(toolbar.getByRole("radio", { name: "Neighbor connections" })).toHaveFocus();
    expect(onDepthChange).toHaveBeenCalledWith(1, "neighbors");
    expect(toolbar.getByRole("radio", { name: "Two-step expansion" })).toHaveAttribute("aria-checked", "true");
    expect(toolbar.getByRole("radio", { name: "Neighbor connections" })).toHaveAttribute("aria-disabled", "true");
    await act(async () => { finishRequest(); });
    rerender(graphElement({
      onCollapse: () => Promise.resolve(), onExpand: () => Promise.resolve(),
      onDepthChange, payload: { ...payload, query: { ...payload.query, depth: 1 } },
    }));
    expect(toolbar.getByRole("radio", { name: "Neighbor connections" })).toHaveAttribute("aria-checked", "true");
    expect(toolbar.getByRole("radio", { name: "Neighbor connections" })).toHaveFocus();
    expect(toolbar.getByRole("radio", { name: "Two-step expansion" })).toHaveAttribute("aria-disabled", "false");
  });

  it("explains and disables unsupported two-hop history instead of pretending to offer it", async () => {
    const user = userEvent.setup();
    const onDepthChange = vi.fn(() => Promise.resolve());
    renderGraph({ onDepthChange });
    const toolbar = within(screen.getByRole("radiogroup", { name: "Relationship scope" }));
    expect(toolbar.getByRole("radio", { name: "Direct connections" })).toHaveAttribute("aria-checked", "true");
    const unsupported = toolbar.getByRole("radio", { name: "Two-step expansion" });
    expect(unsupported).toHaveAttribute("aria-disabled", "true");
    expect(unsupported).toHaveAttribute("title", "History views show direct relationships to this document, with session context for activities. Broader scopes are available in Current relationships.");
    expect(unsupported).toHaveAccessibleDescription("History views show direct relationships to this document, with session context for activities. Broader scopes are available in Current relationships.");
    await user.click(unsupported);
    expect(onDepthChange).not.toHaveBeenCalled();
  });

  it("keeps the last successful depth and reports a failed depth request", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    payload.history.mode = "current";
    payload.query.time = { mode: "current" };
    payload.query.depth = 2;
    renderGraph({ payload, onDepthChange: () => Promise.reject(new Error("failed")) });
    await user.click(screen.getByRole("radio", { name: "Neighbor connections" }));
    expect(screen.getByText("The graph could not be updated. Try again.")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Two-step expansion" })).toHaveAttribute("aria-checked", "true");
  });

  it("provides operable DOM selection and gates expansion to supported Context-page seeds", async () => {
    const user = userEvent.setup();
    const onCollapse = vi.fn((_node: GraphNode) => Promise.resolve());
    const onExpand = vi.fn((_node: GraphNode) => Promise.resolve());
    const { rerender } = renderGraph({ onCollapse, onExpand });
    const contents = screen.getByTestId("graph-list-view");

    await user.click(rowByLabel(contents, "Why was this chosen?"));
    let selectedDetails = screen.getByRole("complementary", { name: "Details" });
    const askExpand = within(selectedDetails).getByRole("button", { name: "Expand" });
    expect(askExpand).toBeDisabled();
    expect(askExpand)
      .toHaveAccessibleDescription("Expansion is available for Context pages only.");
    expect(within(selectedDetails).getByText("Expansion is available for Context pages only."))
      .toHaveAttribute("id", askExpand.getAttribute("aria-describedby"));
    expect(askExpand).not.toHaveAttribute("title");

    await user.click(rowByLabel(contents, "Decision Log"));
    selectedDetails = screen.getByRole("complementary", { name: "Details" });
    const primaryExpand = within(selectedDetails).getByRole("button", { name: "Expand" });
    expect(primaryExpand).toBeDisabled();
    expect(primaryExpand)
      .toHaveAccessibleDescription("The primary seed stays in the graph.");
    expect(within(selectedDetails).getByText("The primary seed stays in the graph."))
      .toHaveAttribute("id", primaryExpand.getAttribute("aria-describedby"));

    await user.click(rowByLabel(contents, "Architecture Notes"));
    const expand = within(screen.getByRole("complementary", { name: "Details" })).getByRole("button", { name: "Expand" });
    expect(expand).toBeEnabled();
    await user.click(expand);
    expect(onExpand).toHaveBeenCalledWith(
      expect.objectContaining({ entity_kind: "context_page", entity_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }),
    );

    const expandedPayload = graphFixture();
    expandedPayload.query.seeds.push({ kind: "context_page", id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" });
    rerender(graphElement({ onCollapse, onExpand, payload: expandedPayload }));
    await user.click(within(screen.getByRole("complementary", { name: "Details" })).getByRole("button", { name: "Collapse" }));
    expect(onCollapse).toHaveBeenCalledWith(
      expect.objectContaining({ entity_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }),
    );

    await user.click(rowByLabel(contents, "Decision Log"));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside");
    expect(details).not.toBeNull();
    expect(within(details!.querySelector<HTMLElement>(".graph-details-meta")!).getByTitle("Context document")).toHaveTextContent("Context");
    expect(within(details!).getByText("Published")).toBeInTheDocument();
    await user.click(rowByLabel(contents, "Architecture Notes"));
    expect(within(details!).getByRole("link", { name: "Open" }))
      .toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/context/pages/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
  });

  it("scopes Escape to Graph View and restores root focus for DOM and Canvas selections", async () => {
    const user = userEvent.setup();
    renderGraph();
    canvasHandle.pauseLayout.mockClear();
    const graph = screen.getByTestId("graph-view");
    const askSelect = rowByLabel(screen.getByTestId("graph-list-view"), "Why was this chosen?");

    await user.click(askSelect);
    expect(askSelect).toHaveAttribute("aria-pressed", "true");
    expect(within(screen.getByRole("heading", { name: "Details" }).closest("aside")!)
      .getByRole("heading", { level: 5, name: "Why was this chosen?" })).toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(askSelect).toHaveAttribute("aria-pressed", "true");

    fireEvent.keyDown(askSelect, { key: "Escape" });
    await waitFor(() => expect(graph).toHaveFocus());
    expect(askSelect).toHaveAttribute("aria-pressed", "false");
    expect(within(screen.getByRole("heading", { name: "Details", hidden: true }).closest("aside")!)
      .getByText("Select a node or relationship to inspect it."))
      .toBeInTheDocument();
    expect(screen.queryByRole("complementary", { name: "Details" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    expect(graph).toHaveFocus();
    expect(screen.getByTestId("mock-graph-canvas"))
      .toHaveAttribute("data-selected", "node:ask");

    fireEvent.keyDown(graph, { key: "Escape" });
    await waitFor(() => expect(graph).toHaveFocus());
    expect(screen.getByTestId("mock-graph-canvas"))
      .toHaveAttribute("data-selected", "");
    expect(within(screen.getByRole("heading", { name: "Details", hidden: true }).closest("aside")!)
      .getByText("Select a node or relationship to inspect it."))
      .toBeInTheDocument();
    expect(screen.queryByRole("complementary", { name: "Details" })).not.toBeInTheDocument();
    expect(canvasHandle.pauseLayout).not.toHaveBeenCalled();
  });

  it("uses the latest selected node for contextual actions and preserves busy, error, and capability guards", async () => {
    const user = userEvent.setup();
    let rejectExpansion!: (error: Error) => void;
    const onExpand = vi.fn((_node: GraphNode) => new Promise<void>((_resolve, reject) => {
      rejectExpansion = reject;
    }));
    const onCollapse = vi.fn((_node: GraphNode) => Promise.resolve());
    const payload = graphFixture();
    const workspaceGraphHref = (node: GraphNode) => `/graph?focus=${node.entity_id}`;
    const { rerender } = renderGraph({ payload, onCollapse, onExpand, workspaceGraphHref });
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Architecture Notes"));
    const details = screen.getByRole("complementary", { name: "Details" });
    const updated = { ...payload, nodes: payload.nodes.map((node) => node.id === "node:related"
      ? { ...node, label: "Latest document title" } : { ...node }) };
    rerender(graphElement({ payload: updated, onCollapse, onExpand, workspaceGraphHref }));
    expect(within(details).getByRole("link", { name: "Show in Knowledge Map" }))
      .toHaveAttribute("href", "/graph?focus=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
    const expand = within(details).getByRole("button", { name: "Expand" });
    await user.click(expand);
    expect(onExpand).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      id: "node:related", entity_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", label: "Latest document title",
    }));
    expect(expand).toBeDisabled();
    await user.click(expand);
    expect(onExpand).toHaveBeenCalledTimes(1);
    await act(async () => { rejectExpansion(new Error("private server failure")); });
    expect(screen.getByText("The graph could not be updated. Try again.")).toBeVisible();
    expect(screen.queryByText("private server failure")).not.toBeInTheDocument();
    expect(expand).toBeEnabled();
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:related");
    rerender(graphElement({
      payload: { ...updated, capabilities: {
        as_of: false, evidence: true, expand: false, export: true, saved_view: false, timeline: true,
      } },
      onCollapse, onExpand, workspaceGraphHref,
    }));
    expect(expand).toBeDisabled();
    expect(expand).toHaveAccessibleDescription("This graph projection does not support expansion.");
    expect(onCollapse).not.toHaveBeenCalled();
    expect(onExpand).toHaveBeenCalledTimes(1);
  });

  it("pauses timeline playback without permanently stopping the physical layout", async () => {
    renderGraph();
    canvasHandle.pauseLayout.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Play" }));
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(canvasHandle.pauseLayout).not.toHaveBeenCalled();
  });

  it("announces dynamic expansion availability while keeping the disabled reason adjacent", async () => {
    const onCollapse = vi.fn((_node: GraphNode) => Promise.resolve());
    const onExpand = vi.fn((_node: GraphNode) => Promise.resolve());
    const { rerender } = renderGraph({ onCollapse, onExpand });
    const liveRegion = screen.getByTestId("graph-view")
      .querySelector(".visually-hidden[aria-live='polite']");
    expect(liveRegion).toBeInTheDocument();

    const payload = graphFixture();
    payload.query.seeds = [
      payload.query.seeds[0],
      ...Array.from({ length: 7 }, (_, index) => ({
        kind: "context_page" as const,
        id: `expanded-${index}`,
      })),
    ];
    rerender(graphElement({ onCollapse, onExpand, payload }));

    const relatedRow = screen.getByText("Architecture Notes").closest("li");
    await userEvent.click(within(relatedRow!).getByRole("button"));
    const details = screen.getByRole("complementary", { name: "Details" });
    const expand = within(details).getByRole("button", { name: "Expand" });
    expect(expand).toBeDisabled();
    expect(expand).toHaveAccessibleDescription(
      "Collapse an expanded page before adding another (maximum 8 seeds).",
    );
    expect(within(details).getByText(
      "Collapse an expanded page before adding another (maximum 8 seeds).",
    )).toHaveAttribute("id", expand.getAttribute("aria-describedby"));
    expect(liveRegion).toHaveTextContent(
      "Collapse an expanded page before adding another (maximum 8 seeds).",
    );

    rerender(graphElement({ onCollapse, onExpand, payload: graphFixture() }));
    expect(within(details)
      .getByRole("button", { name: "Expand" })).toBeEnabled();
    expect(liveRegion)
      .toHaveTextContent("Additional Context pages can be expanded again.");
  });

  it("clears selection from a blank stage click and explicit button without resetting pins", async () => {
    const user = userEvent.setup();
    renderGraph();
    const graph = screen.getByTestId("graph-view");
    const canvas = screen.getByTestId("mock-graph-canvas");

    await pinAskFromDetails(user);
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    // A pin is "Pinning" until the canvas reports the coordinate it keeps;
    // the muted button stays a live cancel action and keeps its menu open.
    const pending = within(details).getByRole("button", { name: "Pinning: press to cancel" });
    expect(pending).toHaveTextContent("Pinning…");
    expect(pending.querySelector(".spinner[aria-hidden='true']")).not.toBeNull();
    await user.click(pending);
    expect(canvasHandle.pin).toHaveBeenLastCalledWith("node:ask", false);
    expect(canvas).toHaveAttribute("data-pinned", "");
    await user.click(within(details).getByRole("button", { name: "Pin" }));
    await user.click(screen.getByRole("button", { name: "Report Ask pin from canvas" }));
    const pinned = within(details).getByRole("button", { name: "Unpin" });
    expect(pinned).not.toHaveClass("graph-details-pin-pending");
    expect(pinned.querySelector(".spinner")).toBeNull();
    expect(screen.getByText("Pinned: Why was this chosen?")).toHaveAttribute("aria-live", "polite");
    await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    expect(within(details).queryByText(/Other nodes stay visible but dimmed/)).not.toBeInTheDocument();
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");

    await user.click(within(details).getByRole("button", { name: "Clear selection" }));
    expect(canvas).toHaveAttribute("data-selected", "");
    expect(canvas).toHaveAttribute("data-focused", "");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
    expect(graph).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    await user.click(screen.getByRole("button", { name: "Clear from canvas stage" }));
    expect(canvas).toHaveAttribute("data-selected", "");
    expect(canvas).toHaveAttribute("data-focused", "");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
    expect(within(details).getByText("Select a node or relationship to inspect it."))
      .toBeInTheDocument();
    expect(details).toHaveAttribute("hidden");
    expect(screen.queryByRole("complementary", { name: "Details" })).not.toBeInTheDocument();
  });

  it("keeps transient canvas hover separate from list focus and persistent selection", () => {
    renderGraph();
    const canvas = screen.getByTestId("mock-graph-canvas");
    fireEvent.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    expect(canvas).toHaveAttribute("data-selected", "node:ask");
    expect(canvas).toHaveAttribute("data-focused", "");

    fireEvent.click(screen.getByRole("button", { name: "Hover relationship from canvas" }));
    expect(canvas).toHaveAttribute("data-selected", "node:ask");
    expect(canvas).toHaveAttribute("data-focused", "");
    fireEvent.click(screen.getByRole("button", { name: "Hover node from canvas" }));
    expect(canvas).toHaveAttribute("data-focused", "");

    const relatedSelect = rowByLabel(screen.getByTestId("graph-list-view"), "Architecture Notes");
    fireEvent.focus(relatedSelect);
    expect(canvas).toHaveAttribute("data-focused", "node:related");
    fireEvent.click(screen.getByRole("button", { name: "Hover relationship from canvas" }));
    expect(canvas).toHaveAttribute("data-focused", "node:related");
  });

  it("starts changed node, edge, and blank Details at the top without scrolling the graph or stealing pointer focus", async () => {
    const user = userEvent.setup();
    renderGraph();
    const graph = screen.getByTestId("graph-view");
    const details = screen.getByRole("heading", { name: "Details", hidden: true }).closest("aside")!;
    const contents = screen.getByTestId("graph-list-view");
    const selectAsk = rowByLabel(contents, "Why was this chosen?");
    const selectRelated = rowByLabel(contents, "Architecture Notes");
    graph.scrollTop = 35;
    details.scrollTop = 160;
    for (const mock of Object.values(canvasHandle)) mock.mockClear();

    await user.click(selectAsk);
    expect.soft(details.scrollTop).toBe(0);
    expect(selectAsk).toHaveFocus();
    details.scrollTop = 240;
    await user.click(selectRelated);
    expect.soft(details.scrollTop).toBe(0);
    expect(selectRelated).toHaveFocus();
    expect(within(details).getByRole("link", { name: "Open" })).toBeInTheDocument();
    expect(within(details).getByRole("button", { name: "Center" })).toBeInTheDocument();
    expect(within(details).getByRole("button", { name: "Pin" })).toBeInTheDocument();

    // A seed relation opens from the relation chip on the path card.
    details.scrollTop = 320;
    await user.click(seedPathRelation(details, /^Architecture Notes to Decision Log/));
    expect.soft(details.scrollTop).toBe(0);
    expect(details).not.toHaveFocus();
    details.scrollTop = 400;
    await user.click(within(details).getByRole("button", { name: "Clear selection" }));
    expect.soft(details.scrollTop).toBe(0);
    expect(details).toHaveTextContent("Select a node or relationship to inspect it.");
    expect(details).toBeInTheDocument();
    expect(details).toHaveAttribute("hidden");
    expect(graph.scrollTop).toBe(35);
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-pinned", "");
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
  });

  it.each(["en", "ko"])("puts the title first and groups primary and secondary actions without losing accessible names or reasons in %s", async (locale) => {
    const user = userEvent.setup();
    await act(async () => { await i18n.changeLanguage(locale); });
    try {
      const payload = graphFixture();
      const fullTitle = `${"Long selected document ".repeat(20)}full title ending`;
      payload.nodes = payload.nodes.map((node) => node.id === "node:seed"
        ? { ...node, label: fullTitle } : node);
      const workspaceId = "00000000-0000-4000-8000-000000000001";
      renderGraph({ payload, workspaceId, workspaceGraphHref: (node) => `/graph?focus=${node.entity_id}` });
      await user.click(screen.getByTestId("graph-view")
        .querySelector<HTMLButtonElement>('button[data-local-graph-id="node:seed"]')!);

      const details = screen.getByRole("complementary", { name: i18n.t("graph:graph.details.heading") });
      const controls = details.querySelector(".graph-detail-actions");
      const title = details.querySelector("h5")!;
      expect(within(details).getByRole("link", { name: i18n.t("graph:graph.open") }))
        .toHaveAttribute("href", `/workspaces/${workspaceId}/context/pages/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`);
      expect(controls).not.toBeNull();
      expect(controls!.querySelectorAll("a,button")).toHaveLength(5);
      expect(title.compareDocumentPosition(controls!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(title).toHaveTextContent(fullTitle);
      const clearLabel = i18n.t("graph:graph.details.clearSelection");
      const clear = within(details).getByRole("button", { name: clearLabel });
      expect(clear).toHaveAttribute("aria-label", clearLabel);
      expect(clear.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      expect(clear).toHaveAccessibleName(clearLabel);

      const expand = within(details).getByRole("button", { name: i18n.t("graph:graph.expand") });
      expect(expand).toBeDisabled();
      const reason = document.getElementById(expand.getAttribute("aria-describedby")!)!;
      expect(reason).toHaveTextContent(i18n.t("graph:graph.primarySeed"));
      expect(controls).not.toContainElement(reason);
      expect(title.compareDocumentPosition(reason) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      act(() => details.focus());
      await user.tab();
      expect(clear).toHaveFocus();
    } finally {
      await act(async () => { await i18n.changeLanguage("en"); });
    }
  });

  it("retains Details scroll for a same-selection rerender with new node objects", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    const { rerender } = renderGraph({ payload });
    await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    details.scrollTop = 260;
    const updated = { ...payload, nodes: payload.nodes.map((node) => ({
      ...node, label: node.id === "node:ask" ? "Updated Ask title" : node.label,
    })) };
    rerender(graphElement({
      onCollapse: () => Promise.resolve(), onExpand: () => Promise.resolve(), payload: updated,
    }));
    expect(within(details).getByRole("heading", { level: 5, name: "Updated Ask title" })).toBeInTheDocument();
    expect(details.scrollTop).toBe(260);
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:ask");
  });

  it("keeps list rows selection-only and offers exact scene-only members in selected folder details", async () => {
    const user = userEvent.setup();
    renderGraph({ payload: graphNavigationFixture() });
    const list = screen.getByTestId("graph-list-view");
    const folderRow = rowByLabel(list, "Primary Folder").closest("li")!;
    expect(folderRow.querySelector("details")).toBeNull();
    // One control per row; open links stay out of the Tab order (the row selects, Details opens).
    expect(within(folderRow).getAllByRole("button")).toHaveLength(1);
    expect(screen.getByTestId("graph-list-view").querySelectorAll("a:not([tabindex='-1'])")).toHaveLength(0);

    await user.click(rowByLabel(list, "Primary Folder"));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const members = within(details).getByRole("list", { name: "Outgoing" });
    expect(within(members).getAllByRole("button")).toHaveLength(2);
    expect(within(members).getByRole("button", { name: /Decision Log/ })).toBeVisible();
    expect(within(members).getByRole("button", { name: /Nested Folder/ })).toBeVisible();
    expect(within(members).queryByText("Nested Page")).not.toBeInTheDocument();
    expect(details).not.toHaveTextContent(/Primary placement|All members|omitted/);

    await user.click(within(members).getByRole("button", { name: /Decision Log/ }));
    const folders = within(details).getByRole("list", { name: "Incoming" });
    expect(folders).toHaveTextContent("Primary Folder");
    expect(folders).toHaveTextContent("Secondary Folder");
  });

  it("searches the list, opens matching rings, and shows how a two-step item is reached from the seed", async () => {
    const user = userEvent.setup();
    renderGraph({ payload: graphNavigationFixture() });
    const list = screen.getByTestId("graph-list-view");
    const canvas = screen.getByTestId("mock-graph-canvas");
    const related = rowByLabel(list, "Architecture Notes").closest("li")!;
    const open = within(related).getByRole("link", { name: "Open document: Architecture Notes" });
    expect(open).toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/context/pages/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
    expect(open).toHaveAttribute("tabindex", "-1");
    expect(within(list).getByRole("button", { name: /^Two or more steps away/ })).toHaveAttribute("aria-expanded", "false");

    const search = within(list).getByRole("searchbox", { name: "Find in the graph list" });
    await user.type(search, "Distance");
    expect(within(list).getByRole("status")).toHaveTextContent("“Distance” matches 1 node · 1 relationship");
    expect(within(list).getByRole("button", { name: /^Two or more steps away/ })).toHaveAttribute("aria-expanded", "true");
    const twoStep = rowByLabel(list, "Distance Two");
    expect(twoStep).toHaveTextContent("via Architecture Notes");
    await user.click(twoStep);
    expect(canvas).toHaveAttribute("data-selected", "node:hop2");
    const path = screen.getByRole("complementary", { name: "Details" }).querySelector<HTMLElement>(".graph-seed-path")!;
    expect(path).toHaveTextContent("2 steps · 1 in between");
    expect([...path.querySelectorAll("li strong")].map((name) => name.textContent))
      .toEqual(["Decision Log", "Architecture Notes", "Distance Two"]);

    // Escape clears the list search first; the selection stays.
    act(() => search.focus());
    await user.keyboard("{Escape}");
    expect(search).toHaveValue("");
    expect(canvas).toHaveAttribute("data-selected", "node:hop2");

    // A node kind name finds the seed's relations by their far end: the
    // matched node is listed, not only counted.
    await user.type(search, "Ask turn");
    expect(within(list).getByRole("status")).toHaveTextContent("“Ask turn” matches 1 node · 1 relationship");
    expect(rowByLabel(list, "Why was this chosen?")).toBeVisible();

    // Every seed row has the seed at its near end, so the seed's own name
    // matches none of them; the seed itself is listed, as it is counted.
    await user.clear(search);
    await user.type(search, "Decision Log");
    expect(within(list).getByRole("status")).toHaveTextContent("“Decision Log” matches 1 node · 0 relationships");
    expect(list.querySelector('[data-local-graph-id="node:seed"]')).toBeVisible();
  });

  it("uses one relation Tab entry and arrow emphasis without selecting, pinning, or moving the view", async () => {
    const user = userEvent.setup();
    const onExpand = vi.fn(() => Promise.resolve());
    const onCollapse = vi.fn(() => Promise.resolve());
    const onDepthChange = vi.fn(() => Promise.resolve());
    renderGraph({ payload: graphNavigationFixture(), onExpand, onCollapse, onDepthChange });
    await pinAskFromDetails(user);
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Decision Log"));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const incoming = within(details).getByRole("list", { name: "Incoming" });
    const buttons = within(incoming).getAllByRole("button");
    expect(buttons).toHaveLength(4);
    expect(details).not.toHaveTextContent(/Distance Two|Unknown Neighbor/);
    const canvas = screen.getByTestId("mock-graph-canvas");
    const sceneCount = canvas.getAttribute("data-edges");
    for (const mock of Object.values(canvasHandle)) mock.mockClear();
    act(() => buttons[0].focus());
    expect(canvas).toHaveAttribute("data-focused", "node:related");
    await user.keyboard("{ArrowDown}");
    expect(buttons[1]).toHaveFocus();
    expect(canvas).toHaveAttribute("data-focused", "node:primary-folder");
    await user.keyboard("{ArrowUp}");
    expect(buttons[0]).toHaveFocus();
    expect(canvas).toHaveAttribute("data-selected", "node:seed");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
    expect(canvas).toHaveAttribute("data-edges", sceneCount!);
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
    expect(onExpand).not.toHaveBeenCalled();
    expect(onCollapse).not.toHaveBeenCalled();
    expect(onDepthChange).not.toHaveBeenCalled();

    await user.tab();
    expect(buttons.every((button) => button !== document.activeElement)).toBe(true);
    await user.tab({ shift: true });
    expect(buttons[0]).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(canvas).toHaveAttribute("data-selected", "node:related");
    expect(details).toHaveFocus();
    const seed = within(within(details).getByRole("list", { name: "Outgoing" })).getByRole("button", { name: /Decision Log/ });
    // No manual focus after selection: the actual keyboard sequence must
    // reach the newly selected object's controls and its relation list.
    // The action row (open, center, pin, map, expand) comes before the relations.
    for (let step = 0; step < 16 && document.activeElement !== seed; step++) await user.tab();
    expect(seed).toHaveFocus();
    await user.tab({ shift: true });
    expect(seed).not.toHaveFocus();
    await user.tab();
    expect(seed).toHaveFocus();
    await user.keyboard(" ");
    expect(canvas).toHaveAttribute("data-selected", "node:seed");
    expect(details).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(canvas).toHaveAttribute("data-selected", "");
    expect(canvas).toHaveAttribute("data-focused", "");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
  });

  it.each(["{Enter}", " "])("keeps DOM focus inside Graph and handles immediate Escape after neighbor selection with %j", async (key) => {
    const user = userEvent.setup();
    renderGraph({ payload: graphNavigationFixture() });
    await pinAskFromDetails(user);
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Decision Log"));
    const graph = screen.getByTestId("graph-view");
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const target = within(within(details).getByRole("list", { name: "Outgoing" })).getByRole("button", { name: /Why was this chosen/ });
    act(() => target.focus());
    await user.keyboard(key);
    const canvas = screen.getByTestId("mock-graph-canvas");
    expect(canvas).toHaveAttribute("data-selected", "node:ask");
    expect(target).not.toBeInTheDocument();
    expect.soft(document.activeElement).not.toBe(document.body);
    expect.soft(graph.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    expect(canvas).toHaveAttribute("data-selected", "");
    expect(canvas).toHaveAttribute("data-focused", "");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
  });

  it("preserves pointer focus but hands a connected keyboard row and same-ID activation to Details", async () => {
    const user = userEvent.setup();
    renderGraph({ payload: graphNavigationFixture() });
    const contents = screen.getByTestId("graph-list-view");
    await user.click(rowByLabel(contents, "Decision Log"));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const focusDetails = vi.spyOn(details, "focus");
    await user.click(within(within(details).getByRole("list", { name: "Outgoing" })).getByRole("button", { name: /Why was this chosen/ }));
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:ask");
    expect(focusDetails).not.toHaveBeenCalled();

    const member = rowByLabel(contents, "Decision Log");
    act(() => member.focus());
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:seed");
    expect(member).toBeInTheDocument();
    expect(details).toHaveFocus();
    expect(focusDetails).toHaveBeenCalledTimes(1);
    expect(focusDetails).toHaveBeenLastCalledWith();
    act(() => member.focus());
    await user.keyboard("{Enter}");
    expect(details).toHaveFocus();
    expect(focusDetails).toHaveBeenCalledTimes(2);
    expect(focusDetails).toHaveBeenLastCalledWith();
  });

  it("recovers only Graph root after a removed pointer member so immediate Escape works without changing pins or issuing actions", async () => {
    const user = userEvent.setup();
    const onExpand = vi.fn(() => Promise.resolve());
    const onCollapse = vi.fn(() => Promise.resolve());
    const onDepthChange = vi.fn(() => Promise.resolve());
    renderGraph({ payload: graphNavigationFixture(), onExpand, onCollapse, onDepthChange });
    await pinAskFromDetails(user);
    const graph = screen.getByTestId("graph-view");
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Primary Folder"));
    const details = screen.getByRole("complementary", { name: "Details" });
    const focusDetails = vi.spyOn(details, "focus");
    const focusGraph = vi.spyOn(graph, "focus");
    const member = within(within(details).getByRole("list", { name: "Outgoing" }))
      .getByRole("button", { name: /Decision Log/ });
    const canvas = screen.getByTestId("mock-graph-canvas");
    const nodeCount = canvas.getAttribute("data-nodes");
    const edgeCount = canvas.getAttribute("data-edges");
    for (const mock of Object.values(canvasHandle)) mock.mockClear();
    await user.click(member);
    expect(member).not.toBeInTheDocument();
    expect(graph).toHaveFocus();
    expect(focusGraph).toHaveBeenCalledExactlyOnceWith({ preventScroll: true });
    expect(focusDetails).not.toHaveBeenCalled();
    expect(canvas).toHaveAttribute("data-selected", "node:seed");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
    await user.keyboard("{Escape}");
    expect(canvas).toHaveAttribute("data-selected", "");
    expect(details).toHaveAttribute("hidden");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
    expect(canvas).toHaveAttribute("data-nodes", nodeCount!);
    expect(canvas).toHaveAttribute("data-edges", edgeCount!);
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
    for (const action of [onExpand, onCollapse, onDepthChange]) expect(action).not.toHaveBeenCalled();
  });

  it.each([
    ["inactive", "keyboard"], ["new-scene", "keyboard"], ["workspace", "keyboard"],
    ["inactive", "pointer"], ["new-scene", "pointer"], ["workspace", "pointer"],
  ] as const)("does not restore a stale selection after %s scope change for %s origin", async (change, modality) => {
    const user = userEvent.setup();
    const payload = graphNavigationFixture();
    const { rerender } = renderGraph({ payload, workspaceId: "workspace:one" });
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Decision Log"));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const focusDetails = vi.spyOn(details, "focus");
    const focusGraph = vi.spyOn(screen.getByTestId("graph-view"), "focus");
    const target = within(within(details).getByRole("list", { name: "Outgoing" }))
      .getByRole("button", { name: /Why was this chosen/ });
    act(() => target.focus());
    act(() => {
      fireEvent.click(target, { detail: modality === "keyboard" ? 0 : 1 });
      rerender(graphElement({
        active: change !== "inactive",
        onCollapse: () => Promise.resolve(), onExpand: () => Promise.resolve(),
        payload: change === "new-scene" ? graphFixture() : payload,
        workspaceId: change === "workspace" ? "workspace:two" : "workspace:one",
      }));
    });
    expect(target).not.toBeInTheDocument();
    expect(focusDetails).not.toHaveBeenCalled();
    expect(focusGraph).not.toHaveBeenCalled();
  });

  it("updates a folder's relations when filters or the timeline remove them", async () => {
    const user = userEvent.setup();
    const payload = graphNavigationFixture();
    renderGraph({ payload });
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Primary Folder"));
    await user.click(screen.getByLabelText("Contains", { selector: "input" }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    expect(details).toHaveTextContent("No relationships are shown in this scene.");
    expect(within(details).queryByRole("list", { name: "Outgoing" })).not.toBeInTheDocument();
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:primary-folder");
    await user.click(screen.getByLabelText("Contains", { selector: "input" }));
    expect(within(within(details).getByRole("list", { name: "Outgoing" })).getAllByRole("button")).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Timeline position"), { target: { value: "0" } });
    expect(details).toHaveTextContent("No relationships are shown in this scene.");
  });

  it("clears transient neighbor focus when its relationship disappears but its node remains visible", async () => {
    const user = userEvent.setup();
    renderGraph({ payload: graphNavigationFixture() });
    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Decision Log"));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    act(() => within(within(details).getByRole("list", { name: "Incoming" })).getByRole("button", { name: /Architecture Notes/ }).focus());
    const canvas = screen.getByTestId("mock-graph-canvas");
    expect(canvas).toHaveAttribute("data-focused", "node:related");
    // Preserve DOM focus while changing the scene, like an incoming update.
    fireEvent.click(screen.getByLabelText("Wiki link", { selector: "input" }));
    expect(canvas).toHaveAttribute("data-nodes", "9");
    expect(canvas).toHaveAttribute("data-focused", "");
    expect(canvas).toHaveAttribute("data-selected", "node:seed");
    expect(within(details).getByRole("list", { name: "Incoming" }))
      .not.toHaveTextContent("Architecture Notes");
  });

  it("localizes the list view and a folder's relations in Korean", async () => {
    const user = userEvent.setup();
    await act(async () => { await i18n.changeLanguage("ko"); });
    try {
      renderGraph({ payload: graphNavigationFixture() });
      const list = screen.getByTestId("graph-list-view");
      expect(within(list).getByRole("heading", { name: "목록" })).toBeInTheDocument();
      expect(within(list).getByRole("list", { name: "들어오는 관계" })).toBeInTheDocument();
      expect(list).toHaveTextContent("다른 노드 → 기준 문서");
      await user.click(rowByLabel(list, "Primary Folder"));
      const details = screen.getByRole("heading", { name: "상세 정보" }).closest("aside")!;
      expect(within(within(details).getByRole("list", { name: "나가는 관계" })).getAllByRole("button")).toHaveLength(2);
      expect(details).toHaveTextContent("선택한 노드 → 다른 노드");
      expect(details).toHaveTextContent("기준 문서와의 관계");
      expect(details).not.toHaveTextContent(/Incoming|Outgoing|Relationship to the seed/);
    } finally {
      await act(async () => { await i18n.changeLanguage("en"); });
    }
  });

  it("mounts Mermaid only while the static view is requested, never for hidden print content", async () => {
    const user = userEvent.setup();
    renderGraph();
    expect(screen.queryByTestId("mock-mermaid-diagram")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("Wiki link", { selector: "input" }));
    expect(screen.queryByTestId("mock-mermaid-diagram")).not.toBeInTheDocument();
    await user.click(screen.getByText("Static graph view"));
    expect(screen.getAllByTestId("mock-mermaid-diagram")).toHaveLength(1);
    expect(screen.getByTestId("mock-mermaid-diagram")).not.toHaveTextContent("Wiki link");
    await user.click(screen.getByText("Static graph view"));
    await waitFor(() => expect(screen.queryByTestId("mock-mermaid-diagram"))
      .not.toBeInTheDocument());
  });

  it("keeps the current direct static scene across canvas scopes, history modes, and playback", async () => {
    const user = userEvent.setup();
    const direct = graphFixture();
    direct.history.mode = "current";
    direct.query.time = { mode: "current" };
    direct.query.relation_scope = "direct";
    const props = {
      onCollapse: () => Promise.resolve(), onExpand: () => Promise.resolve(),
      staticPayload: direct, payload: direct,
    };
    const { rerender } = render(graphElement(props));
    await user.click(screen.getByText("Static graph view"));
    const directSource = screen.getByTestId("mock-mermaid-diagram").textContent;
    const neighborEdge = { ...direct.edges[0], id: "edge:neighbors", source: "node:related", target: "node:ask", label: "Neighbor-only relationship" };
    const neighbors = { ...direct, query: { ...direct.query, relation_scope: "neighbors" as const }, edges: [direct.edges[0], neighborEdge] };
    rerender(graphElement({ ...props, payload: neighbors }));
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-rendered-edge-ids", expect.stringContaining("edge:neighbors"));
    // The broader query omitted a direct edge under its own budget; the
    // static view must recover it from its dedicated payload, not trim canvas.
    expect(screen.getByTestId("mock-mermaid-diagram")).toHaveTextContent("Included");
    expect(screen.getByTestId("mock-mermaid-diagram").textContent).toBe(directSource);

    const distantNode = { ...direct.nodes[1], id: "node:distant", label: "Second-hop document" };
    const extended = { ...neighbors, query: { ...neighbors.query, depth: 2 as const },
      nodes: [...neighbors.nodes, distantNode],
      edges: [...neighbors.edges, { ...neighborEdge, id: "edge:distant", target: distantNode.id }],
    };
    rerender(graphElement({ ...props, payload: extended }));
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-nodes", "4");
    expect(screen.getByTestId("mock-mermaid-diagram").textContent).toBe(directSource);
    fireEvent.click(screen.getByLabelText("Ask turn"));
    expect(screen.getByTestId("mock-mermaid-diagram")).not.toHaveTextContent("Why was this chosen?");
    expect(screen.getByTestId("mock-mermaid-diagram")).toHaveTextContent("Architecture Notes");

    fireEvent.click(screen.getByLabelText("Ask turn"));
    rerender(graphElement(props));
    fireEvent.click(screen.getByLabelText("Usage"));
    expect(screen.getByTestId("mock-mermaid-diagram")).not.toHaveTextContent("Included");
    rerender(graphElement({ ...props, payload: neighbors }));
    expect(screen.getByTestId("mock-mermaid-diagram")).not.toHaveTextContent("Included");
    expect(screen.getByLabelText("Usage")).not.toBeChecked();
    fireEvent.click(screen.getByLabelText("Usage"));
    expect(screen.getByTestId("mock-mermaid-diagram")).toHaveTextContent("Included");

    const appearance = graphFixture();
    rerender(graphElement({ ...props, payload: appearance }));
    expect(screen.getByTestId("mock-mermaid-diagram").textContent).toBe(directSource);
    fireEvent.change(screen.getByLabelText("Timeline position"), { target: { value: "0" } });
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-edges", "0");
    expect(screen.getByTestId("mock-mermaid-diagram").textContent).toBe(directSource);

    const action = { ...appearance, history: { ...appearance.history, mode: "action" as const },
      query: { ...appearance.query, time: { mode: "action" as const } },
    };
    rerender(graphElement({ ...props, payload: action }));
    expect(screen.getByTestId("mock-mermaid-diagram").textContent).toBe(directSource);
    fireEvent.click(screen.getByLabelText("Current relationships", { selector: "input" }));
    expect(screen.getByTestId("mock-mermaid-diagram").textContent).toBe(directSource);
    fireEvent.change(screen.getByLabelText("Timeline position"), { target: { value: "0" } });
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-edges", "0");
    expect(screen.getByTestId("mock-mermaid-diagram").textContent).toBe(directSource);
  });

  it("keeps static coverage independent of history and canvas notice dismissal in both static views", async () => {
    const current = graphFixture();
    const budget = { nodes: 250, edges: 750 };
    current.history = { ...current.history, mode: "current", status: "complete", warnings: [] };
    current.query = { ...current.query, depth: 1, relation_scope: "direct", time: { mode: "current" }, budget };
    current.budget = { requested: budget, applied: budget };
    current.truncated = true;
    current.completeness = "capped";
    current.warnings = ["graph_budget_applied"];
    current.omissions = [{ scope: "node", reason: "node_quota", hop: 1 }];
    const history = graphFixture();
    const props = { onCollapse: () => Promise.resolve(), onExpand: () => Promise.resolve(),
      payload: history, staticPayload: current };
    const { container, rerender } = render(graphElement(props));
    await userEvent.click(screen.getByText("Static graph view", { selector: "summary" }));
    const fallback = within(container.querySelector<HTMLElement>(".graph-static-fallback")!);
    expect(fallback.getByTestId("mock-mermaid-diagram")).toBeInTheDocument();
    expect(fallback.getByRole("alert")).toHaveTextContent("Some current direct relationships are not shown.");
    expect(fallback.getByRole("alert")).toHaveTextContent("Node display limit · Distance 1");
    expect(fallback.getByRole("alert")).toHaveTextContent("Applied display limit: 250 nodes · 750 relationships.");

    rerender(graphElement({ ...props, payload: current }));
    await userEvent.click(screen.getByRole("button", { name: "Dismiss coverage notice" }));
    expect(fallback.getByRole("alert")).toBeVisible();
    rerender(graphElement(props));
    expect(fallback.getByRole("alert")).toBeVisible();

    // The list fallback has a separate render branch above Mermaid's edge limit.
    const textPayload = { ...current, edges: Array.from({ length: 501 }, (_, index) => ({
      ...current.edges[0], id: `edge:static:${index}`,
    })) };
    rerender(graphElement({ ...props, staticPayload: textPayload }));
    expect(fallback.queryByTestId("mock-mermaid-diagram")).not.toBeInTheDocument();
    expect(fallback.getByRole("list", { name: "Relationships" }).children).toHaveLength(501);
    expect(fallback.getByRole("alert")).toBeVisible();

    rerender(graphElement({ ...props, staticPayload: { ...current, truncated: false,
      completeness: "complete", omissions: [], warnings: ["history_coverage_partial"],
      history: { ...current.history, status: "partial", warnings: ["history_coverage_partial"] },
    } }));
    expect(fallback.queryByRole("alert")).not.toBeInTheDocument();
    expect(fallback.queryByText("Timeline coverage is limited.")).not.toBeInTheDocument();
  });

  it.each([501, 750])("uses a complete text fallback for %i relationships instead of invalid Mermaid", (count) => {
    const payload = graphFixture();
    payload.edges = Array.from({ length: count }, (_, index) => ({
      ...payload.edges[0], id: `edge:link:${index}`,
    }));
    const { container } = renderGraph({ payload });

    // This models an adapter callback, not a user gesture. Avoid computing
    // accessible names for every relationship button just to dispatch it.
    fireEvent.click(within(screen.getByTestId("mock-graph-canvas"))
      .getByText("Simulate renderer error"));
    const fallback = container.querySelector<HTMLElement>(".graph-static-fallback")!;
    expect(fallback).toHaveAttribute("open");
    expect(container.querySelector("[data-testid='mock-mermaid-diagram']")).toBeNull();
    expect(within(fallback).getByText(/exceeds the diagram limit of 500 relationships/))
      .toBeInTheDocument();
    const [nodeList, edgeList] = fallback.querySelectorAll(".graph-text-contents > ul");
    expect(nodeList).toHaveAttribute("aria-label", "Nodes");
    expect(edgeList).toHaveAttribute("aria-label", "Relationships");
    expect(nodeList.children).toHaveLength(payload.nodes.length);
    expect(edgeList.children).toHaveLength(count);
    expect([...edgeList.children].every((item) => item.tagName === "LI")).toBe(true);
    expect(edgeList.firstElementChild).toHaveTextContent("Architecture Notes —Wiki link→ Decision Log");
    expect(edgeList.lastElementChild).toHaveTextContent("Architecture Notes —Wiki link→ Decision Log");
  });

  it("keeps the static diagram available at its 500-relationship parsing boundary", () => {
    const payload = graphFixture();
    payload.edges = Array.from({ length: 500 }, (_, index) => ({
      ...payload.edges[0], id: `edge:link:${index}`,
    }));
    renderGraph({ payload });

    fireEvent.click(within(screen.getByTestId("mock-graph-canvas"))
      .getByText("Simulate renderer error"));
    const diagram = screen.getByTestId("mock-mermaid-diagram");
    expect(diagram).toHaveTextContent("Decision Log");
    expect(diagram.textContent?.match(/-->/g)).toHaveLength(500);
    expect(screen.queryByText(/exceeds the diagram limit/)).not.toBeInTheDocument();
  });

  it("renders text only for printing and removes it after printing", () => {
    renderGraph();
    const printable = screen.getByTestId("graph-print-fallback");
    expect(within(printable).queryByRole("list", { hidden: true })).not.toBeInTheDocument();
    act(() => window.dispatchEvent(new Event("beforeprint")));
    expect(within(printable).getByText("Decision Log")).toBeInTheDocument();
    expect(within(printable).getByRole("list", { name: "Relationships", hidden: true }))
      .toBeInTheDocument();
    expect(screen.queryByTestId("mock-mermaid-diagram")).not.toBeInTheDocument();
    act(() => window.dispatchEvent(new Event("afterprint")));
    expect(within(printable).queryByText("Decision Log")).not.toBeInTheDocument();
  });

  it("does not prepare a print scene for an inactive retained Graph tab", () => {
    const { rerender } = renderGraph({ active: false });
    act(() => window.dispatchEvent(new Event("beforeprint")));
    const printable = screen.getByTestId("graph-print-fallback");
    expect(within(printable).queryByText("Decision Log")).not.toBeInTheDocument();

    rerender(graphElement({
      active: true,
      onCollapse: () => Promise.resolve(),
      onExpand: () => Promise.resolve(),
      payload: graphFixture(),
    }));
    act(() => window.dispatchEvent(new Event("beforeprint")));
    expect(within(printable).getByText("Decision Log")).toBeInTheDocument();
    act(() => window.dispatchEvent(new Event("afterprint")));
  });

  it("exports only the same filtered scene shown by canvas and DOM parity", async () => {
    const user = userEvent.setup();
    const createObjectURL = vi.fn((_blob: Blob) => "blob:filtered-graph");
    const revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    renderGraph();

    await user.click(screen.getByLabelText("Ask turn"));
    await user.click(screen.getByRole("button", { name: "Export filtered graph" }));

    const blob = createObjectURL.mock.calls[0]?.[0] as Blob;
    const exported = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error);
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(blob);
    });
    expect(exported).toContain("Decision Log");
    expect(exported).toContain("Architecture Notes");
    expect(exported).not.toContain("Why was this chosen?");
    expect(exported).not.toContain("included as evidence");
    act(() => window.dispatchEvent(new Event("beforeprint")));
    const printable = screen.getByTestId("graph-print-fallback").textContent ?? "";
    expect(printable).toContain("Decision Log");
    expect(printable).not.toContain("Why was this chosen?");
    act(() => window.dispatchEvent(new Event("afterprint")));
    expect(click).toHaveBeenCalledOnce();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:filtered-graph");
  });

  it("supports discrete timeline reset/step/scrub controls and history mode queries", async () => {
    const user = userEvent.setup();
    const onHistoryModeChange = vi.fn((_mode: GraphHistoryMode) => Promise.resolve());
    renderGraph({ onHistoryModeChange });

    fireEvent.change(screen.getByLabelText("Timeline position"), { target: { value: "0" } });
    expect(screen.getByText("1 nodes · 0 edges")).toBeInTheDocument();
    expect(screen.getByText("0 / 3")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next event" }));
    expect(screen.getByTestId("graph-timeline")).toHaveTextContent("Architecture Notes");
    expect(screen.getByTestId("graph-timeline")).toHaveTextContent("Activity · Created");
    expect(screen.getByText("2 nodes · 0 edges")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Timeline position"), {
      target: { value: "3" },
    });
    expect(screen.getByText("3 nodes · 2 edges")).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: /Activity history/ }));
    expect(onHistoryModeChange).toHaveBeenCalledWith("action");

    const staticSummary = screen.getByText("Static graph view");
    expect(staticSummary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByTestId("graph-print-fallback"))
      .toHaveClass("graph-print-fallback");
    expect(within(screen.getByTestId("graph-print-fallback"))
      .queryByText(/flowchart LR/)).not.toBeInTheDocument();
    await user.click(staticSummary);
    expect(within(staticSummary.closest("details")!).getByText(/flowchart LR/))
      .toBeInTheDocument();
  });

  it("shows a current-scene explanation instead of an unusable zero-event timeline", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    payload.history.mode = "current";
    payload.history.basis = "current_projection";
    payload.timeline = [];
    const onHistoryModeChange = vi.fn((_mode: GraphHistoryMode) => Promise.resolve());
    renderGraph({ payload, onHistoryModeChange });

    expect(screen.getByText(/There is no time navigation/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Timeline/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("slider", { name: "Timeline position" })).not.toBeInTheDocument();
    expect(screen.queryByText("0 / 0")).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /Change history/ }));
    expect(onHistoryModeChange).toHaveBeenCalledWith("appearance");
  });

  it("clears a recovered renderer failure without closing the user-opened static fallback", async () => {
    const user = userEvent.setup();
    renderGraph();

    await user.click(screen.getByRole("button", { name: "Simulate renderer error" }));
    const failedSummary = screen.getByText("Static graph fallback");
    expect(failedSummary.closest("details")).toHaveAttribute("open");

    await user.click(screen.getByRole("button", { name: "Simulate renderer ready" }));
    const recoveredSummary = screen.getByText("Static graph view");
    expect(recoveredSummary.closest("details")).toHaveAttribute("open");
  });

  it("retains the recorded event identity and selection across refresh, earlier records, and view round-trips", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    const onRefresh = vi.fn(async () => undefined);
    const noAction = vi.fn(async () => undefined);
    const props = { payload, onRefresh, onCollapse: noAction, onExpand: noAction };
    const view = renderGraph(props);
    fireEvent.change(screen.getByLabelText("Timeline position"), { target: { value: "2" } });
    const canvas = screen.getByTestId("mock-graph-canvas");
    const selected = screen.getByTestId("graph-view").querySelector<HTMLButtonElement>('[data-local-graph-id="node:related"]')!;
    await user.click(selected);
    expect(canvas).toHaveAttribute("data-selected", "node:related");
    const records = screen.getByRole("complementary", { name: "Details" }).querySelector<HTMLElement>(".graph-item-history")!;
    expect(records.querySelectorAll("li")).toHaveLength(2);
    await user.click(within(records).getAllByRole("button")[0]);
    expect(screen.getByRole("slider", { name: "Timeline position" })).toHaveValue("1");
    expect(canvas).toHaveAttribute("data-selected", "node:related");
    fireEvent.change(screen.getByLabelText("Timeline position"), { target: { value: "2" } });
    Object.values(canvasHandle).forEach((mock) => mock.mockClear());
    await user.click(screen.getByRole("button", { name: "Refresh graph" }));
    expect(onRefresh).toHaveBeenCalledOnce();
    const refreshed = { ...payload, generated_at: "2026-08-05T00:00:00Z", timeline: [
      { ...payload.timeline![0], id: "earlier-seed", node_ids: ["node:seed"], occurred_at: "2026-08-01T00:00:00Z" },
      ...payload.timeline!,
    ] };
    view.rerender(graphElement({ ...props, payload: refreshed }));
    expect(screen.getByRole("slider", { name: "Timeline position" })).toHaveValue("3");
    expect(canvas).toHaveAttribute("data-selected", "node:related");
    expect(canvas).toHaveAttribute("data-edges", "1");
    expect(canvasHandle.reset).not.toHaveBeenCalled();
    expect(canvasHandle.fit).not.toHaveBeenCalled();
    view.rerender(graphElement({ ...props, payload: { ...refreshed, history: { ...payload.history, mode: "current" } } }));
    expect(screen.queryByRole("slider", { name: "Timeline position" })).not.toBeInTheDocument();
    view.rerender(graphElement({ ...props, payload: refreshed }));
    expect(screen.getByRole("slider", { name: "Timeline position" })).toHaveValue("3");
    view.rerender(graphElement({ ...props, payload: { ...refreshed, timeline: refreshed.timeline.filter((event) => event.id !== payload.timeline![1].id) } }));
    expect(screen.getByRole("slider", { name: "Timeline position" })).toHaveValue("3");
    expect(screen.getByText("The record you were viewing is no longer in the results. Moved to the latest record."))
      .toHaveAttribute("role", "status");
  });

  it("previews creation and removal from their own filtered event frames without moving the replay cursor", () => {
    const payload = graphFixture();
    const current = { ...payload.edges[0], label: "Re-added relationship", count: 17 };
    const original = { ...current, label: "Original relationship", count: 9, provenance: "observed" as const };
    payload.edges = [current];
    const event = { ...payload.timeline![1], node_ids: [current.source, current.target] };
    payload.timeline = [
      { ...event, id: "original", occurred_at: "2026-08-02T00:00:00Z", sequence: 1, edge_snapshots: [original] },
      { ...event, id: "removed", operation: "edge_removed", action_kind: "removed", occurred_at: "2026-08-03T00:00:00Z", sequence: 2, edge_snapshots: [] },
      { ...event, id: "re-added", occurred_at: "2026-08-04T00:00:00Z", sequence: 3, edge_snapshots: [current] },
    ];
    renderGraph({ payload, openContents: false });
    const canvas = screen.getByTestId("mock-graph-canvas");
    const mark = screen.getByTestId("graph-timeline").querySelector<HTMLButtonElement>('.local-history-mark[aria-label^="2/3"]')!;
    expect(canvas).toHaveAttribute("data-edges", "1");
    fireEvent.mouseEnter(mark);
    expect(canvas).toHaveAttribute("data-removed-edges", "1");
    expect(screen.getByRole("slider", { name: "Timeline position" })).toHaveValue("3");

    fireEvent.click(screen.getByLabelText("Observed"));
    expect(canvas).toHaveAttribute("data-removed-edges", "0");
    expect(canvas).toHaveAttribute("data-edges", "1");
    fireEvent.click(screen.getByLabelText("Observed"));
    expect(canvas).toHaveAttribute("data-removed-edges", "1");
    fireEvent.mouseLeave(mark);
    fireEvent.change(screen.getByRole("slider", { name: "Timeline position" }), { target: { value: "2" } });
    fireEvent.click(screen.getByLabelText("Observed"));
    const creation = screen.getByTestId("graph-timeline").querySelector<HTMLButtonElement>('.local-history-mark[aria-label^="1/3"]')!;
    fireEvent.mouseEnter(creation);
    expect(canvas).toHaveAttribute("data-edges", "0");
    expect(canvas).toHaveAttribute("data-removed-edges", "0");
    expect(screen.getByRole("slider", { name: "Timeline position" })).toHaveValue("2");
    fireEvent.mouseLeave(creation);
    fireEvent.focus(creation);
    expect(canvas).toHaveAttribute("data-edges", "0");
    fireEvent.click(screen.getByLabelText("Observed"));
    expect(canvas).toHaveAttribute("data-edges", "1");
    expect(canvas).toHaveAttribute("data-edge-counts", "9");
    expect(screen.getByRole("slider", { name: "Timeline position" })).toHaveValue("2");
    fireEvent.click(screen.getByRole("button", { name: /Historical relationships/ }));
    expect(canvas).toHaveAttribute("data-edges", "0");
    expect(canvas).toHaveAttribute("data-removed-edges", "0");
  });

  it("keeps the final recorded snapshot after autoplay completion", () => {
    vi.useFakeTimers();
    try {
      const payload = graphFixture();
      const observedSnapshot = { ...payload.edges[0], provenance: "usage" as const };
      payload.timeline = [{
        id: "event:observed-wikilink",
        operation: "edge_created",
        action_kind: "referenced",
        label: "Observed Wiki link",
        occurred_at: "2026-08-02T00:01:00Z",
        sequence: 1,
        node_ids: [],
        edge_ids: [observedSnapshot.id],
        edge_snapshots: [observedSnapshot],
        evidence: null,
      }];
      renderGraph({ payload });
      fireEvent.click(screen.getByLabelText("Usage"));
      expect(screen.getByText("3 nodes · 0 edges")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Play" }));
      expect(screen.getByText("3 nodes · 0 edges")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(1_000));

      expect(screen.getByText("3 nodes · 0 edges")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a one-event reduced-motion timeline on its recorded snapshot", () => {
    const restoreMatchMedia = setReducedMotionPreference(true);
    try {
      const payload = graphFixture();
      const observedSnapshot = { ...payload.edges[0], provenance: "usage" as const };
      payload.timeline = [{
        id: "event:observed-wikilink",
        operation: "edge_created",
        action_kind: "referenced",
        label: "Observed Wiki link",
        occurred_at: "2026-08-02T00:01:00Z",
        sequence: 1,
        node_ids: [],
        edge_ids: [observedSnapshot.id],
        edge_snapshots: [observedSnapshot],
        evidence: null,
      }];
      renderGraph({ payload });
      fireEvent.click(screen.getByLabelText("Usage"));

      expect(screen.getByText("3 nodes · 0 edges")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Play" }));
      expect(screen.getByText("3 nodes · 0 edges")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Play" }));
      expect(screen.getByText("3 nodes · 0 edges")).toBeInTheDocument();
    } finally {
      restoreMatchMedia();
    }
  });

  it("describes a selected action edge as source, action, and target", async () => {
    const user = userEvent.setup();
    renderGraph();

    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Why was this chosen?"));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    await user.click(seedPathRelation(details, /^Decision Log to Why was this chosen\?/));
    expect(details.querySelector(".relation-list-ends")).toHaveTextContent(/Decision Log.*Included.*Why was this chosen\?/);
  });

  it("loads retained citation evidence only for a selected pair and discards another reader's pending page", async () => {
    // The inspector's asynchronous, actor-bound pagination has a separate
    // failure mechanism from layout geometry; keep it in one UI lifecycle.
    const user = userEvent.setup();
    const pending: Array<{ request: graphApi.GraphCitationEvidenceRequest; signal?: AbortSignal; resolve: (value: graphApi.GraphCitationEvidencePayload) => void }> = [];
    const query = vi.spyOn(graphApi, "queryGraphCitationEvidence").mockImplementation((_workspace, request, signal) => new Promise((resolve) => { pending.push({ request, signal, resolve }); }));
    const auth = vi.spyOn(authContext, "useOptionalAuth").mockReturnValue({ user: { user_id: "evidence-reader-one" } } as ReturnType<typeof authContext.useOptionalAuth>);
    try {
      const payload = graphFixture();
      payload.history.mode = "current";
      payload.query.time = { mode: "current" };
      const targetId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
      payload.nodes.push({ ...payload.nodes[1], id: "node:source", entity_kind: "library_item", entity_id: targetId, label: "Source document" });
      payload.edges.push({ ...payload.edges[0], id: "edge:legacy-citation", source: "node:seed", target: "node:source", edge_kind: "citation_source", layer: "evidence" });
      const props = { payload, onCollapse: (_node: GraphNode) => Promise.resolve(), onExpand: (_node: GraphNode) => Promise.resolve() };
      const view = renderGraph(props);
      expect(query).not.toHaveBeenCalled();
      await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Source document"));
      await user.click(seedPathRelation(screen.getByRole("complementary", { name: "Details" }),
        /^Decision Log to Source document, Source citation/i));
      expect(query).toHaveBeenCalledTimes(1);
      expect(pending[0].request).toMatchObject({ source_page_id: payload.nodes[0].entity_id, target_kind: "library_item", target_id: targetId, limit: 20, cursor: null });
      const item = { id: "evidence-one", owner_kind: "context_page_version", owner_id: "source-published-version", source_version_id: "source-published-version", citation_type: "derived_artifact", library_item_version_id: "original-library-version", library_artifact_id: "original-artifact", snippet: "First retained quotation", verification_status: "strong", created_via: "manual_input" };
      const answer = { ...pending[0].request, source_version_id: "source-published-version", items: [item], next_cursor: "page-two" } as graphApi.GraphCitationEvidencePayload;
      await act(async () => { pending[0].resolve(answer); });
      expect(screen.getByText("original-artifact")).toBeInTheDocument();
      expect(screen.getByText("original-library-version")).toBeInTheDocument();
      expect(screen.getByText("First retained quotation")).toBeInTheDocument();
      expect(screen.getByText("Strong")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Load more evidence" }));
      expect(pending[1].request.cursor).toBe("page-two");
      await act(async () => { pending[1].resolve({ ...answer, items: [{ ...item, id: "evidence-two", snippet: "Second retained quotation" }], next_cursor: "page-three" }); });
      expect(screen.getByText("First retained quotation")).toBeInTheDocument();
      expect(screen.getByText("Second retained quotation")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Load more evidence" }));
      auth.mockReturnValue({ user: { user_id: "evidence-reader-two" } } as ReturnType<typeof authContext.useOptionalAuth>);
      view.rerender(graphElement(props));
      expect(screen.queryByText("First retained quotation")).not.toBeInTheDocument();
      expect(pending[2].signal?.aborted).toBe(true);
      await act(async () => { pending[2].resolve({ ...answer, items: [{ ...item, id: "evidence-late", snippet: "Private late quotation" }] }); });
      expect(screen.queryByText("Private late quotation")).not.toBeInTheDocument();
      await act(async () => { pending[3].resolve({ ...answer, items: [], next_cursor: "continue-visible-scan" }); });
      expect(screen.queryByText("This relationship has no retained version or artifact evidence.")).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Load more evidence" }));
      await act(async () => { pending[4].resolve({ ...answer, items: [], next_cursor: null }); });
      expect(screen.getByText("This relationship has no retained version or artifact evidence.")).toBeInTheDocument();
      view.rerender(graphElement({ ...props, payload: { ...payload, history: { ...payload.history, mode: "appearance" }, query: { ...payload.query, time: { mode: "appearance" } } } }));
      expect(screen.queryByText("Retained citation evidence")).not.toBeInTheDocument();
      expect(query).toHaveBeenCalledTimes(5);
    } finally {
      query.mockRestore();
      auth.mockRestore();
    }
  });

  it("names a chat as Chats does and reads its details only while it is selected", async () => {
    // What a chat did to the document comes whole with the graph relation;
    // the chat's own detail, read only on selection, adds its flow and can
    // never shorten that tally (its artifact list is capped). A late answer
    // for a chat no longer selected never reaches the panel.
    const user = userEvent.setup();
    const pending: Array<{ id: string; signal?: AbortSignal; resolve: (value: operations.McpChatDetail) => void }> = [];
    const read = vi.spyOn(operations, "readMcpChat").mockImplementation((_workspace, id, init) =>
      new Promise((resolve) => { pending.push({ id, signal: init?.signal ?? undefined, resolve }); }));
    try {
      const payload = graphFixture();
      payload.history.mode = "action";
      payload.query.time = { mode: "action" };
      const seed = payload.nodes[0];
      const chat = (key: string, client: string, action: "edit" | "read"): GraphNode => ({
        ...payload.nodes[1], id: `node:${key}`, entity_kind: "mcp_chat_session",
        entity_id: `${key}-0000-4000-8000-000000000000`, family: "chat", status: "active",
        label: "raw fallback", actor_label: client,
        chat_presentation: { title_source: "artifact", action, subject: "Decision Log", subject_kind: "context_page", primary_tool_name: null },
      });
      const editing = chat("1111aaaa", "Claude Code", "edit");
      const reading = chat("2222bbbb", "Codex", "read");
      const version: GraphNode = { ...payload.nodes[1], id: "node:v2", entity_kind: "context_page_version", family: "document", label: "Version 2", status: "current_draft", review_status: "changes_requested" };
      const relation = (source: GraphNode, target: GraphNode, action: string, count: number | null,
        actionCounts: Array<[string, number]> = []) => ({
        ...payload.edges[0], id: `edge:${source.id}:${target.id}`, source: source.id, target: target.id,
        edge_kind: action, action_kind: action, label: action, layer: "action" as const, provenance: "domain" as const,
        aggregate: true, count, window_from: "2026-08-03T01:00:00Z", window_to: "2026-08-03T01:05:00Z",
        action_counts: actionCounts.map(([action_kind, occurrences]) => ({ action_kind, count: occurrences })),
      });
      const outlined: GraphNode = { ...payload.nodes[1], id: "node:outlined", entity_id: "outlined", label: "Outline only" };
      payload.nodes = [seed, editing, reading, version, outlined];
      payload.edges = [relation(editing, seed, "edited", 3, [["edited", 2], ["viewed", 1]]), relation(editing, version, "created", null),
        relation(reading, seed, "viewed", 1), relation(reading, outlined, "referenced", 1)];
      payload.timeline = [];
      const props = { payload, onRefresh: vi.fn(async () => undefined),
        onCollapse: (_node: GraphNode) => Promise.resolve(), onExpand: (_node: GraphNode) => Promise.resolve() };
      const view = renderGraph(props);

      // A document only a chat's structure-only look brought in starts hidden
      // with that look, rather than floating unconnected.
      const canvas = screen.getByTestId("mock-graph-canvas");
      expect(canvas).toHaveAttribute("data-nodes", "4");
      const lookChip = screen.getByRole("button", { name: /Structure-only looks/ });
      await user.click(lookChip);
      expect(canvas).toHaveAttribute("data-nodes", "5");
      await user.click(lookChip);

      expect(read).not.toHaveBeenCalled();
      const nodeList = screen.getByTestId("graph-list-view");
      await user.click(rowByLabel(nodeList, "Edited Decision Log"));
      expect(read).toHaveBeenCalledTimes(1);
      const details = screen.getByRole("complementary", { name: "Details" });
      expect(details).toHaveTextContent("Edited ×2 · Viewed");
      // The review says where the draft stands, not the lifecycle state alone.
      expect(details).toHaveTextContent("Produced Version 2 · Changes requested");
      expect(within(details).getByRole("link", { name: "Open" }))
        .toHaveAttribute("href", `/workspaces/00000000-0000-4000-8000-000000000001/chats/${editing.entity_id}`);

      const step = (id: string, action: operations.McpChatTimelineItem["action"]) => ({
        id, tool_name: action === "search" ? "search" : "read", action, result_status: "succeeded",
        title_source: "artifact", subject: "Decision Log", subject_kind: "context_page", primary_tool_name: null,
      }) as unknown as operations.McpChatTimelineItem;
      // A long, search-heavy chat: its capped artifact list kept only a view.
      const detail = (actions: operations.McpChatTimelineItem["action"][]) => ({
        linked_artifacts: [{ resource_kind: "context_page", resource_id: seed.entity_id, verb: "viewed", occurrence_count: 1 }],
        timeline: actions.map((action, index) => step(`${action}-${index}`, action)),
      }) as unknown as operations.McpChatDetail;
      const flowSteps = () => within(screen.getByRole("complementary", { name: "Details" })).queryAllByRole("listitem")
        .filter((item) => item.closest(".graph-session-flow")).length;
      await act(async () => { pending[0].resolve(detail(["search", "read", "edit"])); });
      expect(details).toHaveTextContent("Edited ×2 · Viewed");
      expect(flowSteps()).toBe(3);

      view.rerender(graphElement(props));
      expect(read).toHaveBeenCalledTimes(1);
      await user.click(screen.getByRole("button", { name: "Refresh graph" }));
      expect(props.onRefresh).toHaveBeenCalledOnce();
      // Session activity can change without advancing the edge projection;
      // the tally follows the new relation at once.
      const refreshed = { ...payload, generated_at: "2026-08-05T00:00:00Z",
        edges: payload.edges.map((edge) => edge.source === editing.id && edge.target === seed.id
          ? relation(editing, seed, "edited", 6, [["edited", 5], ["viewed", 1]]) : edge) };
      view.rerender(graphElement({ ...props, payload: refreshed }));
      expect(read).toHaveBeenCalledTimes(2);
      expect(details).toHaveTextContent("Edited ×5 · Viewed");
      view.rerender(graphElement({ ...props, payload: { ...refreshed, generated_at: "2026-08-06T00:00:00Z" } }));
      expect(read).toHaveBeenCalledTimes(3);
      expect(pending[1].signal?.aborted).toBe(true);
      await act(async () => { pending[1].resolve(detail(["search", "read", "edit", "read", "edit"])); });
      expect(flowSteps()).toBe(0);
      await act(async () => { pending[2].resolve(detail(["read", "edit"])); });
      expect(flowSteps()).toBe(2);

      await user.click(rowByLabel(nodeList, "Read Decision Log"));
      expect(read).toHaveBeenCalledTimes(4);
      await user.click(rowByLabel(nodeList, "Edited Decision Log"));
      expect(pending[3].signal?.aborted).toBe(true);
      await act(async () => { pending[3].resolve(detail(["search", "search", "read", "edit"])); });
      expect(flowSteps()).toBe(0);
    } finally {
      read.mockRestore();
    }
  });

  it("removes session-only documents consistently when filters hide their conversation", async () => {
    // Membership comes from the unfiltered graph; reachability comes from
    // the displayed edges. Ordinary documents and versions retain history's
    // node visibility even when their independent relationships are hidden.
    const user = userEvent.setup();
    const payload = graphFixture();
    payload.history.mode = "action";
    payload.query.time = { mode: "action" };
    const seed = payload.nodes[0];
    const chat: GraphNode = { ...payload.nodes[1], id: "node:conversation", entity_id: "conversation",
      entity_kind: "mcp_chat_session", family: "chat", label: "Shared conversation" };
    const coUsed: GraphNode = { ...payload.nodes[1], id: "node:co-used", entity_id: "co-used", label: "Session-only document" };
    const independent = { ...payload.nodes[1], label: "Independently related document" };
    const version: GraphNode = { ...payload.nodes[1], id: "node:version", entity_id: "version",
      entity_kind: "context_page_version", label: "Independent version" };
    const relation = (id: string, source: GraphNode, target: GraphNode, action: string) => ({
      ...payload.edges[0], id, source: source.id, target: target.id, edge_kind: action, action_kind: action,
      layer: "action" as const, provenance: "domain" as const,
    });
    payload.nodes = [seed, chat, coUsed, independent, version];
    payload.edges = [relation("edge:session-seed", chat, seed, "edited"),
      relation("edge:session-only", chat, coUsed, "viewed"),
      relation("edge:session-independent", chat, independent, "viewed"),
      { ...payload.edges[0], source: independent.id, target: seed.id },
      relation("edge:version", version, seed, "created")];
    payload.timeline = [];
    renderGraph({ payload });
    const canvas = screen.getByTestId("mock-graph-canvas");
    const contents = screen.getByTestId("graph-list-view");
    expect(canvas).toHaveAttribute("data-nodes", "5");
    await user.click(within(contents).getByRole("button", { name: /^Two or more steps away/ }));
    expect(rowByLabel(contents, "Session-only document")).toBeVisible();
    await user.click(screen.getByText("Refine"));
    await user.click(screen.getByLabelText("Wiki link", { selector: "input" }));
    expect(canvas).toHaveAttribute("data-nodes", "5");
    await user.click(screen.getByLabelText("Conversation", { selector: "input" }));
    expect(canvas).toHaveAttribute("data-nodes", "3");
    expect(within(contents).queryByText("Session-only document")).not.toBeInTheDocument();
    await user.click(within(contents).getByRole("button", { name: /^Not connected to the seed/ }));
    expect(rowByLabel(contents, "Independently related document")).toBeVisible();
    expect(rowByLabel(contents, "Independent version")).toBeVisible();

    await user.click(screen.getByLabelText("Conversation", { selector: "input" }));
    expect(canvas).toHaveAttribute("data-nodes", "5");
    const actionFilter = screen.getByRole("group", { name: "Action" });
    await user.click(within(actionFilter).getByLabelText("Edited"));
    await user.click(within(actionFilter).getByLabelText("Viewed"));
    expect(canvas).toHaveAttribute("data-nodes", "3");
    expect(within(contents).queryByText("Session-only document")).not.toBeInTheDocument();
    await user.click(within(actionFilter).getByLabelText("Created"));
    expect(canvas).toHaveAttribute("data-nodes", "3");
    expect(canvas).toHaveAttribute("data-edges", "0");
    expect(rowByLabel(contents, "Independently related document")).toBeVisible();
    expect(rowByLabel(contents, "Independent version")).toBeVisible();

    const filters = screen.getByTestId("graph-view").querySelector<HTMLElement>(".graph-frame-filters")!;
    await user.click(within(filters).getByRole("button", { name: "Clear all" }));
    expect(canvas).toHaveAttribute("data-nodes", "5");
    await user.click(screen.getByRole("button", { name: /Historical relationships/ }));
    expect(canvas).toHaveAttribute("data-nodes", "3");
    expect(within(contents).queryByText("Session-only document")).not.toBeInTheDocument();
  });

  it("says what a read-together relationship rests on", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    payload.edges = [
      ...payload.edges,
      {
        id: "edge:co-access",
        source: "node:seed",
        target: "node:related",
        edge_kind: "co_access",
        action_kind: null,
        label: "Often read together",
        layer: "activity",
        provenance: "usage",
        direction: "undirected",
        weight: 2.4,
        rank_score: 0.0175,
        aggregate: true,
        occurred_at: "2026-08-24T12:00:00Z",
        first_observed_at: "2026-08-12T12:00:00Z",
        support_session_count: 3,
      },
    ];
    payload.facets = {
      ...payload.facets!,
      edge_kinds: [...(payload.facets!.edge_kinds ?? []), "co_access"],
      layers: [...(payload.facets!.layers ?? []), "activity"],
    };
    renderGraph({ payload });

    const sentence = "Read together in 3 sessions over 12 days (last 2026-08-24)";
    // The canvas label it shows while the edge is hovered or selected.
    expect(screen.getByTestId("mock-graph-canvas").dataset.edgeLabels).toContain(
      sentence,
    );

    await user.click(rowByLabel(screen.getByTestId("graph-list-view"), "Architecture Notes"));
    await user.click(seedPathRelation(screen.getByRole("complementary", { name: "Details" }),
      /^Decision Log and Architecture Notes/));

    const details = screen.getByRole("heading", { name: "Details" }).closest("aside");
    expect(within(details!).getByText(sentence)).toBeInTheDocument();
    // The decayed weight behind it is not a number a reader can use.
    expect(within(details!).queryByText("Aggregate")).not.toBeInTheDocument();
  });

  it("explains that read-together relationships are switched off", () => {
    const payload = graphFixture();
    payload.warnings = ["usage_relation_signals_disabled"];

    renderGraph({ payload });

    expect(
      screen.getByText("Read-together relationships are turned off for this workspace."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Some graph data could not be shown.")).not.toBeInTheDocument();
  });

  it("uses localized non-color enum labels in Korean", async () => {
    const createObjectURL = vi.fn((_blob: Blob) => "blob:korean-graph");
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await act(async () => { await i18n.changeLanguage("ko"); });
    renderGraph();

    expect(screen.getAllByText("문서").length).toBeGreaterThan(0);
    expect(screen.getAllByText("지식").length).toBeGreaterThan(0);
    expect(screen.getByRole("radio", { name: /변화 이력/ })).toHaveAttribute("aria-checked", "true");
    expect(within(screen.getByTestId("graph-timeline")).getByRole("button", { name: /포함됨.*Why was this chosen/ }))
      .toBeInTheDocument();
    expect(screen.getByTestId("graph-timeline")).toHaveTextContent("활동 · 포함됨");
    expect(screen.queryByText("Current projection")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("graph-view").querySelector(".graph-canvas-more > summary")!);
    await userEvent.click(screen.getByRole("button", { name: "필터링된 그래프 내보내기" }));
    const exported = await readBlob(createObjectURL.mock.calls[0]?.[0] as Blob);
    expect(exported).toContain("포함됨");
    expect(exported).not.toContain("included as evidence");
    await act(async () => { await i18n.changeLanguage("en"); });
  });

  it("maps known warning codes and fails closed for unknown server codes", () => {
    const payload = graphFixture();
    payload.completeness = "capped";
    payload.warnings = [
      "graph_budget_applied",
      "graph_timeline_catalog_bounded",
      "internal_future_warning_code",
    ];
    renderGraph({ payload });

    expect(screen.getByText(/limited to the requested display budget/))
      .toBeInTheDocument();
    expect(screen.getByText(/timeline catalog reached its inspection limit/))
      .toBeInTheDocument();
    expect(screen.getByText(/Some graph data could not be shown/))
      .toBeInTheDocument();
    expect(screen.queryByText(/internal_future_warning_code/))
      .not.toBeInTheDocument();
  });

  it("explains folder-only omissions without claiming the node or relationship budget was reached", async () => {
    const payload = graphFixture();
    payload.history.mode = "current";
    payload.completeness = "capped";
    payload.truncated = true;
    payload.warnings = ["graph_budget_applied"];
    payload.omissions = [{ scope: "node", reason: "folder_node", hop: 1 }];
    renderGraph({ payload });

    const notice = screen.getByRole("status");
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveTextContent("Some folders are not shown.");
    expect(screen.queryByText(/limited to the requested display budget/)).not.toBeInTheDocument();
    expect(screen.queryByText("What was left out")).not.toBeInTheDocument();
    const summary = screen.getByText("Some folders omitted");
    const details = summary.closest("details")!;
    expect(details.closest(".graph-local-stage")).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    await userEvent.click(summary);
    expect(within(details).getByText("Folder grouping limit · Distance 1")).toBeVisible();
    expect(within(details).getByText("The server does not report how many items were excluded.")).toBeVisible();
    expect(screen.queryByText(/folder_node|graph_budget_applied/)).not.toBeInTheDocument();
  });

  it("closes folder information on its first Escape without clearing graph selection or pins", async () => {
    const user = userEvent.setup();
    renderGraph({ payload: folderOnlyCoverageFixture() });
    const canvas = screen.getByTestId("mock-graph-canvas");
    await pinAskFromDetails(user);
    await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    const summary = screen.getByText("Some folders omitted");
    const disclosure = summary.closest("details")!;
    await user.click(summary);
    expect(disclosure).toHaveAttribute("open");
    await user.keyboard("{Escape}");
    expect(disclosure).not.toHaveAttribute("open");
    expect(summary).toHaveFocus();
    expect(canvas).toHaveAttribute("data-selected", "node:ask");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByTestId("graph-view")).toHaveFocus());
    expect(canvas).toHaveAttribute("data-selected", "");
    expect(canvas).toHaveAttribute("data-pinned", "node:ask");
  });

  it("closes folder information on outside input without blocking the next graph action", async () => {
    const user = userEvent.setup();
    const onExpand = vi.fn(async () => undefined);
    const onDepthChange = vi.fn(async () => undefined);
    renderGraph({ openContents: false, payload: folderOnlyCoverageFixture(), onExpand, onDepthChange });
    const summary = screen.getByText("Some folders omitted");
    const disclosure = summary.closest("details")!;
    const canvas = screen.getByTestId("mock-graph-canvas");
    await user.click(summary);
    fireEvent.click(screen.getByTestId("graph-view").querySelector(".graph-view-options > summary")!);
    const labels = screen.getByRole("checkbox", { name: "Show labels" });
    const outside = new PointerEvent("pointerdown", { bubbles: true, cancelable: true });
    expect(labels.dispatchEvent(outside)).toBe(true);
    expect(disclosure).not.toHaveAttribute("open");
    expect(summary).toHaveFocus();
    await user.click(labels);
    expect(labels).toHaveFocus();
    expect(canvas).toHaveAttribute("data-show-labels", "false");
    await user.click(summary);
    await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    expect(disclosure).not.toHaveAttribute("open");
    expect(canvas).toHaveAttribute("data-selected", "node:ask");
    expect(screen.getByTestId("mock-graph-canvas")).toBe(canvas);
    expect(onExpand).not.toHaveBeenCalled();
    expect(onDepthChange).not.toHaveBeenCalled();
  });

  it("retires open folder information on inactivity or a new payload and keeps real warnings immediate", async () => {
    const payload = folderOnlyCoverageFixture();
    const noAction = vi.fn(async () => undefined);
    const props = { payload, onCollapse: noAction, onExpand: noAction };
    const view = renderGraph(props);
    const summary = screen.getByText("Some folders omitted");
    await userEvent.click(summary);
    view.rerender(graphElement({ ...props, active: false }));
    expect(summary.closest("details")).not.toHaveAttribute("open");
    view.rerender(graphElement({ ...props, active: true }));
    await userEvent.click(summary);
    view.rerender(graphElement({ ...props, payload: { ...payload, projection_revision: 8 } }));
    expect(summary.closest("details")).not.toHaveAttribute("open");
    await userEvent.click(summary);
    view.rerender(graphElement({ ...props, payload: {
      ...payload, completeness: "bounded_sample", warnings: ["graph_projection_source_bounded"],
    } }));
    expect(screen.queryByText("Some folders omitted")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The current graph inspection reached a source limit");
    expect(screen.getByText("What was left out").closest("details")).not.toHaveAttribute("open");
    expect(noAction).not.toHaveBeenCalled();
  });

  it("keeps quota omissions as warnings and explains distance and relationship group without inventing omitted counts", async () => {
    const payload = graphFixture();
    payload.completeness = "capped";
    payload.truncated = true;
    payload.warnings = ["graph_budget_applied"];
    payload.omissions = [
      { scope: "node", reason: "node_quota", hop: 2 },
      { scope: "relation", reason: "relation_quota", relation_class: "h12" },
      { scope: "relation", reason: "relation_quota", relation_class: "h12" },
    ];
    renderGraph({ payload });

    expect(screen.getByText(/limited to the requested display budget/).closest("[role=alert],[role=status]"))
      .toHaveAttribute("role", "alert");
    const details = screen.getByText("What was left out").closest("details")!;
    await userEvent.click(within(details).getByText("What was left out"));
    expect(within(details).getByText("Node display limit · Distance 2")).toBeVisible();
    expect(within(details).getAllByText("Relationship display limit · Between distance 1 and 2")).toHaveLength(1);
    expect(within(details).getByText("Applied display limit: 50 nodes · 150 relationships.")).toBeVisible();
    expect(within(details).getByText("The server does not report how many items were excluded.")).toBeVisible();
  });

  it("distinguishes source inspection limits from the requested display limit", async () => {
    const payload = graphFixture();
    payload.completeness = "bounded_sample";
    payload.truncated = true;
    payload.warnings = ["graph_projection_source_bounded"];
    payload.omissions = [
      { scope: "node", reason: "frontier_cap", hop: 2 },
      { scope: "node", reason: "folder_source_cap", hop: 1 },
      { scope: "relation", reason: "relation_source_cap", relation_class: "seed" },
    ];
    renderGraph({ payload });

    expect(screen.getByText("The current graph inspection reached a source limit; some nodes or relationships may be missing.")
      .closest("[role=alert],[role=status]")).toHaveAttribute("role", "alert");
    expect(screen.queryByText("Some graph data could not be shown.")).not.toBeInTheDocument();
    const details = screen.getByText("What was left out").closest("details")!;
    await userEvent.click(within(details).getByText("What was left out"));
    expect(within(details).getByText("Expansion inspection limit · Distance 2")).toBeVisible();
    expect(within(details).getByText("Folder inspection limit · Distance 1")).toBeVisible();
    expect(within(details).getByText("Relationship inspection limit · Seed relationships")).toBeVisible();
    expect(within(details).queryByText(/Applied display limit/)).not.toBeInTheDocument();
  });

  it("identifies metadata-only history restrictions when the Current scene is authoritative", () => {
    const payload = graphFixture();
    payload.history.mode = "current";
    payload.history.status = "partial";
    payload.completeness = "complete";
    payload.truncated = false;
    payload.warnings = [];
    payload.history.warnings = ["history_coverage_partial"];
    renderGraph({ payload });

    expect(screen.getByText("Timeline coverage is limited.").closest("[role=alert],[role=status]"))
      .toHaveAttribute("role", "alert");
    expect(screen.getByText("Timeline coverage is partial.")).toBeInTheDocument();
    expect(screen.queryByText(/limited to the requested display budget/)).not.toBeInTheDocument();
    expect(screen.queryByText(/current graph is complete/i)).not.toBeInTheDocument();
  });

  it("warns that current relationships can be missing when the observation source is bounded despite complete history status", () => {
    const payload = graphFixture();
    payload.query.time = { mode: "current" };
    payload.history.mode = "current";
    payload.history.status = "complete";
    payload.completeness = "bounded_sample";
    payload.truncated = true;
    payload.warnings = ["graph_history_source_bounded"];
    payload.omissions = [];
    renderGraph({ payload });

    expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role=alert],[role=status]"))
      .toHaveAttribute("role", "alert");
    expect(screen.getByText("Observation inspection reached a source limit; current relationships may be missing."))
      .toBeInTheDocument();
    expect(screen.queryByText("Timeline coverage is limited.")).not.toBeInTheDocument();
    expect(screen.queryByText("Only a bounded portion of graph history was inspected.")).not.toBeInTheDocument();
  });

  it("does not describe a truncated Current scene as timeline-only even with timeline catalog warnings", () => {
    const payload = graphFixture();
    payload.history.mode = "current";
    payload.history.status = "complete";
    payload.completeness = "capped";
    payload.truncated = true;
    payload.warnings = ["graph_budget_applied", "graph_timeline_catalog_bounded"];
    renderGraph({ payload });

    expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role=alert],[role=status]"))
      .toHaveAttribute("role", "alert");
    expect(screen.queryByText("Timeline coverage is limited.")).not.toBeInTheDocument();
    expect(screen.getByText(/limited to the requested display budget/)).toBeInTheDocument();
  });

  it("keeps unknown omissions conservative without exposing raw metadata", () => {
    const payload = graphFixture();
    payload.omissions = [{ scope: "node", reason: "future_internal_omission" } as unknown as NonNullable<GraphQueryPayload["omissions"]>[number]];
    renderGraph({ payload });

    expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role=alert],[role=status]"))
      .toHaveAttribute("role", "alert");
    expect(screen.getByText("Unspecified exclusion")).toBeInTheDocument();
    expect(screen.queryByText(/future_internal_omission/)).not.toBeInTheDocument();
  });

  it("describes intentional local filtering separately from server coverage warnings", async () => {
    renderGraph();
    await userEvent.click(screen.getByLabelText("Wiki link", { selector: "input" }));
    await userEvent.click(screen.getByText("Refine"));

    expect(screen.getByText("Some items are hidden by your active filters. Clear filters to show them again."))
      .toBeVisible();
    expect(screen.queryByText("This graph has bounded or partial coverage.")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(screen.queryByText(/Some items are hidden by your active filters/)).not.toBeInTheDocument();
  });

  it.each(["graph_projection_source_bounded", "future_internal_warning"])(
    "does not downgrade folder omissions when %s also restricts coverage",
    (code) => {
      const payload = graphFixture();
      payload.completeness = "capped";
      payload.truncated = true;
      payload.warnings = ["graph_budget_applied", code];
      payload.omissions = [{ scope: "node", reason: "folder_node", hop: 1 }];
      renderGraph({ payload });

      expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role=alert],[role=status]"))
        .toHaveAttribute("role", "alert");
      expect(screen.queryByText("Some folders are not shown.")).not.toBeInTheDocument();
      expect(screen.queryByText(/future_internal_warning/)).not.toBeInTheDocument();
    },
  );

  it("separates partial Timeline coverage from a real history-scene display budget warning", () => {
    const payload = graphFixture();
    const noAction = () => Promise.resolve();
    payload.history.status = "partial";
    payload.history.warnings = ["history_coverage_partial"];
    payload.completeness = "complete";
    payload.truncated = false;
    payload.warnings = ["history_coverage_partial"];
    payload.omissions = [];
    const view = renderGraph({ payload });
    expect(within(screen.getByTestId("graph-timeline")).getByText("Recorded history only")).toBeVisible();
    expect(screen.queryByText("Some items omitted")).not.toBeInTheDocument();
    expect(screen.queryByText("What was left out")).not.toBeInTheDocument();

    view.rerender(graphElement({ onCollapse: noAction, onExpand: noAction, payload: { ...payload, completeness: "capped", truncated: true,
      warnings: ["graph_budget_applied", "graph_history_source_bounded"] } }));

    expect(screen.getByText(/limited to the requested display budget/).closest("[role=alert],[role=status]"))
      .toHaveAttribute("role", "alert");
    expect(screen.queryByText("Timeline coverage is limited.")).not.toBeInTheDocument();
  });

  it("does not mislabel mixed folder and source exclusions as a display quota", () => {
    const payload = graphFixture();
    payload.completeness = "bounded_sample";
    payload.truncated = true;
    payload.warnings = ["graph_budget_applied", "graph_projection_source_bounded"];
    payload.omissions = [
      { scope: "node", reason: "folder_node", hop: 1 },
      { scope: "relation", reason: "relation_source_cap", relation_class: "folder" },
    ];
    renderGraph({ payload });

    expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role=alert],[role=status]"))
      .toHaveAttribute("role", "alert");
    expect(screen.queryByText(/limited to the requested display budget/)).not.toBeInTheDocument();
    expect(screen.getByText(/The current graph inspection reached a source limit/)).toBeInTheDocument();
  });

  it("localizes folder exclusions and their distance in Korean", async () => {
    const payload = graphFixture();
    payload.completeness = "capped";
    payload.truncated = true;
    payload.warnings = ["graph_budget_applied"];
    payload.omissions = [{ scope: "node", reason: "folder_node", hop: 1 }];
    await act(async () => { await i18n.changeLanguage("ko"); });
    try {
      renderGraph({ payload });
      expect(screen.getByRole("status")).toHaveTextContent("일부 폴더를 표시하지 않습니다.");
      await userEvent.click(screen.getByText("일부 폴더 생략"));
      expect(screen.getByText("폴더 묶음 한도 · 거리 1")).toBeVisible();
      expect(screen.queryByText(/folder_node|graph_budget_applied|Folder grouping/)).not.toBeInTheDocument();
    } finally {
      await act(async () => { await i18n.changeLanguage("en"); });
    }
  });

  it("offers an explicit selected Local to Workspace Knowledge Map round-trip when enabled", async () => {
    renderGraph({
      workspaceGraphHref: (node) => node.entity_kind === "context_page"
        ? `/graph?focus=${node.entity_id}`
        : null,
    });
    await userEvent.click(rowByLabel(screen.getByTestId("graph-list-view"), "Decision Log"));
    const links = within(screen.getByRole("complementary", { name: "Details" })).getAllByRole("link", { name: "Show in Knowledge Map" });
    expect(links.some((link) => link.getAttribute("href") === "/graph?focus=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
      .toBe(true);
  });

  it("pauses playback while retaining the persistent canvas scene when inactive", async () => {
    const user = userEvent.setup();
    const payload = graphFixture();
    const { rerender } = renderGraph({ payload });

    const canvas = screen.getByTestId("mock-graph-canvas");
    expect(canvas).toHaveAttribute("data-active", "true");
    expect(canvas).toHaveAttribute("data-nodes", "3");

    await user.click(screen.getByRole("button", { name: "Play" }));
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    const nodesBeforeInactive = canvas.getAttribute("data-nodes") ?? "";

    rerender(graphElement({
      active: false,
      onCollapse: (_node: GraphNode) => Promise.resolve(),
      onExpand: (_node: GraphNode) => Promise.resolve(),
      payload,
    }));
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
    expect(canvas).toHaveAttribute("data-active", "false");
    expect(canvas).toHaveAttribute("data-nodes", nodesBeforeInactive);

    rerender(graphElement({
      active: true,
      onCollapse: (_node: GraphNode) => Promise.resolve(),
      onExpand: (_node: GraphNode) => Promise.resolve(),
      payload,
    }));
    expect(canvas).toHaveAttribute("data-active", "true");
    expect(canvas).toHaveAttribute("data-nodes", nodesBeforeInactive);
  });
});

/** The row (or seed pill) that selects the item with this visible name. */
function rowByLabel(container: HTMLElement, label: string) {
  const row = [...container.querySelectorAll<HTMLButtonElement>("button[data-local-graph-id]")]
    .find((candidate) => candidate.querySelector("strong")?.textContent === label);
  if (!row) throw new Error(`No row for ${label}`);
  return row;
}

/** A relation chip on the Details path card: where a seed relation opens from the list. */
function seedPathRelation(details: HTMLElement, name: RegExp) {
  return within(details.querySelector<HTMLElement>(".graph-seed-path")!).getByRole("button", { name });
}

/** Every relation row of the selected item's flow in Details. */
function detailsRelationRows(details: HTMLElement) {
  return [...details.querySelectorAll<HTMLButtonElement>(".relation-list-flow-row > button")];
}

async function pinAskFromDetails(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
  await user.click(within(screen.getByRole("complementary", { name: "Details" }))
    .getByRole("button", { name: "Pin" }));
}

function localListBudgetFixture(): GraphQueryPayload {
  const payload = graphFixture();
  payload.nodes = [...payload.nodes, ...Array.from({ length: 247 }, (_, index) => ({
    ...payload.nodes[1], id: `node:extra:${index}`, entity_id: `extra:${index}`,
    label: `Document ${index}`, is_seed: false,
  }))];
  payload.edges = Array.from({ length: 750 }, (_, index) => ({
    ...payload.edges[0], id: `edge:budget:${index}`, label: `Relationship ${index}`,
    source: payload.nodes[index % 250].id, target: payload.nodes[(index + 1) % 250].id,
  }));
  payload.timeline = [];
  payload.query.budget = { nodes: 250, edges: 750 };
  return payload;
}

function folderOnlyCoverageFixture(): GraphQueryPayload {
  const payload = graphFixture();
  payload.history.mode = "current";
  payload.query.time = { mode: "current" };
  payload.completeness = "capped";
  payload.truncated = true;
  payload.warnings = ["graph_budget_applied"];
  payload.omissions = [{ scope: "node", reason: "folder_node", hop: 1 }];
  return payload;
}

function renderGraph({
  active = true,
  onCollapse = (_node: GraphNode) => Promise.resolve(),
  onDepthChange = (_depth: 1 | 2) => Promise.resolve(),
  onRefresh,
  onExpand = (_node: GraphNode) => Promise.resolve(),
  onHistoryModeChange = (_mode: GraphHistoryMode) => Promise.resolve(),
  payload = graphFixture(),
  openContents = true,
  workspaceGraphHref,
  workspaceId = "00000000-0000-4000-8000-000000000001",
}: {
  active?: boolean;
  openContents?: boolean;
  onCollapse?: (node: GraphNode) => Promise<void>;
  onDepthChange?: (depth: 1 | 2) => Promise<void>;
  onRefresh?: () => Promise<void>;
  onExpand?: (node: GraphNode) => Promise<void>;
  onHistoryModeChange?: (mode: GraphHistoryMode) => Promise<void>;
  payload?: GraphQueryPayload;
  workspaceGraphHref?: (node: GraphNode) => string | null;
  workspaceId?: string | null;
} = {}) {
  const view = render(graphElement({
    active,
    onCollapse,
    onDepthChange,
    onRefresh,
    onExpand,
    onHistoryModeChange,
    payload,
    workspaceGraphHref,
    workspaceId,
  }));
  // List interaction regressions explicitly switch the canvas area to the list view.
  if (openContents) {
    fireEvent.click(within(view.container.querySelector<HTMLElement>(".graph-status-actions")!)
      .getByRole("button", { name: /^(List|목록)$/ }));
  }
  return view;
}

function readBlob(blob: Blob) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(String(reader.result));
    reader.readAsText(blob);
  });
}

function graphNavigationFixture(): GraphQueryPayload {
  const payload = graphFixture();
  const folder = (id: string, label: string): GraphNode => ({
    ...payload.nodes[0], id, entity_id: id, entity_kind: "workspace_tree_node",
    family: "structural", label, is_seed: false, evidence: null,
  });
  payload.nodes.push(
    folder("node:primary-folder", "Primary Folder"),
    folder("node:secondary-folder", "Secondary Folder"),
    folder("node:nested-folder", "Nested Folder"),
    { ...payload.nodes[1], id: "node:nested-page", entity_id: "nested-page", label: "Nested Page" },
    { ...payload.nodes[1], id: "node:hop2", entity_id: "hop2", label: "Distance Two" },
    { ...payload.nodes[1], id: "node:unknown", entity_id: "unknown", label: "Unknown Neighbor" },
  );
  const relation = (id: string, source: string, target: string, kind: string) => ({
    ...payload.edges[0], id, source, target, label: kind, edge_kind: kind,
  });
  payload.edges.push(
    relation("membership:primary", "node:primary-folder", "node:seed", "tree"),
    relation("membership:secondary", "node:secondary-folder", "node:seed", "tree"),
    relation("membership:duplicate", "node:secondary-folder", "node:seed", "tree"),
    relation("membership:nested", "node:primary-folder", "node:nested-folder", "tree"),
    relation("membership:nested-page", "node:nested-folder", "node:nested-page", "tree"),
    relation("hop2", "node:related", "node:hop2", "wikilink"),
    relation("unknown", "node:seed", "node:unknown", "future_private_kind"),
  );
  payload.timeline!.push({
    ...payload.timeline![1], id: "event:folders", label: "Folder memberships appeared",
    occurred_at: "2026-08-04T00:00:00Z", sequence: 4,
    edge_ids: payload.edges.filter((edge) => edge.edge_kind === "tree").map((edge) => edge.id),
  });
  return payload;
}

function graphRowCueFixture(): GraphQueryPayload {
  const payload = graphFixture();
  const node = (id: string, kind: string, family: GraphNode["family"], label: string): GraphNode => ({
    ...payload.nodes[1], id, entity_id: id, entity_kind: kind, family, label, is_seed: false,
  });
  // These are the actual Local API Library identities, both family=document;
  // a library_item-only fixture would miss the production classification bug.
  payload.nodes.push(
    node("node:library-version", "library_item_version", "document", "Source Version"),
    node("node:library-artifact", "library_artifact", "document", "Source Artifact"),
    node("node:context-version", "context_page_version", "document", "Published Snapshot"),
    node("node:chat", "mcp_chat_session", "chat", "Chat Session"),
    node("node:source-folder", "workspace_tree_node", "structural", "Visible Source Folder"),
  );
  const relation = (id: string, source: string, target: string, kind: string) => ({
    ...payload.edges[0], id, source, target, edge_kind: kind,
  });
  const members = ["node:seed", "node:library-version", "node:library-artifact", "node:context-version", "node:chat"];
  for (const member of members) {
    payload.edges.push(relation(`membership:${member}`, "node:source-folder", member, "tree"));
  }
  for (const member of members.filter((id) => id !== "node:seed")) {
    payload.edges.push(relation(`link:${member}`, "node:seed", member, "wikilink"));
  }
  payload.query.time = { mode: "current" };
  payload.history = { ...payload.history, mode: "current" };
  payload.timeline = [];
  return payload;
}

function rowCueKind(entityKind: string, family: string) {
  if (entityKind === "context_page") return "context";
  if (entityKind === "context_page_version") return "version";
  if (entityKind === "library_item_version" || entityKind === "library_artifact") return "library";
  return family === "structural" ? "folder" : "conversation";
}

/** Circle paths are drawn with arcs; polygons by their vertex count. */
function cueOutline(path: Element | null) {
  const data = path?.getAttribute("d") ?? "";
  return data.includes("a") ? "circle" : `${data.split("L").length}-gon`;
}

/** The current document's rows show the legend's "This document" glyph; others their kind's. */
function expectRowCue(payload: GraphQueryPayload, button: HTMLElement, node: GraphNode, forcedColors: boolean) {
  const primary = payload.query.seeds[0];
  if (node.entity_kind !== primary.kind || node.entity_id !== primary.id) {
    expectCanonicalRowCue(button, rowCueKind(node.entity_kind, node.family), forcedColors);
    return;
  }
  const cue = button.querySelector('[data-node-visual-kind="seed"]');
  expect.soft(cue, `Missing seed cue on ${button.textContent}`).not.toBeNull();
  expect(cue?.querySelector("circle")).toHaveAttribute("fill", forcedColors ? "rgb(255, 255, 255)" : "#7c3aed");
}

function expectCanonicalRowCue(
  button: HTMLElement,
  kind: "context" | "version" | "library" | "conversation" | "folder",
  forcedColors: boolean,
) {
  const cue = button.querySelector(`[data-node-visual-kind="${kind}"]`);
  expect.soft(cue, `Missing ${kind} cue on ${button.textContent}`).not.toBeNull();
  if (!cue) return;
  expect(cue.tagName.toLowerCase()).toBe("svg");
  expect(cue).toHaveAttribute("aria-hidden", "true");
  const palette = { context: "#7c3aed", version: "#7c3aed", library: "#0f9f8f", conversation: "#0284c7", folder: "#64748b" };
  const color = forcedColors ? "rgb(255, 255, 255)" : palette[kind];
  if (kind === "folder") {
    expect(cue.querySelectorAll("path")).toHaveLength(0);
    expect(cue.querySelector("circle")).toHaveAttribute("fill", "none");
    expect(cue.querySelector("circle")).toHaveAttribute("stroke", color);
    return;
  }
  // Shape says what it is: disc document, triangle version, square source, diamond record.
  const paths = cue.querySelectorAll("path");
  expect(paths).toHaveLength(1);
  expect(paths[0]).toHaveAttribute("fill", color);
  expect(cueOutline(paths[0])).toBe({ context: "circle", version: "3-gon", library: "4-gon", conversation: "4-gon" }[kind]);
}

function setGraphForcedColorsPreference(matches: boolean) {
  const originalDescriptor = Object.getOwnPropertyDescriptor(window, "matchMedia");
  const originalStyle = document.documentElement.getAttribute("style");
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      addEventListener: vi.fn(), addListener: vi.fn(), dispatchEvent: vi.fn(),
      matches: query === "(forced-colors: active)" && matches, media: query, onchange: null,
      removeEventListener: vi.fn(), removeListener: vi.fn(),
    } satisfies MediaQueryList)),
    writable: true,
  });
  if (matches) {
    document.documentElement.style.color = "rgb(255, 255, 255)";
    document.documentElement.style.backgroundColor = "rgb(0, 0, 0)";
  }
  return () => {
    if (originalDescriptor) Object.defineProperty(window, "matchMedia", originalDescriptor);
    else Reflect.deleteProperty(window, "matchMedia");
    if (originalStyle === null) document.documentElement.removeAttribute("style");
    else document.documentElement.setAttribute("style", originalStyle);
  };
}

function setReducedMotionPreference(matches: boolean) {
  const originalDescriptor = Object.getOwnPropertyDescriptor(window, "matchMedia");
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      addEventListener: vi.fn(),
      addListener: vi.fn(),
      dispatchEvent: vi.fn(),
      matches: query === "(prefers-reduced-motion: reduce)" && matches,
      media: query,
      onchange: null,
      removeEventListener: vi.fn(),
      removeListener: vi.fn(),
    } satisfies MediaQueryList)),
    writable: true,
  });
  return () => {
    if (originalDescriptor) {
      Object.defineProperty(window, "matchMedia", originalDescriptor);
    } else {
      Reflect.deleteProperty(window, "matchMedia");
    }
  };
}

function graphElement({
  active = true,
  onCollapse,
  onDepthChange = () => Promise.resolve(),
  onRefresh,
  onExpand,
  onHistoryModeChange,
  payload,
  staticPayload,
  workspaceGraphHref,
  workspaceId = "00000000-0000-4000-8000-000000000001",
}: {
  active?: boolean;
  onCollapse: (node: GraphNode) => Promise<void>;
  onDepthChange?: (depth: 1 | 2) => Promise<void>;
  onRefresh?: () => Promise<void>;
  onExpand: (node: GraphNode) => Promise<void>;
  onHistoryModeChange?: (mode: GraphHistoryMode) => Promise<void>;
  payload: GraphQueryPayload;
  staticPayload?: GraphQueryPayload;
  workspaceGraphHref?: (node: GraphNode) => string | null;
  workspaceId?: string | null;
}) {
  return (
    <MemoryRouter>
      <GraphView
        active={active}
        onCollapse={onCollapse}
        onDepthChange={onDepthChange}
        onRefresh={onRefresh}
        onExpand={onExpand}
        onHistoryModeChange={onHistoryModeChange ?? (() => Promise.resolve())}
        payload={payload}
        staticPayload={staticPayload ?? {
          ...payload,
          query: { ...payload.query, depth: 1, relation_scope: "direct", time: { mode: "current" } },
          history: { ...payload.history, mode: "current" },
          timeline: [],
        }}
        workspaceGraphHref={workspaceGraphHref}
        workspaceId={workspaceId}
      />
    </MemoryRouter>
  );
}
