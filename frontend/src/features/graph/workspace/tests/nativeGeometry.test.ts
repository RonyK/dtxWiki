import { expect, it } from "vitest";
import { measureNativeGeometry } from "./nativeGeometry";

it("counts each body-overlap pair once and distinguishes intentional boundary pins", () => {
  const result = measureNativeGeometry(Float32Array.from([0, 1, 1, 1, 3, 1, 10, 9]),
    Float32Array.from([1, 1, 1, 1]), 10, [0]);
  expect(result).toMatchObject({ nodes: 4, boundaryNodes: 2, unpinnedBoundaryNodes: 1,
    overlapPairs: 1, nodesWithOverlappingBodyDisks: 2 });
});

it("observes unequal radii across adjacent diagnostic cells without changing inputs", () => {
  const positions = Float32Array.from([7, 1, 8.5, 1, 14, 1]);
  const radii = Float32Array.from([1, 1, 3]);
  expect(measureNativeGeometry(positions, radii, 100, []).overlapPairs).toBe(1);
  expect([...positions]).toEqual([7, 1, 8.5, 1, 14, 1]);
});

it.each([NaN, Infinity, -Infinity])("rejects nonfinite position %s instead of reporting zero boundaries", coordinate => {
  expect(() => measureNativeGeometry([coordinate, 1], Float32Array.from([1]), 100, [])).toThrow();
});

it.each([0, -1, NaN, Infinity])("rejects invalid native space %s", spaceSize => {
  expect(() => measureNativeGeometry([1, 1], Float32Array.from([1]), spaceSize, [])).toThrow();
});
