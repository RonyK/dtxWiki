import { GRAPH_LINE_OPACITY, GRAPH_REMOVED_EDGE_COLOR, type GraphVisualTokens } from "../shared/graphTokens";
import type { CosmosLayoutSolver } from "../shared/cosmos/layoutTypes";
import type { CosmosRendererBinding } from "../shared/cosmos/renderBinding";
import { buildCosmosPresentationBuffers } from "../shared/cosmos/sceneBuffers";
import { cosmosSceneIdentityKey, type CosmosSceneColumns } from "../shared/cosmos/sceneTypes";
import { rgb } from "../workspace/cosmosColor";
import { createLocalCosmosOverlay } from "./cosmosOverlay";

/** Styles 10–19 carry only the Local lines already drawn by the existing legend. */
export const LOCAL_COSMOS_EDGE_STYLES = {
  link: 10, citation: 11, membership: 12, coAccess: 13, actionWrite: 14,
  actionCite: 15, actionRead: 16, actionReference: 17, action: 18,
} as const;

function mix(foreground: string, background: string, share: number) {
  const front = rgb(foreground), back = rgb(background);
  return front.map((value, channel) => value * share + back[channel] * (1 - share));
}

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
    linkEndpointMarkers: true, customLabels: true, customRelationColors: true,
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
      columns.nodeIds.forEach((_, index) => {
        if (columns.nodeSeedGlyph?.[index]) {
          buffers.pointShapes[index] = 0;
          buffers.pointSizes[index] = columns.nodeSizes[index];
          buffers.drawPointSizes[index] = columns.nodeSizes[index];
          buffers.collisionPointSizes[index] = columns.nodeSizes[index];
          buffers.pointImageIndices[index] = -1;
        }
        if (columns.nodeMuted?.[index] && !columns.nodeIsCurrent?.[index]) {
          const color = buffers.pointColors.slice(index * 4, index * 4 + 3), surface = rgb(tokens.surface);
          color.forEach((value, channel) => { buffers.pointColors[index * 4 + channel] = value * .14 + surface[channel] * .86; });
        }
      });
      columns.edgeVisualKinds!.forEach((kind, index) => {
        const style = columns.edgeRemoved?.[index] ? 19 : LOCAL_COSMOS_EDGE_STYLES[kind];
        // The existing history emphasis has its own no-fade state, independent
        // of selection. Forced colours deliberately keep plain opacity.
        buffers.linkStyles[index] = style + (columns.edgeHighlighted?.[index] && !forcedColors ? 32 : 0);
        const color = columns.edgeRemoved?.[index] ? GRAPH_REMOVED_EDGE_COLOR
          : columns.edgeHighlighted?.[index] ? tokens.highlight : tokens.edge[kind];
        const quiet = columns.edgeMuted?.[index] && !columns.edgeHighlighted?.[index] && !columns.edgeRemoved?.[index];
        buffers.linkColors.set([...(quiet ? mix(color, tokens.surface, GRAPH_LINE_OPACITY.dimmed) : rgb(color)),
          forcedColors ? 1 : kind === "citation" ? GRAPH_LINE_OPACITY.citation : GRAPH_LINE_OPACITY.line], index * 4);
      });
      return buffers;
    },
    relationColors(columns, colors, _groups, incident) {
      if (!incident || !latestTokens) return colors;
      const selected = new Set(incident), next = colors.slice(), surface = rgb(latestTokens.surface);
      columns.edgeIds.forEach((_, index) => {
        if (selected.has(index) || columns.edgeMuted?.[index] || columns.edgeHighlighted?.[index] || columns.edgeRemoved?.[index]) return;
        for (let channel = 0; channel < 3; channel++) next[index * 4 + channel] = colors[index * 4 + channel] * GRAPH_LINE_OPACITY.dimmed
          + surface[channel] * (1 - GRAPH_LINE_OPACITY.dimmed);
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
