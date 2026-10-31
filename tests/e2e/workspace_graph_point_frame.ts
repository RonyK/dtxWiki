import type { Page } from "@playwright/test";

export const WORKSPACE_GRAPH_HOST_SELECTOR = '[data-testid="graph-canvas"]';

export type WorkspacePointFrame = {
  camera: { x: number; y: number; k: number } | null;
  points: Array<{ id: string; x: number; y: number }>;
};

/** Serializable browser observer: bounded GPU-backed coordinates, not text rectangles. */
export function readWorkspacePointFrame(root: Element | string): WorkspacePointFrame {
  // This function is serialized alone: resolve the current host in the same
  // browser invocation as the observation, never through a cached handle.
  const roots = typeof root === "string" ? document.querySelectorAll(root) : [root];
  if (roots.length !== 1) throw new Error("Point observer requires exactly one connected current-document host");
  const element = roots[0];
  if (!element.isConnected || element.ownerDocument !== document) {
    throw new Error("Point observer requires a connected current-document host");
  }
  const native = element.querySelector("canvas") as HTMLCanvasElement & { __zoom?: { x: number; y: number; k: number } };
  const camera = native?.__zoom;
  const labels = Array.from(element.querySelectorAll<HTMLElement>(
    "[data-workspace-map-labels] span[data-node-id][data-point-x][data-point-y]",
  ));
  if (labels.length > 40) throw new Error("Native point-coordinate observation exceeds its 40-row budget");
  const ids = new Set<string>();
  // Text packing may set display:none after these native coordinates publish.
  const points = labels.filter(label => label.dataset.fullTitle !== "true")
    .flatMap(label => {
      const id = label.dataset.nodeId!, rawX = label.dataset.pointX!, rawY = label.dataset.pointY!;
      const x = Number(rawX), y = Number(rawY);
      if (!id.trim() || ids.has(id) || !rawX.trim() || !rawY.trim() || !Number.isFinite(x) || !Number.isFinite(y)) return [];
      ids.add(id); return [{ id, x, y }];
    });
  return { camera: camera ? { x: camera.x, y: camera.y, k: camera.k } : null, points };
}

/** Separate UI requirement; decluttering may hide text without hiding a graph point. */
export function visibleOrdinaryTitleCount(root: Element | string): number {
  // Keep root resolution inside this independently serializable observer too.
  const roots = typeof root === "string" ? document.querySelectorAll(root) : [root];
  if (roots.length !== 1) throw new Error("Point observer requires exactly one connected current-document host");
  const element = roots[0];
  if (!element.isConnected || element.ownerDocument !== document) {
    throw new Error("Point observer requires a connected current-document host");
  }
  return Array.from(element.querySelectorAll<HTMLElement>("[data-workspace-map-labels] span[data-full-title=\"false\"]"))
    .filter(label => {
      const style = getComputedStyle(label), rectangle = label.getBoundingClientRect();
      return Boolean(label.textContent?.trim()) && style.display !== "none" && style.visibility !== "hidden"
        && Number(style.opacity) > 0 && rectangle.width > 0 && rectangle.height > 0;
    }).length;
}

/** One browser invocation owns root resolution and observation; no handle adoption/prewait. */
export function readWorkspacePointFrameOnPage(page: Pick<Page, "evaluate">, selector = WORKSPACE_GRAPH_HOST_SELECTOR) {
  return page.evaluate(readWorkspacePointFrame, selector);
}

export function visibleOrdinaryTitleCountOnPage(page: Pick<Page, "evaluate">, selector = WORKSPACE_GRAPH_HOST_SELECTOR) {
  return page.evaluate(visibleOrdinaryTitleCount, selector);
}

export function movedPointCount(before: WorkspacePointFrame, after: WorkspacePointFrame, tolerance = .1): number {
  const old = new Map(before.points.map(point => [point.id, point]));
  return after.points.filter(point => {
    const previous = old.get(point.id);
    return previous && Math.hypot(point.x - previous.x, point.y - previous.y) > tolerance;
  }).length;
}
