import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
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
  centerOn: vi.fn(), fit: vi.fn(), pauseLayout: vi.fn(), resumeLayout: vi.fn(),
  pin: vi.fn(() => null), reset: vi.fn(), setViewport: vi.fn(), zoomIn: vi.fn(), zoomOut: vi.fn(),
} }));

vi.mock("../GraphCanvas", async () => {
  const { useImperativeHandle } = await import("react");
  return {
  GraphCanvas: ({
    active,
    focusedId,
    onHover,
    onRendererStatusChange,
    onSelect,
    pinnedIds,
    scene,
    selectedId,
    showLabels,
    ref,
  }: {
    active: boolean;
    focusedId: string | null;
    onHover: (id: string | null) => void;
    onRendererStatusChange: (status: "loading" | "ready" | "error") => void;
    onSelect: (id: string | null) => void;
    pinnedIds: string[];
    scene: GraphRenderScene;
    selectedId: string | null;
    showLabels?: boolean;
    ref?: Ref<GraphCanvasHandle>;
  }) => {
    useImperativeHandle(ref, () => canvasHandle);
    return (
    <div
      data-edge-labels={scene.edges.map((edge) => edge.label).join("|")}
      data-rendered-edge-ids={buildLocalScene(scene).edges.map((edge) => edge.graphId).sort().join("|")}
      data-edges={scene.edges.length}
      data-active={String(active)}
      data-focused={focusedId ?? ""}
      data-nodes={scene.nodes.length}
      data-pinned={pinnedIds.join("|")}
      data-selected={selectedId ?? ""}
      data-show-labels={String(showLabels)}
      data-testid="mock-graph-canvas"
    >
      <button onClick={() => onSelect("node:ask")} type="button">
        Select Ask from canvas
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
      <button onClick={() => onRendererStatusChange("ready")} type="button">
        Simulate renderer ready
      </button>
    </div>
    );
  },
  };
});

vi.mock("../../pages/MarkdownView", () => ({
  MarkdownView: ({ body }: { body: string }) => <pre data-testid="mock-markdown-view">{body}</pre>,
}));

describe("GraphView", () => {
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

  it("shows the canvas before relationship chips, collapsed advanced filters, and legend", async () => {
    const user = userEvent.setup();
    renderGraph();

    const graph = screen.getByTestId("graph-view");
    const stage = graph.querySelector(".graph-local-stage");
    const relationships = graph.querySelector(".graph-local-toolbar");
    const filters = graph.querySelector(".graph-filter-bar");
    const legend = graph.querySelector(".graph-legend");
    expect(stage).not.toBeNull();
    expect(relationships).not.toBeNull();
    expect(filters).not.toBeNull();
    expect(legend).not.toBeNull();
    expect(stage!.compareDocumentPosition(filters!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(stage!.compareDocumentPosition(relationships!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(stage!.compareDocumentPosition(legend!) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    expect(filters).not.toHaveAttribute("open");
    expect(within(graph.querySelector(".graph-canvas-toolbar")!).getByRole("button", {
      name: "Labels",
    })).toBeInTheDocument();

    await user.click(screen.getByText("More filters"));
    expect(filters).toHaveAttribute("open");
    expect(screen.getByLabelText("Ask turn")).toBeInTheDocument();
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

  it("keeps 250 nodes and 750 relationships at two row Tab entries with highlight-only navigation and keyboard Details handoff", async () => {
    const user = userEvent.setup();
    const payload = localListBudgetFixture();
    const onCollapse = vi.fn((_node: GraphNode) => Promise.resolve());
    const onExpand = vi.fn((_node: GraphNode) => Promise.resolve());
    const onDepthChange = vi.fn((_depth: 1 | 2) => Promise.resolve());
    const onHistoryModeChange = vi.fn((_mode: GraphHistoryMode) => Promise.resolve());
    renderGraph({ payload, onCollapse, onDepthChange, onExpand, onHistoryModeChange });
    const main = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    const nodeList = within(main).getByRole("list", { name: "Nodes" });
    // Row identity and Tab behavior are the subject here; avoid recomputing
    // accessible names for all 1,000 rows before exercising the keyboard.
    const nodes = Array.from(nodeList.querySelectorAll<HTMLButtonElement>("button"));
    expect(nodes).toHaveLength(250);
    expect(nodeList.querySelector("a,.graph-list-actions,details")).toBeNull();
    expect(nodes.map((node) => node.getAttribute("data-local-graph-id")).sort())
      .toEqual(payload.nodes.map((node) => node.id).sort());
    fireEvent.click(nodes[0]);
    const details = screen.getByRole("complementary", { name: "Details" });
    fireEvent.click(within(details).getByRole("button", { name: "Pin" }));
    await user.click(within(main).getByText("Relationships (750)"));
    const edgeList = within(main).getByRole("list", { name: "Relationships" });
    const edges = Array.from(edgeList.querySelectorAll<HTMLButtonElement>("button"));
    expect(edges).toHaveLength(750);
    expect(edges.map((edge) => edge.getAttribute("data-local-graph-id")).sort())
      .toEqual(payload.edges.map((edge) => edge.id).sort());
    const lastEdgeId = edges[749].getAttribute("data-local-graph-id")!;
    expect(nodes.filter((node) => node.tabIndex === 0)).toHaveLength(1);
    expect(edges.filter((edge) => edge.tabIndex === 0)).toHaveLength(1);
    const canvas = screen.getByTestId("mock-graph-canvas");
    for (const mock of Object.values(canvasHandle)) mock.mockClear();
    act(() => nodes[0].focus());
    await user.keyboard("{ArrowDown}{End}{Home}");
    expect(nodes[0]).toHaveFocus();
    expect(canvas).toHaveAttribute("data-focused", payload.nodes[0].id);
    expect(canvas).toHaveAttribute("data-selected", payload.nodes[0].id);
    // JSDOM's Tab model skips native summary; the browser gate covers that
    // disclosure stop. The rows still contribute one entry, not 250 stops.
    await user.tab();
    expect(edges[0]).toHaveFocus();
    await user.keyboard("{End}{ArrowUp}{ArrowDown}");
    expect(edges[749]).toHaveFocus();
    expect(canvas).toHaveAttribute("data-focused", lastEdgeId);
    expect(canvas).toHaveAttribute("data-selected", payload.nodes[0].id);
    expect(canvas).toHaveAttribute("data-pinned", payload.nodes[0].id);
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
    for (const action of [onCollapse, onDepthChange, onExpand, onHistoryModeChange]) expect(action).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(details).toHaveFocus();
    expect(canvas).toHaveAttribute("data-selected", lastEdgeId);
    expect(canvas).toHaveAttribute("data-nodes", "250");
    expect(canvas).toHaveAttribute("data-edges", "750");
    expect(canvas).toHaveAttribute("data-pinned", payload.nodes[0].id);
    for (const mock of Object.values(canvasHandle)) expect(mock).not.toHaveBeenCalled();
    for (const action of [onCollapse, onDepthChange, onExpand, onHistoryModeChange]) expect(action).not.toHaveBeenCalled();
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
    const title = within(details).getByRole("heading", { level: 5 });
    expect(details.querySelector(".graph-details-actions")!
      .compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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

    await user.click(screen.getByText("More filters"));
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

  it("explains actual point/square/ring encodings and relationship patterns instead of retired families", () => {
    const payload = graphFixture();
    payload.nodes.push(
      { ...payload.nodes[1], id: "node:library", entity_kind: "library_item_version", entity_id: "library", label: "Source" },
      { ...payload.nodes[1], id: "node:folder", entity_kind: "workspace_tree_node", entity_id: "folder", family: "structural", label: "Folder" },
    );
    payload.edges.push({ ...payload.edges[0], id: "edge:action", edge_kind: "edited", action_kind: "edited", layer: "action" });
    renderGraph({ payload });
    const legend = screen.getByTestId("graph-view").querySelector(".graph-legend")!;
    expect(legend.querySelector('[data-node-visual-kind="context"] circle')).toHaveAttribute("fill", "#7c3aed");
    expect(legend.querySelector('[data-node-visual-kind="library"] rect')).toHaveAttribute("fill", "#0f9f8f");
    expect(legend.querySelector('[data-node-visual-kind="folder"] circle')).toHaveAttribute("fill", "none");
    expect(legend.querySelector('[data-node-visual-kind="conversation"] circle')).toHaveAttribute("fill", "#ea580c");
    expect(legend.querySelector('[data-node-visual-kind="seed"] circle')).toHaveAttribute("stroke-width", "2.64");
    expect(legend.querySelector('[data-edge-visual-kind="action"] line')).toHaveAttribute("stroke-dasharray", "10 3");
    expect(legend.querySelector('[data-edge-visual-kind="link"] line')).not.toHaveAttribute("stroke-dasharray");
    expect(legend.querySelector(".graph-node-swatch-ask,.graph-edge-swatch-knowledge")).toBeNull();
  });

  it.each([false, true])("uses canonical actual-API node cues in main rows with forced colors=%s without changing labels or scene counts", (forcedColors) => {
    const restoreMedia = setGraphForcedColorsPreference(forcedColors);
    let view: ReturnType<typeof renderGraph> | null = null;
    try {
      const payload = graphRowCueFixture();
      view = renderGraph({ payload });
      const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
      const rows = contents.querySelectorAll(":scope > details:first-of-type > ul > li");
      expect(rows).toHaveLength(8);
      expect(screen.getByText("8 nodes · 11 edges")).toBeInTheDocument();
      for (const node of payload.nodes) {
        const row = [...rows].find((candidate) =>
          candidate.querySelector(":scope > button strong")?.textContent === node.label)!;
        const button = row.querySelector<HTMLButtonElement>(":scope > button.graph-list-select")!;
        expect(button).toHaveTextContent(node.label);
        expect(button).toHaveAccessibleName(new RegExp(node.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
        expect(button).toHaveAttribute("aria-pressed", "false");
        expectCanonicalRowCue(button, rowCueKind(node.entity_kind, node.family), forcedColors);
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
      const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
      const rows = [...contents.querySelectorAll<HTMLLIElement>(":scope > details:first-of-type > ul > li")];
      const rowFor = (label: string) => rows.find((candidate) =>
        candidate.querySelector(":scope > button strong")?.textContent === label)!;
      const folderRow = rowFor("Visible Source Folder");
      expect(folderRow.querySelector("details,.graph-list-actions")).toBeNull();
      const checkRows = (list: HTMLElement, expectedLabels: string[]) => {
        const buttons = within(list).getAllByRole("button");
        expect(buttons).toHaveLength(expectedLabels.length);
        expect(buttons.map((button) => button.querySelector("strong")!.textContent).sort())
          .toEqual([...expectedLabels].sort());
        for (const button of buttons) {
          const node = payload.nodes.find((candidate) => candidate.label === button.querySelector("strong")!.textContent)!;
          expectCanonicalRowCue(button, rowCueKind(node.entity_kind, node.family), forcedColors);
        }
      };
      const memberLabels = ["Decision Log", "Source Version", "Source Artifact", "Published Snapshot", "Chat Session"];
      await user.click(folderRow.querySelector<HTMLButtonElement>(":scope > button.graph-list-select")!);
      const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
      checkRows(within(details).getByRole("list", { name: "Members in this scene" }), memberLabels);

      await user.click(rowFor("Decision Log").querySelector<HTMLButtonElement>(":scope > button.graph-list-select")!);
      const neighbors = within(details).getByRole("list", { name: "Direct neighbors in this scene" });
      checkRows(neighbors, payload.nodes.filter((node) => node.id !== "node:seed").map((node) => node.label));
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
      const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
      const rows = [...contents.querySelectorAll(":scope > details:first-of-type > ul > li")];
      const rowFor = (label: string) => rows.find((candidate) =>
        candidate.querySelector(":scope > button strong")?.textContent === label)!;
      await act(async () => {
        graph.style.setProperty("--graph-node-library", "#123456");
        graph.style.setProperty("--graph-surface", "#abcdef");
      });
      const libraryRow = rowFor("Source Version");
      expect(libraryRow.querySelector('[data-node-visual-kind="library"] rect'))
        .toHaveAttribute("fill", "#123456");
      const legend = graph.querySelector(".graph-legend")!;
      expect(legend.querySelector('[data-node-visual-kind="library"] rect'))
        .toHaveAttribute("fill", "#123456");
      const user = userEvent.setup();
      await user.click(rowFor("Decision Log").querySelector<HTMLButtonElement>(":scope > button.graph-list-select")!);
      const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
      const neighbors = within(details).getByRole("list", { name: "Direct neighbors in this scene" });
      expect(within(neighbors).getByRole("button", { name: /Source Version/ })
        .querySelector('[data-node-visual-kind="library"] rect')).toHaveAttribute("fill", "#123456");
      await act(async () => { document.documentElement.setAttribute("data-theme", "dark"); });
      expect(rowFor("Decision Log").querySelector('[data-node-visual-kind="context"] circle'))
        .toHaveAttribute("fill", "#8b5cf6");
      expect(legend.querySelector('[data-node-visual-kind="context"] circle'))
        .toHaveAttribute("fill", "#8b5cf6");
      expect(libraryRow.querySelector('[data-node-visual-kind="library"] rect'))
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
    renderGraph();

    expect(screen.getByText("3 nodes · 2 edges")).toBeInTheDocument();
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-nodes", "3");
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-edges", "2");
    expect(screen.getByRole("button", { name: "Reset viewport, pins, and selection" }))
      .toBeInTheDocument();
    await user.click(screen.getByText("More filters"));

    await user.click(screen.getByRole("button", { name: /Why was this chosen.*Ask turn/ }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside");
    expect(within(details!).getByText("Why was this chosen?")).toBeInTheDocument();
    expect(within(details!).queryByRole("link", { name: "Open" })).not.toBeInTheDocument();
    expect(within(details!).getByRole("button", { name: "Center" })).toBeInTheDocument();
    expect(within(details!).getByRole("button", { name: "Pin" })).toBeInTheDocument();
    await user.click(screen.getByLabelText("Ask turn"));
    expect(screen.getByText("2 nodes · 1 edges")).toBeInTheDocument();
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-nodes", "2");
    expect(screen.queryByText("Why was this chosen?")).not.toBeInTheDocument();
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
  });

  it("opens the new Local renderer with relation chips and a live label toggle", async () => {
    const user = userEvent.setup();
    renderGraph();

    const canvas = screen.getByTestId("mock-graph-canvas");
    expect(canvas).toHaveAttribute("data-show-labels", "true");
    const labels = screen.getByRole("button", { name: "Labels" });
    await user.click(labels);
    expect(labels).toHaveAttribute("aria-pressed", "false");
    expect(canvas).toHaveAttribute("data-show-labels", "false");

    const links = screen.getByRole("button", { name: "Links 1" });
    expect(links).toHaveAttribute("aria-pressed", "true");
    await user.click(links);
    expect(links).toHaveAttribute("aria-pressed", "false");
    expect(canvas).toHaveAttribute("data-edges", "1");
    expect(screen.getByRole("button", { name: "Read together 0" })).toBeDisabled();
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
    const actions = screen.getByRole("button", { name: "Actions 3" });
    const relationshipList = screen.getByTestId("graph-view")
      .querySelector<HTMLUListElement>(".graph-relationship-list > details:nth-of-type(2) > ul")!;
    expect(actions).toHaveAttribute("aria-pressed", "true");
    expect(canvas).toHaveAttribute("data-rendered-edge-ids", "action:created|action:edited|action:published");
    expect(relationshipList.children).toHaveLength(3);
    await user.click(actions);
    expect(canvas).toHaveAttribute("data-rendered-edge-ids", "");
    expect(relationshipList.children).toHaveLength(0);
    await user.click(actions);
    await user.click(screen.getByText("More filters"));
    const actionFilter = screen.getByRole("group", { name: "Action" });
    await user.click(within(actionFilter).getByLabelText("Edited"));
    expect(canvas).toHaveAttribute("data-rendered-edge-ids", "action:created|action:published");
    expect(relationshipList.children).toHaveLength(2);
    expect(screen.getByText("4 nodes · 2 edges")).toBeInTheDocument();
    await user.click(screen.getByText("Legend", { exact: true }));
    expect(screen.getByText("Long-dashed lines show action occurrences, not document links or citations."))
      .toBeVisible();
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
    const toolbar = within(screen.getByRole("toolbar", { name: "Graph view controls" }));
    expect(toolbar.getByRole("button", { name: "Depth 2" })).toHaveAttribute("aria-pressed", "true");
    await user.click(toolbar.getByRole("button", { name: "Depth 1" }));
    expect(onDepthChange).toHaveBeenCalledWith(1);
    expect(toolbar.getByRole("button", { name: "Depth 2" })).toHaveAttribute("aria-pressed", "true");
    expect(toolbar.getByRole("button", { name: "Depth 1" })).toBeDisabled();
    await act(async () => { finishRequest(); });
    rerender(graphElement({
      onCollapse: () => Promise.resolve(), onExpand: () => Promise.resolve(),
      onDepthChange, payload: { ...payload, query: { ...payload.query, depth: 1 } },
    }));
    expect(toolbar.getByRole("button", { name: "Depth 1" })).toHaveAttribute("aria-pressed", "true");
    expect(toolbar.getByRole("button", { name: "Depth 2" })).toBeEnabled();
  });

  it("explains and disables unsupported two-hop history instead of pretending to offer it", async () => {
    const user = userEvent.setup();
    const onDepthChange = vi.fn(() => Promise.resolve());
    renderGraph({ onDepthChange });
    const toolbar = within(screen.getByRole("toolbar", { name: "Graph view controls" }));
    expect(toolbar.getByRole("button", { name: "Depth 1" })).toHaveAttribute("aria-pressed", "true");
    const unsupported = toolbar.getByRole("button", { name: "Depth 2" });
    expect(unsupported).toBeDisabled();
    expect(unsupported).toHaveAccessibleDescription("History views currently support depth 1 only.");
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
    await user.click(screen.getByRole("button", { name: "Depth 1" }));
    expect(screen.getByText("The graph could not be updated. Try again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Depth 2" })).toHaveAttribute("aria-pressed", "true");
  });

  it("provides operable DOM selection and gates expansion to supported Context-page seeds", async () => {
    const user = userEvent.setup();
    const onCollapse = vi.fn((_node: GraphNode) => Promise.resolve());
    const onExpand = vi.fn((_node: GraphNode) => Promise.resolve());
    const { rerender } = renderGraph({ onCollapse, onExpand });
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;

    const askRow = within(contents).getByText("Why was this chosen?").closest("li");
    expect(askRow).not.toBeNull();
    await user.click(within(askRow!).getByRole("button"));
    let selectedDetails = screen.getByRole("complementary", { name: "Details" });
    const askExpand = within(selectedDetails).getByRole("button", { name: "Expand" });
    expect(askExpand).toBeDisabled();
    expect(askExpand)
      .toHaveAccessibleDescription("Expansion is available for Context pages only.");
    expect(within(selectedDetails).getByText("Expansion is available for Context pages only."))
      .toHaveAttribute("id", askExpand.getAttribute("aria-describedby"));
    expect(askExpand).not.toHaveAttribute("title");

    const primaryRow = within(contents).getByText("Decision Log").closest("li");
    await user.click(within(primaryRow!).getByRole("button"));
    selectedDetails = screen.getByRole("complementary", { name: "Details" });
    const primaryExpand = within(selectedDetails).getByRole("button", { name: "Expand" });
    expect(primaryExpand).toBeDisabled();
    expect(primaryExpand)
      .toHaveAccessibleDescription("The primary seed stays in the graph.");
    expect(within(selectedDetails).getByText("The primary seed stays in the graph."))
      .toHaveAttribute("id", primaryExpand.getAttribute("aria-describedby"));

    const relatedRow = within(contents).getByText("Architecture Notes").closest("li");
    expect(relatedRow).not.toBeNull();
    await user.click(within(relatedRow!).getByRole("button"));
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

    await user.click(within(contents).getByRole("button", { name: /Decision Log.*Document/ }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside");
    expect(details).not.toBeNull();
    expect(within(details!).getByText("Context page")).toBeInTheDocument();
    expect(within(details!).getByText("Published")).toBeInTheDocument();
    await user.click(within(contents).getByRole("button", { name: /Architecture Notes.*Document/ }));
    expect(within(details!).getByRole("link", { name: "Open" }))
      .toHaveAttribute("href", "/workspaces/00000000-0000-4000-8000-000000000001/context/pages/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
  });

  it("scopes Escape to Graph View and restores root focus for DOM and Canvas selections", async () => {
    const user = userEvent.setup();
    renderGraph();
    canvasHandle.pauseLayout.mockClear();
    const graph = screen.getByTestId("graph-view");
    const askSelect = screen.getByRole("button", { name: /Why was this chosen.*Ask turn/ });

    await user.click(askSelect);
    expect(askSelect).toHaveAttribute("aria-pressed", "true");
    expect(within(screen.getByRole("heading", { name: "Details" }).closest("aside")!)
      .getByText("Why was this chosen?")).toBeInTheDocument();

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
    await user.click(within(screen.getByRole("list", { name: "Nodes" }))
      .getByRole("button", { name: /Architecture Notes.*Document/ }));
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
      .querySelector(".sr-only[aria-live='polite']");
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
    await user.click(screen.getByRole("button", { name: "Select Ask from canvas" }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    expect(within(details).getByText(/Other nodes stay visible but dimmed/)).toBeInTheDocument();
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

    const relatedSelect = screen.getByRole("button", { name: /Architecture Notes.*Document/ });
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
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    const selectAsk = within(contents).getByRole("button", { name: /Why was this chosen.*Ask turn/ });
    const selectRelated = within(contents).getByRole("button", { name: /Architecture Notes.*Document/ });
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

    const relationships = contents.querySelectorAll("details")[1];
    await user.click(within(relationships).getByText("Relationships (2)"));
    details.scrollTop = 320;
    await user.click(within(relationships).getAllByRole("button")[0]);
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

  it.each(["en", "ko"])("puts every selected action before the full title without losing accessible names or reasons in %s", async (locale) => {
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
      const controls = details.querySelector(".graph-details-control-region");
      const title = details.querySelector("h5")!;
      expect(within(details).getByRole("link", { name: i18n.t("graph:graph.open") }))
        .toHaveAttribute("href", `/workspaces/${workspaceId}/context/pages/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`);
      expect(controls).not.toBeNull();
      expect(controls!.querySelectorAll("a,button")).toHaveLength(6);
      expect(controls!.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(title).toHaveTextContent(fullTitle);
      const clearLabel = i18n.t("graph:graph.details.clearSelection");
      const clear = within(details).getByRole("button", { name: clearLabel });
      expect(clear).toHaveAttribute("aria-label", clearLabel);
      expect(clear).toHaveAttribute("title", clearLabel);
      expect(clear.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      expect(clear).toHaveTextContent(clearLabel);

      const expand = within(details).getByRole("button", { name: i18n.t("graph:graph.expand") });
      expect(expand).toBeDisabled();
      const reason = document.getElementById(expand.getAttribute("aria-describedby")!)!;
      expect(reason).toHaveTextContent(i18n.t("graph:graph.primarySeed"));
      expect(controls).not.toContainElement(reason);
      expect(title.compareDocumentPosition(reason) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      act(() => details.focus());
      await user.tab();
      expect(within(details).getByRole("link", { name: i18n.t("graph:graph.open") })).toHaveFocus();
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
    expect(within(details).getByText("Updated Ask title")).toBeInTheDocument();
    expect(details.scrollTop).toBe(260);
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:ask");
  });

  it("keeps main rows selection-only and offers exact scene-only members in selected folder details", async () => {
    const user = userEvent.setup();
    renderGraph({ payload: graphNavigationFixture() });
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    const folderRow = within(contents).getByText("Primary Folder").closest("li")!;
    expect(folderRow.querySelector("details,.graph-list-actions")).toBeNull();
    expect(within(folderRow).getAllByRole("button")).toHaveLength(1);

    await user.click(within(folderRow).getByRole("button", { name: /Primary Folder.*Structure/ }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const detailsMembers = within(details).getByRole("list", { name: "Members in this scene" });
    expect(within(detailsMembers).getAllByRole("button")).toHaveLength(2);
    expect(within(detailsMembers).getByRole("button", { name: /Decision Log/ })).toBeVisible();
    expect(within(detailsMembers).getByRole("button", { name: /Nested Folder/ })).toBeVisible();
    expect(within(detailsMembers).queryByText("Nested Page")).not.toBeInTheDocument();
    expect(details).not.toHaveTextContent(/Primary placement|All members|omitted/);

    await user.click(within(detailsMembers).getByRole("button", { name: /Decision Log/ }));
    const folders = within(details).getByRole("list", { name: "Folders in this scene", hidden: true });
    expect(within(folders).getAllByRole("button", { hidden: true })).toHaveLength(2);
    expect(folders).toHaveTextContent("Primary Folder");
    expect(folders).toHaveTextContent("Secondary Folder");
  });

  it("uses one neighbor Tab entry and arrow emphasis without selecting, pinning, or moving the view", async () => {
    const user = userEvent.setup();
    const onExpand = vi.fn(() => Promise.resolve());
    const onCollapse = vi.fn(() => Promise.resolve());
    const onDepthChange = vi.fn(() => Promise.resolve());
    renderGraph({ payload: graphNavigationFixture(), onExpand, onCollapse, onDepthChange });
    await pinAskFromDetails(user);
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    await user.click(within(contents).getByRole("button", { name: /Decision Log.*Document.*Published/ }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const neighbors = within(details).getByRole("list", { name: "Direct neighbors in this scene" });
    const buttons = within(neighbors).getAllByRole("button");
    expect(buttons).toHaveLength(4);
    expect(neighbors).not.toHaveTextContent(/Distance Two|Unknown Neighbor/);
    const canvas = screen.getByTestId("mock-graph-canvas");
    const sceneCount = canvas.getAttribute("data-edges");
    for (const mock of Object.values(canvasHandle)) mock.mockClear();
    act(() => buttons[0].focus());
    expect(canvas).toHaveAttribute("data-focused", "node:ask");
    await user.keyboard("{ArrowDown}");
    expect(buttons[1]).toHaveFocus();
    expect(canvas).toHaveAttribute("data-focused", "node:related");
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
    expect(canvas).toHaveAttribute("data-selected", "node:ask");
    expect(details).toHaveFocus();
    const nextNeighbors = within(details).getByRole("list", { name: "Direct neighbors in this scene" });
    const seed = within(nextNeighbors).getByRole("button", { name: /Decision Log/ });
    // No manual focus after selection: the actual keyboard sequence must
    // reach the newly selected object's controls and its new neighbor list.
    for (let step = 0; step < 8 && document.activeElement !== seed; step++) await user.tab();
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
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    await user.click(within(contents).getByRole("button", { name: /Decision Log.*Document.*Published/ }));
    const graph = screen.getByTestId("graph-view");
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const neighbors = within(details).getByRole("list", { name: "Direct neighbors in this scene" });
    const target = within(neighbors).getByRole("button", { name: /Why was this chosen/ });
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
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    await user.click(within(contents).getByRole("button", { name: /Decision Log.*Document.*Published/ }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const focusDetails = vi.spyOn(details, "focus");
    const neighbors = within(details).getByRole("list", { name: "Direct neighbors in this scene" });
    await user.click(within(neighbors).getByRole("button", { name: /Why was this chosen/ }));
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:ask");
    expect(focusDetails).not.toHaveBeenCalled();

    const member = within(contents).getByRole("button", { name: /Decision Log.*Document.*Published/ });
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
    const main = screen.getByRole("list", { name: "Nodes" });
    await user.click(within(main).getByRole("button", { name: /Primary Folder.*Structure/ }));
    const details = screen.getByRole("complementary", { name: "Details" });
    const focusDetails = vi.spyOn(details, "focus");
    const focusGraph = vi.spyOn(graph, "focus");
    const member = within(within(details).getByRole("list", { name: "Members in this scene" }))
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
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    await user.click(within(contents).getByRole("button", { name: /Decision Log.*Document.*Published/ }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const focusDetails = vi.spyOn(details, "focus");
    const focusGraph = vi.spyOn(screen.getByTestId("graph-view"), "focus");
    const target = within(within(details).getByRole("list", { name: "Direct neighbors in this scene" }))
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

  it("updates scene membership and neighbors when filters or the timeline remove their relationships", async () => {
    const user = userEvent.setup();
    const payload = graphNavigationFixture();
    renderGraph({ payload });
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    const folderRow = within(contents).getByText("Primary Folder").closest("li")!;
    await user.click(within(folderRow).getByRole("button", { name: /Primary Folder.*Structure/ }));
    await user.click(screen.getByRole("button", { name: "Membership 5" }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    expect(details).toHaveTextContent("Members in this scene (0)");
    expect(details).toHaveTextContent("No members are connected in this scene.");
    expect(details).toHaveTextContent("No direct neighbors are connected in this scene.");
    expect(screen.getByTestId("mock-graph-canvas")).toHaveAttribute("data-selected", "node:primary-folder");
    await user.click(screen.getByRole("button", { name: "Membership 5" }));
    expect(details).toHaveTextContent("Members in this scene (2)");
    await user.click(screen.getByRole("button", { name: "Reset timeline" }));
    expect(details).toHaveTextContent("Members in this scene (0)");
    expect(details).toHaveTextContent("No members are connected in this scene.");
  });

  it("clears transient neighbor focus when its relationship disappears but its node remains visible", async () => {
    const user = userEvent.setup();
    renderGraph({ payload: graphNavigationFixture() });
    const contents = screen.getByRole("heading", { name: "Accessible graph contents" }).closest("section")!;
    await user.click(within(contents).getByRole("button", { name: /Decision Log.*Document.*Published/ }));
    const details = screen.getByRole("heading", { name: "Details" }).closest("aside")!;
    const neighbors = within(details).getByRole("list", { name: "Direct neighbors in this scene" });
    act(() => within(neighbors).getByRole("button", { name: /Architecture Notes/ }).focus());
    const canvas = screen.getByTestId("mock-graph-canvas");
    expect(canvas).toHaveAttribute("data-focused", "node:related");
    // Preserve DOM focus while changing the scene, like an incoming update.
    fireEvent.click(screen.getByRole("button", { name: "Links 2" }));
    expect(canvas).toHaveAttribute("data-nodes", "9");
    expect(canvas).toHaveAttribute("data-focused", "");
    expect(canvas).toHaveAttribute("data-selected", "node:seed");
    expect(within(details).getByRole("list", { name: "Direct neighbors in this scene" }))
      .not.toHaveTextContent("Architecture Notes");
  });

  it("localizes scene membership and keyboard navigation in Korean", async () => {
    const user = userEvent.setup();
    await act(async () => { await i18n.changeLanguage("ko"); });
    try {
      renderGraph({ payload: graphNavigationFixture() });
      const contents = screen.getByRole("heading", { name: "접근 가능한 그래프 내용" }).closest("section")!;
      const row = within(contents).getByText("Primary Folder").closest("li")!;
      await user.click(within(row).getByRole("button", { name: /Primary Folder.*구조.*게시됨/ }));
      const details = screen.getByRole("heading", { name: "상세 정보" }).closest("aside")!;
      expect(details).toHaveTextContent("이 장면의 소속 항목 (2)");
      expect(within(details).getByRole("list", { name: "이 장면의 직접 이웃" })).toBeInTheDocument();
      expect(details).toHaveTextContent("Tab으로 목록에 한 번 진입하세요. 위/아래 방향키 또는 Home/End로 항목을 강조하고 Enter 또는 Space로 선택하면 상세로 이동합니다.");
      expect(details).not.toHaveTextContent(/Members in this scene|Direct neighbors in this scene/);
    } finally {
      await act(async () => { await i18n.changeLanguage("en"); });
    }
  });

  it("mounts Mermaid only while the static view is requested, never for hidden print content", async () => {
    const user = userEvent.setup();
    renderGraph();
    expect(screen.queryByTestId("mock-markdown-view")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Links 1" }));
    expect(screen.queryByTestId("mock-markdown-view")).not.toBeInTheDocument();
    await user.click(screen.getByText("Static graph view"));
    expect(screen.getAllByTestId("mock-markdown-view")).toHaveLength(1);
    expect(screen.getByTestId("mock-markdown-view")).not.toHaveTextContent("Wiki link");
    await user.click(screen.getByText("Static graph view"));
    await waitFor(() => expect(screen.queryByTestId("mock-markdown-view"))
      .not.toBeInTheDocument());
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
    expect(container.querySelector("[data-testid='mock-markdown-view']")).toBeNull();
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
    const diagram = screen.getByTestId("mock-markdown-view");
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
    expect(screen.queryByTestId("mock-markdown-view")).not.toBeInTheDocument();
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

    await user.click(screen.getByRole("button", { name: "Reset timeline" }));
    expect(screen.getByText("1 nodes · 0 edges")).toBeInTheDocument();
    expect(screen.getByText("0 / 3")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next event" }));
    expect(screen.getByText("Architecture Notes · Created")).toBeInTheDocument();
    expect(screen.getByText("2 nodes · 0 edges")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Timeline position"), {
      target: { value: "3" },
    });
    expect(screen.getByText("3 nodes · 2 edges")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("History mode"), "action");
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

    expect(screen.getByText("Showing the current graph. Choose a history mode to inspect past events."))
      .toBeInTheDocument();
    expect(screen.queryByRole("slider", { name: "Timeline position" })).not.toBeInTheDocument();
    expect(screen.queryByText("0 / 0")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("History mode"), "appearance");
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

  it("normalizes autoplay completion to the current projection like scrub and step", () => {
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
      expect(screen.getByText("3 nodes · 1 edges")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Play" }));
      expect(screen.getByText("3 nodes · 0 edges")).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(1_000));

      expect(screen.getByText("3 nodes · 1 edges")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("steps a one-event reduced-motion timeline from its historical frame back to current", () => {
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

      expect(screen.getByText("3 nodes · 1 edges")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Play" }));
      expect(screen.getByText("3 nodes · 0 edges")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Play" }));
      expect(screen.getByText("3 nodes · 1 edges")).toBeInTheDocument();
    } finally {
      restoreMatchMedia();
    }
  });

  it("describes a selected action edge as source, action, and target", async () => {
    const user = userEvent.setup();
    renderGraph();

    await user.click(screen.getByText("Relationships (2)"));
    await user.click(screen.getByRole("button", { name: /Included.*Decision Log/ }));

    const details = screen.getByRole("heading", { name: "Details" }).closest("aside");
    expect(within(details!).getByText("Decision Log —Included→ Why was this chosen?"))
      .toBeInTheDocument();
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

    await user.click(screen.getByText("Relationships (3)"));
    await user.click(screen.getByRole("button", { name: /Often read together/ }));

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
    expect(screen.getByText(/관찰 기록/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /포함됨.*Decision Log/ }))
      .toBeInTheDocument();
    expect(screen.getByText("Decision Log —포함됨→ Why was this chosen?"))
      .toBeInTheDocument();
    expect(screen.queryByText("Current projection")).not.toBeInTheDocument();

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
    expect(details.closest(".graph-canvas-toolbar")).not.toBeNull();
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
    renderGraph({ payload: folderOnlyCoverageFixture(), onExpand, onDepthChange });
    const summary = screen.getByText("Some folders omitted");
    const disclosure = summary.closest("details")!;
    const canvas = screen.getByTestId("mock-graph-canvas");
    await user.click(summary);
    const labels = screen.getByRole("button", { name: "Labels" });
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

    expect(screen.getByText(/limited to the requested display budget/).closest("[role]"))
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
      .closest("[role]")).toHaveAttribute("role", "alert");
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

    expect(screen.getByText("Timeline coverage is limited.").closest("[role]"))
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

    expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role]"))
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

    expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role]"))
      .toHaveAttribute("role", "alert");
    expect(screen.queryByText("Timeline coverage is limited.")).not.toBeInTheDocument();
    expect(screen.getByText(/limited to the requested display budget/)).toBeInTheDocument();
  });

  it("keeps unknown omissions conservative without exposing raw metadata", () => {
    const payload = graphFixture();
    payload.omissions = [{ scope: "node", reason: "future_internal_omission" } as unknown as NonNullable<GraphQueryPayload["omissions"]>[number]];
    renderGraph({ payload });

    expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role]"))
      .toHaveAttribute("role", "alert");
    expect(screen.getByText("Unspecified exclusion")).toBeInTheDocument();
    expect(screen.queryByText(/future_internal_omission/)).not.toBeInTheDocument();
  });

  it("describes intentional local filtering separately from server coverage warnings", async () => {
    renderGraph();
    await userEvent.click(screen.getByRole("button", { name: "Links 1" }));
    await userEvent.click(screen.getByText("More filters"));

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

      expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role]"))
        .toHaveAttribute("role", "alert");
      expect(screen.queryByText("Some folders are not shown.")).not.toBeInTheDocument();
      expect(screen.queryByText(/future_internal_warning/)).not.toBeInTheDocument();
    },
  );

  it("retains a real history-scene display budget warning when no omission metadata is available", () => {
    const payload = graphFixture();
    payload.completeness = "capped";
    payload.truncated = true;
    payload.warnings = ["graph_budget_applied", "graph_history_source_bounded"];
    renderGraph({ payload });

    expect(screen.getByText(/limited to the requested display budget/).closest("[role]"))
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

    expect(screen.getByText("This graph has bounded or partial coverage.").closest("[role]"))
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
    await userEvent.click(screen.getByRole("button", { name: /Decision Log.*Document.*Published/ }));
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
  onExpand = (_node: GraphNode) => Promise.resolve(),
  onHistoryModeChange = (_mode: GraphHistoryMode) => Promise.resolve(),
  payload = graphFixture(),
  workspaceGraphHref,
  workspaceId = "00000000-0000-4000-8000-000000000001",
}: {
  active?: boolean;
  onCollapse?: (node: GraphNode) => Promise<void>;
  onDepthChange?: (depth: 1 | 2) => Promise<void>;
  onExpand?: (node: GraphNode) => Promise<void>;
  onHistoryModeChange?: (mode: GraphHistoryMode) => Promise<void>;
  payload?: GraphQueryPayload;
  workspaceGraphHref?: (node: GraphNode) => string | null;
  workspaceId?: string | null;
} = {}) {
  return render(graphElement({
    active,
    onCollapse,
    onDepthChange,
    onExpand,
    onHistoryModeChange,
    payload,
    workspaceGraphHref,
    workspaceId,
  }));
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
  if (entityKind === "context_page" || entityKind === "context_page_version") return "context";
  if (entityKind === "library_item_version" || entityKind === "library_artifact") return "library";
  return family === "structural" ? "folder" : "conversation";
}

function expectCanonicalRowCue(
  button: HTMLElement,
  kind: "context" | "library" | "conversation" | "folder",
  forcedColors: boolean,
) {
  const cue = button.querySelector(`[data-node-visual-kind="${kind}"]`);
  expect.soft(cue, `Missing ${kind} cue on ${button.textContent}`).not.toBeNull();
  if (!cue) return;
  expect(cue.tagName.toLowerCase()).toBe("svg");
  expect(cue).toHaveAttribute("aria-hidden", "true");
  const palette = { context: "#7c3aed", library: "#0f9f8f", conversation: "#ea580c", folder: "#64748b" };
  const color = forcedColors ? "rgb(255, 255, 255)" : palette[kind];
  if (kind === "library") {
    expect(cue.querySelectorAll("rect")).toHaveLength(1);
    expect(cue.querySelector("rect")).toHaveAttribute("fill", color);
    expect(cue.querySelectorAll("circle")).toHaveLength(0);
  } else {
    expect(cue.querySelectorAll("rect")).toHaveLength(0);
    const circles = cue.querySelectorAll("circle");
    expect(circles).toHaveLength(kind === "conversation" && forcedColors ? 3 : 1);
    expect(circles[0]).toHaveAttribute("fill", kind === "folder" ? "none" : color);
    if (kind === "folder") expect(circles[0]).toHaveAttribute("stroke", color);
    if (kind === "conversation" && forcedColors) {
      expect(circles[1]).toHaveAttribute("fill", "rgb(0, 0, 0)");
      expect(circles[2]).toHaveAttribute("fill", color);
      expect(Number(circles[0].getAttribute("r"))).toBeGreaterThan(Number(circles[1].getAttribute("r")));
      expect(Number(circles[1].getAttribute("r"))).toBeGreaterThan(Number(circles[2].getAttribute("r")));
    }
  }
  expect(button.querySelector(".graph-node-swatch-document,.graph-node-swatch-structural,.graph-node-swatch-chat,.graph-node-swatch-ask"))
    .toBeNull();
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
  onExpand,
  onHistoryModeChange,
  payload,
  workspaceGraphHref,
  workspaceId = "00000000-0000-4000-8000-000000000001",
}: {
  active?: boolean;
  onCollapse: (node: GraphNode) => Promise<void>;
  onDepthChange?: (depth: 1 | 2) => Promise<void>;
  onExpand: (node: GraphNode) => Promise<void>;
  onHistoryModeChange?: (mode: GraphHistoryMode) => Promise<void>;
  payload: GraphQueryPayload;
  workspaceGraphHref?: (node: GraphNode) => string | null;
  workspaceId?: string | null;
}) {
  return (
    <MemoryRouter>
      <GraphView
        active={active}
        onCollapse={onCollapse}
        onDepthChange={onDepthChange}
        onExpand={onExpand}
        onHistoryModeChange={onHistoryModeChange ?? (() => Promise.resolve())}
        payload={payload}
        workspaceGraphHref={workspaceGraphHref}
        workspaceId={workspaceId}
      />
    </MemoryRouter>
  );
}
