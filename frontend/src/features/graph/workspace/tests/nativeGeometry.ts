/** Snapshot-only diagnostics; callers supply already-read positions and body disks. */
export function measureNativeGeometry(positions: ArrayLike<number>, radii: Float32Array, spaceSize: number, pinned: readonly number[]) {
  if (!Number.isFinite(spaceSize) || spaceSize <= 0 || positions.length !== radii.length * 2
    || !radii.every(radius => Number.isFinite(radius) && radius > 0)) {
    throw new Error("Native geometry rows disagree");
  }
  const cellSize = radii.reduce((largest, radius) => Math.max(largest, radius * 2), 1);
  const cells = new Map<string, number[]>(), overlaps = new Set<number>(), pins = new Set(pinned);
  let overlapPairs = 0, boundaryNodes = 0, unpinnedBoundaryNodes = 0;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let index = 0; index < radii.length; index++) {
    const x = positions[index * 2], y = positions[index * 2 + 1];
    if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error("Finite native positions required");
    minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    if (x === 0 || y === 0 || x === spaceSize || y === spaceSize) {
      boundaryNodes++; if (!pins.has(index)) unpinnedBoundaryNodes++;
    }
    const cx = Math.floor(x / cellSize), cy = Math.floor(y / cellSize);
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      for (const other of cells.get(`${cx + dx},${cy + dy}`) ?? []) {
        const distance = Math.hypot(x - positions[other * 2], y - positions[other * 2 + 1]);
        if (distance + 1e-6 < radii[index] + radii[other]) {
          overlapPairs++; overlaps.add(index); overlaps.add(other);
        }
      }
    }
    const key = `${cx},${cy}`;
    const row = cells.get(key) ?? []; row.push(index); cells.set(key, row);
  }
  return { nodes: radii.length, boundaryNodes, unpinnedBoundaryNodes, overlapPairs,
    nodesWithOverlappingBodyDisks: overlaps.size, bounds: { minX, maxX, minY, maxY } };
}
