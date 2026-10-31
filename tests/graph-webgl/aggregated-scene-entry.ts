import { NativeGraph as Graph } from "../../frontend/src/features/graph/workspace/cosmosNative";
import { createWorkspaceCosmosAdapter } from "../../frontend/src/features/graph/workspace/cosmosAdapter";
import { buildCosmosSceneBuffers } from "../../frontend/src/features/graph/workspace/cosmosScene";
import { createWorkspaceNativeCoordinateSpace } from "../../frontend/src/features/graph/workspace/nativeCoordinateSpace";
import { AGGREGATED_CLUSTER_MEMBERS, aggregatedClusterId, aggregatedWorkspaceColumns }
  from "../../frontend/src/features/graph/workspace/tests/aggregatedSceneFixture";
import { readGraphTokens } from "../../frontend/src/features/graph/shared/graphTokens";
import { WORKSPACE_MAP_PC_10K_BUDGET } from "../../frontend/src/features/graph/shared/workspaceMapBudget";

// An aggregated native PC map (2,004 nodes, four clusters of 17,499 / 492 / 12 / 2 documents) mounted by the real
// adapter over the real Cosmos renderer, laid out by hand and kept still, so every pointer position is known. The
// adapter and its picker are the product's; this file only observes them.
const stage = document.getElementById("stage") as HTMLDivElement;
const budget = WORKSPACE_MAP_PC_10K_BUDGET;
const columns = aggregatedWorkspaceColumns({ documents: 2_000, clusters: AGGREGATED_CLUSTER_MEMBERS });
const clusterRows = AGGREGATED_CLUSTER_MEMBERS.map((_, index) => columns.nodeIds.indexOf(aggregatedClusterId(index)));

// Clusters far apart on the left, ordinary documents in a block on the right: at the Fit camera nothing overlaps.
const clusterAt: Array<[number, number]> = [[900, 2600], [900, 1500], [1700, 2600], [1700, 1500]];
const positions = new Float32Array(columns.nodeIds.length * 2);
columns.nodeIds.forEach((_, row) => {
  const cluster = clusterRows.indexOf(row);
  if (cluster >= 0) { positions.set(clusterAt[cluster], row * 2); return; }
  const slot = row - clusterRows.length;
  positions.set([2500 + (slot % 50) * 22, 1600 + Math.floor(slot / 50) * 22], row * 2);
});

const events: Array<{ kind: "hover" | "select"; id: string | null }> = [];
let hovered: string | null = null, selected: string | null = null, runtimeError: string | null = null;
const observed: { graph: Graph | null } = { graph: null };
const nativeRender = Graph.prototype.render;
Graph.prototype.render = function (...args) { observed.graph = this; return nativeRender.apply(this, args); };

const adapter = createWorkspaceCosmosAdapter(stage, columns, {
  budget, forcedColors: false, reducedMotion: true,
  initialCoordinates: { budget, columns, positions },
  callbacks: {
    onHover(id) { hovered = id; events.push({ kind: "hover", id }); },
    onSelect(id) { selected = id; events.push({ kind: "select", id }); },
    onRuntimeError(error) { runtimeError = String(error); },
  },
});

declare global {
  interface Window { __aggregatedScene: {
    /** The 17,499-document cluster's size as the scene builder gives it to the renderer and the picker, and its relation-based one. */
    hubSizes(): { draw: number; relation: number };
    /** CSS pixels, top-left origin, where the node is drawn now. */
    screen(id: string): [number, number];
    /** Centre the camera on a node at `factor` times the zoom of the first Fit. */
    zoomOn(id: string, factor: number): Promise<void>;
    snapshot(): { hovered: string | null; selected: string | null; events: typeof events; pointSizeScale: number;
      glError: number; runtimeError: string | null };
  }; }
}

void adapter.whenSettled().then(async () => {
  const canvas = stage.querySelector("canvas")!;
  const gl = canvas.getContext("webgl2")!;
  const space = createWorkspaceNativeCoordinateSpace(gl.getParameter(gl.MAX_TEXTURE_SIZE) as number);
  const graph = observed.graph!;
  const fitZoom = graph.getZoomLevel();
  const tokens = readGraphTokens(stage, "light");
  const rowOf = (id: string) => {
    const row = columns.nodeIds.indexOf(id);
    if (row < 0) throw new Error(`Unknown node ${id}`);
    return row;
  };
  const screen = (id: string): [number, number] => {
    const native = graph.getPointPositions(), row = rowOf(id);
    return graph.spaceToScreenPosition([native[row * 2], native[row * 2 + 1]]);
  };
  const frames = (count: number) => new Promise<void>(resolve => {
    const step = (left: number) => requestAnimationFrame(() => (left > 1 ? step(left - 1) : resolve()));
    step(count);
  });
  await frames(3);
  window.__aggregatedScene = {
    hubSizes() {
      const built = buildCosmosSceneBuffers(columns, tokens, budget);
      // A scene builder from before the cluster emphasis has no separate draw array: it drew the relation sizes.
      return { draw: (built.drawPointSizes ?? built.pointSizes)[clusterRows[0]], relation: built.pointSizes[clusterRows[0]] };
    },
    screen,
    async zoomOn(id, factor) {
      const row = rowOf(id), native = graph.getPointPositions();
      const [x, y] = space.fromNativePoint([native[row * 2], native[row * 2 + 1]]);
      adapter.setViewport({ zoom: space.fromNativeZoom(fitZoom) * factor, pan: { x, y } });
      const target = fitZoom * factor, box = stage.getBoundingClientRect();
      for (let attempt = 0; attempt < 120; attempt++) {
        await frames(1);
        const at = screen(id);
        if (Math.abs(graph.getZoomLevel() - target) < 1e-6 * target
          && Math.abs(at[0] - box.width / 2) < .01 && Math.abs(at[1] - box.height / 2) < .01) { await frames(2); return; }
      }
      throw new Error("The camera did not settle on the requested node");
    },
    snapshot: () => ({ hovered, selected, events: events.slice(), pointSizeScale: graph.config.pointSizeScale ?? NaN,
      glError: gl.getError(), runtimeError }),
  };
});
