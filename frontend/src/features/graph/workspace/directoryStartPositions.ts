import type { WorkspaceMapColumns } from "../map/sceneColumns";

/** One containment level: the nodes it holds and the deeper levels below it. */
type Level = { node: number; key: string; children: Level[]; members: number[]; count: number };

// Close to the radius a relation layout of this size settles to.
const OUTER_RADIUS_PER_SQRT_DOCUMENT = 22;
const RING_FILL = .86;
const SECTOR_GAP = .02;
const GOLDEN_FRACTION = .6180339887;
const MAX_DOMINANT_BRANCH_SHARE = .6;

/** Each row's TreeRail container row, or -1 at the top. Older columns are flat. */
export function workspaceContainerIndices(columns: Pick<WorkspaceMapColumns, "nodeKeys" | "nodeContainerIndices">): Int32Array {
  return columns.nodeContainerIndices ?? new Int32Array(columns.nodeKeys.length).fill(-1);
}

/**
 * A container sits in its own level with what it holds; a leaf sits in its
 * container's level. Iterative throughout: TreeRail chains may be deep.
 */
function containmentLevels(containers: ArrayLike<number>, keys: readonly string[]): Level {
  const count = containers.length;
  const parentOf = (node: number) => {
    const container = containers[node];
    return container >= 0 && container < count && container !== node ? container : -1;
  };
  // A malformed loop is cut where a walk first meets itself; the API rejects loops.
  const parent = new Int32Array(count).fill(-1), state = new Uint8Array(count);
  for (let start = 0; start < count; start++) {
    const walk: number[] = [];
    let node = start;
    while (node >= 0 && state[node] === 0) { state[node] = 1; walk.push(node); parent[node] = parentOf(node); node = parent[node]; }
    if (node >= 0 && state[node] === 1) parent[walk[walk.length - 1]] = -1;
    for (const visited of walk) state[visited] = 2;
  }
  const holds = new Uint8Array(count);
  for (let node = 0; node < count; node++) if (parent[node] >= 0) holds[parent[node]] = 1;
  const root: Level = { node: -1, key: "", children: [], members: [], count: 0 };
  const levels = new Map<number, Level>();
  const levelOf = (node: number): Level => {
    if (node < 0) return root;
    let level = levels.get(node);
    if (!level) { level = { node, key: keys[node] ?? "", children: [], members: [], count: 0 }; levels.set(node, level); }
    return level;
  };
  for (let node = 0; node < count; node++) {
    if (holds[node]) levelOf(parent[node]).children.push(levelOf(node));
    levelOf(holds[node] ? node : parent[node]).members.push(node);
  }
  // Post-order counts without recursion.
  const order: Level[] = [], stack = [root];
  while (stack.length) { const level = stack.pop()!; order.push(level); stack.push(...level.children); }
  for (let index = order.length - 1; index >= 0; index--) {
    const level = order[index];
    level.children.sort((left, right) => left.key < right.key ? -1 : left.key > right.key ? 1 : 0);
    level.count = level.members.length + level.children.reduce((sum, child) => sum + child.count, 0);
  }
  return root;
}

/**
 * TreeRail root at the center, each containment level one ring further out,
 * and every subtree inside its own angular sector. Start coordinates only;
 * containment lines and relations then shape the map.
 */
export function workspaceDirectoryStartPositions(containers: ArrayLike<number>, keys: readonly string[]): Float32Array {
  const positions = new Float32Array(containers.length * 2);
  let root = containmentLevels(containers, keys);
  // A single wrapping container must not spend the center ring on nothing.
  while (root.members.length === 0 && root.children.length === 1) root = root.children[0];
  let depth = 0;
  for (let frontier = root.children, level = 1; frontier.length; frontier = frontier.flatMap(child => child.children), level++) depth = level;
  const ring = OUTER_RADIUS_PER_SQRT_DOCUMENT * Math.sqrt(containers.length) / (depth + 1);
  const work: Array<{ level: Level; depth: number; start: number; end: number }> = [{ level: root, depth: 0, start: 0, end: Math.PI * 2 }];
  while (work.length) {
    const { level, depth: ringIndex, start, end } = work.pop()!;
    const inner = ringIndex * ring, outer = inner + ring * RING_FILL;
    level.members.forEach((node, index) => {
      const area = (index + .5) / level.members.length;
      const angle = start + (end - start) * ((index * GOLDEN_FRACTION) % 1);
      const radius = Math.sqrt(inner * inner + area * (outer * outer - inner * inner));
      positions[node * 2] = radius * Math.cos(angle);
      positions[node * 2 + 1] = radius * Math.sin(angle);
    });
    const span = end - start, nested = level.count - level.members.length;
    let cursor = start;
    for (const child of level.children) {
      const width = span * child.count / nested, gap = width * SECTOR_GAP;
      work.push({ level: child, depth: ringIndex + 1, start: cursor + gap, end: cursor + width - gap });
      cursor += width;
    }
  }
  return positions;
}

/** True once nodes split into branches, looking past any container holding most of them. */
export function directoryStartIsStructured(containers: ArrayLike<number>, keys: readonly string[]): boolean {
  let level = containmentLevels(containers, keys);
  while (level.children.length > 0) {
    const largest = level.children.reduce((best, child) => child.count > best.count ? child : best);
    if (largest.count <= level.count * MAX_DOMINANT_BRANCH_SHARE) return true;
    level = largest;
  }
  return false;
}
