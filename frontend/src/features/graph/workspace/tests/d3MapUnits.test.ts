import { forceCollide, forceSimulation } from "d3-force";
import { describe, expect, it } from "vitest";

import { LOCAL_LAYOUT_PARAMS } from "../../shared/physics/layoutTypes";
import { buildWorkspaceD3UnitBasis, isReusableWorkspaceD3UnitBasis,
  measureWorkspaceD3BodyOverlap } from "../d3MapUnits";

// CSS-sized bodies must remain separated when one CSS pixel spans ten graph
// units. Feeding CSS radii directly to D3 leaves these same bodies overlapping.
describe("Map D3 scene-owned body units", () => {
  it("separates actual CSS body discs at a zoomed-out Fit and freezes the remount basis", () => {
    const viewport = { width: 1000, height: 500 };
    const basis = buildWorkspaceD3UnitBasis({ positions: Float32Array.from([-4200, 0, 4200, 0]),
      diameters: Float32Array.from([6, 6]), shapes: Float32Array.from([0, 0]), viewport });
    const nodes = [{ x: 0, y: 0 }, { x: 2, y: 0 }];
    expect(measureWorkspaceD3BodyOverlap(Float32Array.from([0, 0, 2, 0]), basis.nominalRadiusCss,
      basis.referenceZoom).overlappingNodes).toBe(2);
    const simulation = forceSimulation(nodes).stop().force("collide", forceCollide<typeof nodes[number]>()
      .radius((_node, index) => basis.radiusModel[index] + LOCAL_LAYOUT_PARAMS.collidePadding));
    simulation.tick(60); simulation.stop();
    const positions = Float32Array.from(nodes.flatMap(node => [node.x, node.y]));
    expect(Math.hypot(nodes[1].x - nodes[0].x, nodes[1].y - nodes[0].y) * basis.referenceZoom)
      .toBeGreaterThanOrEqual(6);
    expect(measureWorkspaceD3BodyOverlap(positions, basis.nominalRadiusCss, basis.referenceZoom)
      .overlappingNodes).toBe(0);
    viewport.width = 320;
    expect(basis.referenceViewport.width).toBe(1000);
    expect(Object.isFrozen(basis)).toBe(true);
    expect(Object.isFrozen(basis.radiusModel)).toBe(true);
    expect(isReusableWorkspaceD3UnitBasis(basis, 2)).toBe(true);
    expect(isReusableWorkspaceD3UnitBasis({ ...basis, radiusModel: [3, 3] }, 2)).toBe(false);
    expect(measureWorkspaceD3BodyOverlap(Float32Array.from([0, 0, 0, 0]), [3, 3], 1, .5,
      new Set([0, 1])).overlappingNodes).toBe(0);
    expect(measureWorkspaceD3BodyOverlap(Float32Array.from([0, 0, 0, 0, 1, 0]), [3, 3, 3], 1, .5,
      new Set([0, 1])).overlappingNodes).toBe(3);
  });

  it("includes the pinned shader zoom growth and hardware clamp without cluster-count emphasis", () => {
    const basis = buildWorkspaceD3UnitBasis({ positions: Float32Array.from([-1, 0, 1, 0]),
      diameters: Float32Array.from([6, 6]), shapes: Float32Array.from([0, 1]),
      viewport: { width: 500, height: 400 }, referenceZoom: 200, maxDiameterCss: 8 });
    // The stock fixed-size shader grows 6 CSS pixels to 12 at k=200, then
    // clamps to the hardware's 8 CSS pixels: circle radius4, square halfside3.2.
    expect(basis.nominalRadiusCss[0]).toBe(4);
    expect(basis.nominalRadiusCss[1]).toBeCloseTo(Math.hypot(3.2, 3.2), 8);
    expect(basis.radiusModel[0]).toBe(Math.fround(.02));
  });
});
