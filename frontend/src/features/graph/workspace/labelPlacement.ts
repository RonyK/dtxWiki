export const WORKSPACE_MAP_LABEL_LIMIT = 40;

export type WorkspaceMapLabelCandidate = {
  screenX: number;
  screenY: number;
  width: number;
  height: number;
  priority: "active" | "explicit" | "ordinary";
};

export type WorkspaceMapLabelPlacement = {
  x: number;
  y: number;
  visible: boolean;
  reason: "placed" | "collision" | "outside-viewport" | "invalid" | "viewport-constrained";
};

type Rectangle = { x: number; y: number; width: number; height: number };
const GAP = 5;

function intersects(left: Rectangle, right: Rectangle): boolean {
  return left.x < right.x + right.width + GAP && left.x + left.width + GAP > right.x
    && left.y < right.y + right.height + GAP && left.y + left.height + GAP > right.y;
}

/** Screen-only placement: no graph coordinates, camera, or relation geometry is changed. */
export function placeWorkspaceMapLabels(
  candidates: readonly WorkspaceMapLabelCandidate[],
  viewport: { width: number; height: number },
): WorkspaceMapLabelPlacement[] {
  if (candidates.length > WORKSPACE_MAP_LABEL_LIMIT) throw new RangeError("Workspace Map label limit exceeded");
  const placements = candidates.map((): WorkspaceMapLabelPlacement => ({ x: 0, y: 0, visible: false, reason: "invalid" }));
  if (![viewport.width, viewport.height].every((value) => Number.isFinite(value) && value > 0)) return placements;
  const occupied: Rectangle[] = [];
  const explicit: Rectangle[] = [];
  const rank = { active: 0, explicit: 1, ordinary: 2 };
  const order = candidates.map((candidate, index) => ({ candidate, index }))
    .sort((left, right) => rank[left.candidate.priority] - rank[right.candidate.priority] || left.index - right.index);
  for (const { candidate, index } of order) {
    const { screenX, screenY, width, height, priority } = candidate;
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
    const preferred: [number, number] = [screenX + 8, screenY - 9];
    // Ordinary labels retain their single local placement. Explicit titles
    // get bounded alternatives around the anchor and previously reserved
    // title edges (at most 6 + 2*40 candidates, never a viewport-wide search).
    const alternatives: Array<[number, number]> = priority === "ordinary" ? [preferred] : [
      preferred, [screenX - width - 8, screenY - 9],
      [screenX - width / 2, screenY - height - 8], [screenX - width / 2, screenY + 8],
      [screenX + 8, screenY + 8], [screenX - width - 8, screenY + 8],
      ...explicit.flatMap((rectangle): Array<[number, number]> => [
        [preferred[0], rectangle.y - height - GAP], [preferred[0], rectangle.y + rectangle.height + GAP],
      ]),
    ].map(clamp);
    let fitsViewport = false;
    for (const [x, y] of alternatives) {
      if (x < 0 || y < 0 || x + width > viewport.width || y + height > viewport.height) continue;
      fitsViewport = true;
      const rectangle = { x, y, width, height };
      if (occupied.some((other) => intersects(rectangle, other))) continue;
      placements[index] = { x, y, visible: true, reason: "placed" };
      occupied.push(rectangle);
      if (priority !== "ordinary") explicit.push(rectangle);
      break;
    }
    if (!placements[index].visible) placements[index].reason = fitsViewport ? "collision" : "outside-viewport";
  }
  return placements;
}
