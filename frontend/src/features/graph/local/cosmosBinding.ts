import { GRAPH_LINE_OPACITY, GRAPH_REMOVED_EDGE_COLOR, graphLineStyle, type GraphVisualTokens } from "../shared/graphTokens";
import type { CosmosLayoutSolver } from "../shared/cosmos/layoutTypes";
import type { CosmosRendererBinding } from "../shared/cosmos/renderBinding";
import { buildCosmosPresentationBuffers } from "../shared/cosmos/sceneBuffers";
import { cosmosSceneIdentityKey, type CosmosSceneColumns } from "../shared/cosmos/sceneTypes";
import { blendRgb, rgb } from "../workspace/cosmosColor";
import { createLocalCosmosOverlay, LOCAL_QUIET_SHARE } from "./cosmosOverlay";

/** Local admission is owned by its API query; this binding does not add a new cap. */
export type LocalCosmosBinding = CosmosRendererBinding & { setTracing(id: string | null): void };

export function createLocalCosmosBinding(solver: CosmosLayoutSolver, container: HTMLDivElement): LocalCosmosBinding {
  const overlay = createLocalCosmosOverlay(container);
  let latestTokens: GraphVisualTokens | null = null;
  let forcedColors = false;
  const budget = { documents: 2_000, relations: 6_000 };
  return {
    budget, legacySnapshotBudget: budget, smallSceneLimit: 2_000, solver,
    assertColumns(columns) {
      if (!columns.layoutInput || columns.nodeKeys.length !== columns.nodeIds.length) throw new Error("Invalid Local Cosmos scene");
    },
    admissionCounts: columns => ({ nodeCount: columns.nodeIds.length, edgeCount: columns.edgeIds.length }),
    withinBudget: () => true, overview: () => false, canAnimate: () => true,
    sameGeometry: (a, b) => a.localGeometryKey != null && a.localGeometryKey === b.localGeometryKey,
    customLabels: true, customRelationColors: true, quietPointShare: LOCAL_QUIET_SHARE,
    // The previous Context line shader used a fixed 1.5px uniform, including
    // emphasis; the old adapter's size metadata did not alter that shader.
    interactionWidthIncrease: 0,
    pointSizeScale(zoom, fitZoom) {
      const ratio = fitZoom / zoom;
      return Math.max(.2, Math.min(1, fitZoom) / (ratio <= 1 ? Math.sqrt(ratio) : ratio));
    },
    canDrag: (columns, index) => !columns.nodeIsSeed?.[index],
    // Muted rows already carry their quiet colour. They must not be greyed
    // a second time when selection also places them outside the active star.
    alwaysVisiblePoints: columns => columns.nodeIds.flatMap((_, index) =>
      columns.nodeIsCurrent?.[index] || columns.nodeMuted?.[index] ? [index] : []),
    prepareScene(columns, tokens, options) {
      latestTokens = tokens; forcedColors = options.forcedColors;
      const buffers = buildCosmosPresentationBuffers(columns, tokens, options);
      const surface = rgb(tokens.surface);
      columns.nodeIds.forEach((_, index) => {
        if (columns.nodeSeedGlyph?.[index]) {
          buffers.pointShapes[index] = 0;
          buffers.pointSizes[index] = columns.nodeSizes[index];
          buffers.drawPointSizes[index] = columns.nodeSizes[index];
          buffers.collisionPointSizes[index] = columns.nodeSizes[index];
          buffers.pointImageIndices[index] = -1;
        }
        if (columns.nodeMuted?.[index] && !columns.nodeIsCurrent?.[index]) {
          buffers.pointColors.set(blendRgb(buffers.pointColors.subarray(index * 4, index * 4 + 3), surface, LOCAL_QUIET_SHARE), index * 4);
        }
      });
      columns.edgeVisualKinds!.forEach((kind, index) => {
        // The history emphasis keeps its own no-fade state, independent of
        // selection. Forced colours deliberately keep plain opacity.
        buffers.linkStyles[index] = graphLineStyle(columns.edgeRemoved?.[index] ? "removed" : kind,
          Boolean(columns.edgeHighlighted?.[index]) && !forcedColors);
        const color = columns.edgeRemoved?.[index] ? GRAPH_REMOVED_EDGE_COLOR
          : columns.edgeHighlighted?.[index] ? tokens.highlight : tokens.edge[kind];
        const quiet = columns.edgeMuted?.[index] && !columns.edgeHighlighted?.[index] && !columns.edgeRemoved?.[index];
        buffers.linkColors.set([...(quiet ? blendRgb(rgb(color), surface, GRAPH_LINE_OPACITY.dimmed) : rgb(color)),
          forcedColors ? 1 : kind === "citation" ? GRAPH_LINE_OPACITY.citation : GRAPH_LINE_OPACITY.line], index * 4);
      });
      return buffers;
    },
    relationColors(columns, colors, _groups, incident) {
      if (!incident || !latestTokens) return colors;
      const selected = new Set(incident), next = colors.slice(), surface = rgb(latestTokens.surface);
      columns.edgeIds.forEach((_, index) => {
        if (selected.has(index) || columns.edgeMuted?.[index] || columns.edgeHighlighted?.[index] || columns.edgeRemoved?.[index]) return;
        next.set(blendRgb(colors.subarray(index * 4, index * 4 + 3), surface, GRAPH_LINE_OPACITY.dimmed), index * 4);
      });
      return next;
    },
    rankLabels: columns => columns.nodeIds.map((_, index) => index),
    visualKind: (columns, index) => columns.nodeVisualKinds![index],
    createSeedJob: () => ({
      async compute(columns: CosmosSceneColumns) {
        return { kind: "complete", identityKey: cosmosSceneIdentityKey(columns),
          communityIds: new Uint32Array(columns.nodeIds.length), communityCount: 0,
          initialPositions: new Float32Array(columns.nodeIds.length * 2), computeMs: 0 };
      },
      cancel() {}, destroy() {},
    }),
    initializeDiagnostics() {
      Object.assign(container.dataset, solver.initialDiagnostics());
    },
    diagnose(name, value) {
      const key = `local${name[0].toUpperCase()}${name.slice(1)}`;
      if (value === undefined) delete container.dataset[key]; else container.dataset[key] = value;
    },
    renderOverlay: frame => overlay.render(frame),
    destroyOverlay: () => overlay.destroy(),
    setTracing: id => overlay.setTracing(id),
  };
}
