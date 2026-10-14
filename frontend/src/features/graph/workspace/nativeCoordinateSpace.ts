import type { WorkspaceRelationCollisionBasis } from "./relationPhysics";

export const WORKSPACE_MAP_CANONICAL_SPACE_SIZE = 4096;

/** Only SDK boundaries use native units; public state and snapshots stay canonical. */
export type WorkspaceNativeCoordinateSpace = Readonly<{
  spaceSize: number;
  scale: number;
  toNativePositions(positions: ArrayLike<number>): Float32Array;
  fromNativePositions(positions: ArrayLike<number>): Float32Array;
  toNativePoint(point: readonly [number, number]): [number, number];
  fromNativePoint(point: readonly [number, number]): [number, number];
  toNativeZoom(zoom: number): number;
  fromNativeZoom(zoom: number): number;
  toNativeCollisionBasis(basis: Readonly<WorkspaceRelationCollisionBasis>): WorkspaceRelationCollisionBasis;
}>;

function finiteFloat32(value: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(Math.fround(value))) {
    throw new RangeError("Coordinate must be finite Float32");
  }
  return value;
}

function convertPositions(positions: ArrayLike<number>, scale: number): Float32Array {
  if (!Number.isSafeInteger(positions.length) || positions.length < 0) {
    throw new RangeError("Position length must be a nonnegative safe integer");
  }
  const converted = new Float32Array(positions.length);
  for (let index = 0; index < positions.length; index++) {
    converted[index] = finiteFloat32(finiteFloat32(positions[index]) * scale);
  }
  return converted;
}

function convertPoint(point: readonly [number, number], scale: number): [number, number] {
  return [finiteFloat32(finiteFloat32(point[0]) * scale), finiteFloat32(finiteFloat32(point[1]) * scale)];
}

function positiveFloat32(value: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(Math.fround(value)) || Math.fround(value) <= 0) {
    throw new RangeError("Length or zoom must be positive finite Float32");
  }
  return value;
}

/** Match pinned Cosmos's space reduction without changing canonical layout inputs. */
export function createWorkspaceNativeCoordinateSpace(maxTextureSize: number): WorkspaceNativeCoordinateSpace {
  if (!Number.isSafeInteger(maxTextureSize) || maxTextureSize < 4
    || 2 ** Math.round(Math.log2(maxTextureSize)) !== maxTextureSize) {
    throw new RangeError("Texture size must be a power-of-two safe integer at least 4");
  }
  const spaceSize = maxTextureSize > WORKSPACE_MAP_CANONICAL_SPACE_SIZE
    ? WORKSPACE_MAP_CANONICAL_SPACE_SIZE : maxTextureSize / 2;
  const scale = spaceSize / WORKSPACE_MAP_CANONICAL_SPACE_SIZE;
  return Object.freeze({ spaceSize, scale,
    toNativePositions: (positions: ArrayLike<number>) => convertPositions(positions, scale),
    fromNativePositions: (positions: ArrayLike<number>) => convertPositions(positions, 1 / scale),
    toNativePoint: (point: readonly [number, number]) => convertPoint(point, scale),
    fromNativePoint: (point: readonly [number, number]) => convertPoint(point, 1 / scale),
    toNativeZoom: (zoom: number) => positiveFloat32(positiveFloat32(zoom) / scale),
    fromNativeZoom: (zoom: number) => positiveFloat32(positiveFloat32(zoom) * scale),
    toNativeCollisionBasis: (basis: Readonly<WorkspaceRelationCollisionBasis>) => {
      const radius = positiveFloat32(positiveFloat32(basis.radius) * scale);
      finiteFloat32(basis.padding);
      if (basis.padding < 0) throw new RangeError("Collision padding must be nonnegative finite Float32");
      const padding = basis.padding === 0 ? 0 : positiveFloat32(basis.padding * scale);
      return { radius, padding };
    },
  });
}
