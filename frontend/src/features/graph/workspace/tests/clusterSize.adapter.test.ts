import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import type { GraphVisualTokens } from "../../shared/graphTokens";
import { WORKSPACE_MAP_PC_10K_BUDGET, type WorkspaceMapBudget } from "../../shared/workspaceMapBudget";
import { createWorkspaceCosmosAdapter, type WorkspaceCosmosAdapter } from "../cosmosAdapter";
import { buildCosmosSceneBuffers } from "../cosmosScene";
import type { GraphConfig } from "../cosmosNative";
import { createWorkspaceNativePointPicker } from "../nativePointPicker";

const { graph, createNativeGraph, cpu } = vi.hoisted(() => {
  let positions = new Float32Array();
  const graph = {
    ready: Promise.resolve(),
    attachGpuSimulation: vi.fn(), attachPointPicker: vi.fn(), getTrackedPointPositionsFrame: vi.fn(() => null),
    getGpuSimulationFrame: vi.fn(() => null), get isSimulationRunning() { return false; },
    destroy: vi.fn(), create: vi.fn(), fitView: vi.fn(), getPointPositions: vi.fn(() => positions.slice()),
    getTrackedPointPositionsMap: vi.fn(() => new Map()), getZoomLevel: vi.fn(() => 2), pause: vi.fn(),
    render: vi.fn(), screenToSpacePosition: vi.fn(() => [2048, 2048]), setClusterPositions: vi.fn(),
    setConfigPartial: vi.fn(), setLinkColors: vi.fn(), setLinkStyles: vi.fn(), setLinkStrength: vi.fn(),
    setLinks: vi.fn(), setPinnedPoints: vi.fn(), setPointClusters: vi.fn(), setPointColors: vi.fn(),
    setPointPositions: vi.fn((next: Float32Array) => { positions = next.slice(); }),
    setPointShapes: vi.fn(), setPointSizes: vi.fn(), setPointCollisionRadii: vi.fn(),
    setZoomLevel: vi.fn(), setZoomTransformByPointPositions: vi.fn(),
    spaceToScreenPosition: vi.fn(() => [10, 10]), start: vi.fn(), stop: vi.fn(),
    trackPointPositionsByIndices: vi.fn(), unpause: vi.fn(),
  };
  const createNativeGraph = vi.fn(function (container: HTMLDivElement, _config: GraphConfig) {
    const canvas = container.querySelector("canvas") ?? container.appendChild(document.createElement("canvas"));
    for (const [field, value] of [["clientWidth", 734], ["clientHeight", 608]] as const) {
      if (!Object.getOwnPropertyDescriptor(canvas, field)) Object.defineProperty(canvas, field, { configurable: true, value });
    }
    return graph;
  });
  // The official 2K Worker is replaced by a recorder: its input is what the layout is made of.
  const cpu = { inputs: [] as Array<Record<string, unknown>> };
  return { graph, createNativeGraph, cpu };
});

vi.mock("../cosmosNative", () => ({ NativeGraph: createNativeGraph }));
vi.mock("../cosmosRestLog", () => ({ RestLogGraph: vi.fn() }));
vi.mock("../webglSupport", () => ({ detectWorkspaceMapWebGL: () => ({ supported: true }) }));
vi.mock("../forceAtlas2WorkerLayout", () => ({
  createWorkspaceForceAtlas2WorkerLayout: vi.fn((input: Record<string, unknown>) => {
    cpu.inputs.push(input);
    return {
      start: vi.fn(), stop: vi.fn(), setPositions: vi.fn(), destroy: vi.fn(), isRunning: () => true,
      getProgress: vi.fn(() => input.progress ?? Object.freeze({
        profile: "gephi-lite-2k-v1", phase: "structure", iterations: 0, structureIterations: 0, transitionReason: null,
      })),
      getContactUnits: vi.fn(() => input.contactUnits),
    };
  }),
}));
// A terminal community outcome lets the 2K Worker start; the clusters need no community result of their own.
vi.mock("../../map/communityJob", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../map/communityJob")>();
  return {
    ...actual,
    createWorkspaceMapCommunityJob: () => ({
      cancel: vi.fn(), destroy: vi.fn(),
      compute: vi.fn(async (input: WorkspaceMapColumns) => ({
        kind: "error", identityKey: actual.workspaceMapCommunityIdentityKey(input),
      })),
    }),
  };
});
vi.mock("../nativePointPicker", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../nativePointPicker")>();
  return { ...actual, createWorkspaceNativePointPicker: vi.fn(actual.createWorkspaceNativePointPicker) };
});

const tokens: GraphVisualTokens = {
  surface: "#ffffff",
  node: { context: "#7c3aed", library: "#0f9f8f", conversation: "#ea580c", folder: "#64748b" },
  edge: { link: "#64748b", citation: "#0f9f8f", coAccess: "#ea580c", membership: "#94a3b8", action: "#7c3aed" },
};
// The 20K corpus shape: one dominant link component, the unlinked residue, and two-document pairs.
const MEMBERS = [17_499, 492, 2, 2];

/** An aggregated scene of `count` nodes: clusters first, as cluster keys sort before document keys. */
function aggregatedColumns(count: number, withDocumentCounts: boolean): WorkspaceMapColumns {
  const edges = count - 1;
  return {
    identity: { workspaceId: "w", actorId: "a", surface: "workspace", selectionKey: "combined", visibleFingerprint: "f" },
    nodeIds: Array.from({ length: count }, (_, index) => `n${index}`),
    nodeKeys: Array.from({ length: count }, (_, index) => `${index < MEMBERS.length ? "cluster" : "context"}:${String(index).padStart(5, "0")}`),
    nodeLabels: new Array<string>(count).fill("Node"), nodeFolderKeys: new Array<null>(count).fill(null),
    nodeKinds: Uint8Array.from({ length: count }, (_, index) => index < MEMBERS.length ? 2 : 0),
    nodeSizes: Float32Array.from({ length: count }, (_, index) => index === 0 ? 8.5 : index < MEMBERS.length ? 1.5 : 1.5 + (index % 6)),
    ...(withDocumentCounts ? { nodeDocumentCounts: Uint32Array.from({ length: count }, (_, index) => MEMBERS[index] ?? 1) } : {}),
    edgeIds: Array.from({ length: edges }, (_, index) => `e${index}`),
    edgeSources: Uint32Array.from({ length: edges }, (_, index) => index), edgeTargets: Uint32Array.from({ length: edges }, (_, index) => index + 1),
    edgeKinds: new Uint8Array(edges), edgeCounts: new Uint32Array(edges).fill(1),
    edgeWeights: new Float64Array(edges).fill(1), edgeCrossFolder: new Uint8Array(edges),
  };
}

let adapters: WorkspaceCosmosAdapter[] = [];
beforeEach(() => {
  vi.clearAllMocks();
  cpu.inputs.length = 0;
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    MAX_TEXTURE_SIZE: 0x0D33, getParameter: () => 16_384,
  } as unknown as WebGL2RenderingContext);
  graph.ready = Promise.resolve();
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
    window.setTimeout(() => callback(performance.now()), 0));
  adapters = [];
});
afterEach(() => {
  for (const adapter of adapters) adapter.destroy();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** The recorded calls stand in for their functions (callbacks), so `toEqual` compares everything else by value. */
function stand(value: unknown): unknown {
  if (typeof value === "function") return "[function]";
  if (Array.isArray(value)) return value.map(stand);
  if (value && typeof value === "object" && !ArrayBuffer.isView(value)) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, stand(item)]));
  }
  return value;
}

type RunOptions = { budget?: WorkspaceMapBudget; reducedMotion: boolean };

/** Everything the layout, the force profile and the renderer receive for one scene. */
async function run(input: WorkspaceMapColumns, options: RunOptions) {
  vi.clearAllMocks();
  cpu.inputs.length = 0;
  const container = document.createElement("div");
  document.body.appendChild(container);
  const adapter = createWorkspaceCosmosAdapter(container, input, {
    budget: options.budget, forcedColors: false, reducedMotion: options.reducedMotion,
    callbacks: { onHover: vi.fn(), onSelect: vi.fn() },
  });
  adapters.push(adapter);
  await adapter.whenSettled();
  const snapshot = adapter.captureCoordinates();
  const radiiCalls = graph.setPointCollisionRadii.mock.calls.map(([value]) => value as Float32Array | null);
  const result = {
    physics: container.dataset.mapPhysics,
    workerInput: stand(cpu.inputs.at(-1)),
    layoutBasis: stand({ force: snapshot?.forceBasis, collision: snapshot?.collisionBasis, units: snapshot?.forceAtlas2Units }),
    configs: stand(graph.setConfigPartial.mock.calls.map(([partial]) => partial)),
    radiiCalls: stand(radiiCalls),
    lastRadii: radiiCalls.filter((value): value is Float32Array => value !== null).at(-1),
    drawn: graph.setPointSizes.mock.lastCall?.[0] as Float32Array,
    pickerSizes: vi.mocked(createWorkspaceNativePointPicker).mock.lastCall?.[0],
  };
  adapter.destroy();
  return result;
}

/**
 * The same scene with and without the cluster counts: every physics input must match, while the renderer is
 * handed the scene builder's emphasised sizes.
 */
async function withAndWithoutCounts(count: number, options: RunOptions) {
  const input = aggregatedColumns(count, true);
  const plain = await run(aggregatedColumns(count, false), options);
  const emphasised = await run(input, options);
  expect(emphasised.workerInput).toEqual(plain.workerInput);
  expect(emphasised.layoutBasis).toEqual(plain.layoutBasis);
  expect(emphasised.configs).toEqual(plain.configs);
  expect(emphasised.radiiCalls).toEqual(plain.radiiCalls);
  const built = buildCosmosSceneBuffers(input, tokens, options.budget);
  expect(emphasised.drawn[0]).toBeGreaterThan(built.pointSizes[0]);
  expect(Array.from(emphasised.drawn)).toEqual(Array.from(built.drawPointSizes));
  return { plain, emphasised };
}

describe("Aggregated Map: cluster emphasis is render-only", () => {
  it("hands the official 2K Worker the same input with and without cluster counts, and draws the emphasised sizes", async () => {
    const { plain, emphasised } = await withAndWithoutCounts(2_000, { reducedMotion: false });
    expect(emphasised.physics).toBe("force-atlas2-worker");
    expect(plain.workerInput).toBeDefined();
  });

  it("keeps native collision radii and force profile above 2K independent of the clusters, and picks what is drawn", async () => {
    const { plain, emphasised } = await withAndWithoutCounts(2_001, { budget: WORKSPACE_MAP_PC_10K_BUDGET, reducedMotion: true });
    expect(emphasised.physics).toBe("cosmos-native");
    expect(emphasised.lastRadii).toHaveLength(2_001);
    expect(Array.from(emphasised.lastRadii!)).toEqual(Array.from(plain.lastRadii!));
    // The native picker exists above 2K only; it must hit what is drawn, value for value.
    expect(Array.from(emphasised.pickerSizes!)).toEqual(Array.from(emphasised.drawn));
  });
});
