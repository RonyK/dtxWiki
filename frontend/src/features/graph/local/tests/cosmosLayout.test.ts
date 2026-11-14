import { describe, expect, it, vi } from "vitest";

import type { CosmosSceneColumns } from "../../shared/cosmos/sceneTypes";
import { createLocalCosmosLayoutSolver } from "../cosmosLayout";

const worker = vi.hoisted(() => ({ pause: vi.fn(), resume: vi.fn() }));
vi.mock("../../shared/physics/d3WorkerLayout", () => ({
  createInitialD3Positions: () => ({}),
  createD3WorkerLayout: () => ({ ...worker, isRunning: () => false, destroy: vi.fn() }),
}));

// Right after a topology update the proxy's rows for arriving nodes are
// placeholders; the Worker has already placed them beside a neighbour.
describe("Local Cosmos layout", () => {
  it("pauses the Worker without sending rows back, and only once", () => {
    const columns = { nodeKeys: ["page:a", "page:b"], layoutInput: { nodes: [], edges: [] } } as unknown as CosmosSceneColumns;
    const layout = createLocalCosmosLayoutSolver().createPositionLayout({ columns, positions: new Float32Array(4),
      diameters: new Float32Array(2), shapes: new Float32Array(2), pins: [], reducedMotion: false,
      viewport: { width: 400, height: 300 } }, { onPositions: vi.fn(), onState: vi.fn(), onError: vi.fn() })!;
    layout.start(); layout.stop(); layout.stop();
    expect(worker.pause).toHaveBeenCalledExactlyOnceWith(true);
  });
});
