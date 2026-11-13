import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildLocalCosmosColumns } from "../../shared/cosmos/localScene";
import type { CosmosOverlayFrame } from "../../shared/cosmos/renderBinding";
import { readGraphTokens } from "../../shared/graphTokens";
import type { GraphRenderScene } from "../../shared/rendererTypes";
import { createLocalCosmosOverlay } from "../cosmosOverlay";

function paintContext() {
  return {
    font: "", textAlign: "left", textBaseline: "middle", fillStyle: "", strokeStyle: "", globalAlpha: 1, lineWidth: 1,
    save: vi.fn(), restore: vi.fn(), setTransform: vi.fn(), clearRect: vi.fn(),
    beginPath: vi.fn(), roundRect: vi.fn(), fill: vi.fn(), stroke: vi.fn(), arc: vi.fn(),
    moveTo: vi.fn(), lineTo: vi.fn(), closePath: vi.fn(), strokeText: vi.fn(),
    fillText: vi.fn<(text: string, x: number, y: number, maxWidth?: number) => void>(),
    measureText: (text: string) => ({ width: Array.from(text).length * 11 }),
  };
}
let contexts: Map<HTMLCanvasElement, ReturnType<typeof paintContext>>;
let container: HTMLDivElement;
let overlay: ReturnType<typeof createLocalCosmosOverlay>;
beforeEach(() => {
  contexts = new Map(); container = document.createElement("div");
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (this: HTMLCanvasElement) {
    let context = contexts.get(this);
    if (!context) { context = paintContext(); contexts.set(this, context); }
    return context as unknown as CanvasRenderingContext2D;
  });
  overlay = createLocalCosmosOverlay(container);
});
afterEach(() => { overlay.destroy(); vi.restoreAllMocks(); });

const scene = (): GraphRenderScene => ({
  currentId: "seed",
  nodes: ["other", "neighbor", "seed"].map(id => ({ id, entity_id: id, entity_kind: "context_page",
    family: "document", label: id, is_seed: id === "seed" })),
  edges: [],
});
function frame(input: GraphRenderScene, options: Partial<CosmosOverlayFrame> = {}): CosmosOverlayFrame {
  const columns = buildLocalCosmosColumns(input, { actorId: "owner", workspaceId: "workspace", surface: "local",
    selectionKey: "query", visibleFingerprint: "visible" });
  return { columns, points: Float32Array.from(columns.nodeIds.flatMap(() => [300, 200])),
    diameters: new Float32Array(columns.nodeIds.length).fill(10), selectedId: null, focusedId: null, star: null,
    showLabels: false, zoom: 1, forcedColors: false, tokens: readGraphTokens(container, "light"),
    width: 860, height: 640, ...options };
}
const labels = () => contexts.get(container.querySelector<HTMLCanvasElement>("[data-local-graph-labels]")!)!;

// These are moved Sigma label-drawer scenarios. GPU/browser label coverage
// remains in Local WebGL; these preserve the distinct caption/tiny-frame inputs
// that a normal 390px viewport and native glyph pixels cannot substitute for.
describe("Local Cosmos priority and history labels", () => {
  it("ellipsizes long history captions without compressing the text and leaves short captions intact", () => {
    const caption = "문서 제목이 아주 길 때 변경 사항을 표시하는 라벨 🔎";
    const input = scene(); input.currentId = null; input.nodes.forEach(node => { node.is_seed = false; });
    input.edges = [{ id: "event", source: "neighbor", target: "other", label: "edited", edge_kind: "edited",
      layer: "activity", eventCaption: caption }];
    const current = frame(input);
    current.points = Float32Array.from(current.columns.nodeIds.flatMap(id => id === "neighbor" ? [100, 100] : [300, 100]));
    overlay.render(current);
    const context = labels(), visible = context.fillText.mock.calls[0][0];
    expect(visible).toMatch(/…$/u);
    expect(caption.startsWith(visible.slice(0, -1))).toBe(true);
    expect(context.measureText(visible).width).toBeLessThanOrEqual(168);
    expect(context.fillText).toHaveBeenLastCalledWith(visible, 200, 100);
    current.columns.edgeCaptions![0] = "짧은 제목";
    context.fillText.mockClear(); overlay.render(current);
    expect(context.fillText).toHaveBeenLastCalledWith("짧은 제목", 200, 100);
  });

  it("paints seed, selection and keyboard focus titles together before ordinary titles and clears released priority", () => {
    const current = frame(scene(), { selectedId: "neighbor", focusedId: "other" });
    overlay.render(current);
    const context = labels();
    expect(context.fillText.mock.calls.map(call => call[0])).toEqual(["seed", "neighbor", "other"]);
    const rows = context.fillText.mock.calls.map(call => call[2]);
    expect(Math.abs(rows[0] - rows[1])).toBeGreaterThanOrEqual(16);
    expect(Math.abs(rows[0] - rows[2])).toBeGreaterThanOrEqual(16);
    expect(Math.abs(rows[1] - rows[2])).toBeGreaterThanOrEqual(16);
    context.fillText.mockClear();
    overlay.render({ ...current, selectedId: null, focusedId: null });
    expect(context.fillText.mock.calls.map(call => call[0])).toEqual(["seed"]);
  });

  it("quiets ordinary titles and an expanded seed's ring only outside the renderer's active star", () => {
    const input = scene(); input.currentId = undefined;
    const spread = (current: CosmosOverlayFrame) => {
      current.points = Float32Array.from(current.columns.nodeIds.flatMap((_, index) => [150 + index * 250, 150 + index * 150]));
      return current;
    };
    const rings = () => contexts.get(container.querySelector<HTMLCanvasElement>("[data-local-graph-rings]")!)!;
    // A selected id outside this frame has no star, so nothing is quiet.
    const absent = spread(frame(input, { showLabels: true, selectedId: "absent" }));
    overlay.render(absent);
    const arcs: string[] = [];
    rings().arc.mockImplementation(() => { arcs.push(rings().strokeStyle); });
    labels().fillText.mockClear(); overlay.render(absent);
    expect(labels().fillText.mock.calls.map(call => call[0]).sort()).toEqual(["neighbor", "other", "seed"]);
    expect(arcs).toContain(absent.tokens.ink);
    const neighbor = absent.columns.nodeIds.indexOf("neighbor");
    arcs.length = 0; labels().fillText.mockClear();
    // The star alone (no selection band) quiets every point outside it.
    overlay.render({ ...absent, selectedId: null, star: new Set([neighbor]) });
    expect(labels().fillText.mock.calls.map(call => call[0]).sort()).toEqual(["neighbor", "seed"]);
    expect(arcs).not.toContain(absent.tokens.ink);
  });

  it("keeps all priority titles in a tiny viewport instead of dropping them when collision-free placement is impossible", () => {
    const current = frame(scene(), { selectedId: "neighbor", focusedId: "other", width: 48, height: 20 });
    current.points.fill(0);
    for (let index = 0; index < current.columns.nodeIds.length; index++) {
      current.points[index * 2] = 24; current.points[index * 2 + 1] = 10;
    }
    overlay.render(current);
    const painted = labels().fillText.mock.calls;
    expect(painted).toHaveLength(3);
    expect(new Set(painted.map(call => call[2])).size).toBeLessThan(painted.length);
    for (const [text, x, y, maxWidth] of painted) {
      expect(text.length).toBeGreaterThan(0);
      expect(x).toBeGreaterThanOrEqual(0); expect(x).toBeLessThanOrEqual(48);
      expect(y).toBeGreaterThanOrEqual(0); expect(y).toBeLessThanOrEqual(20);
      expect(maxWidth).toBeLessThanOrEqual(48);
    }
  });
});
