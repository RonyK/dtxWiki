import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GraphRenderScene } from "../../shared/rendererTypes";
import { createLocalLayout } from "../layout";
import { buildLocalScene } from "../sceneInput";
import { createSigmaGraphAdapter, type SigmaGraphAdapter } from "../sigmaAdapter";

const state = vi.hoisted(() => ({
  sigma: null as unknown as {
    settings: Record<string, unknown>;
    events: Map<string, (...args: unknown[]) => void>;
    graph: { nodes(): string[]; getNodeAttributes(key: string): Record<string, unknown>;
      getEdgeAttributes(key: string): Record<string, unknown> };
    emit(name: string, payload?: unknown): void;
  },
  layout: {
    destroy: vi.fn(), beginDrag: vi.fn(), moveDrag: vi.fn(), endDrag: vi.fn(),
    pin: vi.fn(() => ({ x: 4, y: 8 })), unpin: vi.fn(), clearPins: vi.fn(),
    pause: vi.fn(), resume: vi.fn(), getPositions: vi.fn(() => ({})),
  },
  setCameraState: vi.fn(), animateCamera: vi.fn(),
  setCustomBBox: vi.fn(),
  refresh: vi.fn(),
  nativeSquareLabel: vi.fn(),
  nodeCanvas: null as HTMLCanvasElement | null,
  labelPass: null as ((sigma: { graph: {
    nodes(): string[]; getNodeAttributes(key: string): Record<string, unknown>;
  } }) => void) | null,
}));

vi.mock("@sigma/node-border", () => ({
  NodeBorderProgram: class {}, createNodeBorderProgram: () => class {},
}));
vi.mock("@sigma/node-square", () => ({
  NodeSquareProgram: class { drawLabel = state.nativeSquareLabel; },
}));
vi.mock("sigma/rendering", () => ({ NodeCircleProgram: class {} }));
vi.mock("../edgePrograms", () => ({ LOCAL_EDGE_PROGRAMS: {} }));
vi.mock("../layout", () => ({ createLocalLayout: vi.fn(() => state.layout) }));
vi.mock("sigma", () => ({
  default: class {
    events = new Map<string, (...args: unknown[]) => void>();
    camera = {
      on: vi.fn(), off: vi.fn(), getState: () => ({ x: .5, y: .5, ratio: 1 }),
      setState: state.setCameraState, animate: state.animateCamera,
    };
    captor = { on: vi.fn(), off: vi.fn() };
    constructor(public graph: typeof state.sigma.graph, _container: HTMLElement, public settings: Record<string, unknown>) {
      state.sigma = this;
      // Pinned Sigma synchronously renders once before its constructor returns.
      state.labelPass?.(this);
    }
    on(name: string, callback: (...args: unknown[]) => void) { this.events.set(name, callback); }
    emit(name: string, payload?: unknown) { this.events.get(name)?.(payload); }
    refresh() {
      state.refresh();
      this.emit("beforeRender");
      state.labelPass?.(this);
    }
    getCamera() { return this.camera; }
    getMouseCaptor() { return this.captor; }
    getNodeDisplayData(key: string) { return this.graph.getNodeAttributes(key); }
    graphToViewport(point: unknown) { return point; }
    scaleSize(size: number) { return size; }
    getCanvases() { return { nodes: state.nodeCanvas! }; }
    viewportToGraph(point: unknown) { return point; }
    getBBox() { return { x: [-10, 10], y: [-10, 10] }; }
    setCustomBBox = state.setCustomBBox;
    setSetting(name: string, value: unknown) { this.settings[name] = value; }
    kill() {}
  },
}));

const scene: GraphRenderScene = {
  nodes: ["seed", "neighbor", "other"].map((id) => ({
    id, entity_id: id, entity_kind: "context_page", family: "document", label: id, is_seed: id === "seed",
  })),
  edges: [{ id: "relation", source: "seed", target: "neighbor", edge_kind: "wikilink", label: "link", layer: "knowledge" }],
};

const cloneScene = (): GraphRenderScene => ({
  nodes: scene.nodes.map((node) => ({ ...node })),
  edges: scene.edges.map((edge) => ({ ...edge })),
});
const changedGeometry = (): GraphRenderScene => ({
  ...cloneScene(), edges: scene.edges.map((edge) => ({ ...edge, count: 2 })),
});

let adapter: SigmaGraphAdapter;
const callbacks = { onHover: vi.fn(), onSelect: vi.fn(), onPin: vi.fn(), onViewport: vi.fn(), onContextStatus: vi.fn() };
const event = (x = 10, y = 20) => ({ x, y, original: new MouseEvent("mousedown"), preventSigmaDefault: vi.fn() });

beforeEach(() => {
  vi.clearAllMocks();
  state.nodeCanvas = document.createElement("canvas");
  const container = document.createElement("div");
  Object.defineProperties(container, {
    clientWidth: { value: 860 }, clientHeight: { value: 640 },
  });
  adapter = createSigmaGraphAdapter(container, scene, {
    callbacks, forcedColors: false, positions: {}, reducedMotion: true,
  });
});
afterEach(() => {
  adapter.destroy();
  state.labelPass = null;
  vi.unstubAllGlobals();
});

describe("Local Sigma interactions", () => {
  it("reports WebGL loss, stops work, requests fresh restoration, and detaches on destroy", () => {
    const loss = new Event("webglcontextlost", { cancelable: true });
    state.nodeCanvas!.dispatchEvent(loss);
    expect(loss.defaultPrevented).toBe(true);
    expect(callbacks.onContextStatus).toHaveBeenLastCalledWith("lost");
    expect(state.layout.pause).toHaveBeenCalledOnce();
    state.sigma.emit("clickNode", { node: "context_page:neighbor" });
    expect(callbacks.onSelect).not.toHaveBeenCalled();
    state.nodeCanvas!.dispatchEvent(new Event("webglcontextrestored"));
    expect(callbacks.onContextStatus).toHaveBeenLastCalledWith("restored");
    expect(state.layout.resume).not.toHaveBeenCalled();
    adapter.destroy();
    callbacks.onContextStatus.mockClear();
    state.nodeCanvas!.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    expect(callbacks.onContextStatus).not.toHaveBeenCalled();
  });

  it("selects tiny nodes at the 12px screen-space boundary rather than a nearby edge", () => {
    Object.assign(state.sigma.graph.getNodeAttributes("context_page:neighbor"), { x: 100, y: 100 });
    state.sigma.emit("clickEdge", { edge: "relation", event: event(112, 100) });
    expect(callbacks.onSelect).toHaveBeenLastCalledWith("neighbor");
    state.sigma.emit("clickStage", { event: event(113, 100) });
    expect(callbacks.onSelect).toHaveBeenLastCalledWith(null);
  });

  it("chooses the closest node inside overlapping screen-space targets and permits a padded drag", () => {
    Object.assign(state.sigma.graph.getNodeAttributes("context_page:neighbor"), { x: 100, y: 100 });
    Object.assign(state.sigma.graph.getNodeAttributes("context_page:other"), { x: 106, y: 100 });
    state.sigma.emit("clickStage", { event: event(110, 100) });
    expect(callbacks.onSelect).toHaveBeenLastCalledWith("other");
    state.sigma.emit("downEdge", { edge: "relation", event: event(110, 100) });
    state.sigma.emit("moveBody", { event: event(115, 100) });
    expect(state.layout.beginDrag).toHaveBeenCalledWith("context_page:other");
    state.sigma.emit("upStage", { event: event(115, 100) });
    expect(state.layout.endDrag).toHaveBeenCalledWith("context_page:other");
  });
  it("dims outside-star nodes without hiding or removing their pick targets", () => {
    adapter.select("seed");
    const reducer = state.sigma.settings.nodeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    const data = state.sigma.graph.getNodeAttributes("context_page:other");
    const reduced = reducer("context_page:other", data);
    expect(reduced.hidden).toBe(false);
    expect(reduced.color).not.toBe(data.color);
    expect(reduced.color).not.toBe("#ffffff");
    adapter.select(null);
    expect(reducer("context_page:other", data).color).toBe(data.color);
  });

  it("keeps nonincident original relationships dimmed and restores them without layout or camera changes", () => {
    const reducer = state.sigma.settings.edgeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    const data = state.sigma.graph.getEdgeAttributes("relation");
    const layoutCount = vi.mocked(createLocalLayout).mock.calls.length;
    const positions = state.sigma.graph.nodes().map((key) => ({ ...state.sigma.graph.getNodeAttributes(key) }));
    adapter.select("other");
    expect(reducer("relation", data)).toMatchObject({ hidden: false, type: "link", size: data.size });
    expect(reducer("relation", data).color).not.toBe(data.color);
    expect(reducer("relation", data).color).not.toBe("#ffffff");
    adapter.select("seed");
    expect(reducer("relation", data)).toMatchObject({ hidden: false, color: data.color });
    adapter.focus("other");
    expect(reducer("relation", data).hidden).toBe(false);
    expect(reducer("relation", data).color).not.toBe(data.color);
    adapter.focus(null);
    adapter.select(null);
    expect(reducer("relation", data).color).toBe(data.color);
    expect(vi.mocked(createLocalLayout)).toHaveBeenCalledTimes(layoutCount);
    expect(state.setCameraState).not.toHaveBeenCalled();
    expect(state.animateCamera).not.toHaveBeenCalled();
    expect(state.sigma.graph.nodes().map((key) => ({ ...state.sigma.graph.getNodeAttributes(key) }))).toEqual(positions);
  });

  it("edge hover does not replace a selected node's neighborhood", () => {
    adapter.select("other");
    state.sigma.emit("enterEdge", { edge: "relation" });
    const reducer = state.sigma.settings.nodeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    const data = state.sigma.graph.getNodeAttributes("context_page:seed");
    expect(reducer("context_page:seed", data).color).not.toBe(data.color);
  });

  it.each([
    { hovered: "neighbor", focused: "other", bright: "other", quiet: "seed" },
    { hovered: "other", focused: "relation", bright: "seed", quiet: "other" },
  ])("new keyboard focus $focused supersedes stationary hover without changing the scene", ({ hovered, focused, bright, quiet }) => {
    adapter.select("seed");
    const layoutCount = vi.mocked(createLocalLayout).mock.calls.length;
    const positions = state.sigma.graph.nodes().map((key) => {
      const { x, y } = state.sigma.graph.getNodeAttributes(key);
      return { key, x, y };
    });
    state.sigma.emit("enterNode", { node: `context_page:${hovered}` });
    state.refresh.mockClear();
    adapter.focus(focused);
    expect(state.refresh).toHaveBeenCalledOnce();
    const reducer = state.sigma.settings.nodeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    const brightData = state.sigma.graph.getNodeAttributes(`context_page:${bright}`);
    const quietData = state.sigma.graph.getNodeAttributes(`context_page:${quiet}`);
    expect(reducer(`context_page:${bright}`, brightData).color).toBe(brightData.color);
    expect(reducer(`context_page:${quiet}`, quietData).color).not.toBe(quietData.color);
    expect(callbacks.onHover).toHaveBeenLastCalledWith(null);
    expect(callbacks.onSelect).not.toHaveBeenCalled();
    expect(callbacks.onPin).not.toHaveBeenCalled();
    expect(state.setCameraState).not.toHaveBeenCalled();
    expect(state.animateCamera).not.toHaveBeenCalled();
    expect(vi.mocked(createLocalLayout)).toHaveBeenCalledTimes(layoutCount);
    expect(state.sigma.graph.nodes().map((key) => {
      const { x, y } = state.sigma.graph.getNodeAttributes(key);
      return { key, x, y };
    })).toEqual(positions);
    adapter.focus(null);
    const seedData = state.sigma.graph.getNodeAttributes("context_page:seed");
    const otherData = state.sigma.graph.getNodeAttributes("context_page:other");
    expect(reducer("context_page:seed", seedData).color).toBe(seedData.color);
    expect(reducer("context_page:other", otherData).color).not.toBe(otherData.color);
  });

  it("retains later pointer hover across keyboard blur and ignores focus after loss/destroy", () => {
    adapter.focus("other");
    state.sigma.emit("enterNode", { node: "context_page:neighbor" });
    callbacks.onHover.mockClear();
    adapter.focus(null);
    const reducer = state.sigma.settings.nodeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    const seedData = state.sigma.graph.getNodeAttributes("context_page:seed");
    const otherData = state.sigma.graph.getNodeAttributes("context_page:other");
    expect(reducer("context_page:seed", seedData).color).toBe(seedData.color);
    expect(reducer("context_page:other", otherData).color).not.toBe(otherData.color);
    expect(callbacks.onHover).not.toHaveBeenCalled();
    state.nodeCanvas!.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    callbacks.onHover.mockClear();
    adapter.focus("other");
    expect(callbacks.onHover).not.toHaveBeenCalled();
    adapter.destroy();
    adapter.focus("other");
    expect(callbacks.onHover).not.toHaveBeenCalled();
  });

  it.each(["destroy", "context loss"])("does not refresh when keyboard hover clearing triggers %s", (transition) => {
    state.sigma.emit("enterNode", { node: "context_page:neighbor" });
    state.refresh.mockClear();
    callbacks.onHover.mockImplementationOnce(() => {
      if (transition === "destroy") adapter.destroy();
      else state.nodeCanvas!.dispatchEvent(new Event("webglcontextlost", { cancelable: true }));
    });
    adapter.focus("other");
    expect(callbacks.onHover).toHaveBeenLastCalledWith(null);
    expect(state.refresh).not.toHaveBeenCalled();
    if (transition === "context loss") {
      expect(callbacks.onContextStatus).toHaveBeenLastCalledWith("lost");
    }
  });

  it("blank-stage selection clears focus and selection without resetting pins", () => {
    adapter.select("seed");
    adapter.focus("neighbor");
    state.sigma.emit("clickStage", { event: event() });
    expect(callbacks.onSelect).toHaveBeenCalledWith(null);
    expect(state.layout.unpin).not.toHaveBeenCalled();
    const reducer = state.sigma.settings.nodeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    const data = state.sigma.graph.getNodeAttributes("context_page:other");
    expect(reducer("context_page:other", data).color).toBe(data.color);
  });

  it("a tap never starts or settles the force simulation", () => {
    state.sigma.emit("downNode", { node: "context_page:neighbor", event: event() });
    state.sigma.emit("upStage", { event: event() });
    state.sigma.emit("clickNode", { node: "context_page:neighbor" });
    expect(callbacks.onSelect).toHaveBeenCalledWith("neighbor");
    expect(state.layout.beginDrag).not.toHaveBeenCalled();
    expect(state.layout.endDrag).not.toHaveBeenCalled();
    expect(callbacks.onPin).not.toHaveBeenCalled();
  });

  it("starts a drag only after movement and terminates on any up target", () => {
    state.sigma.emit("downNode", { node: "context_page:neighbor", event: event() });
    state.sigma.emit("moveBody", { event: event(11, 21) });
    expect(state.layout.beginDrag).not.toHaveBeenCalled();
    state.sigma.emit("moveBody", { event: event(20, 30) });
    expect(state.layout.beginDrag).toHaveBeenCalledOnce();
    state.sigma.emit("upEdge", { edge: "relation", event: event(20, 30) });
    expect(state.layout.endDrag).toHaveBeenCalledWith("context_page:neighbor");
    expect(callbacks.onPin).toHaveBeenCalledWith("neighbor", { x: 4, y: 8 });
    state.sigma.emit("moveBody", { event: event(40, 50) });
    expect(state.layout.moveDrag).toHaveBeenCalledOnce();
    state.sigma.emit("clickNode", { node: "context_page:neighbor" });
    expect(callbacks.onSelect).not.toHaveBeenCalled();
  });

  it("cancellation releases a drag without creating a persistent pin", () => {
    state.sigma.emit("downNode", { node: "context_page:neighbor", event: event() });
    state.sigma.emit("moveBody", { event: event(20, 30) });
    window.dispatchEvent(new Event("pointercancel"));
    expect(state.layout.endDrag).toHaveBeenCalledWith("context_page:neighbor");
    expect(callbacks.onPin).not.toHaveBeenCalled();
    state.sigma.emit("moveBody", { event: event(50, 60) });
    expect(state.layout.moveDrag).toHaveBeenCalledOnce();
  });

  it("handles rapid clicks classified as doubles on different nodes or the stage", () => {
    state.sigma.emit("clickNode", { node: "context_page:neighbor" });
    state.sigma.emit("doubleClickNode", { node: "context_page:other", event: event() });
    expect(callbacks.onSelect).toHaveBeenLastCalledWith("other");
    const stage = event();
    state.sigma.emit("doubleClickStage", { event: stage });
    expect(callbacks.onSelect).toHaveBeenLastCalledWith(null);
    expect(stage.preventSigmaDefault).toHaveBeenCalledOnce();
  });

  it("supports mouse dragging in browsers without a TouchEvent constructor", () => {
    vi.stubGlobal("TouchEvent", undefined);
    state.sigma.emit("downNode", { node: "context_page:neighbor", event: event() });
    state.sigma.emit("moveBody", { event: event(20, 30) });
    state.sigma.emit("upStage", { event: event(20, 30) });
    expect(state.layout.endDrag).toHaveBeenCalledWith("context_page:neighbor");
  });

  it("reset releases pins in one layout operation, not once per node", () => {
    adapter.reset();
    expect(state.layout.clearPins).toHaveBeenCalledOnce();
    expect(state.layout.unpin).not.toHaveBeenCalled();
  });

  it.each(["unpin", "reset"])(
    "does not restore a released construction-time pin after %s and scene replacement", (release) => {
      adapter.destroy();
      const positions = { neighbor: { x: 123, y: 456 } };
      adapter = createSigmaGraphAdapter(document.createElement("div"), scene, {
        callbacks, forcedColors: false, positions, reducedMotion: true,
      });
      expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
        .toEqual({ "context_page:neighbor": positions.neighbor });
      if (release === "unpin") adapter.pin("neighbor", false);
      else adapter.reset();
      const creates = vi.mocked(createLocalLayout).mock.calls.length;
      adapter.replaceScene(changedGeometry());
      expect(createLocalLayout).toHaveBeenCalledTimes(creates + 1);
      expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
        .not.toHaveProperty("context_page:neighbor");
      // Renderer bookkeeping must not mutate the caller's restore snapshot.
      expect(positions).toEqual({ neighbor: { x: 123, y: 456 } });
    },
  );

  it("retains a current pin at its latest position across scene replacements", () => {
    state.layout.pin.mockReturnValueOnce({ x: 17, y: 23 });
    adapter.pin("neighbor", true, { x: 17, y: 23 });
    adapter.replaceScene(changedGeometry());
    expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
      .toEqual({ "context_page:neighbor": { x: 17, y: 23 } });
    state.layout.pin.mockReturnValueOnce({ x: 31, y: 37 });
    adapter.pin("neighbor", true, { x: 31, y: 37 });
    const creates = vi.mocked(createLocalLayout).mock.calls.length;
    adapter.replaceScene(scene);
    expect(createLocalLayout).toHaveBeenCalledTimes(creates + 1);
    expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
      .toEqual({ "context_page:neighbor": { x: 31, y: 37 } });
  });

  it("forgets an explicitly unpinned node even when it is absent from the current scene", () => {
    adapter.destroy();
    adapter = createSigmaGraphAdapter(document.createElement("div"), scene, {
      callbacks, forcedColors: false, positions: { neighbor: { x: 17, y: 23 } }, reducedMotion: true,
    });
    adapter.replaceScene({ ...scene, nodes: scene.nodes.filter((node) => node.id !== "neighbor"), edges: [] });
    adapter.pin("neighbor", false);
    adapter.replaceScene(scene);
    expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
      .not.toHaveProperty("context_page:neighbor");
  });

  it("replaces released coordinates without losing other still-pinned nodes", () => {
    adapter.destroy();
    adapter = createSigmaGraphAdapter(document.createElement("div"), scene, {
      callbacks, forcedColors: false, positions: {
        neighbor: { x: 17, y: 23 }, other: { x: 31, y: 37 },
      }, reducedMotion: true,
    });
    adapter.pin("neighbor", false);
    adapter.replaceScene(changedGeometry());
    expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
      .toEqual({ "context_page:other": { x: 31, y: 37 } });
  });

  it("remembers the final persistent pointer-drag pin when replacing the scene", () => {
    state.sigma.emit("downNode", { node: "context_page:neighbor", event: event() });
    state.sigma.emit("moveBody", { event: event(20, 35) });
    state.layout.pin.mockReturnValueOnce({ x: 17, y: 23 });
    state.sigma.emit("upStage", { event: event(20, 35) });
    adapter.replaceScene(changedGeometry());
    expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
      .toEqual({ "context_page:neighbor": { x: 17, y: 23 } });
  });

  it("centers reduced-motion scenes immediately without a zero-duration animation", () => {
    adapter.centerOn("neighbor");
    expect(state.setCameraState).toHaveBeenCalledWith({ x: 0, y: 0 });
    expect(state.animateCamera).not.toHaveBeenCalled();
  });

  it("keeps seed and active labels when ordinary labels are disabled", () => {
    adapter.setLabelsVisible(false);
    expect(state.sigma.settings.renderLabels).toBe(true);
    adapter.select("neighbor");
    const reducer = state.sigma.settings.nodeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    for (const key of ["context_page:seed", "context_page:neighbor"]) {
      const data = state.sigma.graph.getNodeAttributes(key);
      expect(reducer(key, data).label).toBe(data.label);
    }
    const other = state.sigma.graph.getNodeAttributes("context_page:other");
    expect(reducer("context_page:other", other).label).toBeNull();
  });

  it("separates nearby forced labels instead of painting seed and selected text over one another", () => {
    const context = {
      save: vi.fn(), restore: vi.fn(), strokeText: vi.fn(), fillText: vi.fn(),
      measureText: () => ({ width: 100 }),
    };
    const draw = state.sigma.settings.defaultDrawNodeLabel as (
      context: unknown, data: { label: string; size: number; x: number; y: number; forceLabel: boolean },
      settings: { labelWeight: string; labelSize: number; labelFont: string },
    ) => void;
    const settings = { labelWeight: "normal", labelSize: 12, labelFont: "sans-serif" };
    const point = { size: 5, x: 300, y: 200, forceLabel: true };
    draw(context, { ...point, label: "Current Context" }, settings);
    draw(context, { ...point, label: "Selected neighbor" }, settings);
    expect(context.fillText).toHaveBeenCalledTimes(2);
    const lines = context.fillText.mock.calls.map((call) => Number(call[2]));
    expect(Math.abs(lines[0] - lines[1])).toBeGreaterThanOrEqual(16);
  });

  const labelContext = () => ({
    save: vi.fn(), restore: vi.fn(), strokeText: vi.fn(), fillText: vi.fn(),
    measureText: (text: string) => ({ width: Array.from(text).length * 6 }),
  });
  const labelSettings = { labelWeight: "normal", labelSize: 12, labelFont: "sans-serif" };
  const drawNodeLabel = (context: ReturnType<typeof labelContext>, key: string) => {
    const reducer = state.sigma.settings.nodeReducer as (
      key: string, data: Record<string, unknown>,
    ) => Record<string, unknown>;
    const draw = state.sigma.settings.defaultDrawNodeLabel as (
      context: unknown, data: Record<string, unknown>, settings: typeof labelSettings,
    ) => void;
    draw(context, { key, ...reducer(key, state.sigma.graph.getNodeAttributes(key)) }, labelSettings);
  };

  it("draws the visible seed before seven ordinary labels even when Sigma calls them first", () => {
    adapter.replaceScene({ nodes: ["seed", ...Array.from({ length: 7 }, (_, i) => `blocker${i}`)].map((id) => ({
      id, entity_id: id, entity_kind: "context_page", family: "document", label: id, is_seed: id === "seed",
    })), edges: [] });
    Object.assign(state.sigma.graph.getNodeAttributes("context_page:seed"), { x: 300, y: 200, size: 5 });
    const context = labelContext();
    state.sigma.emit("beforeRender");
    for (let i = 0; i < 7; i++) {
      const key = `context_page:blocker${i}`;
      Object.assign(state.sigma.graph.getNodeAttributes(key), { x: 300, y: 146 + i * 18, size: 5 });
      drawNodeLabel(context, key);
    }
    drawNodeLabel(context, "context_page:seed");
    const titles = context.fillText.mock.calls.map((call) => call[0]);
    expect(titles[0]).toBe("seed");
    expect(titles.filter((title) => title === "seed")).toHaveLength(1);
    const boxes = context.fillText.mock.calls.map((call) => ({
      x: Number(call[1]), y: Number(call[2]), width: Array.from(String(call[0])).length * 6,
    }));
    for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
      expect(Math.abs(boxes[i].y - boxes[j].y) >= 16
        || boxes[i].x + boxes[i].width + 4 <= boxes[j].x
        || boxes[j].x + boxes[j].width + 4 <= boxes[i].x).toBe(true);
    }
  });

  it("keeps selection and hover labels forced simultaneously when ordinary labels are off", () => {
    adapter.setLabelsVisible(false);
    adapter.select("neighbor");
    state.sigma.emit("enterNode", { node: "context_page:other" });
    const reducer = state.sigma.settings.nodeReducer as (
      key: string, data: Record<string, unknown>,
    ) => Record<string, unknown>;
    for (const id of ["seed", "neighbor", "other"]) {
      const key = `context_page:${id}`;
      Object.assign(state.sigma.graph.getNodeAttributes(key), { x: 300, y: 200, size: 5 });
      expect(reducer(key, state.sigma.graph.getNodeAttributes(key))).toMatchObject({ label: id, forceLabel: true });
    }
    const context = labelContext();
    state.sigma.emit("beforeRender");
    drawNodeLabel(context, "context_page:other");
    expect(context.fillText.mock.calls.map((call) => call[0])).toEqual(["seed", "neighbor", "other"]);
    adapter.select(null);
    state.sigma.emit("leaveNode");
    state.sigma.emit("beforeRender");
    context.fillText.mockClear();
    drawNodeLabel(context, "context_page:seed");
    expect(context.fillText.mock.calls.map((call) => call[0])).toEqual(["seed"]);
  });

  it("finds bounded extra rows for more than seven crowded priority labels without overlaps", () => {
    adapter.replaceScene({ nodes: Array.from({ length: 10 }, (_, i) => ({
      id: `seed${i}`, entity_id: `seed${i}`, entity_kind: "context_page", family: "document",
      label: `seed${i}`, is_seed: true,
    })), edges: [] });
    const context = labelContext();
    for (const key of state.sigma.graph.nodes()) {
      Object.assign(state.sigma.graph.getNodeAttributes(key), { x: 300, y: 200, size: 5 });
    }
    state.sigma.emit("beforeRender");
    // Deliberately reverse Sigma's callback order: priority order is ours.
    for (const key of [...state.sigma.graph.nodes()].reverse()) drawNodeLabel(context, key);
    expect(context.fillText.mock.calls.map((call) => call[0])).toEqual(
      Array.from({ length: 10 }, (_, i) => `seed${i}`),
    );
    const rows = context.fillText.mock.calls.map((call) => Number(call[2]));
    for (let i = 0; i < rows.length; i++) for (let j = i + 1; j < rows.length; j++) {
      expect(Math.abs(rows[i] - rows[j])).toBeGreaterThanOrEqual(16);
    }
    expect(rows.every((y) => y >= 8 && y <= 632)).toBe(true);
  });

  it("uses a bounded in-viewport last resort instead of silently dropping crowded priority titles", () => {
    adapter.destroy();
    const container = document.createElement("div");
    Object.defineProperties(container, { clientWidth: { value: 48 }, clientHeight: { value: 20 } });
    adapter = createSigmaGraphAdapter(container, scene, {
      callbacks, forcedColors: false, positions: {}, reducedMotion: true, showLabels: false,
    });
    adapter.select("neighbor");
    state.sigma.emit("enterNode", { node: "context_page:other" });
    for (const key of state.sigma.graph.nodes()) {
      Object.assign(state.sigma.graph.getNodeAttributes(key), { x: 24, y: 10, size: 5 });
    }
    const context = labelContext();
    state.sigma.emit("beforeRender");
    drawNodeLabel(context, "context_page:other");
    expect(context.fillText).toHaveBeenCalledTimes(3);
    const rows = context.fillText.mock.calls.map((call) => Number(call[2]));
    expect(new Set(rows).size).toBeLessThan(rows.length); // Explicit capacity-limited overlap.
    for (const call of context.fillText.mock.calls) {
      expect(Number(call[1])).toBeGreaterThanOrEqual(0);
      expect(Number(call[1])).toBeLessThanOrEqual(48);
      expect(Number(call[2])).toBeGreaterThanOrEqual(0);
      expect(Number(call[2])).toBeLessThanOrEqual(20);
      expect(String(call[0]).length).toBeGreaterThan(0);
      expect(Number(call[3])).toBeLessThanOrEqual(48);
    }
  });

  it("does not let Sigma's synchronous constructor paint contaminate the first owned label pass", () => {
    adapter.destroy();
    const context = labelContext();
    state.labelPass = (sigma) => {
      for (const key of sigma.graph.nodes()) {
        Object.assign(sigma.graph.getNodeAttributes(key), { x: 300, y: 200, size: 5 });
      }
      // The ordinary label arrives first in both constructor and owned passes.
      drawNodeLabel(context, "context_page:other");
      drawNodeLabel(context, "context_page:seed");
    };
    const container = document.createElement("div");
    Object.defineProperties(container, { clientWidth: { value: 860 }, clientHeight: { value: 640 } });
    adapter = createSigmaGraphAdapter(container, scene, {
      callbacks, forcedColors: false, positions: {}, reducedMotion: true,
    });
    expect(context.fillText.mock.calls.map((call) => call[0])).toEqual(["seed", "other"]);
  });

  it("clears priority titles between frames and does not inherit the old scene's seed", () => {
    for (const key of state.sigma.graph.nodes()) {
      Object.assign(state.sigma.graph.getNodeAttributes(key), { x: 300, y: 200, size: 5 });
    }
    const context = labelContext();
    state.sigma.emit("beforeRender");
    drawNodeLabel(context, "context_page:other");
    expect(context.fillText.mock.calls[0][0]).toBe("seed");
    adapter.replaceScene({ nodes: [{
      id: "fresh", entity_id: "fresh", entity_kind: "context_page", family: "document", label: "Fresh title", is_seed: true,
    }], edges: [] });
    Object.assign(state.sigma.graph.getNodeAttributes("context_page:fresh"), { x: 300, y: 200, size: 5 });
    context.fillText.mockClear();
    state.sigma.emit("beforeRender");
    drawNodeLabel(context, "context_page:fresh");
    expect(context.fillText.mock.calls.map((call) => call[0])).toEqual(["Fresh title"]);
    adapter.replaceScene({ nodes: [{
      id: "fresh", entity_id: "fresh", entity_kind: "context_page", family: "document", label: "Updated title", is_seed: true,
    }], edges: [] });
    context.fillText.mockClear();
    state.sigma.emit("beforeRender");
    drawNodeLabel(context, "context_page:fresh");
    expect(context.fillText.mock.calls.map((call) => call[0])).toEqual(["Updated title"]);
  });

  it.each([{ width: 0, height: 640 }, { width: 860, height: 0 }])(
    "does not allocate a visible title in a zero-area viewport $width x $height", ({ width, height }) => {
      adapter.destroy();
      const container = document.createElement("div");
      Object.defineProperties(container, { clientWidth: { value: width }, clientHeight: { value: height } });
      adapter = createSigmaGraphAdapter(container, scene, {
        callbacks, forcedColors: false, positions: {}, reducedMotion: true,
      });
      for (const key of state.sigma.graph.nodes()) {
        Object.assign(state.sigma.graph.getNodeAttributes(key), { x: 0, y: 0, size: 5 });
      }
      const context = labelContext();
      state.sigma.emit("beforeRender");
      drawNodeLabel(context, "context_page:seed");
      expect(context.fillText).not.toHaveBeenCalled();
    },
  );

  it("does not pull offscreen or hidden priority titles into the viewport", () => {
    Object.assign(state.sigma.graph.getNodeAttributes("context_page:seed"), { x: -1, y: 200, size: 5 });
    Object.assign(state.sigma.graph.getNodeAttributes("context_page:neighbor"), { x: 300, y: 200, size: 5, hidden: true });
    Object.assign(state.sigma.graph.getNodeAttributes("context_page:other"), { x: 300, y: 200, size: 5 });
    adapter.select("neighbor");
    const context = labelContext();
    state.sigma.emit("beforeRender");
    // Callback belongs to the ordinary grid; hidden priorities are not painted.
    drawNodeLabel(context, "context_page:other");
    expect(context.fillText.mock.calls.map((call) => call[0])).not.toContain("seed");
    expect(context.fillText.mock.calls.map((call) => call[0])).not.toContain("neighbor");
  });

  it("routes Library square labels through the same boundary and collision-aware drawer", () => {
    const programs = state.sigma.settings.nodeProgramClasses as Record<string, new () => {
      drawLabel: (...args: unknown[]) => void;
    }>;
    const square = new programs.square();
    const draw = vi.fn();
    const context = {};
    const data = { label: "Library source" };
    const settings = { defaultDrawNodeLabel: draw };
    square.drawLabel(context, data, settings);
    expect(draw).toHaveBeenCalledWith(context, data, settings);
    expect(state.nativeSquareLabel).not.toHaveBeenCalled();
  });

  it.each(["library_item_version", "library_artifact", "library_item"])(
    "renders actual %s Library nodes as teal squares", (kind) => {
      adapter.replaceScene({ nodes: [{
        id: "source", entity_id: "source", entity_kind: kind, family: "document", label: "Library source",
      }], edges: [] });
      expect(state.sigma.graph.getNodeAttributes(`${kind}:source`)).toMatchObject({
        color: "#0f9f8f", type: "square",
      });
    },
  );

  it("renders Context versions as Context points and structural folders as hollow rings", () => {
    adapter.replaceScene({ nodes: [
      { id: "version", entity_id: "version", entity_kind: "context_page_version", family: "document", label: "Version" },
      { id: "folder", entity_id: "folder", entity_kind: "workspace_tree_node", family: "structural", label: "Folder" },
    ], edges: [] });
    expect(state.sigma.graph.getNodeAttributes("context_page_version:version")).toMatchObject({
      color: "#7c3aed", type: "circle",
    });
    expect(state.sigma.graph.getNodeAttributes("workspace_tree_node:folder").type).toBe("folder");
  });

  it("keeps non-color shape distinctions for Library, conversations and folders in forced colors", () => {
    adapter.destroy();
    adapter = createSigmaGraphAdapter(document.createElement("div"), { nodes: [
      { id: "context", entity_id: "context", entity_kind: "context_page", family: "document", label: "Context" },
      { id: "library", entity_id: "library", entity_kind: "library_artifact", family: "document", label: "Source" },
      { id: "conversation", entity_id: "conversation", entity_kind: "mcp_chat_session", family: "chat", label: "Conversation" },
      { id: "folder", entity_id: "folder", entity_kind: "workspace_tree_node", family: "structural", label: "Folder" },
    ], edges: [] }, { callbacks, forcedColors: true, positions: {}, reducedMotion: true });
    const types = state.sigma.graph.nodes().map((key) => state.sigma.graph.getNodeAttributes(key).type);
    expect(new Set(types).size).toBe(4);
  });
});

describe("Local scene refresh boundary", () => {
  const positions = () => state.sigma.graph.nodes().map((key) => {
    const { x, y } = state.sigma.graph.getNodeAttributes(key);
    return { key, x, y };
  });
  const colors = () => {
    const reducer = state.sigma.settings.nodeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    return state.sigma.graph.nodes().map((key) => reducer(key, state.sigma.graph.getNodeAttributes(key)).color);
  };

  it.each([
    { reducedMotion: true, paused: false }, { reducedMotion: true, paused: true },
    { reducedMotion: false, paused: false }, { reducedMotion: false, paused: true },
  ])("retains live geometry and interaction state on metadata refresh ($reducedMotion motion, $paused paused)", ({ reducedMotion, paused }) => {
    adapter.destroy();
    adapter = createSigmaGraphAdapter(document.createElement("div"), scene, {
      callbacks, forcedColors: false, positions: {}, reducedMotion,
    });
    state.sigma.graph.nodes().forEach((key, index) => {
      Object.assign(state.sigma.graph.getNodeAttributes(key), { x: 94 + index * 50, y: 119 - index * 70 });
    });
    state.layout.pin.mockReturnValueOnce({ x: 94, y: 119 });
    adapter.pin("neighbor", true, { x: 94, y: 119 });
    adapter.select("seed");
    adapter.focus("other");
    state.sigma.emit("enterNode", { node: "context_page:neighbor" });
    if (paused) adapter.stopLayout();
    const before = positions();
    const activeColors = colors();
    vi.clearAllMocks();

    const next = cloneScene();
    next.nodes.reverse();
    next.edges.reverse();
    next.nodes = next.nodes.map((node) => ({ ...node, label: `New ${node.label}`, status: "draft" }));
    next.edges = next.edges.map((edge) => ({ ...edge, label: "Updated relationship" }));
    adapter.replaceScene(next);

    expect(createLocalLayout).not.toHaveBeenCalled();
    expect(state.layout.destroy).not.toHaveBeenCalled();
    expect(state.layout.pause).not.toHaveBeenCalled();
    expect(state.layout.resume).not.toHaveBeenCalled();
    expect(state.layout.getPositions).not.toHaveBeenCalled();
    expect(state.layout.clearPins).not.toHaveBeenCalled();
    expect(state.layout.unpin).not.toHaveBeenCalled();
    expect(state.setCustomBBox).not.toHaveBeenCalled();
    expect(state.setCameraState).not.toHaveBeenCalled();
    expect(state.animateCamera).not.toHaveBeenCalled();
    expect(callbacks.onHover).not.toHaveBeenCalled();
    expect(callbacks.onPin).not.toHaveBeenCalled();
    expect(callbacks.onSelect).not.toHaveBeenCalled();
    expect(positions()).toEqual(before);
    expect(colors()).toEqual(activeColors);
    expect(state.sigma.graph.getNodeAttributes("context_page:neighbor").label).toBe("New neighbor");
    expect(state.sigma.graph.getEdgeAttributes("relation").label).toBe("Updated relationship");
    expect(state.refresh).toHaveBeenCalled();

    state.sigma.emit("leaveNode");
    expect(colors()).not.toEqual(activeColors);
    adapter.focus(null);
    const reducer = state.sigma.settings.nodeReducer as (key: string, data: Record<string, unknown>) => Record<string, unknown>;
    const seed = state.sigma.graph.getNodeAttributes("context_page:seed");
    expect(reducer("context_page:seed", seed).color).toBe(seed.color);
    adapter.replaceScene(changedGeometry());
    expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
      .toEqual({ "context_page:neighbor": { x: 94, y: 119 } });
  });

  it("updates family-derived presentation without replacing geometry", () => {
    const initial = cloneScene();
    initial.nodes[1] = { ...initial.nodes[1], entity_kind: "mcp_chat_session", family: "chat" };
    adapter.destroy();
    adapter = createSigmaGraphAdapter(document.createElement("div"), initial, {
      callbacks, forcedColors: false, positions: {}, reducedMotion: true,
    });
    const originalAttributes = { ...state.sigma.graph.getNodeAttributes("mcp_chat_session:neighbor") };
    const next = { ...initial, nodes: initial.nodes.map((node) => ({ ...node })) };
    next.nodes[1].family = "structural";
    next.nodes[1].label = "Updated folder presentation";
    vi.clearAllMocks();
    adapter.replaceScene(next);
    expect(createLocalLayout).not.toHaveBeenCalled();
    expect(state.layout.destroy).not.toHaveBeenCalled();
    expect(state.sigma.graph.getNodeAttributes("mcp_chat_session:neighbor"))
      .toMatchObject({ x: originalAttributes.x, y: originalAttributes.y, type: "folder", label: "Updated folder presentation" });
    expect(state.sigma.graph.getNodeAttributes("mcp_chat_session:neighbor").color).not.toBe(originalAttributes.color);
  });

  it("keeps an active pointer drag and normalization box through metadata refresh", () => {
    state.sigma.emit("downNode", { node: "context_page:neighbor", event: event() });
    state.sigma.emit("moveBody", { event: event(20, 30) });
    expect(state.layout.beginDrag).toHaveBeenCalledOnce();
    vi.clearAllMocks();
    const next = cloneScene();
    next.nodes[1].label = "Changed while dragging";
    adapter.replaceScene(next);
    expect(createLocalLayout).not.toHaveBeenCalled();
    expect(state.layout.endDrag).not.toHaveBeenCalled();
    expect(state.layout.destroy).not.toHaveBeenCalled();
    expect(state.setCustomBBox).not.toHaveBeenCalled();
    expect(callbacks.onPin).not.toHaveBeenCalled();
    state.sigma.emit("moveBody", { event: event(30, 45) });
    expect(state.layout.moveDrag).toHaveBeenCalledWith("context_page:neighbor", { x: 30, y: 45 });
    state.layout.pin.mockReturnValueOnce({ x: 30, y: 45 });
    state.sigma.emit("upStage", { event: event(30, 45) });
    expect(state.layout.endDrag).toHaveBeenCalledOnce();
    expect(callbacks.onPin).toHaveBeenCalledWith("neighbor", { x: 30, y: 45 });
  });

  it("retains live drag position q rather than replaying an already-pinned saved position p", () => {
    const saved = { x: 70, y: 80 };
    const live = { x: 130, y: -45 };
    adapter.destroy();
    adapter = createSigmaGraphAdapter(document.createElement("div"), scene, {
      callbacks, forcedColors: false, positions: { neighbor: saved }, reducedMotion: true,
    });
    const onTick = vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.onTick;
    state.layout.pin.mockReturnValueOnce(saved);
    adapter.pin("neighbor", true, saved);
    onTick?.({ "context_page:neighbor": saved });
    state.sigma.emit("downNode", { node: "context_page:neighbor", event: event(saved.x, saved.y) });
    state.sigma.emit("moveBody", { event: event(live.x, live.y) });
    onTick?.({ "context_page:neighbor": live });
    const before = positions();
    vi.clearAllMocks();
    const next = cloneScene();
    next.nodes[1].label = "Metadata during a pinned drag";
    expect(adapter.replaceScene(next)).toBe("metadata");
    expect(positions()).toEqual(before);
    expect(state.sigma.graph.getNodeAttributes("context_page:neighbor")).toMatchObject(live);
    expect(createLocalLayout).not.toHaveBeenCalled();
    expect(state.layout.destroy).not.toHaveBeenCalled();
    expect(state.layout.pin).not.toHaveBeenCalled();
    expect(state.layout.endDrag).not.toHaveBeenCalled();
    expect(state.setCustomBBox).not.toHaveBeenCalled();
    expect(callbacks.onPin).not.toHaveBeenCalled();
    state.layout.pin.mockReturnValueOnce(live);
    state.sigma.emit("upStage", { event: event(live.x, live.y) });
    expect(state.layout.endDrag).toHaveBeenCalledWith("context_page:neighbor");
    expect(callbacks.onPin).toHaveBeenCalledWith("neighbor", live);
    adapter.replaceScene(changedGeometry());
    expect(vi.mocked(createLocalLayout).mock.calls.at(-1)?.[1]?.fixedPositions)
      .toEqual({ "context_page:neighbor": live });
    expect(saved).toEqual({ x: 70, y: 80 });
  });

  it.each([
    ["node wire ID", (next: GraphRenderScene) => {
      next.nodes[1].id = "new-neighbor";
      next.edges[0].target = "new-neighbor";
    }],
    ["edge wire ID", (next: GraphRenderScene) => { next.edges[0].id = "new-relation"; }],
    ["semantic node identity", (next: GraphRenderScene) => { next.nodes[1].entity_id = "new-neighbor"; }],
    ["endpoint", (next: GraphRenderScene) => { next.edges[0].target = "other"; }],
    ["kind", (next: GraphRenderScene) => { next.edges[0].edge_kind = "tree"; }],
    ["direction", (next: GraphRenderScene) => { next.edges[0].direction = "undirected"; }],
    ["count and derived radius", (next: GraphRenderScene) => { next.edges[0].count = 2; }],
    ["seed", (next: GraphRenderScene) => { next.nodes[1].is_seed = true; }],
    ["visible membership", (next: GraphRenderScene) => { next.nodes = next.nodes.filter(({ id }) => id !== "other"); }],
  ] as const)("rebuilds only current visible input after a changed %s", (_name, mutate) => {
    const next = cloneScene();
    mutate(next);
    next.edges.push({ ...next.edges[0], id: "hidden-decoy", target: "hidden" });
    vi.clearAllMocks();
    adapter.replaceScene(next);
    expect(state.layout.destroy).toHaveBeenCalledOnce();
    expect(createLocalLayout).toHaveBeenCalledExactlyOnceWith(buildLocalScene(next).layout, expect.any(Object));
    expect(vi.mocked(createLocalLayout).mock.calls[0]?.[1]).not.toHaveProperty("cachedPositions");
    expect(state.sigma.graph.nodes()).not.toContain("context_page:hidden");
  });

  it("rebuilds equal wire geometry when its client selection scope changes", () => {
    adapter.destroy();
    const first = { ...cloneScene(), scopeKey: "workspace:actor:selection-a" };
    adapter = createSigmaGraphAdapter(document.createElement("div"), first, {
      callbacks, forcedColors: false, positions: {}, reducedMotion: true,
    });
    vi.clearAllMocks();
    adapter.replaceScene({ ...first, scopeKey: "workspace:actor:selection-b" });
    expect(state.layout.destroy).toHaveBeenCalledOnce();
    expect(createLocalLayout).toHaveBeenCalledExactlyOnceWith(buildLocalScene(first).layout, expect.any(Object));
  });

  it("rebuilds ambiguous semantic aliases rather than retaining an unsafe layout", () => {
    const aliases = cloneScene();
    aliases.nodes.push({ ...aliases.nodes[1], id: "alias-neighbor" });
    adapter.replaceScene(aliases);
    vi.clearAllMocks();
    adapter.replaceScene({ ...aliases, nodes: aliases.nodes.map((node) => ({ ...node, label: `New ${node.label}` })) });
    expect(state.layout.destroy).toHaveBeenCalledOnce();
    expect(createLocalLayout).toHaveBeenCalledOnce();
  });
});
