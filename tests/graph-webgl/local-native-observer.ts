import { NativeGraph } from "../../frontend/src/features/graph/workspace/cosmosNative";
import { buildLocalScene, type LocalScene } from "../../frontend/src/features/graph/local/sceneInput";
import type { GraphRenderScene } from "../../frontend/src/features/graph/shared/rendererTypes";

type Point = { x: number; y: number };
type LabelDraw = { text: string; x: number; y: number };
type NativeInternals = { canvas?: HTMLCanvasElement };

/** Read the actual shipped SDK and label paint. This fixture never drives input. */
export function observeLocalNative(
  getScene: () => GraphRenderScene,
  onCreated?: () => void,
) {
  const renderers: LocalNativeObservation[] = [];
  const byGraph = new WeakMap<NativeGraph, LocalNativeObservation>();
  const draws = new WeakMap<HTMLCanvasElement, LabelDraw[]>();
  const nativeFillText = CanvasRenderingContext2D.prototype.fillText;
  const nativeClearRect = CanvasRenderingContext2D.prototype.clearRect;
  CanvasRenderingContext2D.prototype.fillText = function (...args) {
    if (this.canvas.matches("[data-local-graph-labels]")) {
      const current = draws.get(this.canvas) ?? [];
      current.push({ text: args[0], x: args[1], y: args[2] });
      draws.set(this.canvas, current);
    }
    return nativeFillText.apply(this, args);
  };
  CanvasRenderingContext2D.prototype.clearRect = function (...args) {
    if (this.canvas.matches("[data-local-graph-labels]")) draws.set(this.canvas, []);
    return nativeClearRect.apply(this, args);
  };
  const nativeRender = NativeGraph.prototype.render;
  NativeGraph.prototype.render = function (...args) {
    let observed = byGraph.get(this);
    if (!observed) {
      observed = new LocalNativeObservation(this, getScene, draws);
      byGraph.set(this, observed);
      renderers.push(observed);
      onCreated?.();
    }
    observed.observePointer();
    return nativeRender.apply(this, args);
  };
  return {
    renderers,
    current: () => {
      const value = renderers.at(-1);
      if (!value) throw new Error("Local Cosmos renderer has not painted");
      return value;
    },
  };
}

export class LocalNativeObservation {
  hoveredKey: string | null = null;
  readonly picks: Array<{ kind: "node" | "edge" | "stage"; id: string | null }> = [];
  private observedOver: unknown;
  private observedOut: unknown;
  private observedClick: unknown;
  private observedLinkClick: unknown;
  private observedStageClick: unknown;
  constructor(
    readonly native: NativeGraph,
    private readonly sceneSource: () => GraphRenderScene,
    private readonly draws: WeakMap<HTMLCanvasElement, LabelDraw[]>,
  ) {}
  private scene(): LocalScene { return buildLocalScene(this.sceneSource()); }
  getContainer(): HTMLElement { return this.getCanvases().nodes.parentElement!; }
  getCanvases() {
    const nodes = (this.native as unknown as NativeInternals).canvas;
    if (!nodes) throw new Error("Local native canvas is unavailable");
    const labels = nodes.parentElement!.querySelector<HTMLCanvasElement>("[data-local-graph-labels]");
    if (!labels) throw new Error("Local label canvas is unavailable");
    return { nodes, edges: nodes, labels };
  }
  private scale(): number { return this.native.config.spaceSize / 4096; }
  private point(index: number): Point {
    const positions = this.native.getPointPositions();
    return { x: positions[index * 2] / this.scale(), y: positions[index * 2 + 1] / this.scale() };
  }
  labelDraws(): LabelDraw[] { return [...(this.draws.get(this.getCanvases().labels) ?? [])]; }
  getNodeDisplayedLabels(): Set<string> {
    const painted = new Set(this.labelDraws().map(draw => draw.text));
    return new Set(this.scene().nodes.filter(node => painted.has(node.label)
      || [...painted].some(label => label.endsWith("…") && node.label.startsWith(label.slice(0, -1))))
      .map(node => node.key));
  }
  getCamera() {
    return {
      getState: () => {
        const canvas = this.getCanvases().nodes;
        const center = this.native.screenToSpacePosition([canvas.clientWidth / 2, canvas.clientHeight / 2]);
        return { x: center[0] / this.scale(), y: center[1] / this.scale(), ratio: 1 / (this.native.getZoomLevel() * this.scale()), angle: 0 };
      },
      // Native camera animations are d3 transitions attached to the canvas.
      isAnimated: () => {
        const transitions = (this.getCanvases().nodes as HTMLCanvasElement & { __transition?: Record<string, unknown> }).__transition;
        return Boolean(transitions && Object.keys(transitions).length);
      },
    };
  }
  graphToViewport(point: Point): Point {
    const screen = this.native.spaceToScreenPosition([point.x * this.scale(), point.y * this.scale()]);
    return { x: screen[0], y: screen[1] };
  }
  framedGraphToViewport(point: Point): Point { return this.graphToViewport(point); }
  private color(values: Float32Array, index: number, dim: number): string {
    const rgb = [0, 1, 2].map(channel => Math.round((values[index * 4 + channel] ?? 0) * 255));
    if (dim === 1) return `#${rgb.map(value => value.toString(16).padStart(2, "0")).join("")}`;
    return `rgba(${rgb.join(",")},${(values[index * 4 + 3] ?? 1) * dim})`;
  }
  getNodeDisplayData(key: string) {
    const scene = this.scene();
    const index = scene.nodes.findIndex(node => node.key === key);
    if (index < 0 || index >= this.native.getPointPositions().length / 2) return undefined;
    const highlighted = this.native.config.highlightedPointIndices;
    const dim = highlighted && !Array.from(highlighted).includes(index) ? this.native.config.pointGreyoutOpacity ?? .1 : 1;
    const zoom = this.native.getZoomLevel();
    const scale = this.native.config.scalePointsOnZoom ? zoom : Math.min(5, Math.max(1, zoom * .01));
    return { ...this.point(index), size: (this.native.getPointSizes()[index] ?? 0) * this.native.config.pointSizeScale * scale / 2,
      color: this.color(this.native.getPointColors(), index, dim), hidden: false,
      forceLabel: this.getNodeDisplayedLabels().has(key) };
  }
  getEdgeDisplayData(id: string) {
    const index = this.scene().edges.findIndex(edge => edge.graphId === id);
    if (index < 0) return undefined;
    const highlighted = this.native.config.highlightedLinkIndices;
    const dim = highlighted && !Array.from(highlighted).includes(index) ? this.native.config.linkGreyoutOpacity : 1;
    return { color: this.color(this.native.getLinkColors(), index, dim), hidden: false };
  }
  getGraph() {
    const scene = this.scene();
    return {
      order: scene.nodes.length,
      size: scene.edges.length,
      nodes: () => scene.nodes.map(node => node.key),
      edges: () => scene.edges.map(edge => edge.graphId),
      hasNode: (key: string) => scene.nodes.some(node => node.key === key),
      getNodeAttributes: (key: string) => {
        const index = scene.nodes.findIndex(node => node.key === key);
        if (index < 0) throw new Error(`Visible native node is unavailable: ${key}`);
        return this.point(index);
      },
    };
  }
  getBBox(): { x: [number, number]; y: [number, number] } {
    const rows = this.scene().nodes.map((_node, index) => this.point(index));
    return { x: [Math.min(...rows.map(row => row.x)), Math.max(...rows.map(row => row.x))],
      y: [Math.min(...rows.map(row => row.y)), Math.max(...rows.map(row => row.y))] };
  }
  getCustomBBox(): null { return null; }
  once(_event: "afterRender", callback: () => void): void { requestAnimationFrame(() => callback()); }
  observePointer(): void {
    const config = this.native.config;
    if (config.onPointMouseOver !== this.observedOver) {
      const previous = config.onPointMouseOver;
      this.observedOver = config.onPointMouseOver = (...args) => {
        this.hoveredKey = this.scene().nodes[args[0]]?.key ?? null;
        previous?.(...args);
      };
    }
    if (config.onPointMouseOut !== this.observedOut) {
      const previous = config.onPointMouseOut;
      this.observedOut = config.onPointMouseOut = (...args) => { this.hoveredKey = null; previous?.(...args); };
    }
    if (config.onPointClick !== this.observedClick) {
      const previous = config.onPointClick;
      this.observedClick = config.onPointClick = (...args) => {
        this.picks.push({ kind: "node", id: this.scene().nodes[args[0]]?.key ?? null }); previous?.(...args);
      };
    }
    if (config.onLinkClick !== this.observedLinkClick) {
      const previous = config.onLinkClick;
      this.observedLinkClick = config.onLinkClick = (...args) => {
        this.picks.push({ kind: "edge", id: this.scene().edges[args[0]]?.graphId ?? null }); previous?.(...args);
      };
    }
    if (config.onBackgroundClick !== this.observedStageClick) {
      const previous = config.onBackgroundClick;
      this.observedStageClick = config.onBackgroundClick = (...args) => { this.picks.push({ kind: "stage", id: null }); previous?.(...args); };
    }
  }
}
