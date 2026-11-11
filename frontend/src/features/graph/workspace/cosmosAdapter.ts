import { NativeGraph, type GraphConfig } from "./cosmosNative";

import {
  createWorkspaceMapCommunityJob,
  workspaceMapCommunityIdentityKey,
  type WorkspaceMapCommunityOutcome,
} from "../map/communityJob";
import { WORKSPACE_MAP_NODE_KIND, workspaceMapAdmissionCounts, workspaceMapContainedDocumentCounts,
  type WorkspaceMapColumns } from "../map/sceneColumns";
import { createGraphHoverChip, GRAPH_EDGE_CHIP_OFFSET_PX, graphEdgeChipAnchor } from "../shared/hoverChip";
import { GRAPH_EDGE_PATTERN, GRAPH_EDGE_WIDTH_PX, readGraphTokens } from "../shared/graphTokens";
import { FORCED_OUTLINE_INSETS, GRAPH_RING_BAND_PX, GRAPH_RING_GAP_PX, GRAPH_RING_WIDTH_PX, outlinePathData, shapeOutline } from "../shared/nodeShapes";
import { sameGraphVisibleIdentity, type GraphPoint, type GraphViewport, type GraphVisibleIdentity } from "../shared/rendererTypes";
import { canAnimateWorkspaceRelationLayout } from "../shared/workspaceLayoutPolicy";
import { assertWorkspaceMapWithinBudget, isWorkspaceMapWithinBudget, WORKSPACE_MAP_DEFAULT_BUDGET, type WorkspaceMapBudget } from "../shared/workspaceMapBudget";
import { sameWorkspaceMapIndexedGeometry } from "../shared/workspaceMapGeometry";
import {
  buildCosmosRelationColors, buildCosmosSceneBuffers, incidentStar,
  isReadableWorkspaceMapScene, type CosmosSceneBuffers,
} from "./cosmosScene";
import {
  createWorkspaceCountImageCache, createWorkspaceCountMeasure, workspaceAnnulusImage, workspaceClusterCountText,
} from "./pointImages";
import { buildWorkspaceRelationCollisionBasis, type WorkspaceRelationCollisionBasis } from "./relationPhysics";
import { detectWorkspaceMapWebGL } from "./webglSupport";
import { createInitialWorkspaceCamera, type InitialWorkspaceCamera } from "./initialCamera";
import { createWorkspaceNativeCoordinateSpace, WORKSPACE_MAP_CANONICAL_SPACE_SIZE } from "./nativeCoordinateSpace";
import { createWorkspaceLayoutSolver, type WorkspacePositionLayout, type WorkspacePositionUnitBasis } from "./layoutSolver";
import { placeWorkspaceMapLabels } from "./labelPlacement";
import { createWorkspaceNativePointPicker } from "./nativePointPicker";
import { createWorkspaceTrackedPositions } from "./gpuTrackedPositions";

/** Layout progress is separate from GPU readiness and never promises convergence. */
export type WorkspaceMapLayoutStatus = "arranging" | "live" | "paused" | "motion-off";

export type WorkspaceCosmosCallbacks = {
  onHover(id: string | null): void;
  onSelect(id: string | null): void;
  onPin?(id: string, position: GraphPoint): void;
  /** `pan` stores the graph-space center, not Cytoscape's pixel translation. */
  onViewport?(viewport: GraphViewport): void;
  /** On restoration, the host should remount a fresh engine. */
  onContextStatus?(status: "lost" | "restored"): void;
  /** One-shot late GPU failure; host retires this scene and uses the list. */
  onRuntimeError?(error: unknown): void;
  /** Change-only lifecycle state for the current visible scene. */
  onLayoutStatusChange?(status: WorkspaceMapLayoutStatus | null, identity: GraphVisibleIdentity): void;
};

export type WorkspaceCosmosOptions = {
  /** The caller's admitted device/scale budget, fixed for this renderer. */
  budget?: WorkspaceMapBudget;
  hoverChip?: HTMLDivElement | null;
  /** Localized name of one relation of a WORKSPACE_MAP_EDGE_KIND; without it, relation hover shows no chip. */
  relationLabel?(kind: number): string;
  callbacks: WorkspaceCosmosCallbacks;
  forcedColors: boolean;
  initialViewport?: GraphViewport | null;
  /** Preserved user pause must apply before native construction/worker ready. */
  initialPaused?: boolean;
  /** Ephemeral coordinates from a healthy renderer of this exact visible scene. */
  initialCoordinates?: WorkspaceMapCoordinateSnapshot | null;
  reducedMotion: boolean;
  showEdges?: boolean;
  showLabels?: boolean;
  /** Canvas pixels covered by floating host controls, read at each Fit/Center. */
  getSafeInsets?(): WorkspaceMapSafeInsets | null;
};

/** CSS pixels from each canvas edge that host overlays cover. */
export type WorkspaceMapSafeInsets = { top: number; right: number; bottom: number; left: number };

/** The canvas size with its covered edges, all in CSS pixels. */
type UncoveredFrame = WorkspaceMapSafeInsets & { width: number; height: number };

export type WorkspaceMapCoordinateSnapshot = {
  /** Older, budgetless snapshots belong only to the default 2K budget. */
  budget?: WorkspaceMapBudget;
  columns: WorkspaceMapColumns;
  positions: Float32Array;
  /** Same-scene transient physics ownership; never browser/server storage. */
  collisionBasis?: WorkspaceRelationCollisionBasis;
  /** Force units are independent of the accepted worker's collision clearance. */
  forceBasis?: WorkspaceRelationCollisionBasis;
  /** Only accepted solved rows may skip cold D3 startup on a same-scene remount. */
  layoutReady?: boolean;
  /** Immutable CSS-to-world collision reference belonging to these solved rows. */
  unitBasis?: WorkspacePositionUnitBasis;
};

export interface WorkspaceCosmosAdapter {
  captureCoordinates(): WorkspaceMapCoordinateSnapshot | null;
  centerOn(id: string): void;
  destroy(): void;
  fit(): void;
  focus(id: string | null): void;
  getViewport(): GraphViewport | null;
  pin(id: string, pinned: boolean, position?: GraphPoint, intent?: { reconcile?: boolean }): GraphPoint | null;
  /** Explicit navigation only; host ownership is checked again at paint time. */
  revealAfterLayout(id: string, signal: AbortSignal, isCurrent: () => boolean, onFinished?: () => void): void;
  replaceColumns(columns: WorkspaceMapColumns): void;
  reset(): void;
  resize(): void;
  resumeLayout(reheat?: boolean): void;
  select(id: string | null): void;
  setEdgesVisible(enabled: boolean): void;
  setLabelsVisible(enabled: boolean): void;
  setReducedMotion(enabled: boolean): void;
  setSearchMatches(ids: ReadonlySet<string> | null): void;
  setViewport(viewport: GraphViewport): void;
  stopLayout(preserveInitialFit?: boolean): void;
  whenSettled(): Promise<void>;
  zoomBy(factor: number): void;
}

/** A dedicated failure type lets the host choose the list fallback. */
export class WorkspaceMapWebGLError extends Error {
  constructor(public readonly reason: "webgl2" | "float-render-target") {
    super(`Workspace Map requires WebGL2 with float render targets (${reason})`);
    this.name = "WorkspaceMapWebGLError";
  }
}

/** Smallest uncovered canvas span that Fit and Center frame into. */
const MIN_UNCOVERED_PIXELS = 96;
/** Fit leaves this fraction of the frame empty on each side. */
const FIT_PADDING = 0.08;
/** Titles republish at least this often, without a GPU read; moving sources publish every frame. */
const LABEL_HEARTBEAT_MS = 200;
/** Title anchors that moved less than this keep their last publication. */
const LABEL_MOTION_EPSILON_PX = 0.5;
/** Titles fade in and out instead of popping, unless motion is reduced. */
const LABEL_FADE_MS = 120;
/** Overview label slots reserved for the TreeRail folders holding the most documents. */
const MAJOR_FOLDER_LABELS = 8;
/** Titles and cluster counts share one face. */
const WORKSPACE_MAP_LABEL_FONT_FAMILY = "system-ui, sans-serif";
/** A camera gesture without zoom events this long no longer holds titles. */
const LABEL_GESTURE_LAPSE_MS = 1_000;
/** GPU rows reach titles a few simulation steps late; titles look this far ahead at most. */
const LABEL_PREDICTION_STEPS = 4;

function validViewport(viewport: GraphViewport | null | undefined): viewport is GraphViewport {
  return Boolean(viewport && Number.isFinite(viewport.zoom) && viewport.zoom > 0
    && Number.isFinite(viewport.pan.x) && Number.isFinite(viewport.pan.y));
}

function validCollisionBasis(basis: WorkspaceRelationCollisionBasis | null | undefined): basis is WorkspaceRelationCollisionBasis {
  // Independent snapshot collision units no longer pass through the force
  // builder. The shader adds both padded radii using Float32 uniforms.
  return Boolean(basis && [basis.radius, basis.padding, 2 * (basis.radius + basis.padding)]
    .every((value) => Number.isFinite(value) && value > 0
      && Number.isFinite(Math.fround(value)) && Math.fround(value) > 0));
}

function restoredCoordinates(
  columns: WorkspaceMapColumns,
  snapshot: WorkspaceMapCoordinateSnapshot | null | undefined,
  budget: WorkspaceMapBudget,
): Float32Array | null {
  const snapshotBudget = snapshot?.budget ?? WORKSPACE_MAP_DEFAULT_BUDGET;
  if (!snapshot || snapshotBudget.documents !== budget.documents || snapshotBudget.relations !== budget.relations
    || !isWorkspaceMapWithinBudget(workspaceMapAdmissionCounts(snapshot.columns), budget)
    || !isWorkspaceMapWithinBudget(workspaceMapAdmissionCounts(columns), budget)
    || !sameGraphVisibleIdentity(columns.identity, snapshot.columns.identity)
    || !sameWorkspaceMapIndexedGeometry(columns, snapshot.columns)
    || !(snapshot.positions instanceof Float32Array)
    || snapshot.positions.length !== columns.nodeIds.length * 2
    || !snapshot.positions.every(Number.isFinite)) return null;
  return snapshot.positions.slice();
}

/**
 * Cosmos is imported only with this Map-only module. The caller should first
 * run detectWorkspaceMapWebGL from the tiny support module, then import here.
 */
export function createWorkspaceCosmosAdapter(
  container: HTMLDivElement,
  initialColumns: WorkspaceMapColumns,
  options: WorkspaceCosmosOptions,
): WorkspaceCosmosAdapter {
  const budget = { ...(options.budget ?? WORKSPACE_MAP_DEFAULT_BUDGET) };
  const solver = createWorkspaceLayoutSolver(budget);
  assertWorkspaceMapWithinBudget(workspaceMapAdmissionCounts(initialColumns), budget);
  const support = detectWorkspaceMapWebGL();
  if (!support.supported) throw new WorkspaceMapWebGLError(support.reason);
  const mode = options.forcedColors ? "forced"
    : document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  const tokens = readGraphTokens(container, mode);
  const interactionInk = options.forcedColors ? tokens.node.context
    : mode === "dark" ? "#f8fafc" : "#0f172a";
  let columns = initialColumns;
  let admittedFor: WorkspaceMapColumns | null = null;
  let admittedValue = { nodeCount: 0, edgeCount: 0 };
  // Budget and scale decisions count documents and relations, not TreeRail structure.
  const admitted = () => {
    if (admittedFor !== columns) { admittedFor = columns; admittedValue = workspaceMapAdmissionCounts(columns); }
    return admittedValue;
  };
  let sparseScene = admitted().edgeCount <= 1_000;
  let readableScene = isReadableWorkspaceMapScene(admitted().nodeCount, admitted().edgeCount, budget);
  // The explicit renderer budget bounds forces as well as drawing. This is
  // not a target-hardware performance qualification. Folders exert no force.
  const canAnimateLayout = () => canAnimateWorkspaceRelationLayout(admitted().nodeCount, budget);
  // Cluster counts: the reader's locale, measured and drawn in the label face.
  const countLocale = document.documentElement.lang || undefined;
  const sceneOptions = {
    forcedColors: options.forcedColors,
    countText: (count: number) => workspaceClusterCountText(count, countLocale),
    measureCount: createWorkspaceCountMeasure(WORKSPACE_MAP_LABEL_FONT_FAMILY),
  };
  let buffers = buildCosmosSceneBuffers(columns, tokens, budget, sceneOptions);
  buffers.positions = solver.initialPositions(columns, buffers.positions);
  // Canonical worker/public/snapshot units never depend on GPU capability.
  // Resolve this SDK-only conversion before the first coordinate upload.
  let nativeSpace = createWorkspaceNativeCoordinateSpace(WORKSPACE_MAP_CANONICAL_SPACE_SIZE * 2);
  // Freeze force units from the original visible seed. Collision clearance
  // instead follows the accepted initial geometry, before user pins are merged.
  let forceSeed = buffers.positions.slice();
  let collisionSeed = forceSeed;
  const initialCoordinates = restoredCoordinates(columns, options.initialCoordinates, budget);
  if (initialCoordinates) buffers.positions = initialCoordinates;
  let collisionBasis = initialCoordinates && validCollisionBasis(options.initialCoordinates?.collisionBasis)
    ? { ...options.initialCoordinates.collisionBasis } : null;
  // A legacy snapshot used one basis for both. An explicitly invalid new
  // force basis must not adopt a worker-derived collision basis as force units.
  const restoredForceBasis = options.initialCoordinates?.forceBasis === undefined
    ? options.initialCoordinates?.collisionBasis : options.initialCoordinates.forceBasis;
  let forceBasis = initialCoordinates && validCollisionBasis(restoredForceBasis)
    ? { ...restoredForceBasis } : null;
  let forceBasisApplied = false;
  let collisionBasisApplied = false;
  let calibrationViewport: { width: number; height: number } | null = null;
  let preservesSessionCoordinates = initialCoordinates !== null;
  let layoutPhase = "arranging";
  let positionLayoutReady = initialCoordinates !== null && options.initialCoordinates?.layoutReady === true;
  let positionUnitBasis = initialCoordinates ? options.initialCoordinates?.unitBasis : undefined;
  let nodeById = new Map(columns.nodeIds.map((id, index) => [id, index]));
  let edgeById = new Map(columns.edgeIds.map((id, index) => [id, index]));
  let destroyed = false;
  let ready = false;
  let nativeContext: WebGL2RenderingContext | null = null;
  let contextLost = false;
  let simulationFailed = false;
  let reducedMotion = options.reducedMotion;
  let showEdges = options.showEdges !== false;
  let showLabels = options.showLabels !== false;
  let paused = options.initialPaused === true;
  let selectedId: string | null = null;
  let focusedId: string | null = null;
  let searchMatchedIds: ReadonlySet<string> | null = null;
  const chip = createGraphHoverChip(container, options.hoverChip);
  let hoveredIndex: number | null = null;
  let hoveredEdge: number | null = null;
  let pointerOnCanvas = true;
  // Client coordinates of the latest canvas pointer, for the relation chip anchor.
  let pointerClient: { x: number; y: number } | null = null;
  // Preserve native pick ownership for drag independently of the chip.
  let nativeHoveredIndex: number | null = null;
  let draggedPoint: { index: number; nodeKey: string; identityKey: string; startX: number; startY: number; moved: boolean } | null = null;
  let initialFadeZoom: number | null = null;
  let appliedFadeZoom: number | null = null;
  // The native shader multiplies every point by this zoom-dependent scale.
  let appliedPointSizeScale = 1;
  let pointSpriteLimit: { ratio: number; diameter: number } | null = null;
  let fitReferencePending = false;
  let pendingViewport = options.initialViewport;
  // Native restoration narrows centers to Float32. Its asynchronous zoom end
  // is not new user intent and must not replace the caller's saved viewport.
  // Explicit navigation retires this ownership, not a synchronous call scope.
  let restoredCameraIntent = validViewport(options.initialViewport);
  let cameraInteractedBeforeFit = false;
  let fitAfterCommunity = !preservesSessionCoordinates && !validViewport(options.initialViewport);
  let firstFitCompleted = false;
  let publishedPositionFitPending = solver.shouldFitPublishedRows(preservesSessionCoordinates || validViewport(options.initialViewport));
  let publishedPositionFitFrame: number | null = null;
  let communityTerminal = false;
  let pendingReveal: { id: string; signal: AbortSignal; isCurrent: () => boolean; identityKey: string; onFinished?: () => void } | null = null;
  let revealFrame: number | null = null;
  let dataCommitted = false;
  let pendingCommunity: Extract<WorkspaceMapCommunityOutcome, { kind: "complete" }> | null = null;
  let appliedCommunity: Extract<WorkspaceMapCommunityOutcome, { kind: "complete" }> | null = null;
  let appliedLinkColorSource: Float32Array | null = null;
  let appliedLinkColorCommunity: Uint32Array | undefined;
  let appliedLinkColorStar = "";
  let scheduledCommunityFit: Extract<WorkspaceMapCommunityOutcome, { kind: "complete" }> | null = null;
  const communityJob = createWorkspaceMapCommunityJob();
  const pinned = new Set<number>();
  const pinOwners = new Map<number, string>();
  // Only the actual applied owner of each pin may retire. Repeated projection
  // metadata updates must not accumulate aliases for all intermediate IDs.
  const retiredPinIds = new Map<string, number>();
  let labelLayer: HTMLDivElement | null = null;
  let selectionRing: { svg: SVGSVGElement; gap: SVGPathElement; ring: SVGPathElement } | null = null;
  let labelElements: HTMLSpanElement[] = [];
  let rankedLabelIndices: number[] = [];
  let labelIndices: number[] = [];
  let labelTimer: number | null = null;
  let trackedPositions: ReturnType<typeof createWorkspaceTrackedPositions> | null = null;
  let nativePointPickerAttached = false;
  let cachedLabelPositions: ReadonlyMap<number, [number, number]> | null = null;
  const labelMeasurements = new WeakMap<HTMLSpanElement, {
    text: string; full: boolean; maxWidth: number; width: number; height: number;
  }>();
  // The dragged point follows the pointer; its other sources lag the gesture.
  let dragLabelPoint: { index: number; point: [number, number] } | null = null;
  // A user camera gesture or point drag keeps the ordinary shown titles. The
  // camera hold lapses without zoom events, should its end never arrive.
  let zoomGesture = false;
  let lastGestureZoom = -Infinity;
  let dragGesture = false;
  // Set whenever the next publication must rewrite every title, even if no
  // anchor moved: candidates, text, size, gesture state or the heartbeat.
  let labelPublicationStale = true;
  let publishedAnchors: Array<[number, number] | null> = [];
  // Placement history by node, not by DOM slot: a candidate change rebinds slots.
  let placedLabelNodes = new Set<number>();
  let shownLabelNodes = new Set<number>();
  let labelViewport: { width: number; height: number } | null = null;
  // At most one opacity fade per title. Every fade starts at the opacity on
  // screen, so reversing or interrupting one never jumps (linear: exact).
  const labelFades = new WeakMap<HTMLSpanElement, { animation: Animation; from: number; to: number }>();
  // Asynchronous GPU-tracked rows: one read per simulation frame, one in flight.
  let trackedReadQueued = false;
  let trackedReadExplicit = false;
  // A wanted read was refused behind an unsignaled one: read once it lands.
  let trackedReadRetry = false;
  // A GPU read delivers the rows of the step it was issued at, one or more
  // steps after that step was drawn. Titles advance each delivered row along
  // its per-step velocity to the step on screen, so they stay on their points.
  let simulationStep = 0;
  let trackedReadStep = 0;
  // Velocity spans asynchronous deliveries only; the prediction horizon
  // starts at whichever read last filled the title rows.
  let deliveredRowsStep = 0;
  let cachedRowsStep = 0;
  let deliveredTrackedRows: ReadonlyMap<number, [number, number]> | null = null;
  let trackedVelocity = new Map<number, [number, number]>();
  // GPU rows equal the last upload until a read lands after it: titles follow
  // an upload in its own frame instead of waiting for that read.
  let uploadedRows: Float32Array | null = null;
  let initialCamera: InitialWorkspaceCamera | null = null;
  let positionLayout: WorkspacePositionLayout | null = null;
  let positionLayoutEpoch = 0;
  let positionLayoutTicks = 0;
  let publishedLayoutStatus: { status: WorkspaceMapLayoutStatus | null; identity: GraphVisibleIdentity } | null = null;

  function baseLinkDistanceRange(): [number, number] {
    return sparseScene ? [40, 350] : readableScene ? [30, 220] : [10, 60];
  }

  function linkTransparencyFloor(): number {
    return options.forcedColors ? 1 : sparseScene ? 0.65 : readableScene ? 0.35 : 0.03;
  }

  function keepOverviewContext(): boolean {
    return admitted().nodeCount > 1_000 && readableScene;
  }

  function canInitializeRelationPhysics(): boolean {
    return !destroyed && !contextLost && !simulationFailed && firstFitCompleted && collisionBasisApplied
      // Display the fallback without animating arbitrary ID-order geometry.
      // A restored session already owns its coordinates; a failed/timed-out
      // worker may explicitly release the fallback instead of waiting forever.
      && (preservesSessionCoordinates || appliedCommunity !== null || communityTerminal)
      && canAnimateLayout();
  }

  function shouldRunRelationPhysics(): boolean {
    return canInitializeRelationPhysics()
      // A Worker is not throttled with the hidden page's RAF. Stop its CPU
      // loop explicitly without changing the existing native PC lifecycle.
      && solver.permitsVisibility(document.visibilityState === "hidden")
      && !paused && !reducedMotion && canAnimateLayout();
  }

  function reheatRelationPhysics(alpha: number) {
    if (!ready || !shouldRunRelationPhysics()) return;
    syncRelationPhysics(alpha);
  }

  function wakeReleasedNativePins(released: boolean) {
    // A settled PC map has no running forces to adopt a released body. Wake
    // that real edit once; pin restoration, no-op echoes and live alpha stay
    // untouched. The normal permission gate still owns pause/motion/lifecycle.
    if (released && admitted().nodeCount > WORKSPACE_MAP_DEFAULT_BUDGET.documents
      && !graph.isSimulationRunning) reheatRelationPhysics(.15);
  }

  function initializeRelationCollision(): boolean {
    if (!ready || destroyed || contextLost || !canAnimateLayout() || collisionBasisApplied) return false;
    const canvas = container.querySelector<HTMLCanvasElement>("canvas");
    // A valid restored basis still needs a drawable native scene before it
    // may be applied. Keep its values while a hidden remount awaits Resize.
    if (!canvas || !Number.isInteger(canvas.clientWidth) || !Number.isInteger(canvas.clientHeight)
      || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) return false;
    // Worker timing or a resize while it runs must not choose new world units.
    calibrationViewport ??= { width: canvas.clientWidth, height: canvas.clientHeight };
    const { width, height } = calibrationViewport;
    forceBasis ??= buildWorkspaceRelationCollisionBasis(forceSeed, buffers.pointSizes, width, height);
    if (!forceBasis) return false;
    if (!collisionBasis) {
      collisionBasis = buildWorkspaceRelationCollisionBasis(collisionSeed, buffers.pointSizes,
        width, height);
    }
    if (!collisionBasis) return false;
    // Only the first commit owns forces. A worker seed can replace provisional
    // fallback collision clearance, never gravity/charge/spring/rest distance.
    const nativeCollision = nativeSpace.toNativeCollisionBasis(collisionBasis);
    const radii = solver.collisionRadii(buffers.pointSizes, buffers.pointShapes, collisionBasis,
      maxPointDiameterCss(), nativeSpace.scale, admitted().nodeCount, buffers.collisionPointSizes);
    if (radii) graph.setPointCollisionRadii(radii);
    graph.setConfigPartial(solver.collisionConfig(forceBasisApplied,
      nativeSpace.toNativeCollisionBasis(forceBasis), nativeCollision, nativeSpace.scale, admitted().nodeCount));
    forceBasisApplied = true;
    collisionBasisApplied = true;
    return true;
  }

  const graph = new NativeGraph(container, {
    backgroundColor: tokens.surface,
    spaceSize: WORKSPACE_MAP_CANONICAL_SPACE_SIZE,
    randomSeed: initialColumns.identity.workspaceId,
    fitViewOnInit: false,
    rescalePositions: false,
    transitionDuration: 0,
    pointSizeScale: 1,
    scalePointsOnZoom: false,
    enableSimulation: false,
    simulationLinkDistRandomVariationRange: [1, 1],
    simulationCluster: 0,
    // Every link has the legend's one width, at every scene size.
    linkDefaultWidth: GRAPH_EDGE_WIDTH_PX,
    hoveredLinkWidthIncrease: 0.6,
    focusedLinkWidthIncrease: 0.6,
    // Legend dashes: citation on the dashed style, containment on the dotted one.
    linkDashLength: GRAPH_EDGE_PATTERN.citation[0],
    linkDashGap: GRAPH_EDGE_PATTERN.citation[1],
    linkDottedDashLength: GRAPH_EDGE_PATTERN.membership[0],
    linkDottedDashGap: GRAPH_EDGE_PATTERN.membership[1],
    // Legend folder/grouping ring: the native ring shape's band, CSS px.
    pointRingWidth: GRAPH_RING_BAND_PX,
    // Cluster counts and the Agent outline are distance-field images.
    pointImageSdf: true,
    // Native shaders emit straight RGB with faded alpha; source-over blending
    // is required before the premultiplied canvas is composited by the browser.
    linkBlending: true,
    linkVisibilityDistanceRange: baseLinkDistanceRange(),
    // Small maps need readable long links. Dense scenes keep the low-opacity
    // ramp so tens of thousands of lines do not turn into an opaque blanket.
    linkVisibilityMinTransparency: linkTransparencyFloor(),
    // A selected star in the 2k overview must not erase every other community.
    // Keep non-neighbors subdued but visible so users retain their location.
    pointGreyoutColor: keepOverviewContext() ? tokens.node.folder : undefined,
    pointGreyoutOpacity: keepOverviewContext() ? 0.3 : undefined,
    linkGreyoutOpacity: keepOverviewContext() ? 0.08 : 0.1,
    // No native focused point: its ring is always a circle and its colour
    // keeps a fixed alpha. The shape-following overlay draws the selection.
    hoveredPointRingColor: interactionInk,
    renderHoveredPointRing: false,
    hoveredPointCursor: "pointer",
    hoveredLinkCursor: "pointer",
    renderLinks: showEdges,
    enableDrag: true,
    // A paused or cooled simulation has no further frames: read its last rows once.
    onSimulationPause() { if (ready && !destroyed && !contextLost) queueTrackedRead(true); },
    onSimulationEnd() { if (ready && !destroyed && !contextLost) queueTrackedRead(true); },
    onSimulationTick(alpha) {
      simulationStep += 1;
      if (ready && !destroyed && !contextLost) { updateHoverChip(); queueTrackedRead(); }
      // Keep the current 2K relation graph live after natural cooling without
      // an interval or stale generation callback. Initial camera framing has
      // its own bounded readback/ownership window, independent of physics.
      // Equilibrium
      // need not visibly jitter; a later drag/pin still reaches live forces.
      initialCamera?.tick(alpha);
      // Expanded default-native PC maps retain upstream natural cooling.
      // Explicit Resume and moved drag/pin gestures still wake their forces.
      if (alpha < 0.02 && admitted().nodeCount <= WORKSPACE_MAP_DEFAULT_BUDGET.documents) reheatRelationPhysics(0.04);
    },
    onDragStart(event) {
      draggedPoint = nativeHoveredIndex !== null && columns.nodeKeys[nativeHoveredIndex] && !destroyed && !contextLost
        && Number.isFinite(event.x) && Number.isFinite(event.y)
        ? { index: nativeHoveredIndex, nodeKey: columns.nodeKeys[nativeHoveredIndex], identityKey: workspaceMapCommunityIdentityKey(columns),
          startX: event.x, startY: event.y, moved: false } : null;
      if (draggedPoint) { dragGesture = true; stopPositionLayout(); }
    },
    onDrag(event) {
      const drag = draggedPoint;
      if (!drag || destroyed || contextLost || drag.identityKey !== workspaceMapCommunityIdentityKey(columns)
        || columns.nodeKeys[drag.index] !== drag.nodeKey || !Number.isFinite(event.x) || !Number.isFinite(event.y)) return;
      if (!drag.moved && Math.hypot(event.x - drag.startX, event.y - drag.startY) >= 1) {
        drag.moved = true;
        reheatRelationPhysics(0.15);
      }
      // Native Drag writes the pointer position into the GPU row this frame;
      // CPU rows and GPU reads only catch up after the gesture.
      const point = graph.screenToSpacePosition([event.x, event.y]);
      if (point.every(Number.isFinite)) {
        dragLabelPoint = { index: drag.index, point: nativeSpace.fromNativePoint(point) };
        updateLabels(undefined, true);
      }
    },
    onPointMouseOver(index, position, event) {
      if (destroyed || contextLost) return;
      nativeHoveredIndex = index;
      if (!allowsHover(event)) return;
      hoveredEdge = null;
      hoveredIndex = index;
      cachePickedLabelPosition(index, position);
      options.callbacks.onHover(columns.nodeIds[index] ?? null);
      trackHover();
      updateHoverChip();
    },
    onPointMouseOut() {
      if (destroyed || contextLost) return;
      nativeHoveredIndex = null;
      clearHover();
    },
    onLinkMouseOver(index) {
      if (destroyed || contextLost || !showEdges || !pointerOnCanvas || hoveredIndex !== null) return;
      hoveredEdge = index;
      options.callbacks.onHover(columns.edgeIds[index] ?? null);
      trackHover();
      updateHoverChip();
    },
    onLinkMouseOut() { if (!destroyed && !contextLost && hoveredEdge !== null) clearHover(); },
    onPointClick(index, position) {
      if (destroyed || contextLost) return;
      const id = columns.nodeIds[index];
      if (!id) return;
      cachePickedLabelPosition(index, position);
      selectHit(id);
    },
    onLinkClick(index, event) {
      if (destroyed || contextLost) return;
      // A tiny dot's accessible hit target takes precedence over a nearby
      // link, even when the GPU's exact point pick misses its visual radius.
      const node = nativePointPickerAttached ? null : nearestNode(event);
      const id = node === null ? columns.edgeIds[index] : columns.nodeIds[node];
      if (id) selectHit(id);
    },
    onBackgroundClick(event) {
      if (destroyed || contextLost) return;
      const node = nativePointPickerAttached ? null : nearestNode(event);
      selectHit(node === null ? null : columns.nodeIds[node]);
    },
    onDragEnd(event) {
      const drag = draggedPoint;
      draggedPoint = null;
      const draggedLabel = dragLabelPoint;
      dragLabelPoint = null;
      dragGesture = false;
      try { endPointDrag(drag, event); } finally { if (drag) finishLabelDrag(draggedLabel, event); }
    },
    onZoomStart(_event, userDriven) {
      if (userDriven) { zoomGesture = true; lastGestureZoom = performance.now(); }
      if (userDriven && !initialCamera?.isInterrupting()) markCameraInteraction();
    },
    onZoomEnd(_event, userDriven) {
      if (zoomGesture) {
        zoomGesture = false;
        labelPublicationStale = true;
        updateLabels(undefined, true);
      }
      if (initialCamera?.isInterrupting()) return;
      initialCamera?.nativeEnd();
      if (destroyed || contextLost || !ready) return;
      if (userDriven) restoredCameraIntent = false;
      if (restoredCameraIntent) return;
      // The host restores a parked camera as the user's own, which ends automatic
      // framing for good. Park only a camera that no later Fit will replace.
      if (!userDriven && initialCamera?.isProvisional()) return;
      const viewport = getViewport();
      if (viewport) options.callbacks.onViewport?.(viewport);
    },
    onZoom(event) {
      if (!ready || destroyed || contextLost || initialCamera?.isInterrupting()) return;
      const zoom = nativeSpace.fromNativeZoom(event.transform.k);
      if (fitReferencePending) {
        // fitView schedules a D3 transition even at duration=0. Its actual
        // transform, not the preceding camera, is the scene's fit reference.
        initialFadeZoom = zoom;
        appliedFadeZoom = null;
        fitReferencePending = false;
      }
      syncLinkFade(zoom);
      if (zoomGesture) lastGestureZoom = performance.now();
      const positions = initialCamera?.positionsForLabels();
      if (positions) updateLabels(positions);
      else updateLabels(undefined, true);
    },
  });

  function endPointDrag(drag: typeof draggedPoint, event: Parameters<NonNullable<GraphConfig["onDragEnd"]>>[0]) {
    if (!drag || destroyed || contextLost || drag.identityKey !== workspaceMapCommunityIdentityKey(columns)
      || columns.nodeKeys[drag.index] !== drag.nodeKey) return;
    if (typeof event.sourceEvent?.type === "string" && event.sourceEvent.type.endsWith("cancel")) {
      solver.afterPositionEdit(() => { synchronizeDraggedPositions(); syncRelationPhysics(.15); });
      return;
    }
    const moved = drag.moved || (Number.isFinite(event.x) && Number.isFinite(event.y)
      && Math.hypot(event.x - drag.startX, event.y - drag.startY) >= 1);
    if (!moved) {
      solver.afterPositionEdit(() => syncRelationPhysics(.15));
      return; // A stationary tap is selection, never a saved pin or native reheat.
    }
    // Native Drag clears its owner before the callback; its last texture
    // update happens in a later RAF. Commit the final event position rather
    // than allowing a stale CPU frame or earlier GPU drag position to win.
    let validDragCoordinates = true;
    solver.afterPositionEdit(() => {
      const positions = readPositions();
      if (Number.isFinite(event.x) && Number.isFinite(event.y)) {
        const nativePoint = graph.screenToSpacePosition([event.x, event.y]);
        if (!nativePoint.every(Number.isFinite)) {
          positionLayout?.setPositions(positions, [...pinned]);
          syncRelationPhysics(.15);
          validDragCoordinates = false;
          return;
        }
        const point = nativeSpace.fromNativePoint(nativePoint);
        positions[drag.index * 2] = point[0]; positions[drag.index * 2 + 1] = point[1];
      }
      buffers.positions = positions;
      writePositions(positions);
      graph.render(undefined, 0);
    });
    if (!validDragCoordinates) return;
    if (!options.callbacks.onPin) {
      solver.synchronizePositionRows(positionLayout, buffers.positions, [...pinned]);
      reheatRelationPhysics(.15);
      return;
    }
    // SDK Drag.subject contains only {x,y}, and clears its index before this
    // callback. Use the public pick captured at start, fenced to this scene.
    const index = drag.index;
    // One explicit readback per completed drag; never per frame or hover.
    const positions = solver.positions(buffers.positions, readPositions);
    const position = { x: positions[index * 2], y: positions[index * 2 + 1] };
    if (!Number.isFinite(position.x) || !Number.isFinite(position.y)) return;
    pinned.add(index);
    pinOwners.set(index, columns.nodeIds[index]);
    for (const [id, owner] of retiredPinIds) if (owner === index) retiredPinIds.delete(id);
    graph.setPinnedPoints([...pinned]);
    solver.afterPositionEdit(() => { positionLayout?.setPositions(buffers.positions, [...pinned]); syncRelationPhysics(.15); },
      () => reheatRelationPhysics(.15));
    options.callbacks.onPin(columns.nodeIds[index], position);
  }

  function finishLabelDrag(last: typeof dragLabelPoint, event: Parameters<NonNullable<GraphConfig["onDragEnd"]>>[0]) {
    if (!ready || destroyed || contextLost) return;
    if (last) solver.afterNativePositionEdit(() => {
      // Reads issued during the drag predate the release, and Native Drag
      // writes its last row in the next frame. Keep the released pointer
      // position, then read after that write; CPU rows are committed above.
      trackedPositions?.invalidate();
      const released = Number.isFinite(event.x) && Number.isFinite(event.y)
        ? graph.screenToSpacePosition([event.x, event.y]) : null;
      cacheLabelPoint(last.index, released?.every(Number.isFinite) ? nativeSpace.fromNativePoint(released) : last.point);
      forgetTrackedMotion(last.index);
      if (trackedPositions) requestAnimationFrame(() => queueTrackedRead(true));
    });
    labelPublicationStale = true;
    updateLabels(undefined, true);
  }

  // Point images are signed distance fields (crisp at any drawn size): the
  // forced-colours Agent outline, then one image per distinct cluster count.
  const outlineImage = workspaceAnnulusImage(tokens.surface, FORCED_OUTLINE_INSETS.circle[1], FORCED_OUTLINE_INSETS.circle[0]);
  const countImage = createWorkspaceCountImageCache(tokens.surface, WORKSPACE_MAP_LABEL_FONT_FAMILY);
  let uploadedImageKey: string | null = null;
  function uploadPointImages(next: CosmosSceneBuffers): boolean {
    if (!outlineImage) return false;
    const key = next.countImages.map(({ text, diameterCss }) => `${text}\u0000${diameterCss}`).join("\u0001");
    if (key === uploadedImageKey) return true;
    const images = [outlineImage, ...next.countImages.map(({ text, diameterCss }) => countImage(text, diameterCss))];
    if (!images.every((image): image is ImageData => image !== null)) return false;
    graph.setImageData(images);
    uploadedImageKey = key;
    return true;
  }

  function readPositions(): Float32Array {
    return nativeSpace.fromNativePositions(graph.getPointPositions());
  }

  function writePositions(positions: Float32Array) {
    // A tracked readback issued before this upload holds pre-upload rows. Drop
    // it and read the new texture once uploaded: a settled scene runs no
    // simulation frame that would request it.
    trackedPositions?.invalidate();
    deliveredTrackedRows = null;
    trackedVelocity = new Map();
    cachedLabelPositions = null;
    uploadedRows = solver.uploadedRows(positions);
    graph.setPointPositions(nativeSpace.toNativePositions(positions));
    queueTrackedRead(true);
  }

  function projectPoint(point: [number, number]): [number, number] {
    return graph.spaceToScreenPosition(nativeSpace.toNativePoint(point));
  }

  function canonicalZoom(): number {
    return nativeSpace.fromNativeZoom(graph.getZoomLevel());
  }

  function fitPositions(positions: Float32Array, duration: number, zoom: number | undefined, padding: number, simulate: boolean) {
    graph.setZoomTransformByPointPositions(nativeSpace.toNativePositions(positions), duration,
      zoom === undefined ? undefined : nativeSpace.toNativeZoom(zoom), padding, simulate);
  }

  // Floating host controls cover part of the canvas. Fit and Center frame into
  // the uncovered rectangle; with nothing covered they keep the native camera.
  function uncoveredFrame(): UncoveredFrame | null {
    const insets = options.getSafeInsets?.();
    const canvas = container.querySelector<HTMLCanvasElement>("canvas");
    if (!insets || !canvas || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) return null;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    const clamp = (value: number, limit: number) => Number.isFinite(value) ? Math.min(Math.max(value, 0), limit) : 0;
    const top = clamp(insets.top, height), bottom = clamp(insets.bottom, height);
    const left = clamp(insets.left, width), right = clamp(insets.right, width);
    // Too little uncovered space to frame anything: keep the full-canvas camera.
    if (width - left - right < MIN_UNCOVERED_PIXELS || height - top - bottom < MIN_UNCOVERED_PIXELS) return null;
    return top || bottom || left || right ? { width, height, top, right, bottom, left } : null;
  }

  // Native space vectors for one screen pixel along x and y at a native zoom.
  function nativePixelVectors(frame: UncoveredFrame, nativeZoom: number) {
    const cx = frame.width / 2, cy = frame.height / 2;
    const origin = graph.screenToSpacePosition([cx, cy]);
    const alongX = graph.screenToSpacePosition([cx + 1, cy]);
    const alongY = graph.screenToSpacePosition([cx, cy + 1]);
    const scale = graph.getZoomLevel() / nativeZoom;
    return {
      x: [(alongX[0] - origin[0]) * scale, (alongX[1] - origin[1]) * scale],
      y: [(alongY[0] - origin[0]) * scale, (alongY[1] - origin[1]) * scale],
    };
  }

  // Puts a native point at the centre of the uncovered rectangle.
  function frameNativePoint(frame: UncoveredFrame,
    point: [number, number], nativeZoom: number, duration: number, simulate: boolean) {
    const dx = (frame.left - frame.right) / 2, dy = (frame.top - frame.bottom) / 2;
    const pixel = nativePixelVectors(frame, nativeZoom);
    const center = [point[0] - dx * pixel.x[0] - dy * pixel.y[0], point[1] - dx * pixel.x[1] - dy * pixel.y[1]];
    if (!center.every(Number.isFinite)) return false;
    graph.setZoomTransformByPointPositions(new Float32Array(center), duration, nativeZoom, 0, simulate);
    return true;
  }

  // Fits canonical positions, or the current GPU positions, into the uncovered
  // rectangle with the native Fit padding. False leaves the native Fit to run.
  function fitUncovered(positions: Float32Array | null, simulate: boolean): boolean {
    const frame = uncoveredFrame();
    if (!frame) return false;
    const native = positions ? nativeSpace.toNativePositions(positions) : graph.getPointPositions();
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (let index = 0; index + 1 < native.length; index += 2) {
      const x = native[index], y = native[index + 1];
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    }
    if (!Number.isFinite(minX)) return false;
    const currentZoom = graph.getZoomLevel();
    const pixel = nativePixelVectors(frame, currentZoom);
    const spanX = (maxX - minX) / Math.hypot(pixel.x[0], pixel.x[1]);
    const spanY = (maxY - minY) / Math.hypot(pixel.y[0], pixel.y[1]);
    const usableFraction = 1 - 2 * FIT_PADDING;
    const ratio = Math.min(
      spanX > 0 ? (frame.width - frame.left - frame.right) * usableFraction / spanX : Infinity,
      spanY > 0 ? (frame.height - frame.top - frame.bottom) * usableFraction / spanY : Infinity,
    );
    const nativeZoom = Number.isFinite(ratio) && ratio > 0 ? currentZoom * ratio : currentZoom;
    return frameNativePoint(frame, [(minX + maxX) / 2, (minY + maxY) / 2], nativeZoom, 0, simulate);
  }

  function publishLayoutStatus() {
    if (destroyed) return;
    let status: WorkspaceMapLayoutStatus | null = null;
    if (ready && !contextLost && !simulationFailed && document.visibilityState !== "hidden") {
      status = solver.layoutStatus(reducedMotion ? "motion-off" : paused ? "paused"
        : layoutPhase === "arranging" ? "arranging" : "live");
    }
    if (publishedLayoutStatus?.status === status
      && sameGraphVisibleIdentity(publishedLayoutStatus.identity, columns.identity)) return;
    const identity = { ...columns.identity };
    publishedLayoutStatus = { status, identity };
    options.callbacks.onLayoutStatusChange?.(status, identity);
  }

  function retirePositionLayout() {
    positionLayoutEpoch++;
    positionLayout?.destroy();
    positionLayout = null;
    container.dataset.mapLayoutRunning = "false";
    publishLayoutStatus();
  }

  function stopPositionLayout() {
    positionLayout?.stop();
    container.dataset.mapLayoutRunning = "false";
    publishLayoutStatus();
  }

  function failRelationSimulation(error: unknown) {
    if (destroyed || simulationFailed) return;
    simulationFailed = true;
    retirePositionLayout();
    initialCamera?.cancel(false); cancelReveal(); communityJob.cancel();
    pendingCommunity = null;
    container.dataset.mapPhysics = "failed";
    options.callbacks.onRuntimeError?.(error);
  }

  function ensurePositionLayout() {
    if (positionLayout || !columns.nodeIds.length || simulationFailed) return;
    const epoch = ++positionLayoutEpoch;
    const identityKey = workspaceMapCommunityIdentityKey(columns);
    try {
      const canvas = container.querySelector<HTMLCanvasElement>("canvas");
      if (!canvas || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) return;
      let acceptedFrame = false;
      const availableFrame = uncoveredFrame();
      const layoutViewport = availableFrame ? {
        width: availableFrame.width - availableFrame.left - availableFrame.right,
        height: availableFrame.height - availableFrame.top - availableFrame.bottom,
      } : { width: canvas.clientWidth, height: canvas.clientHeight };
      positionLayout = solver.createPositionLayout({ columns, positions: buffers.positions,
        diameters: buffers.collisionPointSizes, shapes: buffers.pointShapes,
        pins: [...pinned], reducedMotion, restoredSolved: positionLayoutReady,
        unitBasis: positionUnitBasis, viewport: layoutViewport,
        maxDiameterCss: maxPointDiameterCss() }, {
        onUnitBasis(basis) {
          if (epoch === positionLayoutEpoch && identityKey === workspaceMapCommunityIdentityKey(columns)
            && !destroyed && !contextLost && !simulationFailed) positionUnitBasis = basis;
        },
        onPositions(positions) {
          if (epoch !== positionLayoutEpoch || identityKey !== workspaceMapCommunityIdentityKey(columns)
            || !ready || destroyed || contextLost || simulationFailed || paused || draggedPoint
            || document.visibilityState === "hidden" || positionLayoutReady && reducedMotion) return;
          acceptedFrame = true;
          const changed = positions.length !== buffers.positions.length
            || positions.some((value, index) => value !== buffers.positions[index]);
          buffers.positions = positions;
          container.dataset.mapLayoutTicks = String(++positionLayoutTicks);
          if (changed) { writePositions(positions); graph.render(undefined, 0); }
          updateLabels(undefined, true);
          scheduleReveal();
        },
        onState(state) {
          if (epoch !== positionLayoutEpoch || identityKey !== workspaceMapCommunityIdentityKey(columns)
            || destroyed || contextLost || simulationFailed) return;
          if (state.ready && acceptedFrame) positionLayoutReady = true;
          layoutPhase = !state.ready ? "arranging" : state.phase === "cooled" ? "cooled" : "live";
          container.dataset.mapLayoutPhase = layoutPhase;
          container.dataset.mapLayoutRunning = String(state.running);
          publishLayoutStatus();
          schedulePublishedPositionFit();
          scheduleReveal();
        },
        onError(error) {
          if (epoch === positionLayoutEpoch && identityKey === workspaceMapCommunityIdentityKey(columns)) {
            failRelationSimulation(error);
          }
        },
      });
    } catch (error) { failRelationSimulation(error); }
  }

  function synchronizeDraggedPositions() {
    if (!ready || destroyed || contextLost) return;
    buffers.positions = readPositions(); // explicit drag/pin handoff only
    solver.synchronizePositionRows(positionLayout, buffers.positions, [...pinned]);
  }

  function syncRelationPhysics(alpha: number, reheat = false) {
    if (!ready || destroyed || contextLost || simulationFailed) { stopPositionLayout(); return; }
    const running = solver.syncPhysics({ permitted: shouldRunRelationPhysics(), paused, reducedMotion,
      hidden: document.visibilityState === "hidden", dragging: draggedPoint !== null, alpha,
      canInitialize: canInitializeRelationPhysics(), reheat }, {
      enableNative: enabled => graph.setConfigPartial({ enableSimulation: enabled }),
      startNative: amount => graph.start(amount), pauseNative: () => graph.pause(),
      ensureLayout: () => { ensurePositionLayout(); return positionLayout; }, stopLayout: stopPositionLayout,
    });
    container.dataset.mapLayoutRunning = String(running);
    publishLayoutStatus();
  }

  function attachRelationSimulation() {
    retirePositionLayout();
    layoutPhase = "arranging";
    positionLayoutTicks = 0;
    for (const key of ["mapPhysics", "mapLayoutRunning", "mapLayoutTicks", "mapLayoutPhase"]) delete container.dataset[key];
    Object.assign(container.dataset, solver.initialDiagnostics());
  }
  attachRelationSimulation();

  // One camera per visible scene. `restored` is a camera or coordinates that the
  // caller already owns; scale and layout owner are read when the camera is armed.
  function createSceneCamera(restored: boolean): InitialWorkspaceCamera {
    const policy = solver.cameraPolicy(admitted().nodeCount);
    return createInitialWorkspaceCamera({
      framingPolicy: policy.framingPolicy,
      terminalFit: policy.terminalFit,
      eligible: !restored && !paused && !reducedMotion && canAnimateLayout()
        && solver.permitsVisibility(document.visibilityState === "hidden")
        && policy.enabled,
      canFrame: () => {
        const canvas = container.querySelector<HTMLCanvasElement>("canvas");
        return ready && firstFitCompleted && shouldRunRelationPhysics()
          && policy.enabled
          && (appliedCommunity !== null || communityTerminal)
          && scheduledCommunityFit === null && !fitReferencePending
          && document.visibilityState !== "hidden" && Boolean(canvas && canvas.clientWidth > 0 && canvas.clientHeight > 0);
      },
      readPositions: () => solver.positions(buffers.positions, readPositions).slice(),
      applyFit: (positions, enableSimulation) => {
        fitReferencePending = true;
        if (fitUncovered(positions, enableSimulation)) return;
        if (positions) fitPositions(positions, 0, undefined, FIT_PADDING, enableSimulation);
        else graph.fitView(0, FIT_PADDING, enableSimulation);
      },
      interruptFit: () => {
        fitReferencePending = false;
        // Unlike position-Fit, duration=0 scaleTo is synchronous and interrupts
        // queued D3 transitions through the pinned SDK's public camera API.
        graph.setZoomLevel(graph.getZoomLevel(), 0, shouldRunRelationPhysics());
      },
    });
  }
  initialCamera = createSceneCamera(preservesSessionCoordinates || validViewport(options.initialViewport));

  // Only a PC native map above 2K contracts for ~8 s after it is shown and frames
  // itself while it does; 2K and the diagnostic physics keep their own camera.
  function framesNativePcScene(): boolean {
    return solver.cameraPolicy(admitted().nodeCount).terminalFit !== undefined;
  }

  // Every new visible scene of such a map, not only the mount, gets a fresh camera
  // lifetime; replaceColumns has cancelled the previous one.
  function rearmSceneCamera() {
    if (framesNativePcScene()) initialCamera = createSceneCamera(false);
  }

  function retireInitialFitIntent() {
    publishedPositionFitPending = false;
    if (publishedPositionFitFrame !== null) cancelAnimationFrame(publishedPositionFitFrame);
    publishedPositionFitFrame = null;
    initialCamera?.cancel();
    fitAfterCommunity = false;
    scheduledCommunityFit = null;
  }

  // The host replays the selection, focus and search it carries into a fresh
  // renderer before the first fit. That restores state; it is not the user taking
  // the camera, so it must not end the framing of a PC scene. Later changes do.
  function retireForPresentation() {
    if (!firstFitCompleted && (framesNativePcScene() || !solver.hasUsablePositionRows(positionLayoutReady))) return;
    retireInitialFitIntent();
  }

  function markCameraInteraction() {
    // An automatic camera is not reported while a Fit may still replace it. The
    // first gesture makes the one on screen the user's: park it before it moves.
    const provisional = initialCamera?.isProvisional() === true;
    cancelReveal();
    retireInitialFitIntent(); restoredCameraIntent = false;
    if (!firstFitCompleted) { cameraInteractedBeforeFit = true; pendingViewport = null; }
    if (provisional) {
      const viewport = getViewport();
      if (viewport) options.callbacks.onViewport?.(viewport);
    }
  }
  container.addEventListener("pointerdown", markCameraInteraction, { capture: true });
  container.addEventListener("wheel", markCameraInteraction, { capture: true, passive: true });
  container.addEventListener("touchstart", markCameraInteraction, { capture: true, passive: true });
  const onVisibilityChange = () => {
    if (document.visibilityState === "hidden") {
      trackedPositions?.invalidate();
      // A hidden initial scene still owns its first solved Fit when shown.
      if (publishedPositionFitPending) initialCamera?.cancel(); else retireInitialFitIntent();
      solver.afterPositionEdit(stopPositionLayout);
    } else if (trackedPositions) {
      // A read the hidden page invalidated is never re-requested otherwise.
      queueTrackedRead(true);
    } else {
      // Start from the last published coordinates and fixed pins. Visibility
      // restores permission only; it never re-arms automatic camera framing.
      solver.afterPositionEdit(() => syncRelationPhysics(.15));
    }
    schedulePublishedPositionFit();
    scheduleReveal();
  };
  document.addEventListener("visibilitychange", onVisibilityChange);

  function cancelReveal() {
    const previous = pendingReveal;
    pendingReveal = null;
    if (revealFrame !== null) cancelAnimationFrame(revealFrame);
    revealFrame = null;
    previous?.onFinished?.();
  }

  function schedulePublishedPositionFit() {
    const eligible = () => publishedPositionFitPending && positionLayoutReady && ready && firstFitCompleted
      && !destroyed && !contextLost && !simulationFailed && !paused && !draggedPoint && !pendingReveal
      && document.visibilityState !== "hidden";
    if (!eligible() || publishedPositionFitFrame !== null) return;
    const canvas = container.querySelector<HTMLCanvasElement>("canvas");
    if (!canvas || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) return;
    publishedPositionFitFrame = requestAnimationFrame(() => {
      publishedPositionFitFrame = null;
      if (!eligible()) return;
      publishedPositionFitPending = false;
      fitAfterCommunity = false;
      fitReferencePending = true;
      initialCamera?.fitInitial(buffers.positions.slice());
      captureFitZoom();
    });
  }

  function scheduleReveal() {
    const pending = pendingReveal;
    if (!pending || pending.signal.aborted || destroyed || contextLost || simulationFailed) { cancelReveal(); return; }
    if (!ready || !firstFitCompleted || paused || document.visibilityState === "hidden"
      || !solver.hasUsablePositionRows(positionLayoutReady) || (!appliedCommunity && !communityTerminal)
      || revealFrame !== null) return;
    revealFrame = requestAnimationFrame(() => {
      revealFrame = null;
      if (pendingReveal !== pending) return;
      if (paused || document.visibilityState === "hidden" || !solver.hasUsablePositionRows(positionLayoutReady)) return;
      if (pending.signal.aborted || destroyed || contextLost
        || pending.identityKey !== workspaceMapCommunityIdentityKey(columns) || !pending.isCurrent()) {
        cancelReveal(); return;
      }
      // The host has replayed saved pins before arming. Read current GPU
      // positions after the accepted upload, never the worker's unpinned seed.
      cancelReveal();
      centerOn(pending.id);
    });
  }

  function scheduleCommunityFit(outcome: Extract<WorkspaceMapCommunityOutcome, { kind: "complete" }>, positions: Float32Array) {
    if (!solver.acceptsCommunityPositions()) return;
    if (firstFitCompleted && fitAfterCommunity && scheduledCommunityFit !== outcome) {
      scheduledCommunityFit = outcome;
      requestAnimationFrame(() => {
        if (scheduledCommunityFit !== outcome) return;
        scheduledCommunityFit = null;
        if (destroyed || contextLost || outcome.identityKey !== workspaceMapCommunityIdentityKey(columns)) return;
        // Intent can change after the worker queued this RAF. A newer pan,
        // zoom, explicit Fit or list-center action owns the camera; unlike
        // Pause, it permanently retires this automatic initial fit.
        if (paused || !fitAfterCommunity) return;
        fitAfterCommunity = false;
        // fitView reads Cosmos's evolving GPU positions, which may still
        // reflect the previous scene. Fit the exact worker coordinates that
        // were just uploaded instead of an asynchronous readback.
        initialCamera?.fitInitial(positions);
        captureFitZoom();
      });
    }
  }

  function applyCommunity(outcome: Extract<WorkspaceMapCommunityOutcome, { kind: "complete" }>) {
    if (!ready || !dataCommitted || destroyed || contextLost || simulationFailed
      || outcome.identityKey !== workspaceMapCommunityIdentityKey(columns)) return;
    // Initial pre-ready static layout may initialize; an explicit post-ready
    // pause freezes coordinates and camera, not just the native force flag.
    if (paused && firstFitCompleted) return;
    if (appliedCommunity === outcome) {
      // Starting hints never become persistent constraints or weaken bridges.
      // Resume must not replay old worker coordinates.
      scheduleCommunityFit(outcome, buffers.positions);
      return;
    }
    if (preservesSessionCoordinates || !solver.acceptsCommunityPositions()) {
      // A palette/context renderer replacement is not a new layout request.
      // Deterministic starting positions must not replace the user's scene.
      appliedCommunity = outcome;
      container.dataset.mapCommunityLayoutStatus = "applied";
      applyHighlight();
      graph.render(undefined, 0);
      solver.afterPositionEdit(() => syncRelationPhysics(.35));
      scheduleReveal();
      return;
    }
    // This is the exact unpinned seed whose canonical initial Fit defines the
    // glyph-to-world conversion. Do not use evolving GPU points or the camera.
    // The identity/pause/remount guards above own whether it may be installed.
    collisionSeed = outcome.initialPositions.slice();
    collisionBasis = null;
    collisionBasisApplied = false;
    retirePositionLayout();
    initializeRelationCollision();
    const nextPositions = outcome.initialPositions.slice();
    if (pinned.size) {
      // One readback for the entire scene update preserves any user-placed pins.
      const current = readPositions();
      for (const index of pinned) {
        nextPositions[index * 2] = current[index * 2];
        nextPositions[index * 2 + 1] = current[index * 2 + 1];
      }
    }
    buffers.positions = nextPositions;
    cachedLabelPositions = null;
    writePositions(nextPositions);
    appliedCommunity = outcome;
    container.dataset.mapCommunityLayoutStatus = "applied";
    applyHighlight();
    graph.render(undefined, 0);
    syncRelationPhysics(.35);
    scheduleCommunityFit(outcome, nextPositions);
    scheduleReveal();
  }

  function startCommunity(nextColumns: WorkspaceMapColumns) {
    communityTerminal = false;
    pendingCommunity = null;
    appliedCommunity = null;
    scheduledCommunityFit = null;
    communityJob.cancel();
    delete container.dataset.mapCommunityCount;
    container.dataset.mapCommunityLayoutStatus = "pending";
    void communityJob.compute(nextColumns, { generation: solver.communityGeneration() }).then((outcome) => {
      if (destroyed || contextLost || outcome.identityKey !== workspaceMapCommunityIdentityKey(columns)) return;
      if (outcome.kind !== "complete") {
        if (outcome.kind === "error" || outcome.kind === "timeout") {
          // No later layout from this finished job can move the fallback.
          communityTerminal = true;
          if (ready && !contextLost) {
            syncRelationPhysics(.35);
          }
          scheduleReveal();
        }
        return;
      }
      pendingCommunity = outcome;
      // This is the computed relation community count, not proof that its
      // coordinates/clusters have been applied to a paused native engine.
      container.dataset.mapCommunityCount = String(outcome.communityCount);
      container.dataset.mapCommunityLayoutStatus = "queued";
      applyCommunity(outcome);
    });
  }

  startCommunity(initialColumns);

  function syncLinkFade(zoom: number) {
    if (!initialFadeZoom || !Number.isFinite(zoom) || zoom <= 0 || zoom === appliedFadeZoom) return;
    const scale = zoom / initialFadeZoom;
    const [near, far] = baseLinkDistanceRange();
    // Native's fixed-size shader still grows at k>100. Preserve canonical
    // CSS bodies when a smaller native space needs a larger SDK zoom.
    appliedPointSizeScale = Math.min(4, Math.max(1, Math.sqrt(scale)))
      * Math.min(5, Math.max(1, zoom * .01))
      / Math.min(5, Math.max(1, nativeSpace.toNativeZoom(zoom) * .01));
    graph.setConfigPartial({
      linkVisibilityDistanceRange: [near * scale, far * scale],
      pointSizeScale: appliedPointSizeScale,
    });
    appliedFadeZoom = zoom;
  }

  /**
   * The GPU's largest point sprite in CSS pixels; the native shader clamps to
   * it. Read once per device pixel ratio: overlays ask on every frame.
   */
  function maxPointDiameterCss(): number {
    const ratio = window.devicePixelRatio || 2;
    if (pointSpriteLimit?.ratio === ratio) return pointSpriteLimit.diameter;
    const gl = nativeContext;
    const range = gl?.getParameter(gl.ALIASED_POINT_SIZE_RANGE) as Float32Array | undefined;
    if (!range) return Infinity;
    pointSpriteLimit = { ratio, diameter: range[1] / ratio };
    return pointSpriteLimit.diameter;
  }

  /**
   * A point's drawn CSS diameter at the current camera: the native shader's
   * expression (size × pointSizeScale × held 1–5× zoom, clamped to the GPU
   * sprite), which the picker mirrors too. Overlays must not use the bare size.
   */
  function renderedPointDiameter(index: number): number {
    const zoomFactor = Math.min(5, Math.max(1, graph.getZoomLevel() * .01));
    return Math.min(buffers.drawPointSizes[index] * appliedPointSizeScale * zoomFactor, maxPointDiameterCss());
  }

  function captureFitZoom() {
    initialFadeZoom = canonicalZoom();
    appliedFadeZoom = null;
    syncLinkFade(initialFadeZoom);
  }

  function nearestNode(event: MouseEvent): number | null {
    if (!ready || !Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) return null;
    const bounds = container.getBoundingClientRect();
    const x = event.clientX - bounds.left, y = event.clientY - bounds.top;
    // A single GPU snapshot on an explicit click/tap; never a frame or hover
    // scan. Only this actor-visible scene's columns can become hit targets.
    const positions = readPositions();
    let nearest: number | null = null;
    let distance = 12 * 12;
    for (let index = 0; index < columns.nodeIds.length; index += 1) {
      const position: [number, number] = [positions[index * 2], positions[index * 2 + 1]];
      if (!position.every(Number.isFinite)) continue;
      const [screenX, screenY] = projectPoint(position);
      const squared = (screenX - x) ** 2 + (screenY - y) ** 2;
      if (squared <= distance && (nearest === null || squared < distance)) {
        nearest = index;
        distance = squared;
      }
    }
    return nearest;
  }

  function selectHit(id: string | null) {
    retireInitialFitIntent();
    cancelReveal();
    selectedId = id;
    focusedId = null;
    clearHover();
    applyHighlight();
    options.callbacks.onSelect(id);
  }

  function applyData(next: CosmosSceneBuffers) {
    appliedLinkColorSource = null;
    cachedLabelPositions = null;
    // Indices of a replaced scene name other nodes: no placement or motion history.
    placedLabelNodes = new Set();
    shownLabelNodes = new Set();
    deliveredTrackedRows = null;
    trackedVelocity = new Map();
    detachNativePointPicker();
    trackedPositions?.destroy(); trackedPositions = null;
    graph.setPointCollisionRadii(null);
    writePositions(next.positions);
    graph.setPointColors(next.pointColors);
    graph.setPointShapes(next.pointShapes);
    if (uploadPointImages(next)) {
      graph.setPointImageIndices(next.pointImageIndices);
      graph.setPointImageSizes(next.pointImageSizes);
    }
    // Draw and pick the drawn sizes; forces read pointSizes, collision collisionPointSizes.
    graph.setPointSizes(next.drawPointSizes);
    graph.setLinks(next.links);
    graph.setLinkStrength(next.linkStrength);
    graph.setLinkColors(next.linkColors);
    graph.setLinkStyles(next.linkStyles);
    graph.render(undefined, 0);
    solver.attachLargePointPicker(admitted().nodeCount, () => {
      graph.attachPointPicker(createWorkspaceNativePointPicker(next.drawPointSizes, next.pointShapes, failRelationSimulation));
      nativePointPickerAttached = true;
    });
    // Titles track their GPU rows in every GPU-laid-out scene, whatever its
    // size: at most 42 rows are read, and never synchronously. CPU rows are
    // already in JS.
    solver.attachPositionTracking(() => {
      const getFrame = graph.getTrackedPointPositionsFrame.bind(graph);
      const owner = workspaceMapCommunityIdentityKey(columns);
      trackedPositions = createWorkspaceTrackedPositions({
        getFrame: () => destroyed || contextLost || simulationFailed
          || (!showLabels && hoveredIndex === null && hoveredEdge === null && activeIndex() === null)
          || document.visibilityState === "hidden"
          ? null : getFrame(),
        onPositions(positions) {
          if (destroyed || contextLost || simulationFailed || owner !== workspaceMapCommunityIdentityKey(columns)) return;
          const next = new Map([...positions].map(([index, point]) => [index, nativeSpace.fromNativePoint(point)]));
          noteTrackedVelocity(next, trackedReadStep);
          cachedLabelPositions = next;
          uploadedRows = null;
          updateLabels(undefined, true);
          if (trackedReadRetry) { trackedReadRetry = false; queueTrackedRead(true); }
        },
        onError: failRelationSimulation,
      });
    });
  }

  function detachNativePointPicker() {
    nativePointPickerAttached = false;
    graph.attachPointPicker(null);
  }

  function resetLabelCandidates() {
    const byKey = (left: number, right: number) =>
      columns.nodeKeys[left] < columns.nodeKeys[right] ? -1 : columns.nodeKeys[left] > columns.nodeKeys[right] ? 1 : 0;
    const rows = Array.from({ length: columns.nodeIds.length }, (_, index) => index);
    // Major folders name the map's regions first: the TreeRail folders holding
    // the most documents, except one that holds every document.
    const contained = workspaceMapContainedDocumentCounts(columns), documents = admitted().nodeCount;
    const folders = rows.filter((index) => columns.nodeKinds[index] === WORKSPACE_MAP_NODE_KIND.folder
      && contained[index] > 0 && contained[index] < documents)
      .sort((left, right) => contained[right] - contained[left] || byKey(left, right))
      .slice(0, MAJOR_FOLDER_LABELS);
    rankedLabelIndices = [...folders, ...rows.filter((index) => columns.nodeKinds[index] !== WORKSPACE_MAP_NODE_KIND.folder)
      .sort((left, right) => columns.nodeSizes[right] - columns.nodeSizes[left] || byKey(left, right))
      .slice(0, 40 - folders.length)];
    syncLabelCandidates(true);
  }

  function cachePickedLabelPosition(index: number, position: [number, number]) {
    if (position?.length !== 2 || !position.every(Number.isFinite)) return;
    // The native pick already returned this point. Publish its title without
    // waiting for (or adding to) the bounded GPU tracking cadence.
    cacheLabelPoint(index, nativeSpace.fromNativePoint(position));
  }

  function cacheLabelPoint(index: number, point: [number, number]) {
    if (!Number.isInteger(index) || !columns.nodeIds[index] || !point.every(Number.isFinite)) return;
    const positions = new Map(cachedLabelPositions);
    positions.set(index, point);
    cachedLabelPositions = positions;
    // An exact (picked or released) point is current: never extrapolate it.
    trackedVelocity.delete(index);
  }

  /**
   * Asynchronous GPU-tracked rows (every GPU-laid-out scene). One read is in
   * flight at most. Each simulation frame asks for one and republishes titles
   * at its step; new rows, new indices and shown titles ask once. Requests wait
   * for the current task: Cosmos uploads written rows at render(), and a
   * frame's step is drawn by then.
   */
  function queueTrackedRead(explicit = false) {
    if (!trackedPositions) return;
    const first = !trackedReadQueued && !trackedReadExplicit;
    if (explicit) trackedReadExplicit = true;
    else trackedReadQueued = true;
    if (!first) return;
    queueMicrotask(() => {
      trackedReadQueued = trackedReadExplicit = false;
      if (!ready || destroyed || contextLost || !trackedPositions) return;
      // A signaled earlier read may publish inside request(), with its own step.
      if (trackedPositions.request()) trackedReadStep = simulationStep;
      else trackedReadRetry = true;
      // A frame advanced the drawn rows, or an upload replaced them: publish.
      updateLabels(undefined, true);
    });
  }

  function noteTrackedVelocity(next: ReadonlyMap<number, [number, number]>, step: number) {
    const previous = deliveredTrackedRows, steps = step - deliveredRowsStep;
    const velocity = new Map<number, [number, number]>();
    if (previous && steps > 0) {
      for (const [index, [x, y]] of next) {
        const before = previous.get(index);
        if (before) velocity.set(index, [(x - before[0]) / steps, (y - before[1]) / steps]);
      }
    }
    deliveredTrackedRows = next;
    deliveredRowsStep = step;
    cachedRowsStep = step;
    trackedVelocity = velocity;
  }

  /**
   * The GPU row behind a title: the last read (advanced to the step on
   * screen) or an exact pick/release, else the last upload while no read has
   * landed since it.
   */
  function gpuLabelRow(index: number, rows: ReadonlyMap<number, [number, number]> | null): [number, number] | undefined {
    const row = rows?.get(index);
    if (row) return predictTrackedPoint(index, row);
    const x = uploadedRows?.[index * 2], y = uploadedRows?.[index * 2 + 1];
    return x !== undefined && y !== undefined && Number.isFinite(x) && Number.isFinite(y) ? [x, y] : undefined;
  }

  /**
   * A hand-moved row (a released drag, pinned or cancelled) is not physics:
   * the next read must not measure the drag as velocity.
   */
  function forgetTrackedMotion(index: number) {
    trackedVelocity.delete(index);
    if (!deliveredTrackedRows?.has(index)) return;
    const rows = new Map(deliveredTrackedRows);
    rows.delete(index);
    deliveredTrackedRows = rows;
  }

  /**
   * A GPU-tracked row advanced to the simulation step on screen. Only a free
   * node moved by the layout is extrapolated: a pinned row is exact.
   */
  function predictTrackedPoint(index: number, point: [number, number]): [number, number] {
    if (pinned.has(index)) return point;
    const velocity = trackedVelocity.get(index);
    const steps = Math.min(LABEL_PREDICTION_STEPS, simulationStep - cachedRowsStep);
    return velocity && steps > 0 ? [point[0] + velocity[0] * steps, point[1] + velocity[1] * steps] : point;
  }

  function syncLabelCandidates(force = false, star = interactionStar()): boolean {
    // A selected/focused node must not disappear merely because it falls
    // outside the scene's top-40 labels. Reserve the first slots for the
    // explicit interaction while retaining the 40-label DOM cap. Hover tracks
    // at most two extra endpoints without changing these label candidates.
    const priority = [focusedId, selectedId]
      .map((id) => id === null ? undefined : nodeById.get(id))
      .filter((index): index is number => index !== undefined);
    // Search labels must describe the active search, including matches outside
    // the normal top-40. Explicit selected/focused titles still take precedence.
    const ordinary = searchMatchedIds === null ? rankedLabelIndices
      : [...searchMatchedIds].flatMap((id) => {
        const index = nodeById.get(id); return index === undefined ? [] : [index];
      }).sort((left, right) => columns.nodeSizes[right] - columns.nodeSizes[left]
        || left - right).slice(0, 40);
    // Name the same endpoints the GPU highlights, even outside the global
    // top-40. Search keeps its matching-only policy; ordinary overview labels
    // fill the remaining slots without changing graph geometry or camera.
    const related = searchMatchedIds === null ? star?.points ?? [] : [];
    const next = [...new Set([...priority, ...related, ...ordinary])].slice(0, 40);
    const changed = force || next.length !== labelIndices.length
      || next.some((index, slot) => index !== labelIndices[slot]);
    labelIndices = next;
    if (changed && cachedLabelPositions) {
      cachedLabelPositions = new Map(next.flatMap(index => {
        const position = cachedLabelPositions?.get(index);
        return position ? [[index, position] as const] : [];
      }));
    }
    if (!ready || !labelLayer) return changed;
    if (changed) {
      trackedPositions?.invalidate();
      graph.trackPointPositionsByIndices(trackingIndices());
      queueTrackedRead(true);
    }
    let textChanged = false;
    for (let index = 0; index < labelElements.length; index += 1) {
      const label = labelElements[index];
      const node = labelIndices[index];
      const full = priority.includes(node);
      const title = node === undefined ? "" : columns.nodeLabels[node];
      const codePoints = Array.from(title);
      const text = full || codePoints.length <= 24 ? title : `${codePoints.slice(0, 24).join("")}…`;
      const priorityKind = full ? node === priority[0] ? "active" : "explicit" : "ordinary";
      if (label.textContent !== text || label.dataset.fullTitle !== String(full)
        || label.dataset.labelPriority !== priorityKind) {
        textChanged = true;
        delete label.dataset.placementReason;
        delete label.dataset.viewportConstrained;
        label.style.maxHeight = "";
      }
      label.textContent = text;
      label.dataset.fullTitle = String(full);
      label.dataset.labelPriority = priorityKind;
      const folder = node !== undefined && columns.nodeKinds[node] === WORKSPACE_MAP_NODE_KIND.folder;
      label.dataset.labelKind = folder ? "folder" : "document";
      label.style.fontWeight = folder ? "600" : "";
      const nodeId = node === undefined ? "" : columns.nodeIds[node];
      // A reused slot must not publish the previous node's GPU coordinates
      // while its new identity waits for the bounded tracking refresh. A slot
      // that keeps its node keeps its title on screen.
      if (label.dataset.nodeId !== nodeId) {
        delete label.dataset.pointX;
        delete label.dataset.pointY;
        stopLabelFade(label);
        label.style.display = "none";
      }
      label.dataset.nodeId = nodeId;
      label.title = title;
      label.style.whiteSpace = full ? "normal" : "nowrap";
      label.style.overflowWrap = full ? "anywhere" : "normal";
    }
    if (changed || textChanged) labelPublicationStale = true;
    return changed || textChanged;
  }

  function updateLabels(positions?: Float32Array, reprojectOnly = false) {
    if (!ready || destroyed || contextLost || !labelLayer
      || (!showLabels && hoveredIndex === null && hoveredEdge === null && activeIndex() === null)) return;
    // Rows already in JS cost no read: the CPU layout's published frame, or
    // the scene an initial camera Fit has just read. Every call publishes them.
    const knownPositions = positions ?? solver.knownPositions(buffers.positions);
    // GPU rows are never read synchronously: a caller asking for fresh rows
    // gets one asynchronous read after this task; the last rows publish now.
    if (!reprojectOnly && !knownPositions) queueTrackedRead(true);
    const known = knownPositions ? new Map(trackingIndices().map((index) =>
      [index, [knownPositions[index * 2], knownPositions[index * 2 + 1]] as [number, number]])) : null;
    const tracked = known ?? cachedLabelPositions;
    if (!tracked && !uploadedRows) return;
    if (known) solver.afterNativePositionEdit(() => {
      // An initial camera Fit read these rows at the step on screen; camera
      // motion reprojects them without another GPU read.
      cachedLabelPositions = known;
      uploadedRows = null; // a read newer than the last upload
      cachedRowsStep = simulationStep;
    });
    updateHoverChip();
    if (!showLabels) return;
    const drag = dragLabelPoint;
    const anchors = labelElements.map((_, slot): [number, number] | null => {
      const node = labelIndices[slot];
      const point = node === undefined ? undefined : drag !== null && drag.index === node ? drag.point
        : known ? known.get(node) : gpuLabelRow(node, tracked);
      return point ? projectPoint(point) : null;
    });
    // Sub-epsilon motion (a cooling layout) keeps the last publication; it
    // accumulates against the published anchors, never frame to frame.
    if (!labelPublicationStale && anchors.length === publishedAnchors.length && anchors.every((anchor, slot) => {
      const published = publishedAnchors[slot];
      return anchor === null ? published === null : published !== null
        && Math.hypot(anchor[0] - published[0], anchor[1] - published[1]) < LABEL_MOTION_EPSILON_PX;
    })) return;
    labelPublicationStale = false;
    publishedAnchors = anchors;
    // Resize() refreshes this; one read per publication would force layout.
    const bounds = labelViewport ?? container.getBoundingClientRect();
    if (!labelViewport && bounds.width > 0 && bounds.height > 0) labelViewport = { width: bounds.width, height: bounds.height };
    const candidates: Array<{
      label: HTMLSpanElement; screenX: number; screenY: number;
      text: string; full: boolean; maxWidth: number;
      width: number; height: number; measure: boolean; node: number; wasVisible: boolean | undefined; onScreen: boolean;
    }> = [];
    for (let index = 0; index < labelElements.length; index += 1) {
      const label = labelElements[index];
      // The last placement of this node, whichever slot showed it. A node
      // without a placement (new, or without an anchor) has no history.
      const node = labelIndices[index];
      const wasVisible = node === undefined || !placedLabelNodes.has(node) ? undefined : shownLabelNodes.has(node);
      // What this element painted last frame, before measuring hides it.
      const onScreen = label.style.display !== "none" && label.style.visibility !== "hidden";
      setLabelStyle(label, "maxHeight", "");
      const anchor = anchors[index];
      if (!anchor) {
        delete label.dataset.placementReason;
        delete label.dataset.viewportConstrained;
        stopLabelFade(label);
        setLabelStyle(label, "display", "none");
        delete label.dataset.pointX;
        delete label.dataset.pointY;
        continue;
      }
      const [screenX, screenY] = anchor;
      // The title may be clamped into view; keep its actual bounded tracked
      // point separate for position-aware consumers and browser acceptance.
      label.dataset.pointX = String(screenX);
      label.dataset.pointY = String(screenY);
      const full = label.dataset.fullTitle === "true";
      const maxWidth = full ? Math.max(0, Math.min(420, bounds.width - 16)) : 144;
      setLabelStyle(label, "maxWidth", `${maxWidth}px`);
      const text = label.textContent;
      const cached = labelMeasurements.get(label);
      const measure = !cached || cached.text !== text || cached.full !== full || cached.maxWidth !== maxWidth;
      if (measure) {
        // display:none has no measurable rectangle. Prepare every changed
        // title before reading any dimensions, so one bounded layout pass
        // measures first-paint wrapping and non-Latin glyph widths correctly.
        // A running fade continues: opacity does not affect the rectangle.
        label.style.visibility = "hidden";
        label.style.display = "block";
      }
      candidates.push({ label, screenX, screenY, text, full, maxWidth,
        width: cached?.width ?? 0, height: cached?.height ?? 0, measure, node: node!, wasVisible, onScreen });
    }
    for (const candidate of candidates) {
      if (!candidate.measure) continue;
      const { label, text, full, maxWidth } = candidate;
      const rectangle = label.getBoundingClientRect();
      const width = Math.max(rectangle.width, label.offsetWidth);
      const height = Math.max(rectangle.height, label.offsetHeight);
      // Detached/hidden hosts have no layout. Never cache estimates: a later
      // visible frame must measure again. Non-layout test hosts also use this
      // fallback; drawable native hosts always provide the actual rectangle.
      candidate.width = width > 0 ? width : Math.min(maxWidth, text.length * 7 + 8);
      candidate.height = height > 0 ? height : 18;
      if (width > 0 && height > 0) labelMeasurements.set(label, { text, full, maxWidth, width, height });
    }
    const placements = placeWorkspaceMapLabels(candidates.map(({ label, node, screenX, screenY, width, height, wasVisible }) => ({
      screenX, screenY, width, height, wasVisible,
      // A cluster's title starts beside its count disc, never over the number.
      ...columns.nodeKinds[node] === WORKSPACE_MAP_NODE_KIND.cluster
        ? { clearance: renderedPointDiameter(node) / 2 + 3 } : {},
      priority: label.dataset.labelPriority === "active" ? "active"
        : label.dataset.labelPriority === "explicit" ? "explicit" : "ordinary",
    })), bounds, { freeze: dragGesture
      || zoomGesture && performance.now() - lastGestureZoom < LABEL_GESTURE_LAPSE_MS });
    const fades = !reducedMotion;
    placedLabelNodes = new Set();
    shownLabelNodes = new Set();
    for (let index = 0; index < candidates.length; index += 1) {
      const { label, node, wasVisible, onScreen } = candidates[index];
      const { x, y, visible, reason } = placements[index];
      placedLabelNodes.add(node);
      if (visible) shownLabelNodes.add(node);
      label.dataset.placementReason = reason;
      if (reason === "viewport-constrained") {
        label.dataset.viewportConstrained = "true";
        setLabelStyle(label, "maxHeight", `${bounds.height}px`);
      } else delete label.dataset.viewportConstrained;
      if (!visible) hideLabel(label, fades && onScreen);
      // A rebound slot showing a node already on screen elsewhere does not fade in.
      else showLabel(label, x, y, fades, fades && !onScreen && wasVisible !== true);
    }
  }

  function setLabelStyle(label: HTMLSpanElement,
    property: "display" | "visibility" | "transform" | "maxWidth" | "maxHeight", value: string) {
    if (label.style[property] !== value) label.style[property] = value;
  }

  function showLabel(label: HTMLSpanElement, x: number, y: number, fades: boolean, fadeIn: boolean) {
    setLabelStyle(label, "display", "block");
    setLabelStyle(label, "visibility", "visible");
    // Each title is its own compositor layer. Whole device pixels keep its
    // text sharp while it moves; the anchor itself stays exact in data-point-*.
    const ratio = window.devicePixelRatio || 1;
    setLabelStyle(label, "transform", `translate(${Math.round(x * ratio) / ratio}px, ${Math.round(y * ratio) / ratio}px)`);
    const fade = labelFades.get(label);
    if (!fades) stopLabelFade(label);
    // A leaving title turns back from its current opacity; an entering one
    // continues; a hidden one (display may already be set above) starts at 0.
    else if (fade?.to === 0) fadeLabel(label, 1);
    else if (!fade && fadeIn) fadeLabel(label, 1, 0);
  }

  function hideLabel(label: HTMLSpanElement, fade: boolean) {
    // Only a title that was on screen fades out, from its current opacity;
    // a leaving one keeps its last place until it has faded.
    if (fade && typeof label.animate === "function") {
      setLabelStyle(label, "display", "block");
      setLabelStyle(label, "visibility", "visible");
      if (labelFades.get(label)?.to !== 0) fadeLabel(label, 0);
      return;
    }
    stopLabelFade(label);
    setLabelStyle(label, "display", "none");
    setLabelStyle(label, "visibility", "visible");
  }

  function labelOpacity(label: HTMLSpanElement): number {
    const fade = labelFades.get(label);
    if (!fade) return label.style.display === "none" ? 0 : 1;
    const progress = fade.animation.effect?.getComputedTiming().progress;
    return progress === null || progress === undefined ? fade.to : fade.from + (fade.to - fade.from) * progress;
  }

  // `from` defaults to the shown opacity, read before the previous fade is cancelled.
  function fadeLabel(label: HTMLSpanElement, to: 0 | 1, from = labelOpacity(label)) {
    stopLabelFade(label);
    if (typeof label.animate !== "function" || from === to) {
      if (to === 0) { setLabelStyle(label, "display", "none"); setLabelStyle(label, "visibility", "visible"); }
      return;
    }
    const animation = label.animate([{ opacity: from }, { opacity: to }],
      { duration: LABEL_FADE_MS * Math.abs(to - from), easing: "linear", fill: "forwards" });
    labelFades.set(label, { animation, from, to });
    animation.onfinish = () => {
      if (labelFades.get(label)?.animation !== animation) return;
      labelFades.delete(label);
      if (to === 0) {
        label.style.display = "none";
        label.style.visibility = "visible";
      }
      // The inline state now holds the end opacity; drop the filled effect.
      animation.cancel();
    };
  }

  function stopLabelFade(label: HTMLSpanElement) {
    const fade = labelFades.get(label);
    if (!fade) return;
    labelFades.delete(label);
    fade.animation.cancel();
  }

  function attachSelectionRing() {
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("aria-hidden", "true");
    svg.dataset.workspaceMapSelectionRing = "";
    Object.assign(svg.style, {
      // Above titles that may sit beside the point.
      position: "absolute", inset: "0", width: "100%", height: "100%", zIndex: "3",
      pointerEvents: "none", overflow: "visible", display: "none",
    });
    const path = (stroke: string, width: number) => {
      const element = document.createElementNS(namespace, "path");
      element.setAttribute("fill", "none");
      element.setAttribute("stroke", stroke);
      element.setAttribute("stroke-width", String(width));
      element.setAttribute("stroke-linejoin", "miter");
      svg.appendChild(element);
      return element;
    };
    selectionRing = { svg, gap: path(tokens.surface, GRAPH_RING_GAP_PX), ring: path(tokens.selection, GRAPH_RING_WIDTH_PX) };
    container.appendChild(svg);
  }

  function attachLabelLayer() {
    labelLayer = document.createElement("div");
    labelLayer.setAttribute("aria-hidden", "true");
    labelLayer.dataset.workspaceMapLabels = "";
    Object.assign(labelLayer.style, { position: "absolute", inset: "0", zIndex: "2", pointerEvents: "none", overflow: "hidden" });
    labelElements = Array.from({ length: 40 }, () => {
      const label = document.createElement("span");
      Object.assign(label.style, {
        position: "absolute", left: "0", top: "0", display: "none",
        maxWidth: "144px", overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis",
        color: getComputedStyle(container).color || "#e5e7eb", font: `11px ${WORKSPACE_MAP_LABEL_FONT_FAMILY}`,
        lineHeight: "14px", backgroundColor: "transparent", padding: "2px 4px", boxSizing: "border-box",
        // Paint the surface halo before the ink, leaving a 1.5px outline.
        // A system colour keeps forced-colour browsers from remapping it to ink.
        webkitTextStroke: `3px ${options.forcedColors ? "Canvas" : tokens.surface}`,
        paintOrder: "stroke fill", pointerEvents: "none",
        // Own compositor layer: per-frame moves composite without repainting text.
        willChange: "transform",
      });
      labelLayer?.appendChild(label);
      return label;
    });
    container.appendChild(labelLayer);
    labelLayer.style.display = showLabels ? "block" : "none";
    resetLabelCandidates();
    // Heartbeat: republish every title without a GPU read; moving sources
    // already publish per frame.
    labelTimer = window.setInterval(() => {
      labelPublicationStale = true;
      updateLabels(undefined, true);
    }, LABEL_HEARTBEAT_MS);
  }

  function allowsHover(input: unknown) {
    const source = input && typeof input === "object" && "sourceEvent" in input ? input.sourceEvent : input;
    const event = source instanceof MouseEvent ? source : undefined;
    return pointerOnCanvas && (!source || !!event) && (!event || ((!('pointerType' in event) || event.pointerType !== "touch")
      && (!event.target || event.target instanceof Node && container.contains(event.target))));
  }

  // Up to 40 labels plus at most two hover endpoints: WORKSPACE_TRACKED_POSITION_LIMIT.
  function trackingIndices() {
    const extra = hoveredIndex !== null ? [hoveredIndex] : hoveredEdge !== null
      ? [columns.edgeSources[hoveredEdge], columns.edgeTargets[hoveredEdge]] : [];
    return [...new Set([...labelIndices, ...extra])];
  }

  function trackHover() {
    if (!ready || destroyed || contextLost) return;
    trackedPositions?.invalidate();
    graph.trackPointPositionsByIndices(trackingIndices());
    queueTrackedRead(true);
  }

  function clearHover() {
    const hadHover = hoveredIndex !== null || hoveredEdge !== null;
    hoveredIndex = hoveredEdge = null;
    chip.hide();
    if (hadHover && !destroyed && !contextLost) { trackHover(); options.callbacks.onHover(null); }
  }

  // Native picks resolve asynchronously, so a hovered index can outlive its
  // row or meet a not-yet-placed one. Name nothing rather than throw from a
  // pointer callback inside the native frame.
  function trackedPoint(index: number): [number, number] | undefined {
    const drag = dragLabelPoint;
    const position: [number, number] | undefined = drag !== null && drag.index === index ? drag.point
      : solver.knownPositions(buffers.positions) ? [buffers.positions[index * 2], buffers.positions[index * 2 + 1]]
        : gpuLabelRow(index, cachedLabelPositions);
    return position && Number.isFinite(position[0]) && Number.isFinite(position[1]) ? position : undefined;
  }

  /**
   * The active point's ring follows its glyph (a Library square or a disc),
   * 2px clear of it, in the selection ink. The active point is always a
   * priority label candidate, so its tracked position is already bounded.
   * `data-selected-point` names the selected row (not a focused one).
   */
  function updateSelectionRing() {
    if (!selectionRing) return;
    const index = destroyed || contextLost || !ready ? null : activeIndex();
    const position = index === null ? undefined : trackedPoint(index);
    if (index === null || !position) {
      selectionRing.svg.style.display = "none";
      return;
    }
    const [x, y] = projectPoint(position);
    const library = columns.nodeKinds[index] === WORKSPACE_MAP_NODE_KIND.library;
    // The native square fills 0.8 of the point radius; discs fill all of it.
    const radius = renderedPointDiameter(index) / 2 * (library ? 0.8 : 1);
    const shape = library ? "square" : "circle";
    selectionRing.gap.setAttribute("d", outlinePathData(shapeOutline(shape, x, y, radius, GRAPH_RING_GAP_PX / 2)));
    selectionRing.ring.setAttribute("d", outlinePathData(shapeOutline(shape, x, y, radius, GRAPH_RING_GAP_PX + GRAPH_RING_WIDTH_PX / 2)));
    selectionRing.svg.style.display = "block";
  }

  function updateHoverChip() {
    updateSelectionRing();
    const point = trackedPoint;
    if (hoveredIndex !== null) {
      const position = point(hoveredIndex);
      if (position) {
        const [x, y] = projectPoint(position);
        chip.show(columns.nodeLabels[hoveredIndex], { x, y }, { kind: "node", id: columns.nodeIds[hoveredIndex] },
          renderedPointDiameter(hoveredIndex) / 2);
        return;
      }
    } else if (hoveredEdge !== null && showEdges) {
      const source = point(columns.edgeSources[hoveredEdge]), target = point(columns.edgeTargets[hoveredEdge]);
      const label = options.relationLabel?.(columns.edgeKinds[hoveredEdge]);
      if (source && target && label) {
        const [sourceX, sourceY] = projectPoint(source), [targetX, targetY] = projectPoint(target);
        const bounds = pointerClient ? container.getBoundingClientRect() : null;
        const anchor = graphEdgeChipAnchor({ x: sourceX, y: sourceY }, { x: targetX, y: targetY },
          container.clientWidth, container.clientHeight,
          bounds && pointerClient ? { x: pointerClient.x - bounds.left, y: pointerClient.y - bounds.top } : null);
        if (anchor) {
          chip.show(label, anchor, { kind: "edge", id: columns.edgeIds[hoveredEdge] }, GRAPH_EDGE_CHIP_OFFSET_PX);
          return;
        }
      }
    }
    chip.hide();
  }
  const onPointerMove = (event: PointerEvent) => {
    pointerOnCanvas = event.pointerType !== "touch";
    pointerClient = pointerOnCanvas ? { x: event.clientX, y: event.clientY } : null;
    if (!pointerOnCanvas) clearHover();
    // A relation chip follows the pointer along the same stroke.
    else if (hoveredEdge !== null && ready && !destroyed && !contextLost) updateHoverChip();
  };
  const onPointerLeave = () => { pointerOnCanvas = false; pointerClient = null; clearHover(); };
  container.addEventListener("pointermove", onPointerMove);
  container.addEventListener("pointerleave", onPointerLeave);
  container.addEventListener("touchstart", onPointerLeave);

  function activeIndex(): number | null {
    const id = focusedId ?? selectedId;
    return id === null ? null : nodeById.get(id) ?? null;
  }

  function interactionStar() {
    const index = activeIndex();
    const edge = index === null
      ? edgeById.get(focusedId ?? selectedId ?? "") : undefined;
    // Relationship-list focus emphasizes that relationship and its endpoints;
    // it does not select an endpoint or alter the saved selection/camera.
    return index !== null ? incidentStar(buffers, index)
      : edge !== undefined ? {
        points: [...new Set([columns.edgeSources[edge], columns.edgeTargets[edge]])],
        links: [edge],
      } : null;
  }

  function applyHighlight() {
    if (destroyed || contextLost || !ready) return;
    const star = interactionStar();
    const searchPoints = searchMatchedIds === null ? null
      : new Set([...searchMatchedIds].flatMap((id) => {
        const node = nodeById.get(id);
        return node === undefined ? [] : [node];
      }));
    const searchLinks = searchPoints === null ? null
      : Array.from({ length: columns.edgeIds.length }, (_, edge) => edge)
        .filter((edge) => searchPoints.has(columns.edgeSources[edge])
          && searchPoints.has(columns.edgeTargets[edge]));
    graph.setConfigPartial({
      focusedLinkIndex: selectedId === null ? undefined : edgeById.get(selectedId),
      highlightedPointIndices: searchPoints === null ? star?.points
        : [...new Set([...searchPoints, ...(star?.points ?? [])])],
      highlightedLinkIndices: searchLinks === null ? star?.links
        : [...new Set([...searchLinks, ...(star?.links ?? [])])],
      pointGreyoutColor: searchPoints !== null || keepOverviewContext() ? tokens.node.folder : undefined,
      pointGreyoutOpacity: keepOverviewContext() && star ? 0.3
        : searchPoints !== null ? 0.55 : keepOverviewContext() ? 0.3 : undefined,
      linkGreyoutOpacity: keepOverviewContext() ? 0.08 : 0.1,
      // Multiply the small-map .45/.4 alphas into .945/.84 for the active
      // star. Config-only emphasis preserves any native position transition.
      linkOpacity: star && !keepOverviewContext() && !options.forcedColors ? 2.1 : 1,
      linkVisibilityMinTransparency: star && !options.forcedColors
        ? keepOverviewContext() ? .9 : 1 : linkTransparencyFloor(),
    });
    if (keepOverviewContext() && !options.forcedColors) {
      const communities = appliedCommunity?.identityKey === workspaceMapCommunityIdentityKey(columns)
        ? appliedCommunity.communityIds : undefined;
      const starKey = star?.links.join(",") ?? "";
      if (appliedLinkColorSource !== buffers.linkColors || appliedLinkColorCommunity !== communities
        || appliedLinkColorStar !== starKey) {
        graph.setLinkColors(buildCosmosRelationColors(columns, buffers.linkColors, communities, star?.links, budget));
        graph.render(undefined, 0);
        appliedLinkColorSource = buffers.linkColors;
        appliedLinkColorCommunity = communities;
        appliedLinkColorStar = starKey;
      }
    }
    if (syncLabelCandidates(false, star)) {
      if (cachedLabelPositions?.size) updateLabels(undefined, true);
      const active = activeIndex();
      if (active === null || !cachedLabelPositions?.has(active)) updateLabels();
    }
    const selected = selectedId === null ? undefined : nodeById.get(selectedId);
    if (selectionRing && selected !== undefined) selectionRing.svg.dataset.selectedPoint = String(selected);
    else if (selectionRing) delete selectionRing.svg.dataset.selectedPoint;
    updateSelectionRing();
  }

  function getViewport(): GraphViewport | null {
    if (!ready || destroyed || contextLost || simulationFailed) return null;
    const canvas = container.querySelector<HTMLCanvasElement>("canvas");
    if (!canvas || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) return null;
    // Cosmos stores integer native client dimensions. A fractional CSS rect
    // center is not its camera center and introduces drift on every remount.
    const [x, y] = nativeSpace.fromNativePoint(graph.screenToSpacePosition([canvas.clientWidth / 2, canvas.clientHeight / 2]));
    const zoom = canonicalZoom();
    return Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(zoom)
      ? { zoom, pan: { x, y } } : null;
  }

  function setViewport(viewport: GraphViewport) {
    if (!validViewport(viewport) || destroyed || simulationFailed) return;
    // An explicit/saved camera owns its intent even before native ready. A
    // later community hint may initialize coordinates, never refit this view.
    retireInitialFitIntent();
    restoredCameraIntent = true;
    if (!ready) { pendingViewport = viewport; return; }
    if (!firstFitCompleted) { cameraInteractedBeforeFit = true; pendingViewport = null; }
    fitPositions(
      new Float32Array([viewport.pan.x, viewport.pan.y]), 0, viewport.zoom, 0, false,
    );
  }

  function onContextLost(event: Event) {
    event.preventDefault();
    if (destroyed) return;
    contextLost = true;
    nativeContext = null;
    clearHover();
    detachNativePointPicker();
    trackedPositions?.destroy(); trackedPositions = null;
    retirePositionLayout();
    cachedLabelPositions = null;
    initialCamera?.cancel(false);
    cancelReveal();
    draggedPoint = null; dragLabelPoint = null; dragGesture = false;
    nativeHoveredIndex = null;
    graph.pause();
    options.callbacks.onContextStatus?.("lost");
  }

  function onContextRestored() {
    if (destroyed) return;
    // Cosmos owns GPU textures/FBOs. Recreate rather than reusing stale handles.
    options.callbacks.onContextStatus?.("restored");
  }

  const settle = graph.ready.then(async () => {
    if (destroyed) return;
    const canvas = container.querySelector<HTMLCanvasElement>("canvas");
    const gl = canvas?.getContext("webgl2");
    if (!gl) throw new WorkspaceMapWebGLError("webgl2");
    nativeContext = gl;
    nativeSpace = createWorkspaceNativeCoordinateSpace(gl.getParameter(gl.MAX_TEXTURE_SIZE) as number);
    if (nativeSpace.scale !== 1) {
      // Keep ready=false while the SDK updates its scale/Zoom callbacks.
      graph.setConfigPartial({ spaceSize: nativeSpace.spaceSize });
    }
    ready = true;
    if (canvas) Object.assign(canvas.style, { position: "absolute", inset: "0", zIndex: "1" });
    canvas?.addEventListener("webglcontextlost", onContextLost);
    canvas?.addEventListener("webglcontextrestored", onContextRestored);
    applyData(buffers);
    dataCommitted = true;
    initializeRelationCollision();
    if (pendingCommunity) applyCommunity(pendingCommunity);
    attachSelectionRing();
    attachLabelLayer();
    // fitView reads the committed GPU positions. A fit in the same JS turn as
    // render can use the pre-upload empty bounds and leave the graph off-center.
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    if (destroyed || contextLost) return;
    fitReferencePending = !cameraInteractedBeforeFit;
    let initiallyFittedCommunity = pendingCommunity;
    if (cameraInteractedBeforeFit) {
      fitAfterCommunity = false;
    } else if (pendingCommunity) {
      initialCamera?.fitInitial(buffers.positions);
      fitAfterCommunity = false;
    } else initialCamera?.fitInitial(null);
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    if (destroyed || contextLost) return;
    // A result can land between the fallback fit and its native camera RAF.
    // Finish fitting each already-known initial result before releasing ready;
    // never wait for an unfinished worker. Every additional pass awaits a RAF
    // and consumes a different accepted result, so this is not a busy loop.
    while (fitAfterCommunity && pendingCommunity && appliedCommunity === pendingCommunity
      && initiallyFittedCommunity !== pendingCommunity) {
      initiallyFittedCommunity = pendingCommunity;
      fitReferencePending = true;
      initialCamera?.fitInitial(buffers.positions);
      fitAfterCommunity = false;
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      if (destroyed || contextLost) return;
    }
    captureFitZoom();
    fitReferencePending = false;
    // The fit reference belongs to the scene, not a saved camera zoom.
    if (validViewport(pendingViewport)) setViewport(pendingViewport);
    firstFitCompleted = true;
    syncLinkFade(canonicalZoom());
    pendingViewport = null;
    syncRelationPhysics(.35);
    applyHighlight();
    updateLabels();
    schedulePublishedPositionFit();
    scheduleReveal();
  });

  function centerOn(id: string) {
    if (!ready || destroyed || contextLost || simulationFailed) return;
    const nodeIndex = nodeById.get(id);
    const edgeIndex = edgeById.get(id);
    if (nodeIndex === undefined && edgeIndex === undefined) return;
    const positions = solver.positions(buffers.positions, readPositions);
    const source = nodeIndex ?? columns.edgeSources[edgeIndex!];
    const target = nodeIndex ?? columns.edgeTargets[edgeIndex!];
    const x = (positions[source * 2] + positions[target * 2]) / 2;
    const y = (positions[source * 2 + 1] + positions[target * 2 + 1]) / 2;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    markCameraInteraction();
    // Existing explicit Center semantics preserve nearby overview context.
    const currentZoom = canonicalZoom();
    const overviewZoom = initialFadeZoom || currentZoom;
    const focusZoom = Math.max(currentZoom, Math.min(1, overviewZoom * 2.5));
    const frame = uncoveredFrame();
    if (frame && frameNativePoint(frame, nativeSpace.toNativePoint([x, y]),
      nativeSpace.toNativeZoom(focusZoom), reducedMotion ? 0 : 250, false)) return;
    fitPositions(
      new Float32Array([x, y]), reducedMotion ? 0 : 250, focusZoom, 0, false,
    );
  }

  return {
    captureCoordinates() {
      if (!ready || destroyed || contextLost || simulationFailed
        || !isWorkspaceMapWithinBudget(workspaceMapAdmissionCounts(columns), budget)) return null;
      try {
        const positions = solver.positions(buffers.positions, readPositions).slice();
        if (positions.length !== columns.nodeIds.length * 2 || !positions.every(Number.isFinite)) return null;
        // The host owns one short-lived remount snapshot, never browser/server
        // storage. Copy identity and indexed columns so later caller mutation
        // cannot launder an old actor's coordinates into another visible scene.
        return { budget: { ...budget }, positions,
          collisionBasis: collisionBasis ? { ...collisionBasis } : undefined,
          forceBasis: forceBasis ? { ...forceBasis } : undefined,
          layoutReady: positionLayoutReady, unitBasis: positionUnitBasis,
          columns: { ...columns,
          identity: { ...columns.identity }, nodeIds: [...columns.nodeIds], nodeKeys: [...columns.nodeKeys],
          nodeLabels: [...columns.nodeLabels], nodeFolderKeys: [...columns.nodeFolderKeys],
          nodeKinds: columns.nodeKinds.slice(), nodeSizes: columns.nodeSizes.slice(),
          edgeIds: [...columns.edgeIds], edgeSources: columns.edgeSources.slice(),
          edgeTargets: columns.edgeTargets.slice(), edgeKinds: columns.edgeKinds.slice(),
          edgeCounts: columns.edgeCounts.slice(), edgeWeights: columns.edgeWeights.slice(),
          edgeCrossFolder: columns.edgeCrossFolder.slice() } };
      } catch {
        // A lost/erroring GPU is not a reusable coordinate source.
        return null;
      }
    },
    centerOn,
    revealAfterLayout(id, signal, isCurrent, onFinished) {
      cancelReveal();
      if (signal.aborted || destroyed || contextLost || simulationFailed || !nodeById.has(id)) { onFinished?.(); return; }
      pendingReveal = { id, signal, isCurrent, identityKey: workspaceMapCommunityIdentityKey(columns), onFinished };
      scheduleReveal();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      clearHover();
      chip.destroy();
      container.removeEventListener("pointermove", onPointerMove);
      container.removeEventListener("pointerleave", onPointerLeave);
      container.removeEventListener("touchstart", onPointerLeave);
      retirePositionLayout();
      retireInitialFitIntent();
      cancelReveal();
      communityJob.destroy();
      draggedPoint = null; dragLabelPoint = null; dragGesture = false;
      nativeHoveredIndex = null;
      container.removeEventListener("pointerdown", markCameraInteraction, true);
      container.removeEventListener("wheel", markCameraInteraction, true);
      container.removeEventListener("touchstart", markCameraInteraction, true);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      const canvas = container.querySelector("canvas");
      canvas?.removeEventListener("webglcontextlost", onContextLost);
      canvas?.removeEventListener("webglcontextrestored", onContextRestored);
      if (labelTimer !== null) window.clearInterval(labelTimer);
      trackedPositions?.destroy(); trackedPositions = null;
      labelLayer?.remove();
      labelLayer = null;
      selectionRing?.svg.remove();
      selectionRing = null;
      labelElements = [];
      cachedLabelPositions = null;
      graph.destroy();
      pinned.clear();
      pinOwners.clear();
      retiredPinIds.clear();
      nodeById.clear();
      edgeById.clear();
    },
    fit() {
      if (destroyed || simulationFailed) return;
      markCameraInteraction();
      if (ready && !destroyed && !contextLost) {
        fitReferencePending = true;
        if (!fitUncovered(null, false)) graph.fitView(0, FIT_PADDING, false);
        captureFitZoom();
      }
    },
    focus(id) {
      if (destroyed || simulationFailed) return;
      if (id !== null && id !== focusedId) retireForPresentation();
      focusedId = id;
      applyHighlight();
    },
    getViewport,
    pin(id, shouldPin, position, intent) {
      const index = shouldPin ? nodeById.get(id) : retiredPinIds.get(id) ?? nodeById.get(id);
      if (index === undefined || destroyed || simulationFailed) return null;
      const synchronizePins = () => {
        if (intent?.reconcile) positionLayout?.setPositions(buffers.positions, [...pinned], { reheat: false });
        else positionLayout?.setPositions(buffers.positions, [...pinned]);
        syncRelationPhysics(.15);
      };
      if (shouldPin && !intent?.reconcile) retireInitialFitIntent();
      if (!shouldPin) {
        const released = pinned.delete(index);
        pinOwners.delete(index);
        for (const [ownerId, owner] of retiredPinIds) if (owner === index) retiredPinIds.delete(ownerId);
        if (ready && !contextLost) graph.setPinnedPoints([...pinned]);
        solver.afterPositionEdit(synchronizePins,
          // Retired projection IDs are host reconciliation, not a new user edit.
          () => wakeReleasedNativePins(released && nodeById.get(id) === index));
        return null;
      }
      pinned.add(index);
      pinOwners.set(index, id);
      for (const [ownerId, owner] of retiredPinIds) if (owner === index) retiredPinIds.delete(ownerId);
      if (position && Number.isFinite(position.x) && Number.isFinite(position.y)) {
        const positions = ready && !contextLost ? solver.positions(buffers.positions, readPositions).slice() : buffers.positions.slice();
        positions[index * 2] = position.x;
        positions[index * 2 + 1] = position.y;
        buffers.positions = positions;
        if (ready && !contextLost) {
          writePositions(positions);
          graph.render(undefined, 0);
        }
      }
      if (ready && !contextLost) graph.setPinnedPoints([...pinned]);
      solver.afterPositionEdit(synchronizePins);
      const positions = ready && !contextLost ? solver.positions(buffers.positions, readPositions) : buffers.positions;
      return { x: positions[index * 2], y: positions[index * 2 + 1] };
    },
    replaceColumns(nextColumns) {
      if (destroyed || simulationFailed) return;
      assertWorkspaceMapWithinBudget(workspaceMapAdmissionCounts(nextColumns), budget);
      initialCamera?.cancel();
      const sameIdentity = sameGraphVisibleIdentity(columns.identity, nextColumns.identity);
      const next = buildCosmosSceneBuffers(nextColumns, tokens, budget, sceneOptions);
      next.positions = solver.initialPositions(nextColumns, next.positions);
      const sameScope = columns.identity.actorId === nextColumns.identity.actorId
        && columns.identity.workspaceId === nextColumns.identity.workspaceId;
      if (sameIdentity && sameWorkspaceMapIndexedGeometry(columns, nextColumns)) {
        // Labels/facets/secondary-placement metadata are deliberately absent
        // from geometry identity. Rebind them without replacing coordinates,
        // cancelling a worker, resetting pins or the camera.
        next.positions = buffers.positions;
        next.linkStrength = buffers.linkStrength;
        columns = nextColumns;
        buffers = next;
        nodeById = new Map(columns.nodeIds.map((id, index) => [id, index]));
        edgeById = new Map(columns.edgeIds.map((id, index) => [id, index]));
        retiredPinIds.clear();
        for (const [index, ownerId] of pinOwners) {
          if (columns.nodeIds[index] !== ownerId) retiredPinIds.set(ownerId, index);
        }
        resetLabelCandidates();
        applyHighlight();
        return;
      }
      forceSeed = next.positions.slice();
      collisionSeed = forceSeed;
      cancelReveal();
      forceBasis = null;
      forceBasisApplied = false;
      collisionBasis = null;
      collisionBasisApplied = false;
      positionLayoutReady = false;
      positionUnitBasis = undefined;
      publishedPositionFitPending = solver.shouldFitPublishedRows(sameIdentity);
      calibrationViewport = null;
      if (sameScope && ready && !contextLost && sameIdentity) {
        const current = solver.positions(buffers.positions, readPositions); // native reads only at this update
        const oldIndex = new Map(columns.nodeKeys.map((key, index) => [key, index]));
        nextColumns.nodeKeys.forEach((key, index) => {
          const previous = oldIndex.get(key);
          if (previous === undefined) return;
          next.positions[index * 2] = current[previous * 2];
          next.positions[index * 2 + 1] = current[previous * 2 + 1];
        });
      }
      draggedPoint = null; dragLabelPoint = null; dragGesture = false;
      nativeHoveredIndex = null;
      preservesSessionCoordinates = false;
      appliedCommunity = null;
      communityTerminal = false;
      columns = nextColumns;
      sparseScene = admitted().edgeCount <= 1_000;
      readableScene = isReadableWorkspaceMapScene(admitted().nodeCount, admitted().edgeCount, budget);
      buffers = next;
      nodeById = new Map(columns.nodeIds.map((id, index) => [id, index]));
      edgeById = new Map(columns.edgeIds.map((id, index) => [id, index]));
      clearHover();
      pinned.clear();
      pinOwners.clear();
      retiredPinIds.clear();
      attachRelationSimulation();
      if (!sameIdentity) rearmSceneCamera();
      if (ready && !contextLost) {
        graph.setConfigPartial({
          enableSimulation: false,
          simulationCluster: 0,
          backgroundColor: tokens.surface,
          enableDrag: true,
          linkBlending: true,
          transitionDuration: 0,
          linkVisibilityMinTransparency: linkTransparencyFloor(),
          pointGreyoutColor: keepOverviewContext() ? tokens.node.folder : undefined,
          pointGreyoutOpacity: keepOverviewContext() ? 0.3 : undefined,
          linkGreyoutOpacity: keepOverviewContext() ? 0.08 : 0.1,
        });
        if (!sameIdentity) graph.stop();
        graph.setPinnedPoints([]);
        applyData(buffers);
        initializeRelationCollision();
        resetLabelCandidates();
        if (!sameIdentity) {
          restoredCameraIntent = false;
          fitAfterCommunity = true;
          if (fitAfterCommunity) {
            fitReferencePending = true;
            initialCamera?.fitInitial(null);
            captureFitZoom();
          }
        }
        syncRelationPhysics(.35);
        applyHighlight();
      }
      startCommunity(nextColumns);
    },
    reset() {
      if (destroyed || simulationFailed) return;
      draggedPoint = null; dragLabelPoint = null; dragGesture = false;
      // Reset does not replace indexed geometry or SDK native hover. A later
      // same-index pick emits no MouseOver, so retain that public pick owner.
      selectedId = null;
      focusedId = null;
      clearHover();
      const releasedPins = pinned.size > 0;
      pinned.clear();
      pinOwners.clear();
      retiredPinIds.clear();
      if (!ready || contextLost) return;
      markCameraInteraction();
      graph.setPinnedPoints([]);
      solver.afterPositionEdit(() => { positionLayout?.setPositions(buffers.positions, []); syncRelationPhysics(.15); },
        () => wakeReleasedNativePins(releasedPins));
      applyHighlight();
      fitReferencePending = true;
      if (!fitUncovered(null, false)) graph.fitView(0, FIT_PADDING, false);
      captureFitZoom();
    },
    resize() {
      if (destroyed || simulationFailed) return;
      labelViewport = null;
      labelPublicationStale = true;
      if (ready && !destroyed && !contextLost) {
        graph.create();
        if (initializeRelationCollision()) {
          syncRelationPhysics(.35);
        }
        updateLabels(undefined, true);
        schedulePublishedPositionFit();
      }
    },
    resumeLayout(reheat = true) {
      if (destroyed || simulationFailed) return;
      paused = false;
      if (ready && !destroyed && !contextLost) {
        // Unpause alone cannot restart alpha=0 after stop/natural cooling.
        // Explicit resume also re-enables permission after a paused mode or
        // reduced-motion switch; the SDK otherwise ignores start(). Its
        // false-to-true config side effect wakes at alpha=1, immediately
        // replaced by the deliberate reheat before any GPU frame can run.
        if (pendingCommunity) applyCommunity(pendingCommunity);
        scheduleReveal();
        syncRelationPhysics(.15, reheat);
        schedulePublishedPositionFit();
      }
    },
    select(id) { if (destroyed || simulationFailed) return; if (id !== null && id !== selectedId) retireForPresentation(); selectedId = id; applyHighlight(); },
    setSearchMatches(ids) {
      if (destroyed || simulationFailed) return;
      if (ids !== null) retireForPresentation();
      searchMatchedIds = ids === null ? null : new Set(ids);
      applyHighlight();
    },
    setEdgesVisible(enabled) {
      if (destroyed || simulationFailed) return;
      showEdges = enabled;
      if (!enabled && hoveredEdge !== null) clearHover();
      if (ready && !destroyed && !contextLost) graph.setConfigPartial({ renderLinks: enabled });
    },
    setLabelsVisible(enabled) {
      if (destroyed || simulationFailed) return;
      showLabels = enabled;
      labelPublicationStale = true;
      if (!enabled) trackedPositions?.invalidate();
      if (labelLayer) labelLayer.style.display = enabled ? "block" : "none";
      if (enabled) updateLabels();
    },
    setReducedMotion(enabled) {
      if (destroyed || simulationFailed) return;
      if (enabled) initialCamera?.cancel();
      reducedMotion = enabled;
      if (!ready || destroyed || contextLost) return;
      if (!enabled && canAnimateLayout()) {
        if (pendingCommunity) applyCommunity(pendingCommunity);
      }
      syncRelationPhysics(.15);
    },
    setViewport,
    stopLayout(preserveInitialFit = true) {
      if (destroyed || simulationFailed) return;
      paused = true;
      initialCamera?.cancel();
      if (!preserveInitialFit) solver.afterPositionEdit(retireInitialFitIntent);
      stopPositionLayout();
      if (ready && !destroyed && !contextLost) graph.pause();
    },
    whenSettled() { return settle; },
    zoomBy(factor) {
      if (!ready || destroyed || contextLost || simulationFailed || !Number.isFinite(factor) || factor <= 0) return;
      markCameraInteraction();
      const current = canonicalZoom();
      const lower = Math.min(0.2, (initialFadeZoom || current) / 4, current);
      const next = Math.max(lower, Math.min(Math.max(8, current), current * factor));
      graph.setZoomLevel(nativeSpace.toNativeZoom(next), 0, false);
    },
  };
}
