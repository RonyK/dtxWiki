import { NativeGraph as Graph } from "../../frontend/src/features/graph/workspace/cosmosNative";
import { runRestLogLawProbe } from "./rest-log-law";

import { workspaceMapCommunityIdentityKey } from "../../frontend/src/features/graph/map/communityJob";
import type { WorkspaceMapCommunityRequest, WorkspaceMapCommunityResponse } from "../../frontend/src/features/graph/map/communityWorker";
import type { WorkspaceMapColumns } from "../../frontend/src/features/graph/map/sceneColumns";
import { readGraphTokens } from "../../frontend/src/features/graph/shared/graphTokens";
import { WORKSPACE_MAP_PC_10K_BUDGET } from "../../frontend/src/features/graph/shared/workspaceMapBudget";
import { createWorkspaceCosmosAdapter } from "../../frontend/src/features/graph/workspace/cosmosAdapter";
import { buildCosmosSceneBuffers } from "../../frontend/src/features/graph/workspace/cosmosScene";
import { buildWorkspaceRelationCollisionBasis } from "../../frontend/src/features/graph/workspace/relationPhysics";

const INITIAL_ALPHA = .35; // The production adapter's ordinary scene-start alpha.
const NODE_COUNT = 32;
const query = new URLSearchParams(window.location.search);
const swapped = query.has("swap");
const reassignedFolders = query.has("folders");
const session = query.get("session") === "matched" ? "matched" : "fresh";

type WorkerInputEvidence = {
  identityKey: string;
  workspaceId: string;
  nodeKeys: string[];
  sources: number[];
  targets: number[];
  weights: number[];
};

type ForceSnapshot = {
  tick: number;
  alphaFromTick: number | null;
  positions: number[];
  running: boolean;
  glError: number;
  clusterIndexUploads: number[][];
  clusterTargetUploads: number[][];
  linkStrengthUploads: number[][];
  nativeLinks: number[];
  config: {
    gravity: number; center: number; repulsion: number; spring: number; distance: number;
    cluster: number; friction: number; collision: number;
    collisionRadius: number | null; collisionPadding: number;
    spaceSize: number; simulationEnabled: boolean;
  };
};

type ForceEvidence = {
  scope: string;
  session: "matched" | "fresh";
  initialAlpha: number;
  swapped: boolean;
  reassignedFolders: boolean;
  identityKey: string;
  nodeIds: string[];
  nodeKeys: string[];
  folderKeys: Array<string | null>;
  edgeIds: string[];
  sources: number[];
  targets: number[];
  weights: number[];
  crossFolder: number[];
  degrees: number[];
  calibration: {
    fallbackPositions: number[];
    pointSizes: number[];
    viewport: { width: number; height: number };
  };
  firstPositionUpload: number[];
  acceptedPositionUpload: number[];
  accepted: ForceSnapshot;
  fixturePositions: number[];
  workerInputs: WorkerInputEvidence[];
  worker: { communityIds: number[]; communityCount: number; initialPositions: number[] };
  before: ForceSnapshot;
  samples: ForceSnapshot[];
};

type CollisionCaseEvidence = {
  name: "separated-same-cell" | "separated-translated" | "overlap";
  input: number[];
  before: number[];
  after: number[];
  ticksBefore: number;
  ticksAfter: number;
  alphaAfter: number | null;
  running: boolean;
  glErrors: number[];
  config: {
    gravity: number; center: number; repulsion: number; spring: number; cluster: number;
    collision: number; radius: number; padding: number; friction: number; spaceSize: number;
  };
};

type CollisionEvidence = {
  scope: string;
  pointSizes: number[];
  samples: CollisionCaseEvidence[];
};

declare global {
  interface Window {
    __workspaceRelationForces: {
      status: "loading" | "ready" | "error";
      error?: string;
      manual(ticks: number[]): Promise<ForceEvidence>;
      collisionOnly(): Promise<CollisionEvidence>;
      relationLaw(): ReturnType<typeof runRestLogLawProbe>;
      destroy(): void;
    };
  }
}

const nodeIds = Array.from({ length: NODE_COUNT }, (_, index) => `context:${String(index).padStart(3, "0")}`);
const edges: Array<{ id: string; source: number; target: number; weight: number }> = [];
for (let group = 0; group < 4; group++) {
  for (let source = 0; source < 8; source++) {
    for (let target = source + 1; target < 8; target++) {
      edges.push({ id: `clique:${group}:${source}:${target}`, source: group * 8 + source,
        target: group * 8 + target, weight: 32 });
    }
  }
}
edges.push({ id: "bridge:top", source: 0, target: 8, weight: swapped ? 1 : 64 },
  { id: "bridge:bottom", source: 16, target: 24, weight: swapped ? 64 : 1 });
const folderKeys = nodeIds.map((_, index) => reassignedFolders
  ? `folder:${index % 3}` : `folder:${Math.floor(index / 8)}`);
const columns: WorkspaceMapColumns = {
  identity: { actorId: "relation-force-actor", workspaceId: "workspace-relation-force-fixture",
    surface: "workspace", selectionKey: "combined", visibleFingerprint: `relation-forces:${swapped ? "swapped" : "original"}` },
  nodeIds, nodeKeys: [...nodeIds], nodeLabels: [...nodeIds], nodeFolderKeys: folderKeys,
  nodeKinds: new Uint8Array(NODE_COUNT), nodeSizes: new Float32Array(NODE_COUNT).fill(3),
  edgeIds: edges.map(edge => edge.id), edgeSources: Uint32Array.from(edges.map(edge => edge.source)),
  edgeTargets: Uint32Array.from(edges.map(edge => edge.target)), edgeKinds: new Uint8Array(edges.length),
  edgeCounts: new Uint32Array(edges.length).fill(1), edgeWeights: Float64Array.from(edges.map(edge => edge.weight)),
  edgeCrossFolder: Uint8Array.from(edges.map(edge => Number(folderKeys[edge.source] !== folderKeys[edge.target]))),
};
const identityKey = workspaceMapCommunityIdentityKey(columns);
const degrees = new Uint32Array(NODE_COUNT);
edges.forEach(({ source, target }) => { degrees[source]++; degrees[target]++; });

// Controlled same-session restore/public upload, never a worker override:
// mirrored octagons give the two authored bridges identical geometry and
// endpoint degree. Matched mode preserves one baseline's collision/force units
// through the real adapter's restore contract. Fresh mode separately observes
// worker-derived collision units before any public fixture upload. Neither
// mode is an alternative product layout or a 2K readability qualification.
const fixturePositions = Float32Array.from(nodeIds.flatMap((_, index) => {
  const group = Math.floor(index / 8), member = index % 8;
  const reflectX = group % 2 === 0 ? 1 : -1;
  const reflectY = group < 2 ? 1 : -1;
  const centerX = group % 2 === 0 ? 1648 : 2448;
  const centerY = group < 2 ? 1648 : 2448;
  const angle = member * Math.PI / 4;
  return [centerX + reflectX * 48 * Math.cos(angle), centerY + reflectY * 48 * Math.sin(angle)];
}));

const observed: { native: Graph | null } = { native: null };
const clusterIndexUploads: number[][] = [];
const clusterTargetUploads: number[][] = [];
const linkStrengthUploads: number[][] = [];
let nativeLinks: number[] = [];
let firstPositionUpload: number[] | null = null;
let latestPositionUpload: number[] | null = null;
let nativePointSizes: number[] | null = null;
let calibrationViewport: { width: number; height: number } | null = null;
const originalRender = Graph.prototype.render;
const originalClusters = Graph.prototype.setPointClusters;
const originalTargets = Graph.prototype.setClusterPositions;
const originalStrengths = Graph.prototype.setLinkStrength;
const originalLinks = Graph.prototype.setLinks;
const originalPositions = Graph.prototype.setPointPositions;
const originalSizes = Graph.prototype.setPointSizes;
const originalConfig = Graph.prototype.setConfigPartial;
Graph.prototype.render = function (...args) {
  observed.native = this;
  return originalRender.apply(this, args);
};
Graph.prototype.setPointClusters = function (...args) {
  clusterIndexUploads.push(args[0].map(value => value ?? -1));
  return originalClusters.apply(this, args);
};
Graph.prototype.setClusterPositions = function (...args) {
  clusterTargetUploads.push(args[0].map(value => value ?? -1));
  return originalTargets.apply(this, args);
};
Graph.prototype.setLinkStrength = function (...args) {
  linkStrengthUploads.push(Array.from(args[0]));
  return originalStrengths.apply(this, args);
};
Graph.prototype.setLinks = function (...args) {
  nativeLinks = Array.from(args[0]);
  return originalLinks.apply(this, args);
};
Graph.prototype.setPointPositions = function (...args) {
  // Fresh starts upload the fallback; matched starts upload restored session
  // coordinates. Observe both without replacing any native input.
  if (args[0].length) {
    firstPositionUpload ??= Array.from(args[0]);
    latestPositionUpload = Array.from(args[0]);
  }
  return originalPositions.apply(this, args);
};
Graph.prototype.setPointSizes = function (...args) {
  if (args[0].length && nativePointSizes === null) nativePointSizes = Array.from(args[0]);
  return originalSizes.apply(this, args);
};
Graph.prototype.setConfigPartial = function (...args) {
  const partial = args[0];
  // The first force commit owns the first drawable native CSS viewport. Read
  // it passively at that boundary; backing-store pixels and later Fit/Zoom
  // must not become the spec's independent calibration input.
  if (calibrationViewport === null && firstPositionUpload && nativePointSizes
    && partial.simulationLinkDistance !== undefined && partial.simulationRepulsion !== undefined) {
    const canvas = stage.querySelector<HTMLCanvasElement>("canvas");
    if (canvas && Number.isInteger(canvas.clientWidth) && Number.isInteger(canvas.clientHeight)
      && canvas.clientWidth > 0 && canvas.clientHeight > 0) {
      calibrationViewport = { width: canvas.clientWidth, height: canvas.clientHeight };
    }
  }
  return originalConfig.apply(this, args);
};

const workerInputs: WorkerInputEvidence[] = [];
let resolveWorker!: (response: Extract<WorkspaceMapCommunityResponse, { type: "complete" }>) => void;
let rejectWorker!: (error: Error) => void;
const workerResult = new Promise<Extract<WorkspaceMapCommunityResponse, { type: "complete" }>>((resolve, reject) => {
  resolveWorker = resolve; rejectWorker = reject;
});
const NativeWorker = window.Worker;
// Passive observation only: every original request and response reaches the
// native worker and adapter unchanged; no synthetic partition is injected.
window.Worker = class ObservedWorker extends NativeWorker {
  constructor(scriptURL: string | URL, options?: WorkerOptions) {
    super(scriptURL, options);
    this.addEventListener("message", (event: MessageEvent<WorkspaceMapCommunityResponse>) => {
      const response = event.data;
      if (response?.identityKey !== identityKey) return;
      if (response.type === "complete") resolveWorker(response);
      else if (response.type === "error") rejectWorker(new Error("The actual relation community worker failed"));
    });
    this.addEventListener("error", () => rejectWorker(new Error("The actual relation community worker raised an error")));
  }
  override postMessage(message: unknown, transfer: Transferable[]): void;
  override postMessage(message: unknown, options?: StructuredSerializeOptions): void;
  override postMessage(message: unknown, options: Transferable[] | StructuredSerializeOptions = {}): void {
    const request = message as WorkspaceMapCommunityRequest | null;
    if (request?.type === "compute") {
      workerInputs.push({ identityKey: request.identityKey, workspaceId: request.input.workspaceId,
        nodeKeys: [...request.input.nodeKeys], sources: Array.from(request.input.edgeSources),
        targets: Array.from(request.input.edgeTargets), weights: Array.from(request.input.edgeWeights) });
    }
    if (Array.isArray(options)) super.postMessage(message, options);
    else super.postMessage(message, options);
  }
};

const stage = document.getElementById("stage") as HTMLDivElement;
// Fixture construction only: supply an ordinary, same-visible-scene in-memory
// snapshot. Never override runtime config or the actual worker response. The
// spec independently checks these units against observed native sizes/CSS.
const baselineBuffers = buildCosmosSceneBuffers(columns, readGraphTokens(stage, "light"), WORKSPACE_MAP_PC_10K_BUDGET);
const fallbackPositions = Array.from(baselineBuffers.positions);
const baselineBasis = buildWorkspaceRelationCollisionBasis(baselineBuffers.positions, baselineBuffers.pointSizes,
  stage.clientWidth, stage.clientHeight);
if (!baselineBasis) throw new Error("The fixed native fixture viewport has no valid baseline units");
const adapter = createWorkspaceCosmosAdapter(stage, columns, {
  // The explicit desktop budget owns the complete upstream GPU pipeline;
  // the default 2K budget uses the official CPU Worker. The separate
  // relationLaw probe explicitly owns the historical rest-log diagnostic.
  budget: WORKSPACE_MAP_PC_10K_BUDGET,
  initialPaused: true, forcedColors: false, reducedMotion: false,
  initialCoordinates: session === "matched" ? { columns, budget: WORKSPACE_MAP_PC_10K_BUDGET, positions: fixturePositions.slice(),
    collisionBasis: { ...baselineBasis }, forceBasis: { ...baselineBasis } } : undefined,
  callbacks: { onHover() {}, onSelect() {}, onPin() {} },
});
const frames = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
let tick = 0;
let alphaFromTick: number | null = null;
let worker: Extract<WorkspaceMapCommunityResponse, { type: "complete" }> | null = null;
let acceptedPositionUpload: number[] | null = null;
let accepted: ForceSnapshot | null = null;

function requireNative(): Graph {
  if (!observed.native) throw new Error("The production adapter has not created its native Cosmos graph");
  return observed.native;
}

function snapshot(): ForceSnapshot {
  const native = requireNative(), config = native.config;
  const canvas = stage.querySelector("canvas");
  const gl = canvas?.getContext("webgl2");
  if (!gl) throw new Error("The actual native fixture has no WebGL2 context");
  return { tick, alphaFromTick, positions: Array.from(native.getPointPositions()), running: native.isSimulationRunning,
    glError: gl.getError(), clusterIndexUploads: clusterIndexUploads.map(values => [...values]),
    clusterTargetUploads: clusterTargetUploads.map(values => [...values]),
    linkStrengthUploads: linkStrengthUploads.map(values => [...values]), nativeLinks: [...nativeLinks],
    config: { gravity: config.simulationGravity, center: config.simulationCenter, repulsion: config.simulationRepulsion,
      spring: config.simulationLinkSpring, distance: config.simulationLinkDistance, cluster: config.simulationCluster,
      friction: config.simulationFriction, collision: config.simulationCollision,
      collisionRadius: config.simulationCollisionRadius ?? null, collisionPadding: config.simulationCollisionPadding,
      spaceSize: config.spaceSize, simulationEnabled: config.enableSimulation } };
}

async function isolatedCollisionCase(
  name: CollisionCaseEvidence["name"], input: number[],
): Promise<CollisionCaseEvidence> {
  // A separate SDK-only control, not an override of the production adapter's
  // profile. Fresh native resources per case prevent previous velocity/FBO
  // state from masquerading as a collision response.
  stage.replaceChildren();
  let ticks = 0;
  let alpha: number | null = null;
  const native = new Graph(stage, {
    enableSimulation: false, rescalePositions: false, spaceSize: 4096,
    fitViewOnInit: false, transitionDuration: 0,
    simulationGravity: 0, simulationCenter: 0, simulationRepulsion: 0,
    simulationLinkSpring: 0, simulationCluster: 0,
    simulationCollision: 1, simulationCollisionRadius: 1.5, simulationCollisionPadding: 0,
    onSimulationTick(value) { ticks++; alpha = value; },
  });
  try {
    await native.ready;
    native.setPointPositions(Float32Array.from(input));
    native.setPointSizes(Float32Array.from([3, 3]));
    native.setLinks(new Float32Array());
    native.render(undefined, 0);
    await frames();
    native.setConfigPartial({ enableSimulation: true });
    native.pause();
    await frames();
    const gl = stage.querySelector("canvas")?.getContext("webgl2");
    if (!gl) throw new Error("The collision control has no native WebGL2 context");
    const before = Array.from(native.getPointPositions());
    const ticksBefore = ticks;
    const glErrors = [gl.getError()];
    native.start(INITIAL_ALPHA); native.pause();
    native.step(); // Exactly one real SDK/GPU force step, no automatic RAF ticks.
    const after = Array.from(native.getPointPositions());
    glErrors.push(gl.getError());
    const config = native.config;
    return { name, input: [...input], before, after, ticksBefore, ticksAfter: ticks,
      alphaAfter: alpha, running: native.isSimulationRunning, glErrors,
      config: { gravity: config.simulationGravity, center: config.simulationCenter,
        repulsion: config.simulationRepulsion, spring: config.simulationLinkSpring,
        cluster: config.simulationCluster, collision: config.simulationCollision,
        radius: config.simulationCollisionRadius!, padding: config.simulationCollisionPadding,
        friction: config.simulationFriction, spaceSize: config.spaceSize } };
  } finally { native.destroy(); }
}

window.__workspaceRelationForces = {
  status: "loading",
  async manual(ticks) {
    if (this.status !== "ready" || !worker) throw new Error("The actual worker/native fixture is not ready");
    if (!firstPositionUpload || !nativePointSizes || !calibrationViewport || !acceptedPositionUpload || !accepted) {
      throw new Error("The actual native fallback calibration inputs were not observed");
    }
    if (!ticks.length || ticks.some((value, index) => !Number.isSafeInteger(value) || value < 1 || value > 32
      || (index > 0 && value <= ticks[index - 1]))) throw new Error("Manual samples must be increasing ticks1–32");
    const native = requireNative();
    native.pause();
    native.setPointPositions(fixturePositions.slice());
    native.render(undefined, 0);
    await frames(); // Commit the public upload while paused, without any manual/native force step.
    tick = 0; alphaFromTick = null;
    const before = snapshot();
    native.start(INITIAL_ALPHA);
    native.pause(); // Preserve alpha but prevent automatic RAF simulation between requested steps.
    const samples: ForceSnapshot[] = [];
    for (let step = 1; step <= ticks[ticks.length - 1]; step++) {
      native.step();
      if (ticks.includes(step)) samples.push(snapshot());
    }
    return { scope: session === "matched"
      ? "32-document matched-session actual-worker/native manual shader-attribution oracle; not fresh layout or live2K quality/performance"
      : "32-document fresh actual-worker/native calibration acceptance before controlled manual samples; not pure cross-run attribution or live2K quality/performance",
      session,
      initialAlpha: INITIAL_ALPHA, swapped, reassignedFolders, identityKey,
      nodeIds: [...columns.nodeIds], nodeKeys: [...columns.nodeKeys], folderKeys: [...columns.nodeFolderKeys],
      edgeIds: [...columns.edgeIds], sources: Array.from(columns.edgeSources), targets: Array.from(columns.edgeTargets),
      weights: Array.from(columns.edgeWeights), crossFolder: Array.from(columns.edgeCrossFolder), degrees: Array.from(degrees),
      calibration: { fallbackPositions: [...fallbackPositions], pointSizes: [...nativePointSizes],
        viewport: { ...calibrationViewport } },
      firstPositionUpload: [...firstPositionUpload], acceptedPositionUpload: [...acceptedPositionUpload],
      accepted: structuredClone(accepted),
      fixturePositions: Array.from(fixturePositions), workerInputs: structuredClone(workerInputs),
      worker: { communityIds: Array.from(worker.communityIds), communityCount: worker.communityCount,
        initialPositions: Array.from(worker.initialPositions) }, before, samples };
  },
  async collisionOnly() {
    if (this.status !== "ready") throw new Error("The native fixture is not ready");
    this.destroy();
    // Pinned3.4: h=4096/min(512,max(32,ceil(4096/max(R,8))))=8.
    // R=1.5 => diameter3. At distance5 the own-cell centroid is only2.5
    // away, so self-inclusion creates a phantom response in x-offset0 passes.
    // Translate +3 in x: the SAME distance5 pair is in distinct cells in all
    // four offsets. The distance2 control is a real collision in either SDK.
    const samples: CollisionCaseEvidence[] = [];
    samples.push(await isolatedCollisionCase("separated-same-cell", [1000.5, 1000.5, 1005.5, 1000.5]));
    samples.push(await isolatedCollisionCase("separated-translated", [1003.5, 1000.5, 1008.5, 1000.5]));
    samples.push(await isolatedCollisionCase("overlap", [1000.5, 1000.5, 1002.5, 1000.5]));
    return { scope: "Two-point collision-only actual SDK one-step self-centroid regression; not product profile or live2K acceptance",
      pointSizes: [3, 3], samples };
  },
  async relationLaw() {
    if (this.status !== "ready") throw new Error("The native fixture is not ready");
    this.destroy();
    return runRestLogLawProbe();
  },
  destroy() {
    adapter.destroy(); window.Worker = NativeWorker;
    Graph.prototype.render = originalRender; Graph.prototype.setPointClusters = originalClusters;
    Graph.prototype.setClusterPositions = originalTargets; Graph.prototype.setLinkStrength = originalStrengths;
    Graph.prototype.setLinks = originalLinks;
    Graph.prototype.setPointPositions = originalPositions; Graph.prototype.setPointSizes = originalSizes;
    Graph.prototype.setConfigPartial = originalConfig;
  },
};

void (async () => {
  const [, response] = await Promise.all([adapter.whenSettled(), workerResult]);
  worker = response;
  // The passive message listener runs before the adapter's Promise outcome
  // continuation. Wait for its observable acceptance/queue state, rather than
  // treating our observation Promise as proof that pendingCommunity exists.
  const deadline = performance.now() + 2_000;
  while (stage.dataset.mapCommunityLayoutStatus !== "queued" && stage.dataset.mapCommunityLayoutStatus !== "applied") {
    if (performance.now() >= deadline) throw new Error("The completed worker result did not reach the adapter queue");
    await frames();
  }
  // Resume drains a real late worker result queued by the explicit initial
  // pause. Immediate pause preserves its seed and native permissions without
  // allowing an uncontrolled animation frame before the manual oracle.
  adapter.resumeLayout(); adapter.stopLayout();
  await frames();
  if (stage.dataset.mapCommunityLayoutStatus !== "applied") throw new Error("The actual worker result was not accepted");
  if (!latestPositionUpload) throw new Error("The accepted scene has no actual native position upload");
  acceptedPositionUpload = [...latestPositionUpload];
  accepted = snapshot();
  const native = requireNative(), originalTick = native.config.onSimulationTick;
  native.setConfigPartial({ onSimulationTick(alpha, ...args) {
    tick++; alphaFromTick = alpha;
    originalTick?.call(this, alpha, ...args);
  } });
  window.__workspaceRelationForces.status = "ready";
})().catch((error: unknown) => {
  window.__workspaceRelationForces.status = "error";
  window.__workspaceRelationForces.error = error instanceof Error ? error.message : String(error);
});
