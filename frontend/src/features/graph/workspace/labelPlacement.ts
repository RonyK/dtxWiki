export const WORKSPACE_MAP_LABEL_LIMIT = 40;

export type WorkspaceMapLabelCandidate = {
  screenX: number;
  screenY: number;
  width: number;
  height: number;
  priority: "active" | "explicit" | "ordinary";
  /** The previous placement showed this same node's title. Omitted: no history. */
  wasVisible?: boolean;
  /** Horizontal and vertical distance from the anchor to the title; 8 px unless a wide glyph needs more. */
  clearance?: number;
};

export type WorkspaceMapLabelPlacement = {
  x: number;
  y: number;
  visible: boolean;
  // "deferred": a hidden ordinary title waits for the end of a camera or point gesture.
  reason: "placed" | "collision" | "outside-viewport" | "invalid" | "viewport-constrained" | "deferred";
};

export type WorkspaceMapLabelPlacementOptions = {
  /**
   * A camera or point gesture is in progress. Ordinary titles keep the shown
   * set: none appears, and a shown one yields only to a mandatory title.
   */
  freeze?: boolean;
};

type Rectangle = { x: number; y: number; width: number; height: number };
const GAP = 5;
// A hidden ordinary title needs this much more clearance to appear than a shown
// one needs to stay, so a title at the collision distance does not flicker.
const ENTRY_GAP = GAP + 4;

function intersects(left: Rectangle, right: Rectangle, gap = GAP): boolean {
  return left.x < right.x + right.width + gap && left.x + left.width + gap > right.x
    && left.y < right.y + right.height + gap && left.y + left.height + gap > right.y;
}

/** Screen-only placement: no graph coordinates, camera, or relation geometry is changed. */
export function placeWorkspaceMapLabels(
  candidates: readonly WorkspaceMapLabelCandidate[],
  viewport: { width: number; height: number },
  options: WorkspaceMapLabelPlacementOptions = {},
): WorkspaceMapLabelPlacement[] {
  if (candidates.length > WORKSPACE_MAP_LABEL_LIMIT) throw new RangeError("Workspace Map label limit exceeded");
  const placements = candidates.map((): WorkspaceMapLabelPlacement => ({ x: 0, y: 0, visible: false, reason: "invalid" }));
  if (![viewport.width, viewport.height].every((value) => Number.isFinite(value) && value > 0)) return placements;
  const occupied: Rectangle[] = [];
  const explicit: Rectangle[] = [];
  const rank = { active: 0, explicit: 1, ordinary: 2 };
  // Within a priority, titles already on screen keep their place before a
  // higher-ranked newcomer takes it; without history the input order decides.
  const order = candidates.map((candidate, index) => ({ candidate, index }))
    .sort((left, right) => rank[left.candidate.priority] - rank[right.candidate.priority]
      || Number(right.candidate.wasVisible === true) - Number(left.candidate.wasVisible === true)
      || left.index - right.index);
  for (const { candidate, index } of order) {
    const { screenX, screenY, width, height, priority, wasVisible } = candidate;
    const clear = Math.max(8, candidate.clearance ?? 8);
    if (![screenX, screenY, width, height].every(Number.isFinite) || width <= 0 || height <= 0) continue;
    if (width > viewport.width || height > viewport.height) {
      // An oversized active title cannot fit an immutable stage. Keep its full
      // DOM/title text visible in a constrained surface; Details owns the full
      // readable title. Never drop active intent because of label packing.
      placements[index].reason = "outside-viewport";
      if (priority === "active") {
        const rectangle = { x: 0, y: 0, width: Math.min(width, viewport.width), height: Math.min(height, viewport.height) };
        placements[index] = { x: rectangle.x, y: rectangle.y, visible: true, reason: "viewport-constrained" };
        occupied.push(rectangle); explicit.push(rectangle);
      }
      continue;
    }
    const clamp = ([x, y]: readonly number[]): [number, number] => [
      Math.max(0, Math.min(x, viewport.width - width)), Math.max(0, Math.min(y, viewport.height - height)),
    ];
    const preferred: [number, number] = [screenX + clear, screenY - 9];
    if (priority === "ordinary" && options.freeze) {
      const rectangle = { x: preferred[0], y: preferred[1], width, height };
      if (rectangle.x < 0 || rectangle.y < 0 || rectangle.x + width > viewport.width || rectangle.y + height > viewport.height) {
        placements[index].reason = "outside-viewport";
      } else if (wasVisible !== true) placements[index].reason = "deferred";
      else if (explicit.some((other) => intersects(rectangle, other))) placements[index].reason = "collision";
      else {
        placements[index] = { x: rectangle.x, y: rectangle.y, visible: true, reason: "placed" };
        occupied.push(rectangle);
      }
      continue;
    }
    const entryGap = priority === "ordinary" && wasVisible === false ? ENTRY_GAP : GAP;
    // Ordinary labels retain their single local placement. Explicit titles
    // get bounded alternatives around the anchor and previously reserved
    // title edges (at most 6 + 2*40 candidates, never a viewport-wide search).
    const alternatives: Array<[number, number]> = priority === "ordinary" ? [preferred] : [
      preferred, [screenX - width - clear, screenY - 9],
      [screenX - width / 2, screenY - height - clear], [screenX - width / 2, screenY + clear],
      [screenX + clear, screenY + clear], [screenX - width - clear, screenY + clear],
      ...explicit.flatMap((rectangle): Array<[number, number]> => [
        [preferred[0], rectangle.y - height - GAP], [preferred[0], rectangle.y + rectangle.height + GAP],
      ]),
    ].map(clamp);
    let fitsViewport = false;
    for (const [x, y] of alternatives) {
      if (x < 0 || y < 0 || x + width > viewport.width || y + height > viewport.height) continue;
      fitsViewport = true;
      const rectangle = { x, y, width, height };
      if (occupied.some((other) => intersects(rectangle, other, entryGap))) continue;
      placements[index] = { x, y, visible: true, reason: "placed" };
      occupied.push(rectangle);
      if (priority !== "ordinary") explicit.push(rectangle);
      break;
    }
    if (!placements[index].visible) placements[index].reason = fitsViewport ? "collision" : "outside-viewport";
  }
  return placements;
}
