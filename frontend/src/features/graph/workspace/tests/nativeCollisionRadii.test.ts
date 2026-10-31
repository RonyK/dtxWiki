import { describe, expect, it } from "vitest";
import { buildWorkspaceNativeCollisionRadii } from "../nativeCollisionRadii";
import { buildWorkspaceRelationCollisionBasis, buildWorkspaceRelationForceConfig } from "../relationPhysics";

describe("native per-point glyph collision units", () => {
  it("retains seed world units and force coefficients while removing largest-glyph area inflation", () => {
    const sizes = Float32Array.from({ length: 10_000 }, (_, index) => index === 0 ? 6.8 : 2.5);
    const basis = buildWorkspaceRelationCollisionBasis(Float32Array.from({ length: 20_000 }, (_, index) => index % 4 < 2 ? 0 : 4096), sizes, 734, 608)!;
    const forces = buildWorkspaceRelationForceConfig(basis);
    const radii = buildWorkspaceNativeCollisionRadii(sizes, new Float32Array(10_000), basis);
    expect(radii[0]).toBeCloseTo(basis.radius, 5);
    expect(radii[1] / radii[0]).toBeCloseTo(2.5 / 6.8, 6);
    const area = radii.reduce((sum, radius) => sum + Math.PI * (radius + basis.padding) ** 2, 0);
    const uniformArea = sizes.length * Math.PI * (basis.radius + basis.padding) ** 2;
    expect(area).toBeLessThan(uniformArea / 3);
    expect(uniformArea).toBeGreaterThan(4096 ** 2);
    expect(area).toBeLessThan(4096 ** 2);
    expect(buildWorkspaceRelationForceConfig(basis)).toEqual(forces);
  });

  it("uses the actual square's circumscribed body and retains caller-owned columns", () => {
    const sizes = Float32Array.from([4, 4]), shapes = Float32Array.from([0, 1]);
    const basis = { radius: 8, padding: 1 };
    const radii = buildWorkspaceNativeCollisionRadii(sizes, shapes, basis);
    expect([...radii]).toEqual([8, Math.fround(4 * .4 * Math.SQRT2 * 4)]);
    expect([...sizes]).toEqual([4, 4]);
    expect([...shapes]).toEqual([0, 1]);
  });

  it("matches the reference Fit's native zoom growth and hardware point-size cap", () => {
    const basis = { radius: .002, padding: .001 };
    expect(buildWorkspaceNativeCollisionRadii(Float32Array.from([4]), Float32Array.from([0]), basis)[0])
      .toBeCloseTo(.01, 6);
    expect(buildWorkspaceNativeCollisionRadii(Float32Array.from([4]), Float32Array.from([0]), basis, 8)[0])
      .toBeCloseTo(.004, 6);
  });
});
