import { createCosmosRenderer } from "../../frontend/src/features/graph/shared/cosmos/cosmosCore";
import { buildCosmosPresentationBuffers } from "../../frontend/src/features/graph/shared/cosmos/sceneBuffers";
import { cosmosSceneIdentityKey, type CosmosSceneColumns } from "../../frontend/src/features/graph/shared/cosmos/sceneTypes";
import type { CosmosRendererBinding } from "../../frontend/src/features/graph/shared/cosmos/renderBinding";
import { GRAPH_NODE_VISUALS, type GraphNodeVisualKind } from "../../frontend/src/features/graph/shared/graphTokens";
import { nativeGlyphDiameterScale } from "../../frontend/src/features/graph/shared/workspaceGlyphGeometry";
import { NativeGraph } from "../../frontend/src/features/graph/workspace/cosmosNative";
import { createWorkspaceLayoutSolver } from "../../frontend/src/features/graph/workspace/layoutSolver";

// Context presentation prepared through the same core used by the Map. The
// host supplies visible columns and static positions; the product's Context
// renderer and Worker migration are deliberately not part of this fixture.
const host = document.getElementById("graph") as HTMLDivElement;
const nodeKinds = Object.keys(GRAPH_NODE_VISUALS) as GraphNodeVisualKind[];
const edgeKinds = ["link"] as const;
const budget = { documents: 10_000, relations: 100_000 };
const nodeIds = [...nodeKinds.map(kind => `original:node:${kind}`),
  ...edgeKinds.flatMap(kind => [`original:${kind}:source`, `original:${kind}:target`])];
const positions = new Float32Array(nodeIds.length * 2);
const put = (row: number, x: number, y: number) => positions.set([2048 + x - 360, 2048 - y + 300], row * 2);
nodeKinds.forEach((_, row) => put(row, 80 + row * 80, 90));
edgeKinds.forEach((_, row) => { put(nodeKinds.length + row * 2, 80, 170 + row * 40); put(nodeKinds.length + row * 2 + 1, 640, 170 + row * 40); });
const visuals = [...nodeKinds, ...edgeKinds.flatMap(() => ["context", "context"] as const)];
const columns: CosmosSceneColumns = {
  identity: { workspaceId: "shared-cosmos-fixture", actorId: "visible-reader", surface: "local",
    selectionKey: "context-preparation", visibleFingerprint: "original-nodes-and-relationship" },
  nodeIds, nodeKeys: nodeIds.slice(), nodeLabels: nodeIds.slice(), nodeFolderKeys: nodeIds.map(() => null),
  nodeKinds: new Uint8Array(nodeIds.length), nodeSizes: Float32Array.from(nodeIds, (_, row) => row < nodeKinds.length ? 32 : 16),
  nodeVisualKinds: visuals,
  edgeIds: edgeKinds.map(kind => `original:relation:${kind}`),
  edgeSources: Uint32Array.from(edgeKinds, (_, row) => nodeKinds.length + row * 2),
  edgeTargets: Uint32Array.from(edgeKinds, (_, row) => nodeKinds.length + row * 2 + 1),
  edgeKinds: Uint8Array.from(edgeKinds, (_, row) => row),
  edgeCounts: new Uint32Array(edgeKinds.length).fill(1), edgeWeights: new Float64Array(edgeKinds.length).fill(1),
  edgeCrossFolder: new Uint8Array(edgeKinds.length), edgeVisualKinds: [...edgeKinds],
  edgeRelationKinds: ["wikilink"],
  edgeDirections: edgeKinds.map(() => "outgoing"),
};
const observed: { graph: NativeGraph | null } = { graph: null };
const nativeRender = NativeGraph.prototype.render;
NativeGraph.prototype.render = function (...args) { observed.graph = this; return nativeRender.apply(this, args); };
let selected: string | null = null, runtimeError: string | null = null;
const binding: CosmosRendererBinding = {
  budget, legacySnapshotBudget: budget, smallSceneLimit: 2_000,
  solver: createWorkspaceLayoutSolver(budget),
  assertColumns(input) { if (input.nodeIds.length !== nodeIds.length || input.edgeIds.length !== edgeKinds.length) throw new Error("Fixture identity count changed"); },
  withinBudget: () => true,
  admissionCounts: input => ({ nodeCount: input.nodeIds.length, edgeCount: input.edgeIds.length }),
  overview: () => false, canAnimate: () => false,
  sameGeometry: (left, right) => left.nodeKeys.every((key, row) => key === right.nodeKeys[row])
    && left.edgeIds.every((id, row) => id === right.edgeIds[row]
      && left.edgeSources[row] === right.edgeSources[row] && left.edgeTargets[row] === right.edgeTargets[row]),
  prepareScene(input, tokens, options) {
    const buffers = buildCosmosPresentationBuffers(input, tokens, { forcedColors: options.forcedColors, positions });
    // A surface can recompute its prepared ink when only presentation metadata
    // changes. The renderer must upload that ink without replacing positions.
    if (input.nodeLabels[0] === "Updated visible caption") buffers.linkColors.set([.25, .25, .25, 1], 0);
    return buffers;
  },
  relationColors: (_input, colors) => colors,
  rankLabels: () => [], visualKind: (input, row) => input.nodeVisualKinds![row],
  createSeedJob: () => ({ compute: async input => ({ kind: "cancelled", identityKey: cosmosSceneIdentityKey(input) }), cancel() {}, destroy() {} }),
  initializeDiagnostics() {}, diagnose() {},
  decorateSelectionRing(ring) { ring.dataset.sharedCosmosSelectionRing = ""; },
};
const adapter = createCosmosRenderer(host, columns, {
  forcedColors: true, reducedMotion: true, showLabels: false,
  initialCoordinates: { budget, columns, positions, layoutReady: true },
  initialViewport: { zoom: 1, pan: { x: 2048, y: 2048 } },
  callbacks: {
    onHover() {}, onSelect(id) { selected = id; adapter.select(id); },
    onRuntimeError(error) { runtimeError = String(error); },
  },
}, binding);

const frames = (count: number) => new Promise<void>(resolve => {
  const step = (left: number) => requestAnimationFrame(() => left > 1 ? step(left - 1) : resolve());
  step(count);
});

export type SharedCosmosProbe = {
  repaintInk: number;
  glyphs: Array<{ kind: GraphNodeVisualKind; center: [number, number]; diameter: number;
    centerInk: boolean; topInk: boolean; bottomInk: boolean; cornerInk: boolean; farCornerInk: boolean;
    cutoutInk: boolean; bandInk: boolean; bodyInk: boolean }>;
  glError: number; runtimeError: string | null;
  originalNodeIds: string[]; originalRelationIds: string[];
};

declare global {
  interface Window { __sharedCosmosPreparation: {
    probe(zoom?: number): Promise<SharedCosmosProbe>;
    updateMetadata(): Promise<{ positionsBefore: number[]; positionsAfter: number[];
      cameraBefore: ReturnType<typeof adapter.getViewport>; cameraAfter: ReturnType<typeof adapter.getViewport>; probe: SharedCosmosProbe }>;
    selection(): { selected: string | null; ring: string | null };
    destroy(): void;
  }; }
}

void adapter.whenSettled().then(async () => {
  const graph = observed.graph!, canvas = host.querySelector("canvas")!, gl = canvas.getContext("webgl2")!;
  const screen = (row: number) => {
    const current = graph.getPointPositions();
    return graph.spaceToScreenPosition([current[row * 2], current[row * 2 + 1]]);
  };
  await frames(3);
  window.__sharedCosmosPreparation = {
    async probe(zoom) {
      if (zoom !== undefined) {
        adapter.select(null); selected = null;
        adapter.setViewport({ zoom, pan: { x: 2048, y: 2048 } });
      }
      await frames(3);
      graph.render(undefined, 0); await frames(2);
      const width = canvas.width, height = canvas.height, rgba = new Uint8Array(width * height * 4);
      gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
      const background = Array.from(rgba.slice(0, 3));
      const coverage = (x: number, y: number) => {
        const px = Math.max(0, Math.min(width - 1, Math.floor(x * width / host.clientWidth)));
        const py = Math.max(0, Math.min(height - 1, height - 1 - Math.floor(y * height / host.clientHeight)));
        const offset = (py * width + px) * 4;
        return Math.max(...background.map((channel, index) => Math.abs(rgba[offset + index] - channel)));
      };
      const start = screen(nodeKinds.length), end = screen(nodeKinds.length + 1);
      const repaintInk = Math.max(...[-.5, 0, .5].map(dy => coverage((start[0] + end[0]) / 2, start[1] + dy)));
      const glyphs = nodeKinds.map((kind, row) => {
        const center = screen(row), diameter = 32 * nativeGlyphDiameterScale(GRAPH_NODE_VISUALS[kind].shape)
          * (graph.config.pointSizeScale ?? 1) * Math.min(5, Math.max(1, graph.getZoomLevel() * .01));
        const ink = (dx: number, dy: number) => coverage(center[0] + dx * diameter, center[1] + dy * diameter) >= 96;
        return { kind, center, diameter, centerInk: ink(0, 0), topInk: ink(0, -.4), bottomInk: ink(0, .4),
          cornerInk: ink(.35, .35), farCornerInk: ink(.4, .4), bodyInk: ink(-.3, 0),
          cutoutInk: kind === "agentVersion" ? ink(0, -.285) : ink(.34, 0), bandInk: ink(.46, 0) };
      });
      const saved = adapter.captureCoordinates();
      return { repaintInk, glyphs, glError: gl.getError(), runtimeError,
        originalNodeIds: saved?.columns.nodeIds ?? [], originalRelationIds: saved?.columns.edgeIds ?? [] };
    },
    async updateMetadata() {
      // Move a reader-owned pin away from the binding's original prepared
      // positions, so a reset to the identical fixture seed cannot pass.
      const original = adapter.captureCoordinates()!.positions;
      adapter.pin(nodeIds[0], true, { x: original[0] + 17, y: original[1] + 11 });
      await frames(2);
      const positionsBefore = Array.from(adapter.captureCoordinates()!.positions), cameraBefore = adapter.getViewport();
      adapter.replaceColumns({ ...columns, nodeLabels: ["Updated visible caption", ...columns.nodeLabels.slice(1)] });
      await frames(3);
      return { positionsBefore, positionsAfter: Array.from(adapter.captureCoordinates()!.positions),
        cameraBefore, cameraAfter: adapter.getViewport(), probe: await window.__sharedCosmosPreparation.probe() };
    },
    selection() { return { selected, ring: host.querySelector('[data-shared-cosmos-selection-ring] path:last-child')?.getAttribute("d") ?? null }; },
    destroy() { adapter.destroy(); },
  };
});
