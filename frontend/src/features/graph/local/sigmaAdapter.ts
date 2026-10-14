import { createNodeBorderProgram } from "@sigma/node-border";
import { NodeSquareProgram } from "@sigma/node-square";
import Graph from "graphology";
import Sigma from "sigma";
import { NodeCircleProgram } from "sigma/rendering";
import type { SigmaEdgeEventPayload, SigmaNodeEventPayload, SigmaStageEventPayload } from "sigma/types";
import { colorToArray } from "sigma/utils";

import type { GraphRenderScene, GraphPoint, GraphViewport } from "../shared/rendererTypes";
import { readGraphTokens } from "../shared/graphTokens";
import { LOCAL_EDGE_PROGRAMS } from "./edgePrograms";
import { createLocalLayout, type LocalLayout } from "./layout";
import { buildLocalScene, type LocalScene, type LocalSceneNode, type LocalSceneEdge } from "./sceneInput";
import { localNodeVisualKind } from "./nodeVisual";

export type SigmaGraphAdapterOptions = {
  callbacks: {
    onHover(id: string | null): void;
    onSelect(id: string | null): void;
    onPin(id: string, position: GraphPoint): void;
    onViewport(viewport: GraphViewport): void;
    onContextStatus?(status: "lost" | "restored"): void;
  };
  forcedColors: boolean;
  initialViewport?: GraphViewport | null;
  positions: Record<string, GraphPoint>;
  reducedMotion: boolean;
  showLabels?: boolean;
};

export type LocalSceneUpdate = "metadata" | "rebuild";

/** The Local canvas contract; Workspace Map deliberately retains its own adapter. */
export interface SigmaGraphAdapter {
  centerOn(id: string): void;
  destroy(): void;
  fit(): void;
  focus(id: string | null): void;
  pin(id: string, pinned: boolean, position?: GraphPoint): GraphPoint | null;
  replaceScene(scene: GraphRenderScene): LocalSceneUpdate | void;
  reset(): void;
  resize(): void;
  resumeLayout(): void;
  setLabelsVisible(enabled: boolean): void;
  select(id: string | null): void;
  setViewport(viewport: GraphViewport): void;
  stopLayout(): void;
  whenSettled(): Promise<void>;
  zoomBy(factor: number): void;
}

// Sigma prefers a program's label drawer to its global one. Library squares
// must use the same viewport bounds and collision handling as other nodes.
class LocalSquareNodeProgram extends NodeSquareProgram {
  override drawLabel: typeof NodeSquareProgram.prototype.drawLabel =
    (context, data, settings) => settings.defaultDrawNodeLabel(context, data, settings);
}

const FolderNodeProgram = createNodeBorderProgram({
  borders: [
    { size: { value: 0.24 }, color: { attribute: "color" } },
    { size: { fill: true }, color: { transparent: true } },
  ],
});
const SeedNodeProgram = createNodeBorderProgram({
  borders: [
    { size: { value: 0.24 }, color: { attribute: "borderColor" } },
    { size: { fill: true }, color: { attribute: "color" } },
  ],
});
// In forced colors, shape still distinguishes a conversation from a Context:
// a bullseye, versus a solid point, Library square, and hollow folder ring.
const ConversationNodeProgram = createNodeBorderProgram({
  borders: [
    { size: { value: 0.2 }, color: { attribute: "color" } },
    { size: { value: 0.25 }, color: { attribute: "surfaceColor" } },
    { size: { fill: true }, color: { attribute: "color" } },
  ],
});

function finitePoint(point: GraphPoint | undefined): point is GraphPoint {
  return Boolean(point && Number.isFinite(point.x) && Number.isFinite(point.y));
}

/** Preblend, rather than hide, so quiet points remain visible and pickable. */
function quietColor(color: string, surface: string): string {
  const foreground = colorToArray(color);
  const background = colorToArray(surface);
  const channels = foreground.slice(0, 3).map((value, index) =>
    Math.round(value * 0.14 + background[index] * 0.86));
  return `rgb(${channels.join(", ")})`;
}

function visiblePositions(
  scene: LocalScene,
  positions: Record<string, GraphPoint>,
): Record<string, GraphPoint> {
  const fixed: Record<string, GraphPoint> = {};
  for (const [graphId, point] of Object.entries(positions)) {
    const key = scene.keyByGraphId.get(graphId);
    if (key && finitePoint(point)) fixed[key] = point;
  }
  return fixed;
}

/**
 * Render only the caller's already actor-visible Local scene. No server-sent
 * layout/community values, invisible endpoints, or projection-row coordinates
 * enter Graphology or the force simulation.
 */
export function createSigmaGraphAdapter(
  container: HTMLElement,
  initialScene: GraphRenderScene,
  options: SigmaGraphAdapterOptions,
): SigmaGraphAdapter {
  const mode = options.forcedColors
    ? "forced"
    : document.documentElement.getAttribute("data-theme") === "dark"
      ? "dark"
      : "light";
  const tokens = readGraphTokens(container, mode);
  const ink = getComputedStyle(container).color || "#0f172a";
  const graph = new Graph({ multi: true, type: "mixed" });
  let localScene = buildLocalScene(initialScene);
  let localScopeKey = initialScene.scopeKey ?? null;
  // Construction coordinates are a restore snapshot, not ongoing pin authority.
  // Keep our own live pins so unpin/reset and later drags survive scene changes
  // without mutating (or rereading) the caller's original snapshot.
  const pinnedPositions: Record<string, GraphPoint> = {};
  for (const [id, point] of Object.entries(options.positions)) {
    if (finitePoint(point)) pinnedPositions[id] = { ...point };
  }
  let layout: LocalLayout | null = null;
  let renderer: Sigma | null = null;
  let destroyed = false;
  let contextLost = false;
  let restorationRequested = false;
  let hoveredId: string | null = null;
  let focusedId: string | null = null;
  let selectedId: string | null = null;
  let draggingKey: string | null = null;
  let dragStart: GraphPoint | null = null;
  let dragged = false;
  let suppressClick = false;
  let highlightedStar: { nodes: Set<string>; edges: Set<string> } | null = null;
  let showOrdinaryLabels = options.showLabels ?? true;
  const labelBounds: Array<{ left: number; right: number; top: number; bottom: number }> = [];
  let priorityLabelsPrepared = false;
  const paintedPriorityLabels = new Set<string>();

  const priorityLabelKeys = () => [...new Set([
    ...localScene.nodes.filter((node) => node.isSeed).map((node) => node.key).sort(),
    ...[selectedId, focusedId, hoveredId].flatMap((id) => {
      const key = id ? localScene.keyByGraphId.get(id) : undefined;
      return key ? [key] : [];
    }),
  ])];

  // Passing through an edge's hit corridor must not replace the node star.
  const activeId = () => (hoveredId && localScene.keyByGraphId.has(hoveredId)
    ? hoveredId : null) ?? focusedId ?? selectedId;
  const star = () => {
    const id = activeId();
    if (!id) return null;
    const key = localScene.keyByGraphId.get(id);
    if (key && graph.hasNode(key)) {
      return {
        nodes: new Set([key, ...graph.neighbors(key)]),
        edges: new Set(graph.edges(key)),
      };
    }
    if (graph.hasEdge(id)) {
      const [source, target] = graph.extremities(id);
      return { nodes: new Set([source, target]), edges: new Set([id]) };
    }
    return null;
  };
  const refreshHighlight = () => {
    // Resolve once per interaction, not for every node and edge reducer.
    highlightedStar = star();
    renderer?.refresh();
  };

  const applyPositions = (positions: Record<string, GraphPoint>) => {
    if (destroyed || contextLost || graph.order === 0) return;
    graph.updateEachNodeAttributes((key, attributes) => {
      const position = positions[key];
      return position ? { ...attributes, x: position.x, y: position.y } : attributes;
    }, { attributes: ["x", "y"] });
  };

  const nodeAttributes = (node: LocalSceneNode) => {
    const kind = localNodeVisualKind(node.entityKind, node.family);
    return {
      size: node.radiusPx,
      color: tokens.node[kind],
      quietColor: quietColor(tokens.node[kind], tokens.surface),
      borderColor: ink,
      surfaceColor: tokens.surface,
      quietBorderColor: quietColor(ink, tokens.surface),
      isSeed: node.isSeed,
      label: node.label,
      type: node.isSeed ? "seed"
        : kind === "library" ? "square"
        : kind === "folder" ? "folder"
        : kind === "conversation" && mode === "forced" ? "conversation" : "circle",
    };
  };
  const edgeAttributes = (edge: LocalSceneEdge, label: string) => ({
    type: edge.kind,
    size: 1,
    color: tokens.edge[edge.kind],
    quietColor: quietColor(tokens.edge[edge.kind], tokens.surface),
    label,
  });

  const loadScene = (scene: GraphRenderScene): LocalSceneUpdate => {
    const nextLocalScene = buildLocalScene(scene);
    const nextScopeKey = scene.scopeKey ?? null;
    const edgeLabels = new Map(scene.edges.map((edge) => [edge.id, edge.label]));
    if (layout && nextScopeKey === localScopeKey && nextLocalScene.geometryKey !== null
      && nextLocalScene.geometryKey === localScene.geometryKey) {
      // Keep the LIVE simulation, including alpha, drag, pause and pins. A
      // fresh simulation seeded with saved coordinates is not equivalent.
      localScene = nextLocalScene;
      for (const node of localScene.nodes) graph.mergeNodeAttributes(node.key, nodeAttributes(node));
      for (const edge of localScene.edges) {
        graph.mergeEdgeAttributes(edge.graphId, edgeAttributes(edge, edgeLabels.get(edge.graphId) ?? ""));
      }
      refreshHighlight();
      return "metadata";
    }
    draggingKey = null;
    dragStart = null;
    dragged = false;
    suppressClick = false;
    renderer?.setCustomBBox(null);
    layout?.destroy();
    layout = null;
    localScene = nextLocalScene;
    localScopeKey = nextScopeKey;
    hoveredId = null;
    graph.clear();
    // Recompute from the exact current visible scene. In particular, a filter,
    // actor, or timeline change must not inherit hidden nodes' old positions.
    layout = createLocalLayout(localScene.layout, {
      fixedPositions: visiblePositions(localScene, pinnedPositions),
      onTick: applyPositions,
      reducedMotion: options.reducedMotion,
    });
    const positions = layout.getPositions();
    for (const node of localScene.nodes) {
      const position = positions[node.key] ?? { x: 0, y: 0 };
      graph.addNode(node.key, {
        x: position.x,
        y: position.y,
        ...nodeAttributes(node),
      });
    }
    for (const edge of localScene.edges) {
      if (!graph.hasNode(edge.sourceKey) || !graph.hasNode(edge.targetKey)) continue;
      graph.addEdgeWithKey(edge.graphId, edge.sourceKey, edge.targetKey,
        edgeAttributes(edge, edgeLabels.get(edge.graphId) ?? ""));
    }
    refreshHighlight();
    return "rebuild";
  };

  loadScene(initialScene);
  renderer = new Sigma(graph, container, {
    // Context keeps inactive tabs mounted. Sigma can observe a zero-width
    // container during the tab switch; GraphCanvas resizes it on reactivation.
    allowInvalidContainer: true,
    defaultNodeType: "circle",
    nodeProgramClasses: {
      circle: NodeCircleProgram,
      square: LocalSquareNodeProgram,
      seed: SeedNodeProgram,
      folder: FolderNodeProgram,
      conversation: ConversationNodeProgram,
    },
    edgeProgramClasses: LOCAL_EDGE_PROGRAMS,
    defaultEdgeType: "link",
    enableEdgeEvents: true,
    hideEdgesOnMove: false,
    hideLabelsOnMove: false,
    // The ordinary-label toggle must not suppress seed/active labels as well.
    renderLabels: true,
    renderEdgeLabels: false,
    labelColor: { color: ink },
    labelSize: 12,
    labelDensity: 0.8,
    labelGridCellSize: 72,
    // Local points are intentionally tiny (3–13 CSS px). Sigma's default
    // size gate would suppress every label in a small real Context scene.
    labelRenderedSizeThreshold: 0,
    defaultDrawNodeLabel: function drawNodeLabel(context, data, settings) {
      // The constructor renders synchronously before its public handle exists.
      // A render-only refresh below owns that first visible label pass instead.
      if (data.key && !renderer) return;
      // Sigma 3.0.3 appends forced labels AFTER its ordinary grid candidates.
      // Allocate and paint priority titles on the first real label callback,
      // after Sigma's resize/process/clear, not its earlier beforeRender event.
      if (data.key && !priorityLabelsPrepared) {
        priorityLabelsPrepared = true;
        for (const key of priorityLabelKeys()) {
          const display = renderer?.getNodeDisplayData(key);
          if (!renderer || !display || display.hidden || !display.label || !graph.hasNode(key)) continue;
          const attributes = graph.getNodeAttributes(key);
          const point = renderer.graphToViewport({ x: Number(attributes.x), y: Number(attributes.y) });
          drawNodeLabel(context, {
            ...display, key, ...point, size: renderer.scaleSize(display.size), forceLabel: true,
          }, settings);
        }
      }
      if (data.key && paintedPriorityLabels.has(data.key)) return;
      if (!data.label) return;
      const viewportWidth = container.clientWidth;
      const viewportHeight = container.clientHeight;
      if (viewportWidth <= 0 || viewportHeight <= 0 || !Number.isFinite(data.x) || !Number.isFinite(data.y)) return;
      const forced = Boolean(data.forceLabel);
      // Priority is a visible-node contract, not a reason to pull offscreen
      // titles into the viewport or inherit labels from an old scene.
      if (forced && (data.x < 0 || data.x > viewportWidth || data.y < 0 || data.y > viewportHeight)) return;
      const label = Array.from(data.label).slice(0, 24).join("");
      const shortened = label.length < Array.from(data.label).length ? `${label}…` : label;
      const offset = data.size + 5;
      const margin = Math.min(8, viewportWidth / 4);
      const roomRight = viewportWidth - data.x - offset - 8;
      const roomLeft = data.x - offset - 8;
      const right = roomRight >= roomLeft || roomLeft < 48;
      const room = Math.min(180, forced ? Math.max(right ? roomRight : roomLeft, viewportWidth - 2 * margin)
        : right ? roomRight : roomLeft);
      if (room < 18 && !forced) return;
      const fontSize = forced ? Math.min(settings.labelSize, Math.max(1, viewportHeight - 4)) : settings.labelSize;
      context.save();
      context.font = `${settings.labelWeight} ${fontSize}px ${settings.labelFont}`;
      context.textAlign = right ? "left" : "right";
      context.textBaseline = "middle";
      const graphemes = Array.from(shortened);
      let visible = shortened;
      while (graphemes.length > 1 && context.measureText(visible).width > room) {
        graphemes.pop();
        visible = `${graphemes.join("")}…`;
      }
      const width = Math.min(room, context.measureText(visible).width);
      const anchorX = data.x + (right ? offset : -offset);
      const x = forced ? Math.max(right ? margin : margin + width,
        Math.min(right ? viewportWidth - margin - width : viewportWidth - margin, anchorX)) : anchorX;
      const insetY = Math.min(fontSize / 2 + 2, viewportHeight / 2);
      const anchorY = forced ? Math.max(insetY, Math.min(viewportHeight - insetY, data.y)) : data.y;
      const lineStep = fontSize + 6;
      // At most 63 nearby-to-distant rows, independent of node count or pixels.
      // Ordinary titles retain their original three-candidate policy.
      const offsets = forced ? [0, ...Array.from({ length: Math.min(31, Math.ceil(viewportHeight / lineStep)) },
        (_, index) => [-(index + 1), index + 1]).flat()] : [0, -1, 1];
      const candidates = offsets.map((step) => {
        const candidateY = anchorY + step * lineStep;
        const y = forced ? Math.max(insetY, Math.min(viewportHeight - insetY, candidateY)) : candidateY;
        return {
          y,
          left: right ? x - 2 : x - width - 2,
          right: right ? x + width + 2 : x + 2,
          top: y - insetY,
          bottom: y + insetY,
        };
      });
      const chosen = candidates.find((candidate) => candidate.top >= 0
        && candidate.bottom <= viewportHeight
        && !labelBounds.some((bound) => candidate.left < bound.right
          && candidate.right > bound.left && candidate.top < bound.bottom
          && candidate.bottom > bound.top))
        // A physically full viewport cannot make every title legible. Still
        // paint the shortened in-bounds priority title; overlap is an explicit
        // last resort, while the accessible relationship list stays available.
        ?? (forced ? candidates[0] : undefined);
      if (!chosen) {
        context.restore();
        return;
      }
      labelBounds.push(chosen);
      const y = chosen.y;
      context.lineWidth = 3;
      context.strokeStyle = tokens.surface;
      context.strokeText(visible, x, y, room);
      context.fillStyle = ink;
      context.fillText(visible, x, y, room);
      context.restore();
      if (forced && data.key) paintedPriorityLabels.add(data.key);
    },
    stagePadding: 28,
    minCameraRatio: 0.15,
    maxCameraRatio: 5,
    defaultDrawNodeHover: () => undefined,
    nodeReducer: (key, data) => {
      const quiet = highlightedStar !== null && !highlightedStar.nodes.has(key);
      const id = localScene.graphIdByKey.get(key);
      const importantLabel = Boolean(data.isSeed || (id && [selectedId, focusedId, hoveredId].includes(id)));
      return {
        ...data,
        hidden: false,
        color: quiet ? data.quietColor : data.color,
        borderColor: quiet ? data.quietBorderColor : data.borderColor,
        label: importantLabel || (showOrdinaryLabels && !quiet) ? data.label : null,
        forceLabel: importantLabel,
      };
    },
    edgeReducer: (key, data) => {
      const quiet = highlightedStar !== null && !highlightedStar.edges.has(key);
      return {
        ...data,
        size: hoveredId === key ? Number(data.size) * 1.5 : data.size,
        color: quiet ? data.quietColor : data.color,
        hidden: false,
      };
    },
  });

  const sigma = renderer;
  sigma.on("beforeRender", () => {
    labelBounds.length = 0;
    priorityLabelsPrepared = false;
    paintedPriorityLabels.clear();
  });
  const emitViewport = () => {
    if (destroyed || contextLost) return;
    const camera = sigma.getCamera().getState();
    options.callbacks.onViewport({
      // Local camera state is normalized Sigma space. The old Cytoscape pan
      // values cannot be migrated losslessly, so this is Local-only state.
      zoom: 1 / camera.ratio,
      pan: { x: camera.x, y: camera.y },
    });
  };
  const setHover = (id: string | null) => {
    if (destroyed || contextLost) return;
    if (hoveredId === id) return;
    hoveredId = id;
    options.callbacks.onHover(id);
    refreshHighlight();
  };
  const endDrag = (cancelled = false) => {
    if (!draggingKey) return;
    const key = draggingKey;
    draggingKey = null;
    suppressClick = dragged || cancelled;
    if (dragged) {
      // Keep the final point fixed before releasing transient drag state.
      const position = cancelled ? null : layout?.pin(key);
      layout?.endDrag(key);
      const graphId = localScene.graphIdByKey.get(key);
      if (position && graphId) {
        pinnedPositions[graphId] = { ...position };
        options.callbacks.onPin(graphId, position);
      }
    }
    sigma.setCustomBBox(null);
    dragStart = null;
    dragged = false;
  };
  const releaseDrag = () => endDrag();
  const cancelDrag = () => endDrag(true);
  const clearSelection = () => {
    hoveredId = focusedId = selectedId = null;
    options.callbacks.onHover(null);
    options.callbacks.onSelect(null);
    refreshHighlight();
  };

  // Sigma's GPU picking covers only the visible glyph (often 3–5px). Resolve
  // the caller's bounded Local scene in CSS viewport pixels before accepting
  // its wider edge corridor, without inflating the rendered node geometry.
  const nearestNode = (point: GraphPoint | undefined): string | null => {
    if (!finitePoint(point)) return null;
    let nearest: string | null = null;
    let distance = Infinity;
    for (const key of graph.nodes()) {
      const display = sigma.getNodeDisplayData(key);
      if (!display || display.hidden) continue;
      const attributes = graph.getNodeAttributes(key);
      const center = sigma.graphToViewport({ x: Number(attributes.x), y: Number(attributes.y) });
      const delta = Math.hypot(point.x - center.x, point.y - center.y);
      const radius = Math.max(12, sigma.scaleSize(display.size));
      if (delta <= radius && delta < distance) {
        nearest = key;
        distance = delta;
      }
    }
    return nearest;
  };
  const selectKey = (key: string) => {
    const id = localScene.graphIdByKey.get(key);
    if (!id) return;
    selectedId = id;
    options.callbacks.onSelect(id);
    refreshHighlight();
  };

  sigma.on("enterNode", ({ node }) => setHover(localScene.graphIdByKey.get(node) ?? null));
  sigma.on("leaveNode", () => setHover(null));
  sigma.on("enterEdge", ({ edge }) => setHover(edge));
  sigma.on("leaveEdge", () => setHover(null));
  const selectNode = ({ node, event }: SigmaNodeEventPayload) => {
    if (destroyed || contextLost || suppressClick) return;
    selectKey(nearestNode(event) ?? node);
  };
  const selectEdge = ({ edge, event }: SigmaEdgeEventPayload) => {
    if (destroyed || contextLost || suppressClick) return;
    const node = nearestNode(event);
    if (node) { selectKey(node); return; }
    selectedId = edge;
    options.callbacks.onSelect(edge);
    refreshHighlight();
  };
  const selectStage = ({ event }: SigmaStageEventPayload) => {
    if (destroyed || contextLost || suppressClick) return;
    const node = nearestNode(event);
    if (node) selectKey(node);
    else clearSelection();
  };
  sigma.on("clickNode", selectNode);
  sigma.on("clickEdge", selectEdge);
  sigma.on("clickStage", selectStage);
  // Sigma groups rapid clicks by time alone, including clicks on different
  // points or on the blank stage. Those still need normal selection semantics.
  sigma.on("doubleClickNode", (payload: SigmaNodeEventPayload) => {
    payload.event.preventSigmaDefault();
    selectNode(payload);
  });
  sigma.on("doubleClickEdge", (payload: SigmaEdgeEventPayload) => {
    payload.event.preventSigmaDefault();
    selectEdge(payload);
  });
  sigma.on("doubleClickStage", (payload: SigmaStageEventPayload) => {
    payload.event.preventSigmaDefault();
    selectStage(payload);
  });
  const beginNodeDrag = (node: string, event: SigmaNodeEventPayload["event"]) => {
    if (destroyed || contextLost) return;
    cancelDrag();
    suppressClick = false;
    if (event.original instanceof MouseEvent && event.original.button !== 0) return;
    if (typeof TouchEvent !== "undefined" && event.original instanceof TouchEvent
      && event.original.touches.length !== 1) return;
    if (graph.getNodeAttribute(node, "isSeed")) return;
    draggingKey = node;
    dragStart = { x: event.x, y: event.y };
    dragged = false;
    event.preventSigmaDefault();
  };
  const beginStageGesture = ({ event }: SigmaStageEventPayload) => {
    if (destroyed || contextLost) return;
    const node = nearestNode(event);
    if (node) beginNodeDrag(node, event);
    else { cancelDrag(); suppressClick = false; }
  };
  sigma.on("downStage", beginStageGesture);
  sigma.on("downEdge", beginStageGesture);
  sigma.on("downNode", ({ node, event }) => beginNodeDrag(nearestNode(event) ?? node, event));
  sigma.on("moveBody", ({ event }) => {
    if (destroyed || contextLost || !draggingKey) return;
    if (typeof TouchEvent !== "undefined" && event.original instanceof TouchEvent
      && event.original.touches.length !== 1) {
      cancelDrag();
      return;
    }
    event.preventSigmaDefault();
    const viewport = { x: event.x, y: event.y };
    if (!dragged) {
      if (!dragStart || Math.hypot(viewport.x - dragStart.x, viewport.y - dragStart.y) <= 3) return;
      // Avoid a moving normalization box while the pointer owns a point.
      sigma.setCustomBBox(sigma.getBBox());
      layout?.beginDrag(draggingKey);
      dragged = true;
    }
    const position = sigma.viewportToGraph(viewport);
    layout?.moveDrag(draggingKey, position);
  });
  sigma.on("upNode", releaseDrag);
  sigma.on("upEdge", releaseDrag);
  sigma.on("upStage", releaseDrag);
  // Also release outside the canvas, on cancelled touch/pointer gestures,
  // and when the window loses focus. Sigma's up events handle both captors.
  window.addEventListener("mouseup", releaseDrag);
  window.addEventListener("touchend", releaseDrag);
  window.addEventListener("pointerup", releaseDrag);
  window.addEventListener("touchcancel", cancelDrag, true);
  window.addEventListener("pointercancel", cancelDrag);
  window.addEventListener("blur", cancelDrag);
  sigma.getCamera().on("updated", emitViewport);

  const onContextLost = (event: Event) => {
    event.preventDefault();
    if (destroyed || contextLost) return;
    layout?.pause();
    cancelDrag();
    contextLost = true;
    options.callbacks.onContextStatus?.("lost");
  };
  const onContextRestored = () => {
    if (destroyed || !contextLost || restorationRequested) return;
    restorationRequested = true;
    // Sigma owns programs and picking buffers. Restoration must build a new
    // engine; these old handles must never be marked ready or resumed.
    options.callbacks.onContextStatus?.("restored");
  };
  const contextCanvases = Object.values(sigma.getCanvases());
  for (const canvas of contextCanvases) {
    canvas.addEventListener("webglcontextlost", onContextLost);
    canvas.addEventListener("webglcontextrestored", onContextRestored);
  }

  if (options.initialViewport) {
    const { zoom, pan } = options.initialViewport;
    if (Number.isFinite(zoom) && zoom > 0 && finitePoint(pan)) {
      sigma.getCamera().setState({ ratio: 1 / zoom, x: pan.x, y: pan.y });
    }
  }
  // No reindex, layout or camera change: the constructor's processed scene is
  // ready, and frame bookkeeping can now reserve priority labels correctly.
  sigma.refresh({ partialGraph: { nodes: [], edges: [] }, skipIndexation: true });

  return {
    centerOn(id) {
      if (destroyed || contextLost) return;
      const key = localScene.keyByGraphId.get(id);
      const node = key ? sigma.getNodeDisplayData(key) : null;
      const edge = graph.hasEdge(id) ? graph.extremities(id) : null;
      const source = edge ? sigma.getNodeDisplayData(edge[0]) : null;
      const target = edge ? sigma.getNodeDisplayData(edge[1]) : null;
      const center = node ?? (source && target
        ? { x: (source.x + target.x) / 2, y: (source.y + target.y) / 2 }
        : null);
      if (!center) return;
      const targetState = { x: center.x, y: center.y };
      // Sigma's animate(0) can evaluate 0 / 0 on its first frame. Immediate
      // centering must leave a finite camera, even before another RAF runs.
      if (options.reducedMotion) sigma.getCamera().setState(targetState);
      else sigma.getCamera().animate(targetState, { duration: 250 });
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      draggingKey = null;
      layout?.destroy();
      for (const canvas of contextCanvases) {
        canvas.removeEventListener("webglcontextlost", onContextLost);
        canvas.removeEventListener("webglcontextrestored", onContextRestored);
      }
      window.removeEventListener("mouseup", releaseDrag);
      window.removeEventListener("touchend", releaseDrag);
      window.removeEventListener("pointerup", releaseDrag);
      window.removeEventListener("touchcancel", cancelDrag, true);
      window.removeEventListener("pointercancel", cancelDrag);
      window.removeEventListener("blur", cancelDrag);
      sigma.getCamera().off("updated", emitViewport);
      sigma.kill();
    },
    fit() {
      if (destroyed || contextLost) return;
      sigma.getCamera().setState({ x: 0.5, y: 0.5, angle: 0, ratio: 1 });
      sigma.refresh();
    },
    focus(id) {
      if (destroyed || contextLost) return;
      focusedId = id;
      // A stationary pointer must not mask a newer DOM keyboard focus. A
      // subsequent pointer entry still wins; blur preserves that newer hover.
      if (id !== null && hoveredId !== null) {
        hoveredId = null;
        options.callbacks.onHover(null);
      }
      if (!destroyed && !contextLost) refreshHighlight();
    },
    pin(id, pinned, position) {
      if (destroyed || contextLost) return null;
      // A filter can temporarily remove the point, but an explicit release
      // still revokes its pin before the point is made visible again.
      if (!pinned) delete pinnedPositions[id];
      const key = localScene.keyByGraphId.get(id);
      if (!key) return null;
      if (!pinned) {
        layout?.unpin(key);
        return layout?.getPositions()[key] ?? null;
      }
      const point = layout?.pin(key, position) ?? null;
      if (point) pinnedPositions[id] = { ...point };
      return point;
    },
    replaceScene(scene) {
      if (destroyed || contextLost) return;
      const update = loadScene(scene);
      refreshHighlight();
      return update;
    },
    reset() {
      if (destroyed || contextLost) return;
      cancelDrag();
      for (const id of Object.keys(pinnedPositions)) delete pinnedPositions[id];
      layout?.clearPins();
      this.fit();
    },
    resize() {
      if (destroyed || contextLost) return;
      sigma.resize().refresh();
    },
    resumeLayout() { if (!destroyed && !contextLost) layout?.resume(); },
    setLabelsVisible(enabled) {
      if (destroyed || contextLost) return;
      if (showOrdinaryLabels === enabled) return;
      showOrdinaryLabels = enabled;
      refreshHighlight();
    },
    select(id) {
      if (destroyed || contextLost) return;
      selectedId = id;
      refreshHighlight();
    },
    setViewport(viewport) {
      if (destroyed || contextLost || !Number.isFinite(viewport.zoom) || viewport.zoom <= 0
        || !finitePoint(viewport.pan)) return;
      sigma.getCamera().setState({
        ratio: 1 / viewport.zoom,
        x: viewport.pan.x,
        y: viewport.pan.y,
      });
    },
    stopLayout() { cancelDrag(); layout?.pause(); },
    whenSettled() { return Promise.resolve(); },
    zoomBy(factor) {
      if (destroyed || contextLost || !Number.isFinite(factor) || factor <= 0) return;
      const camera = sigma.getCamera();
      camera.setState({ ratio: camera.getBoundedRatio(camera.ratio / factor) });
    },
  };
}
