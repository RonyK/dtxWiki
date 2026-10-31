import { describe, expect, it } from "vitest";

import {
  projectWorkspaceForceAtlas2Collision,
  snapshotWorkspaceForceAtlas2CollisionInput,
} from "../forceAtlas2Collision";

function overlaps(rows: Float32Array, radius: number): number {
  let count = 0;
  for (let first = 0; first < rows.length / 2; first++) {
    for (let second = first + 1; second < rows.length / 2; second++) {
      if (Math.hypot(rows[first * 2] - rows[second * 2], rows[first * 2 + 1] - rows[second * 2 + 1]) < radius * 2 - 1e-5) count++;
    }
  }
  return count;
}

describe("bounded upstream d3 collision projection", () => {
  it("applies minimal simple-pair clearance without carrying four inertial impulses", () => {
    const rows = Float32Array.from([0, 0, 1, 0]), original = rows.slice();
    const radii = Float32Array.from([3, 3]);
    const result = projectWorkspaceForceAtlas2Collision(rows, { radii });
    expect(rows).toEqual(original);
    expect(radii).toEqual(Float32Array.from([3, 3]));
    expect(Math.hypot(result[2] - result[0], result[3] - result[1])).toBeCloseTo(6, 5);
    expect(result[0]).toBeCloseTo(-2.5, 5);
    expect(result[2]).toBeCloseTo(3.5, 5);
  });

  it("leaves already separated rows exactly unchanged", () => {
    const rows = Float32Array.from([-50, 13, 20, -4, 500, 700]);
    expect(projectWorkspaceForceAtlas2Collision(rows, { radii: Float32Array.from([2, 3, 4]) })).toEqual(rows);
  });

  it("resolves a synthetic 188-pair pattern with a nominal glyph clearance margin", () => {
    // Not the private API corpus: 188 isolated overlapping pairs exercise the
    // same pair count without copying user data or claiming real acceptance.
    const rows = Float32Array.from(Array.from({ length: 188 }, (_, index) => [index * 30, 0, index * 30 + 1, 0]).flat());
    expect(overlaps(rows, 2)).toBe(188);
    const result = projectWorkspaceForceAtlas2Collision(rows, { radii: new Float32Array(376).fill(3) });
    expect(overlaps(result, 2)).toBe(0);
    expect(result).toHaveLength(rows.length);
    expect(result.every(Number.isFinite)).toBe(true);
  });

  it("reduces a dense scene using bounded upstream quadtree passes", () => {
    const rows = Float32Array.from(Array.from({ length: 100 }, (_, index) => [(index % 10) * .4, Math.floor(index / 10) * .4]).flat());
    const result = projectWorkspaceForceAtlas2Collision(rows, { radii: new Float32Array(100).fill(2) });
    expect(overlaps(result, 1)).toBeLessThan(overlaps(rows, 1));
    expect(result.every(Number.isFinite)).toBe(true);
  });

  it("is deterministic for coincident positions", () => {
    const rows = new Float32Array(12), input = { radii: new Float32Array(6).fill(2) };
    expect(projectWorkspaceForceAtlas2Collision(rows, input)).toEqual(projectWorkspaceForceAtlas2Collision(rows, input));
  });

  it("preserves exact fixed/free pin rows including signed zero", () => {
    const rows = Float32Array.from([-0, 0, 1, 0]);
    const result = projectWorkspaceForceAtlas2Collision(rows, { radii: Float32Array.from([3, 3]), pins: [0] });
    expect(Object.is(result[0], rows[0])).toBe(true);
    expect(Object.is(result[1], rows[1])).toBe(true);
    expect(result[2]).toBeGreaterThan(rows[2]);
    expect(overlaps(result, 2)).toBe(0);
  });

  it("does not move conflicting fixed/fixed pins or falsely promise separation", () => {
    const rows = Float32Array.from([0, 0, 1, 0]);
    const result = projectWorkspaceForceAtlas2Collision(rows, { radii: Float32Array.from([3, 3]), pins: [0, 1] });
    expect(result).toEqual(rows);
    expect(overlaps(result, 2)).toBe(1);
  });

  it("accepts exactly 2000 separated original rows without adding topology", () => {
    const rows = Float32Array.from(Array.from({ length: 2000 }, (_, index) => [index * 10, 0]).flat());
    expect(projectWorkspaceForceAtlas2Collision(rows, { radii: new Float32Array(2000).fill(1) })).toEqual(rows);
  });

  it.each([
    new Float32Array(), new Float32Array(2001).fill(1), Float32Array.from([0]),
    Float32Array.from([-1]), Float32Array.from([NaN]), Float32Array.from([Infinity]), Float32Array.from([3e38]),
  ])("rejects invalid radius columns: %s", radii => {
    expect(() => snapshotWorkspaceForceAtlas2CollisionInput({ radii })).toThrow();
  });

  it.each([{ pins: [-1] }, { pins: [1] }, { pins: [0.5] }, { pins: [NaN] }])("rejects invalid pin indices: $pins", ({ pins }) => {
    expect(() => projectWorkspaceForceAtlas2Collision(new Float32Array(2), { radii: Float32Array.from([1]), pins })).toThrow();
  });

  it.each([new Float32Array(1), Float32Array.from([NaN, 0]), Float32Array.from([Infinity, 0])])(
    "rejects invalid position rows: %s", rows => {
      expect(() => projectWorkspaceForceAtlas2Collision(rows, { radii: Float32Array.from([1]) })).toThrow();
    },
  );
});
