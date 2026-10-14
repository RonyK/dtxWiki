import { describe, expect, it } from "vitest";
import { createWorkspaceNativeCoordinateSpace, WORKSPACE_MAP_CANONICAL_SPACE_SIZE } from "../nativeCoordinateSpace";

const capabilities = [
  { limit: 4, space: 2, scale: 1 / 2048 },
  { limit: 2048, space: 1024, scale: .25 },
  { limit: 4096, space: 2048, scale: .5 },
  { limit: 8192, space: 4096, scale: 1 },
  { limit: 16384, space: 4096, scale: 1 },
];

describe("canonical4096 to supported native coordinate units", () => {
  it("keeps the published canonical space fixed", () => {
    expect(WORKSPACE_MAP_CANONICAL_SPACE_SIZE).toBe(4096);
  });

  it.each(capabilities)("selects supported space $space for texture limit $limit", ({ limit, space, scale }) => {
    const units = createWorkspaceNativeCoordinateSpace(limit);
    expect(units.spaceSize).toBe(space);
    expect(units.scale).toBe(scale);
    expect(units.spaceSize).toBeLessThan(limit);
    expect(Object.isFrozen(units)).toBe(true);
  });

  it.each([0, 1, 2, 3, -4, 2047, 4095, 4097, 16.5, NaN, Infinity, Number.MAX_SAFE_INTEGER])(
    "rejects invalid texture capability %s rather than guessing units", limit => {
      expect(() => createWorkspaceNativeCoordinateSpace(limit)).toThrow("power-of-two safe integer at least 4");
    },
  );

  it.each(capabilities)("copies and roundtrips positions without clamping at limit $limit", ({ limit, scale }) => {
    const units = createWorkspaceNativeCoordinateSpace(limit);
    const source = Float32Array.from([-128, 64.25, 0, 4096, 8192, -4096]);
    const original = source.slice();
    const native = units.toNativePositions(source);
    expect([...native]).toEqual([...original].map(value => value * scale));
    expect(native).not.toBe(source);
    expect(source).toEqual(original);
    const restored = units.fromNativePositions(native);
    expect(restored).toEqual(original);
    expect(restored).not.toBe(native);
    expect(native[0]).toBeLessThan(0);
    expect(native[4]).toBeGreaterThan(units.spaceSize);
  });

  it("accepts empty arrays and readonly ArrayLike inputs without mutation", () => {
    const units = createWorkspaceNativeCoordinateSpace(4096);
    const source = Object.freeze({ 0: -64, 1: 4096, length: 2 });
    expect([...units.toNativePositions(source)]).toEqual([-32, 2048]);
    expect(source).toEqual({ 0: -64, 1: 4096, length: 2 });
    expect(units.toNativePositions([])).toEqual(new Float32Array());
  });

  it.each([NaN, Infinity, -Infinity, 1e39, -1e39])("rejects nonfinite or overflowing coordinate %s", value => {
    const units = createWorkspaceNativeCoordinateSpace(4096);
    expect(() => units.toNativePositions([value, 0])).toThrow("finite Float32");
    expect(() => units.fromNativePositions([value, 0])).toThrow("finite Float32");
    expect(() => units.toNativePoint([value, 0])).toThrow("finite Float32");
    expect(() => units.fromNativePoint([0, value])).toThrow("finite Float32");
  });

  it("rejects overflow created by the native-to-canonical conversion", () => {
    const units = createWorkspaceNativeCoordinateSpace(2048);
    expect(() => units.fromNativePositions([1e38, 0])).toThrow("finite Float32");
    expect(() => units.fromNativePoint([0, -1e38])).toThrow("finite Float32");
  });

  it.each(capabilities)("roundtrips point and camera zoom units exactly at limit $limit", ({ limit, scale }) => {
    const units = createWorkspaceNativeCoordinateSpace(limit);
    const point = Object.freeze([-128.5, 3072.25] as const);
    expect(units.toNativePoint(point)).toEqual([-128.5 * scale, 3072.25 * scale]);
    expect(units.fromNativePoint(units.toNativePoint(point))).toEqual(point);
    expect(point).toEqual([-128.5, 3072.25]);
    for (const zoom of [.125, 1, 8]) {
      expect(units.toNativeZoom(zoom)).toBe(zoom / scale);
      expect(units.fromNativeZoom(zoom / scale)).toBe(zoom);
    }
  });

  it.each([0, -1, NaN, Infinity, Number.MIN_VALUE, 1e39])("rejects invalid zoom %s", zoom => {
    const units = createWorkspaceNativeCoordinateSpace(4096);
    expect(() => units.toNativeZoom(zoom)).toThrow("positive finite Float32");
    expect(() => units.fromNativeZoom(zoom)).toThrow("positive finite Float32");
  });

  it.each(capabilities)("copies collision lengths without changing the canonical basis at limit $limit", ({ limit, scale }) => {
    const units = createWorkspaceNativeCoordinateSpace(limit);
    const basis = Object.freeze({ radius: 12, padding: 3 });
    const native = units.toNativeCollisionBasis(basis);
    expect(native).toEqual({ radius: 12 * scale, padding: 3 * scale });
    expect(native).not.toBe(basis);
    expect(basis).toEqual({ radius: 12, padding: 3 });
    expect({ radius: native.radius / scale, padding: native.padding / scale }).toEqual(basis);
    expect(units.toNativeCollisionBasis({ radius: 4, padding: 0 })).toEqual({ radius: 4 * scale, padding: 0 });
  });

  it.each([
    { radius: 0, padding: 1 }, { radius: -1, padding: 1 }, { radius: NaN, padding: 1 },
    { radius: Infinity, padding: 1 }, { radius: 1e39, padding: 1 },
    { radius: 4, padding: -1 }, { radius: 4, padding: NaN }, { radius: 4, padding: Infinity },
  ])("rejects invalid collision lengths $radius/$padding", basis => {
    expect(() => createWorkspaceNativeCoordinateSpace(4096).toNativeCollisionBasis(basis)).toThrow();
  });

  it("rejects positive lengths lost at the Float32 native boundary", () => {
    const units = createWorkspaceNativeCoordinateSpace(2048);
    const smallest = Math.fround(1e-45);
    expect(() => units.toNativeCollisionBasis({ radius: smallest, padding: 0 })).toThrow("positive finite Float32");
    expect(() => units.toNativeCollisionBasis({ radius: 4, padding: smallest })).toThrow("positive finite Float32");
  });
});
