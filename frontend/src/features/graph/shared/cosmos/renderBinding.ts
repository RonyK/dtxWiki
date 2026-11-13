import type { GraphNodeVisualKind, GraphVisualTokens } from "../graphTokens";
import type { CosmosLayoutSolver } from "./layoutTypes";
import type { WorkspaceCountMeasure } from "./glyphImages";
import type { CosmosSceneColumns } from "./sceneTypes";
import type { CosmosSceneBuffers } from "./buffers";

export type CosmosRenderBudget = { documents: number; relations: number };
export type CosmosLayoutSeedOutcome =
  | { kind: "complete"; identityKey: string; communityIds: Uint32Array;
    communityCount: number; initialPositions: Float32Array; computeMs: number }
  | { kind: "cancelled" | "timeout" | "error"; identityKey: string };

export type CosmosLayoutSeedJob = {
  compute(columns: CosmosSceneColumns, options: { generation: "none" | "native-relation" }): Promise<CosmosLayoutSeedOutcome>;
  cancel(): void;
  destroy(): void;
};

/** Surface policy is supplied once; the renderer owns the resulting resources. */
export type CosmosRendererBinding = {
  budget: CosmosRenderBudget;
  legacySnapshotBudget: CosmosRenderBudget;
  smallSceneLimit: number;
  solver: CosmosLayoutSolver;
  assertColumns(columns: CosmosSceneColumns): void;
  withinBudget(columns: CosmosSceneColumns): boolean;
  admissionCounts(columns: CosmosSceneColumns): { nodeCount: number; edgeCount: number };
  overview(columns: CosmosSceneColumns): boolean;
  canAnimate(columns: CosmosSceneColumns): boolean;
  sameGeometry(left: CosmosSceneColumns, right: CosmosSceneColumns): boolean;
  /** Map's indexed geometry includes every input to glyph/collision sizes.
   * Other bindings retain the additional prepared-glyph snapshot comparison. */
  snapshotGeometryIncludesGlyphs?: true;
  /** Map styles encode directed links/citations and source-side containment. */
  linkEndpointMarkers?: true;
  prepareScene(columns: CosmosSceneColumns, tokens: GraphVisualTokens,
    options: { forcedColors: boolean; countText(count: number): string; measureCount: WorkspaceCountMeasure }): CosmosSceneBuffers;
  relationColors(columns: CosmosSceneColumns, colors: Float32Array,
    groups?: Uint32Array, incident?: readonly number[]): Float32Array;
  rankLabels(columns: CosmosSceneColumns): number[];
  visualKind(columns: CosmosSceneColumns, index: number): GraphNodeVisualKind;
  createSeedJob(): CosmosLayoutSeedJob;
  createWebGLError?(reason: "webgl2" | "float-render-target"): Error;
  initializeDiagnostics(): void;
  diagnose(name: string, value?: string): void;
  decorateLabelLayer?(layer: HTMLDivElement): void;
  decorateSelectionRing?(ring: SVGSVGElement): void;
};
