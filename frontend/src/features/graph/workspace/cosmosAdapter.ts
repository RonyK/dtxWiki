import {
  createCosmosRenderer,
  CosmosWebGLError,
  type CosmosRenderer,
  type CosmosRendererCallbacks,
  type CosmosRendererOptions,
  type CosmosLayoutStatus,
  type CosmosCoordinateSnapshot,
  type CosmosSafeInsets,
} from "../shared/cosmos/cosmosCore";
import { sameCosmosIndexedGeometry } from "../shared/workspaceMapGeometry";
import { canAnimateWorkspaceRelationLayout } from "../shared/workspaceLayoutPolicy";
import {
  assertWorkspaceMapWithinBudget, isWorkspaceMapWithinBudget,
  WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget,
} from "../shared/workspaceMapBudget";
import {
  WORKSPACE_MAP_NODE_KIND, workspaceMapAdmissionCounts, workspaceMapContainedDocumentCounts,
  type WorkspaceMapColumns,
} from "../map/sceneColumns";
import { createWorkspaceMapCommunityJob } from "../map/communityJob";
import { buildCosmosSceneBuffers, buildCosmosRelationColors, isDenseWorkspaceMapOverview } from "./cosmosScene";
import { createWorkspaceLayoutSolver } from "./layoutSolver";

export type WorkspaceCosmosAdapter = CosmosRenderer;
export type WorkspaceCosmosCallbacks = CosmosRendererCallbacks;
export type WorkspaceMapLayoutStatus = CosmosLayoutStatus;
export type WorkspaceMapCoordinateSnapshot = CosmosCoordinateSnapshot;
export type WorkspaceMapSafeInsets = CosmosSafeInsets;
export type WorkspaceCosmosOptions = CosmosRendererOptions & { budget?: WorkspaceMapBudget };
export class WorkspaceMapWebGLError extends CosmosWebGLError {
  constructor(reason: "webgl2" | "float-render-target") {
    super(reason);
    this.message = `Workspace Map requires WebGL2 with float render targets (${reason})`;
    this.name = "WorkspaceMapWebGLError";
  }
}

/** The Map's label ordering, independent of native rendering and label placement. */
function rankMapLabels(columns: WorkspaceMapColumns, documents: number): number[] {
  const byKey = (left: number, right: number) =>
    columns.nodeKeys[left] < columns.nodeKeys[right] ? -1 : columns.nodeKeys[left] > columns.nodeKeys[right] ? 1 : 0;
  const rows = Array.from({ length: columns.nodeIds.length }, (_, index) => index);
  const contained = workspaceMapContainedDocumentCounts(columns);
  const folders = rows.filter(index => columns.nodeKinds[index] === WORKSPACE_MAP_NODE_KIND.folder
    && contained[index] > 0 && contained[index] < documents)
    .sort((left, right) => contained[right] - contained[left] || byKey(left, right)).slice(0, 8);
  return [...folders, ...rows.filter(index => columns.nodeKinds[index] !== WORKSPACE_MAP_NODE_KIND.folder)
    .sort((left, right) => columns.nodeSizes[right] - columns.nodeSizes[left] || byKey(left, right))
    .slice(0, 40 - folders.length)];
}

/** Map admission, seed computation and presentation policy bound to the common renderer. */
export function createWorkspaceCosmosAdapter(
  container: HTMLDivElement,
  columns: WorkspaceMapColumns,
  options: WorkspaceCosmosOptions,
): WorkspaceCosmosAdapter {
  const budget = { ...(options.budget ?? WORKSPACE_MAP_DEFAULT_BUDGET) };
  const solver = createWorkspaceLayoutSolver(budget);
  // Columns are replaced atomically. Share one scan across every scene policy
  // and buffer build, without retaining retired columns after their renderer.
  const counts = new WeakMap<WorkspaceMapColumns, ReturnType<typeof workspaceMapAdmissionCounts>>();
  const admissionCounts = (input: WorkspaceMapColumns) => {
    let admitted = counts.get(input);
    if (!admitted) { admitted = workspaceMapAdmissionCounts(input); counts.set(input, admitted); }
    return admitted;
  };
  const withinBudget = (input: WorkspaceMapColumns) => isWorkspaceMapWithinBudget(admissionCounts(input), budget);
  return createCosmosRenderer(container, columns, options, {
    budget,
    legacySnapshotBudget: WORKSPACE_MAP_DEFAULT_BUDGET,
    smallSceneLimit: WORKSPACE_MAP_DEFAULT_BUDGET.documents,
    solver,
    admissionCounts,
    assertColumns: input => assertWorkspaceMapWithinBudget(admissionCounts(input), budget),
    withinBudget,
    overview: input => isDenseWorkspaceMapOverview(input, budget, admissionCounts(input)),
    canAnimate: input => canAnimateWorkspaceRelationLayout(admissionCounts(input).nodeCount, budget),
    sameGeometry: sameCosmosIndexedGeometry,
    snapshotGeometryIncludesGlyphs: true,
    linkEndpointMarkers: true,
    prepareScene: (input, tokens, presentation) => buildCosmosSceneBuffers(input, tokens, budget, presentation, admissionCounts(input)),
    relationColors: (input, colors, groups, incident) => buildCosmosRelationColors(input, colors, groups, incident, budget, admissionCounts(input)),
    rankLabels: input => rankMapLabels(input, admissionCounts(input).nodeCount),
    visualKind: (input, index) => input.nodeVisualKinds?.[index]
      ?? (["context", "library", "cluster", "folder", "agent"] as const)[input.nodeKinds[index]],
    createSeedJob: createWorkspaceMapCommunityJob,
    createWebGLError: reason => new WorkspaceMapWebGLError(reason),
    initializeDiagnostics() {
      for (const key of ["mapPhysics", "mapLayoutRunning", "mapLayoutTicks", "mapLayoutPhase"]) delete container.dataset[key];
      Object.assign(container.dataset, solver.initialDiagnostics());
    },
    diagnose(name, value) {
      const key = `map${name[0].toUpperCase()}${name.slice(1)}`;
      if (value === undefined) delete container.dataset[key];
      else container.dataset[key] = value;
    },
    decorateLabelLayer(layer) { layer.dataset.workspaceMapLabels = ""; },
    decorateSelectionRing(ring) { ring.dataset.workspaceMapSelectionRing = ""; },
  });
}
