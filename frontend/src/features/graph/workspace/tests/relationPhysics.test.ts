import { describe, expect, it } from "vitest";
import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import { buildWorkspaceRelationCollisionBasis, buildWorkspaceRelationForceConfig, buildWorkspaceRelationLinkStrength, WORKSPACE_RELATION_LINK_WEIGHT_CAP, type WorkspaceRelationCollisionBasis } from "../relationPhysics";

function input(): WorkspaceMapColumns {
  return { identity: { actorId: "a", workspaceId: "w", surface: "workspace", selectionKey: "combined", visibleFingerprint: "f" },
    nodeIds: ["a", "b", "c", "d"], nodeKeys: ["a", "b", "c", "d"], nodeLabels: ["a", "b", "c", "d"],
    nodeKinds: new Uint8Array(4), nodeSizes: new Float32Array(4).fill(3), nodeFolderKeys: ["one", "one", "two", "two"],
    edgeIds: ["ab", "cd", "ac", "bd"], edgeSources: Uint32Array.from([0, 2, 0, 1]), edgeTargets: Uint32Array.from([1, 3, 2, 3]),
    edgeKinds: new Uint8Array(4), edgeCounts: new Uint32Array(4).fill(1), edgeWeights: Float64Array.from([1, 1, 64, 64]),
    edgeCrossFolder: Uint8Array.from([0, 0, 1, 1]) };
}
describe("native authored relation spring strengths", () => {
  it("preserves SDK degree normalization and makes strong cross-directory relations stronger", () => {
    const columns = input();
    expect([...buildWorkspaceRelationLinkStrength(columns)]).toEqual([.5, .5, 3.5, 3.5]);
    columns.nodeFolderKeys = ["two", "one", "two", "one"];
    expect([...buildWorkspaceRelationLinkStrength(columns)]).toEqual([.5, .5, 3.5, 3.5]);
    columns.edgeWeights.fill(1);
    expect([...buildWorkspaceRelationLinkStrength(columns)]).toEqual([.5, .5, .5, .5]);
  });
  it("bounds the entire safe integer weight range and rejects invalid inputs", () => {
    const columns = input();
    columns.edgeWeights[2] = Number.MAX_SAFE_INTEGER;
    expect(buildWorkspaceRelationLinkStrength(columns)[2]).toBe(WORKSPACE_RELATION_LINK_WEIGHT_CAP / 2);
    for (const weight of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
      columns.edgeWeights[2] = weight;
      expect(() => buildWorkspaceRelationLinkStrength(columns)).toThrow("occurrence weight");
    }
    columns.edgeWeights = new Float64Array(1);
    expect(() => buildWorkspaceRelationLinkStrength(columns)).toThrow("columns");
  });
  it("does not let a late community partition attenuate authored relations", () => {
    const columns = input();
    const communities = Uint32Array.from([0, 0, 1, 1]);
    const strengths = buildWorkspaceRelationLinkStrength(columns);
    expect([...strengths]).toEqual([.5, .5, 3.5, 3.5]);
    // A runtime caller with an obsolete extra argument must not turn metadata
    // into physics. The public TypeScript signature accepts only the columns.
    const obsoleteCall = Reflect.apply(buildWorkspaceRelationLinkStrength, undefined, [columns, communities]);
    expect(obsoleteCall).toEqual(strengths);
    expect(strengths[2]).toBeGreaterThan(strengths[0]);
    columns.nodeFolderKeys.fill("same-folder");
    columns.edgeCrossFolder.fill(0);
    expect(buildWorkspaceRelationLinkStrength(columns)).toEqual(strengths);
  });
  it("rejects malformed relation endpoints and columns before GPU upload", () => {
    const columns = input();
    columns.edgeTargets[0] = columns.nodeIds.length;
    expect(() => buildWorkspaceRelationLinkStrength(columns)).toThrow("endpoint");
    columns.edgeTargets = new Uint32Array();
    expect(() => buildWorkspaceRelationLinkStrength(columns)).toThrow("columns");
  });
});

const FORCE_BASIS: WorkspaceRelationCollisionBasis = { radius: 4, padding: 1 };

describe("dimensional conversion of the pinned SDK force profile", () => {
  it.each([
    { radius: 2, padding: .5, distance: 10, repulsion: 1 },
    { radius: 4, padding: 1, distance: 20, repulsion: 4 },
    { radius: 12, padding: 3, distance: 60, repulsion: 36 },
    { radius: 4, padding: 0, distance: 16, repulsion: 2.56 },
  ])("converts the SDK world units for radius $radius and padding $padding", ({ radius, padding, distance, repulsion }) => {
    const config = buildWorkspaceRelationForceConfig({ radius, padding });
    expect(config.simulationGravity).toBe(.25);
    expect(config.simulationLinkSpring).toBe(1);
    expect(config.simulationLinkDistance).toBe(distance);
    expect(config.simulationRepulsion).toBeCloseTo(repulsion, 12);
    expect(config.simulationLinkDistRandomVariationRange).toEqual([1, 1]);
    for (const value of [config.simulationGravity, config.simulationLinkSpring,
      config.simulationLinkDistance, config.simulationRepulsion]) {
      expect(Number.isFinite(value)).toBe(true);
      expect(Number.isFinite(Math.fround(value))).toBe(true);
      expect(Math.fround(value)).toBeGreaterThan(0);
    }
  });

  it.each([.25, 2, 3, 100])("scales distance by %s and inverse-distance charge by its square", (scale) => {
    const baseline = buildWorkspaceRelationForceConfig(FORCE_BASIS);
    const scaled = buildWorkspaceRelationForceConfig({ radius: FORCE_BASIS.radius * scale, padding: FORCE_BASIS.padding * scale });
    expect(scaled.simulationLinkDistance).toBe(baseline.simulationLinkDistance * scale);
    expect(scaled.simulationRepulsion).toBeCloseTo(baseline.simulationRepulsion * scale ** 2, 12);
    expect(scaled.simulationGravity).toBe(baseline.simulationGravity);
    expect(scaled.simulationLinkSpring).toBe(baseline.simulationLinkSpring);
    expect(scaled.simulationLinkDistRandomVariationRange).toEqual(baseline.simulationLinkDistRandomVariationRange);
  });

  it("does not mutate the basis or share mutable returned configuration", () => {
    const basis = Object.freeze({ ...FORCE_BASIS });
    const first = buildWorkspaceRelationForceConfig(basis);
    first.simulationLinkDistRandomVariationRange[0] = 10;
    const second = buildWorkspaceRelationForceConfig(basis);
    expect(basis).toEqual(FORCE_BASIS);
    expect(second.simulationLinkDistRandomVariationRange).toEqual([1, 1]);
    expect(second.simulationLinkDistRandomVariationRange).not.toBe(first.simulationLinkDistRandomVariationRange);
    expect(second.simulationRepulsion).toBe(4);
  });

  it("takes only the collision unit rather than arbitrary positions or graph metadata", () => {
    expect(buildWorkspaceRelationForceConfig.length).toBe(1);
    const config = buildWorkspaceRelationForceConfig(FORCE_BASIS);
    const unexpectedInput = new Proxy({}, { get() { throw new Error("Graph data must not participate in the profile"); } });
    expect(Reflect.apply(buildWorkspaceRelationForceConfig, undefined,
      [FORCE_BASIS, unexpectedInput, unexpectedInput])).toEqual(config);
  });

  it.each([
    { radius: 0, padding: 1 }, { radius: -1, padding: 1 },
    { radius: NaN, padding: 1 }, { radius: Infinity, padding: 1 },
    { radius: 4, padding: -1 }, { radius: 4, padding: NaN },
    { radius: 4, padding: Infinity },
  ])("rejects invalid collision calibration $radius/$padding", (basis) => {
    expect(() => buildWorkspaceRelationForceConfig(basis)).toThrow("Invalid relation force collision basis");
  });

  it.each([
    { radius: Number.MAX_VALUE, padding: 0 },
    { radius: Number.MAX_VALUE / 2, padding: Number.MAX_VALUE / 2 },
    { radius: 1e30, padding: 0 },
    { radius: 1e20, padding: 0 },
    { radius: 1e-30, padding: 0 },
    { radius: Number.MIN_VALUE, padding: 0 },
  ])("rejects overflowing or underflowing GPU units $radius/$padding", (basis) => {
    expect(() => buildWorkspaceRelationForceConfig(basis)).toThrow("not representable in Float32");
  });

  it.each([
    { radius: 1e-20, padding: 0 },
    { radius: 1e18, padding: 0 },
  ])("keeps representable small and large GPU units finite for radius $radius", (basis) => {
    const config = buildWorkspaceRelationForceConfig(basis);
    for (const value of [config.simulationLinkDistance, config.simulationRepulsion]) {
      expect(Number.isFinite(value)).toBe(true);
      expect(Number.isFinite(Math.fround(value))).toBe(true);
      expect(Math.fround(value)).toBeGreaterThan(0);
    }
  });
});

describe("scene-owned native collision calibration", () => {
  it.each([
    { positions: [0, 0, 100, 100], zoom: 608 * .84 / 100 },
    { positions: [10, 10], zoom: 608 * .84 },
    { positions: [0, 0, .5, .25], zoom: 734 * .84 / .5 },
    { positions: [0, 0, 1_000_000, 1_000_000], zoom: .001 },
  ])("matches the pinned SDK Fit zoom without using camera state for $positions", ({ positions, zoom }) => {
    const basis = buildWorkspaceRelationCollisionBasis(Float32Array.from(positions), new Float32Array(positions.length / 2).fill(12), 734, 608);
    expect(basis).toEqual({ radius: 12 / (2 * zoom), padding: 1 / zoom });
  });

  it("ignores whole nonfinite pairs without laundering one coordinate into the fit extent", () => {
    const positions = Float32Array.from([0, 0, 100, 100]);
    expect(buildWorkspaceRelationCollisionBasis(Float32Array.from([NaN, 10000, ...positions]), Float32Array.from([12, 12, 12]), 734, 608))
      .toEqual(buildWorkspaceRelationCollisionBasis(positions, Float32Array.from([12, 12]), 734, 608));
  });

  it.each([[0, 608], [734, 0], [-1, 608], [734.5, 608], [Infinity, 608]])(
    "defers nonpositive/noninteger native viewport %s/%s", (width, height) => {
      expect(buildWorkspaceRelationCollisionBasis(Float32Array.from([0, 0, 100, 100]), Float32Array.from([12, 12]), width, height)).toBeNull();
    },
  );

  it("defers invalid seed/size columns instead of installing a giant clamped empty-view radius", () => {
    for (const [positions, sizes] of [
      [[], []], [[0], [12]], [[NaN, NaN], [12]], [[0, 0], [NaN]], [[0, 0], [0]], [[0, 0], []],
    ]) expect(buildWorkspaceRelationCollisionBasis(Float32Array.from(positions), Float32Array.from(sizes), 734, 608)).toBeNull();
  });
});
