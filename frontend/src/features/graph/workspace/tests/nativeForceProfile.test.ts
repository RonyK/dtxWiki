import { describe, expect, it } from "vitest";
import { buildWorkspaceNativeCanonicalForceConfig } from "../nativeForceProfile";

describe("canonical PC native force units", () => {
  it("retains the pinned native profile without camera/glyph/body-derived scaling", () => {
    expect(buildWorkspaceNativeCanonicalForceConfig(1)).toEqual({
      simulationGravity: .25, simulationRepulsion: 1, simulationLinkSpring: 1,
      simulationLinkDistance: 10, simulationLinkDistRandomVariationRange: [1, 1],
    });
  });

  it.each([.5, .25, 1 / 2048])("keeps canonical charge and length invariant through native scale %s", scale => {
    const profile = buildWorkspaceNativeCanonicalForceConfig(scale);
    expect(profile.simulationRepulsion / scale ** 2).toBe(1);
    expect(profile.simulationLinkDistance / scale).toBe(10);
    expect(profile.simulationGravity).toBe(.25);
    expect(profile.simulationLinkSpring).toBe(1);
  });

  it.each([0, -1, NaN, Infinity, 2, 1e-40])("rejects unsupported or lost force units %s", scale => {
    expect(() => buildWorkspaceNativeCanonicalForceConfig(scale)).toThrow(RangeError);
  });

  it("returns independent input objects so one renderer cannot alter another profile", () => {
    const profile = buildWorkspaceNativeCanonicalForceConfig(1);
    profile.simulationLinkDistRandomVariationRange[0] = 9;
    expect(buildWorkspaceNativeCanonicalForceConfig(1).simulationLinkDistRandomVariationRange).toEqual([1, 1]);
  });
});
