import type { GraphPoint } from "./rendererTypes";

export type GraphHoverChipTarget = { kind: "node" | "edge"; id: string };

// Keeps an edge chip clear of the pointer glyph at its anchor.
export const GRAPH_EDGE_CHIP_OFFSET_PX = 10;

/**
 * Anchor an edge chip on the part of the relation that is actually on screen:
 * nearest the pointer when known, otherwise the visible segment's midpoint.
 * A zoomed-in relation whose midpoint is offscreen must still be named.
 */
export function graphEdgeChipAnchor(
  source: GraphPoint,
  target: GraphPoint,
  width: number,
  height: number,
  pointer?: GraphPoint | null,
): GraphPoint | null {
  const dx = target.x - source.x, dy = target.y - source.y;
  let enter = 0, exit = 1;
  // Liang–Barsky clip against the viewport rectangle.
  for (const [p, q] of [[-dx, source.x], [dx, width - source.x], [-dy, source.y], [dy, height - source.y]]) {
    if (p === 0) {
      if (q < 0) return null;
      continue;
    }
    const t = q / p;
    if (p < 0) enter = Math.max(enter, t);
    else exit = Math.min(exit, t);
    if (enter > exit) return null;
  }
  const lengthSquared = dx * dx + dy * dy;
  const along = pointer && Number.isFinite(pointer.x) && Number.isFinite(pointer.y) && lengthSquared > 0
    ? ((pointer.x - source.x) * dx + (pointer.y - source.y) * dy) / lengthSquared
    : (enter + exit) / 2;
  const t = Math.max(enter, Math.min(exit, along));
  return { x: source.x + t * dx, y: source.y + t * dy };
}

/** Imperative overlay: pointer motion never needs a React or label-layout pass. */
export function createGraphHoverChip(container: HTMLElement, supplied?: HTMLDivElement | null) {
  const element = supplied ?? document.createElement("div");
  element.className = "graph-hover-chip";
  element.setAttribute("aria-hidden", "true");
  element.hidden = true;
  if (!supplied) container.appendChild(element);
  let appliedMaxWidth = -1;
  let transform = "";
  // Measure only when the text or width limit changes. Repositioning writes a
  // compositor-only transform, so following a moving node never forces layout.
  let size: { text: string; maxWidth: number; width: number; height: number } | null = null;

  // Distinct from label [data-node-id] hooks, which mean "this label's node".
  const setTarget = (target: GraphHoverChipTarget | null) => {
    const nodeId = target?.kind === "node" ? target.id : undefined;
    const edgeId = target?.kind === "edge" ? target.id : undefined;
    if (element.dataset.hoverNodeId !== nodeId) {
      if (nodeId === undefined) delete element.dataset.hoverNodeId; else element.dataset.hoverNodeId = nodeId;
    }
    if (element.dataset.hoverEdgeId !== edgeId) {
      if (edgeId === undefined) delete element.dataset.hoverEdgeId; else element.dataset.hoverEdgeId = edgeId;
    }
  };
  const hide = () => {
    if (!element.hidden) element.hidden = true;
    setTarget(null);
    if (container.style.cursor) container.style.cursor = "";
  };

  return {
    hide,
    show(text: string, point: GraphPoint, target: GraphHoverChipTarget, radius = 0) {
      // Standalone hosts may append their native label layer after construction.
      if (!supplied && container.lastElementChild !== element) container.appendChild(element);
      const width = container.clientWidth, height = container.clientHeight;
      if (!text || !Number.isFinite(point.x) || !Number.isFinite(point.y)
        || point.x < 0 || point.y < 0 || point.x > width || point.y > height) {
        hide();
        return;
      }
      if (element.textContent !== text) element.textContent = text;
      const maxWidth = Math.max(0, Math.min(320, width - 8));
      if (appliedMaxWidth !== maxWidth) {
        appliedMaxWidth = maxWidth;
        element.style.maxWidth = `${maxWidth}px`;
      }
      setTarget(target);
      if (element.hidden) element.hidden = false;
      if (container.style.cursor !== "pointer") container.style.cursor = "pointer";
      if (!size || size.text !== text || size.maxWidth !== maxWidth) {
        size = { text, maxWidth, width: element.offsetWidth, height: element.offsetHeight };
      }
      const offset = radius + 4;
      const right = point.x + offset;
      const preferred = right + size.width <= width - 4 ? right : point.x - offset - size.width;
      const x = Math.round(Math.max(4, Math.min(width - size.width - 4, preferred)));
      const y = Math.round(Math.max(4, Math.min(height - size.height - 4, point.y - size.height / 2)));
      const next = `translate(${x}px, ${y}px)`;
      if (next !== transform) {
        transform = next;
        element.style.transform = next;
      }
    },
    destroy() {
      hide();
      if (!supplied) element.remove();
    },
  };
}
