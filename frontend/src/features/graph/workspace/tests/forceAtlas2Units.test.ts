import { describe, expect, it } from "vitest";
import {
  buildWorkspaceForceAtlas2Units,
  buildWorkspaceForceAtlas2ContactUnits,
  isReusableWorkspaceForceAtlas2ContactUnits,
  isReusableWorkspaceForceAtlas2Units,
  workspaceModelToRendererPositions,
  workspaceRendererToModelPositions,
} from "../forceAtlas2Units";

type UnitsInput = Parameters<typeof buildWorkspaceForceAtlas2Units>[0];

function scene(overrides: Partial<UnitsInput> = {}): UnitsInput {
  return {
    positions: Float32Array.from([0, 0, 100, 50]),
    diametersCss: Float32Array.from([10, 10]),
    pointShapes: Uint8Array.from([0, 1]),
    origin: [0, 0],
    width: 200,
    height: 100,
    ...overrides,
  };
}

describe("scene-owned ForceAtlas2 physical units", () => {
  it("admits immutable internally consistent units only for their original row count", () => {
    const units = buildWorkspaceForceAtlas2Units(scene());
    expect(isReusableWorkspaceForceAtlas2Units(units, 2)).toBe(true);
    expect(isReusableWorkspaceForceAtlas2Units(units, 3)).toBe(false);
    expect(isReusableWorkspaceForceAtlas2Units({ ...units }, 2)).toBe(false);
  });
  it.each([
    { origin: Object.freeze([Infinity, 0]) },
    { origin: Object.freeze([1e100, 0]) },
    { referenceViewport: Object.freeze({ width: 0, height: 100 }) },
    { referenceZoom: NaN },
    { collisionRadiusModel: Object.freeze([1e100, 1]) },
    { collisionRadiusModel: Object.freeze([1e-100, 1]) },
    { collisionRadiusModel: Object.freeze([2e38, 2e38]) },
    { nominalRadiusCss: Object.freeze([1, 1]) },
    { pairGapCss: -1 },
  ])("rejects frozen malformed remount units %s", overrides => {
    const units = buildWorkspaceForceAtlas2Units(scene());
    expect(isReusableWorkspaceForceAtlas2Units(Object.freeze({ ...units, ...overrides }), 2)).toBe(false);
  });
  it("uses actual native circle and square envelopes rather than the whole square sprite", () => {
    const units = buildWorkspaceForceAtlas2Units(scene());
    expect(units.nominalRadiusCss[0]).toBe(5);
    expect(units.nominalRadiusCss[1]).toBeCloseTo(4 * Math.SQRT2, 14);
    // Cosmos SDF half-side is .4d, not .5d; the protected circle encloses its corners.
    expect(units.nominalRadiusCss[1]).toBeGreaterThan(5);
    expect(units.nominalRadiusCss[1]).toBeLessThan(5 * Math.SQRT2);
    expect(units.referenceZoom).toBe(1.68);
    expect(units.rendererUnitsPerModelUnit).toBe(1);
  });

  it.each([0, 1, 4])("splits the %s CSS-pixel pair gap equally between both protected radii", (pairGapCss) => {
    const units = buildWorkspaceForceAtlas2Units(scene({ pairGapCss }));
    for (let node = 0; node < units.nodeCount; node++) {
      const expected = Math.fround((units.nominalRadiusCss[node] + pairGapCss / 2) / units.referenceZoom);
      expect(units.collisionRadiusModel[node]).toBe(expected);
    }
    const pairClearanceCss = (units.collisionRadiusModel[0] + units.collisionRadiusModel[1])
      * units.rendererUnitsPerModelUnit * units.referenceZoom;
    expect(pairClearanceCss).toBeCloseTo(5 + 4 * Math.SQRT2 + pairGapCss, 6);
  });

  it("preserves the legacy one-CSS-pixel per-node clearance with the default two-pixel pair gap", () => {
    const units = buildWorkspaceForceAtlas2Units(scene());
    expect(units.pairGapCss).toBe(2);
    for (let node = 0; node < units.nodeCount; node++) {
      expect(units.collisionRadiusModel[node]).toBe(Math.fround((units.nominalRadiusCss[node] + 1) / units.referenceZoom));
      expect(units.collisionRadiusModel[node] * units.referenceZoom - units.nominalRadiusCss[node])
        .toBeCloseTo(1, 6);
    }
  });

  it("preserves the reference model scale while mapping each physical glyph radius", () => {
    const units = buildWorkspaceForceAtlas2Units(scene({ diametersCss: [4, 12] }));
    expect(units.rendererUnitsPerModelUnit).toBe(1);
    expect(units.collisionRadiusModel).toEqual([
      Math.fround(3 / 1.68), Math.fround((4.8 * Math.SQRT2 + 1) / 1.68),
    ]);
    expect(Math.max(...units.collisionRadiusModel)).toBeGreaterThan(.5);
    const widerGlyphs = buildWorkspaceForceAtlas2Units(scene({ diametersCss: [8, 24] }));
    expect(widerGlyphs.rendererUnitsPerModelUnit).toBe(1);
    expect(workspaceRendererToModelPositions(widerGlyphs, [0, 0, 100, 50]))
      .toEqual(workspaceRendererToModelPositions(units, [0, 0, 100, 50]));
  });

  it.each([
    { positions: [10, 20], width: 734, height: 608, zoom: 608 * .84 },
    { positions: [0, 0, .5, .25], width: 734, height: 608, zoom: 734 * .84 / .5 },
    { positions: [-5, -3, 95, 97], width: 734, height: 608, zoom: 608 * .84 / 100 },
    { positions: [0, 0, 1_000_000, 1_000_000], width: 734, height: 608, zoom: .001 },
    { positions: [4, -3, 4, 7], width: 734, height: 608, zoom: 608 * .84 / 10 },
  ])("matches pinned canonical Fit, including zero/subunit spans: $positions", ({ positions, width, height, zoom }) => {
    const count = positions.length / 2;
    const units = buildWorkspaceForceAtlas2Units(scene({ positions,
      diametersCss: Array(count).fill(12), pointShapes: Array(count).fill(0), width, height }));
    expect(units.referenceZoom).toBe(zoom);
    expect(units.rendererUnitsPerModelUnit).toBe(1);
    expect(units.collisionRadiusModel).toEqual(Array(count).fill(Math.fround(7 / zoom)));
    expect(units.referenceViewport).toEqual({ width, height });
  });

  it("is translation-invariant in calibration and uses origin only in the affine mapping", () => {
    const base = scene();
    const translated = scene({ positions: [500, -300, 600, -250], origin: [500, -300] });
    const first = buildWorkspaceForceAtlas2Units(base);
    const second = buildWorkspaceForceAtlas2Units(translated);
    expect(second.referenceZoom).toBe(first.referenceZoom);
    expect(second.rendererUnitsPerModelUnit).toBe(first.rendererUnitsPerModelUnit);
    expect(second.collisionRadiusModel).toEqual(first.collisionRadiusModel);
    expect(workspaceRendererToModelPositions(second, translated.positions))
      .toEqual(workspaceRendererToModelPositions(first, base.positions));
  });

  it("does not normalize away a real change in reference coordinates", () => {
    const first = buildWorkspaceForceAtlas2Units(scene());
    const doubled = buildWorkspaceForceAtlas2Units(scene({ positions: [0, 0, 200, 100] }));
    expect(doubled.referenceZoom).toBe(first.referenceZoom / 2);
    expect(doubled.rendererUnitsPerModelUnit).toBe(1);
    expect(doubled.collisionRadiusModel).toEqual(first.collisionRadiusModel.map(radius => radius * 2));
    expect([...workspaceRendererToModelPositions(doubled, [0, 0, 200, 100])]).toEqual([0, 0, 200, 100]);
  });

  it("does not read camera, theme, folders or DPR or apply the native size rule a second time", () => {
    const input = scene({ diametersCss: [20, 20] });
    for (const key of ["camera", "theme", "folders", "dpr"]) {
      Object.defineProperty(input, key, { get() { throw new Error("Presentation must not recalibrate physics"); } });
    }
    const units = buildWorkspaceForceAtlas2Units(input);
    expect(units.nominalRadiusCss[0]).toBe(10);
    expect(units.nominalRadiusCss[1]).toBeCloseTo(8 * Math.SQRT2, 14);
    expect(buildWorkspaceForceAtlas2Units.length).toBe(1);
  });

  it("copies and freezes all owned units without mutating the scene inputs", () => {
    const input = scene();
    const beforePositions = Float32Array.from(input.positions);
    const beforeDiameters = Float32Array.from(input.diametersCss);
    const beforeShapes = Uint8Array.from(input.pointShapes);
    const beforeOrigin = [...input.origin];
    const first = buildWorkspaceForceAtlas2Units(input);
    const second = buildWorkspaceForceAtlas2Units(input);
    expect(first).toEqual(second);
    expect(first).not.toBe(second);
    expect(first.origin).not.toBe(input.origin);
    expect(first.nominalRadiusCss).not.toBe(second.nominalRadiusCss);
    expect(first.collisionRadiusModel).not.toBe(second.collisionRadiusModel);
    for (const owned of [first, first.origin, first.referenceViewport, first.nominalRadiusCss, first.collisionRadiusModel]) {
      expect(Object.isFrozen(owned)).toBe(true);
    }
    expect(input.positions).toEqual(beforePositions);
    expect(input.diametersCss).toEqual(beforeDiameters);
    expect(input.pointShapes).toEqual(beforeShapes);
    expect(input.origin).toEqual(beforeOrigin);
    (input.origin as [number, number])[0] = 999;
    expect(first.origin).toEqual(beforeOrigin);
  });
});

describe("one-time structural handoff contact units", () => {
  it("uses structural Fit at the frozen seed viewport without modifying seed units", () => {
    const seed = buildWorkspaceForceAtlas2Units(scene());
    const original = seed.collisionRadiusModel.slice();
    const boundary = Float32Array.from([0, 0, 1000, 500]);
    const contact = buildWorkspaceForceAtlas2ContactUnits(seed, boundary);
    expect(contact).not.toBe(seed);
    expect(contact.referenceZoom).toBe(.168);
    expect(seed.referenceZoom).toBe(1.68);
    expect(seed.collisionRadiusModel).toEqual(original);
    expect(contact.origin).toEqual(seed.origin);
    expect(contact.referenceViewport).toEqual(seed.referenceViewport);
    expect(contact.nominalRadiusCss).toEqual(seed.nominalRadiusCss);
    expect(contact.rendererUnitsPerModelUnit).toBe(1);
    expect(contact.pairGapCss).toBe(2);
    expect(contact.collisionRadiusModel).toEqual(seed.nominalRadiusCss.map(radius => Math.fround((radius + 1) / .168)));
    expect(isReusableWorkspaceForceAtlas2ContactUnits(contact, seed)).toBe(true);
    for (const owned of [contact, contact.origin, contact.referenceViewport, contact.nominalRadiusCss, contact.collisionRadiusModel]) {
      expect(Object.isFrozen(owned)).toBe(true);
    }
  });

  it("does not shrink contact radii when structure contracts into a smaller extent", () => {
    const seed = buildWorkspaceForceAtlas2Units(scene());
    const contact = buildWorkspaceForceAtlas2ContactUnits(seed, Float32Array.from([0, 0, 10, 5]));
    expect(contact.referenceZoom).toBe(seed.referenceZoom);
    expect(contact.collisionRadiusModel).toEqual(seed.collisionRadiusModel);
    expect(contact.collisionRadiusModel).not.toBe(seed.collisionRadiusModel);
  });

  it("keeps signed origin and fixed model scale unchanged on translation and large expansion", () => {
    const seed = buildWorkspaceForceAtlas2Units(scene({ origin: [2048, -2048] }));
    const contact = buildWorkspaceForceAtlas2ContactUnits(seed, [-1000, -500, 1000, 500]);
    const translated = buildWorkspaceForceAtlas2ContactUnits(seed, [4000, 6500, 6000, 7500]);
    expect(contact.referenceZoom).toBe(.084);
    expect(translated.referenceZoom).toBe(contact.referenceZoom);
    expect(translated.collisionRadiusModel).toEqual(contact.collisionRadiusModel);
    expect(contact.origin).toEqual([2048, -2048]);
    expect(contact.rendererUnitsPerModelUnit).toBe(1);
  });

  it("retains canonical .001 minimum Fit zoom at the structural boundary", () => {
    const seed = buildWorkspaceForceAtlas2Units(scene());
    const contact = buildWorkspaceForceAtlas2ContactUnits(seed, [0, 0, 1e8, 1e8]);
    expect(contact.referenceZoom).toBe(.001);
    expect(contact.collisionRadiusModel[0]).toBe(6000);
  });

  it.each([
    { origin: [1, 0] as const }, { width: 300 }, { height: 150 },
    { diametersCss: [20, 20] }, { pairGapCss: 3 },
  ])("rejects another immutable scene's contact metadata %s", overrides => {
    const seed = buildWorkspaceForceAtlas2Units(scene());
    const other = buildWorkspaceForceAtlas2Units(scene(overrides));
    const contact = buildWorkspaceForceAtlas2ContactUnits(other, [0, 0, 1000, 500]);
    expect(isReusableWorkspaceForceAtlas2ContactUnits(contact, seed)).toBe(false);
  });

  it("rejects a smaller physical radius reference, mutable metadata, and wrong row counts", () => {
    const seed = buildWorkspaceForceAtlas2Units(scene());
    const contracted = buildWorkspaceForceAtlas2Units(scene({ positions: [0, 0, 10, 5] }));
    expect(isReusableWorkspaceForceAtlas2ContactUnits(contracted, seed)).toBe(false);
    expect(isReusableWorkspaceForceAtlas2ContactUnits({ ...seed }, seed)).toBe(false);
    const one = buildWorkspaceForceAtlas2Units(scene({ positions: [0, 0], diametersCss: [10], pointShapes: [0] }));
    expect(isReusableWorkspaceForceAtlas2ContactUnits(one, seed)).toBe(false);
  });

  it.each([[], [0, 0], [0, 0, NaN, 1], [0, 0, Infinity, 1], [0, 0, 1e100, 1]].map(positions => ({ positions })))(
    "rejects malformed handoff rows $positions", ({ positions }) => {
      expect(() => buildWorkspaceForceAtlas2ContactUnits(buildWorkspaceForceAtlas2Units(scene()), positions)).toThrow();
    },
  );

  it("rejects a mutable or inconsistent seed rather than recalibrating it", () => {
    const seed = buildWorkspaceForceAtlas2Units(scene());
    expect(() => buildWorkspaceForceAtlas2ContactUnits({ ...seed }, [0, 0, 100, 50])).toThrow();
    const invalid = Object.freeze({ ...seed, collisionRadiusModel: Object.freeze([999, 999]) });
    expect(() => buildWorkspaceForceAtlas2ContactUnits(invalid, [0, 0, 100, 50])).toThrow();
    expect(isReusableWorkspaceForceAtlas2ContactUnits(seed, invalid)).toBe(false);
  });
});

describe("signed renderer/model affine mapping", () => {
  function exactUnits() {
    // Native Fit changes collision clearance, never the reference's coordinateScale=1.
    return buildWorkspaceForceAtlas2Units(scene({ positions: [0, 0, 100, 100],
      diametersCss: [41, 41], pointShapes: [0, 0], width: 1000, height: 1000, origin: [10, -20] }));
  }

  it("maps signed and former-box-external positions without clamping or rescaling", () => {
    const units = exactUnits();
    expect(units.rendererUnitsPerModelUnit).toBe(1);
    const renderer = Float32Array.from([-4990, -20, 5010, 9980]);
    const before = renderer.slice();
    const model = workspaceRendererToModelPositions(units, renderer);
    expect([...model]).toEqual([-5000, 0, 5000, 10000]);
    expect(workspaceModelToRendererPositions(units, model)).toEqual(renderer);
    expect(renderer).toEqual(before);
    expect(model).not.toBe(renderer);
  });

  it("restores exact captured renderer pin rows rather than affine round-trip approximations", () => {
    const units = exactUnits();
    const captured = Float32Array.from([-123.456789, 2047.9999, 0.0000314, 5000.123]);
    const pins = new Map<number, readonly [number, number]>([
      [0, [captured[0], captured[1]]],
      [1, [captured[2], captured[3]]],
    ]);
    const model = Float32Array.from([900, 900, -900, -900]);
    const before = model.slice();
    const renderer = workspaceModelToRendererPositions(units, model, pins);
    expect(renderer).toEqual(captured);
    expect(model).toEqual(before);
    expect(pins.size).toBe(2);
    expect(pins.get(0)).toEqual([captured[0], captured[1]]);
    // Presentation override does not silently mutate the driver's separately pinned model state.
    expect([...model]).toEqual([900, 900, -900, -900]);
  });

  it("overrides only pinned rows and returns independent arrays on every call", () => {
    const units = exactUnits();
    const model = Float32Array.from([1, 2, 3, 4]);
    const pins = new Map<number, readonly [number, number]>([[1, [-12, 5001]]]);
    const first = workspaceModelToRendererPositions(units, model, pins);
    const second = workspaceModelToRendererPositions(units, model, pins);
    expect([...first]).toEqual([11, -18, -12, 5001]);
    expect(first).not.toBe(second);
    first[0] = 123;
    expect([...second]).toEqual([11, -18, -12, 5001]);
  });

  it.each([-1, 2, .5, NaN, Infinity])("rejects invalid renderer pin index %s", (index) => {
    expect(() => workspaceModelToRendererPositions(exactUnits(), [0, 0, 0, 0],
      new Map([[index, [0, 0] as const]]))).toThrow("pin row");
  });

  it.each([[0], [0, 0, 0], [NaN, 0], [Infinity, 0], [1e100, 0]].map(pin => ({ pin })))(
    "rejects malformed or unrepresentable renderer pin $pin", ({ pin }) => {
    expect(() => workspaceModelToRendererPositions(exactUnits(), [0, 0, 0, 0],
      new Map([[0, pin as unknown as readonly [number, number]]]))).toThrow();
    },
  );

  it.each([[], [0], [0, 0], [0, 0, 0, NaN], [0, 0, Infinity, 0], [0, 0, 1e100, 0]].map(positions => ({ positions })))(
    "rejects malformed positions before either mapping: $positions", ({ positions }) => {
      const units = exactUnits();
      expect(() => workspaceRendererToModelPositions(units, positions)).toThrow();
      expect(() => workspaceModelToRendererPositions(units, positions)).toThrow();
    },
  );

  it("rejects model-coordinate overflow from otherwise representable renderer/origin rows", () => {
    const units = buildWorkspaceForceAtlas2Units(scene({ origin: [-3e38, 0] }));
    expect(() => workspaceRendererToModelPositions(units, [3e38, 0, 0, 0])).toThrow("Model coordinates overflow");
  });

  it("rejects renderer-coordinate overflow rather than clamping it into the former SDK box", () => {
    const units = buildWorkspaceForceAtlas2Units(scene({ origin: [3e38, 0] }));
    expect(() => workspaceModelToRendererPositions(units, [3e38, 0, 0, 0]))
      .toThrow("Renderer coordinates overflow");
  });
});

describe("invalid scene units cannot reach the GPU", () => {
  const malformed: [string, Partial<UnitsInput>][] = [
    ["empty scene", { positions: [], diametersCss: [], pointShapes: [] }],
    ["wrong shape count", { pointShapes: [0] }],
    ["wrong position count", { positions: [0, 0] }],
    ["unknown glyph shape", { pointShapes: [0, 2] }],
    ["fractional glyph shape", { pointShapes: [0, .5] }],
    ["NaN glyph shape", { pointShapes: [0, NaN] }],
    ["zero diameter", { diametersCss: [0, 10] }],
    ["negative diameter", { diametersCss: [-1, 10] }],
    ["NaN diameter", { diametersCss: [NaN, 10] }],
    ["infinite diameter", { diametersCss: [Infinity, 10] }],
    ["Float32 diameter overflow", { diametersCss: [1e100, 10] }],
    ["Float32 diameter underflow", { diametersCss: [1e-100, 10] }],
    ["NaN coordinate", { positions: [NaN, 0, 100, 50] }],
    ["infinite coordinate", { positions: [Infinity, 0, 100, 50] }],
    ["Float32 coordinate overflow", { positions: [1e100, 0, 100, 50] }],
    ["zero viewport", { width: 0 }],
    ["negative viewport", { height: -1 }],
    ["fractional viewport", { width: 2.5 }],
    ["NaN viewport", { height: NaN }],
    ["infinite viewport", { width: Infinity }],
    ["NaN origin", { origin: [NaN, 0] }],
    ["wrong origin row count", { origin: [0] as unknown as readonly [number, number] }],
    ["infinite origin", { origin: [0, Infinity] }],
    ["Float32 origin overflow", { origin: [1e100, 0] }],
    ["negative CSS gap", { pairGapCss: -1 }],
    ["NaN CSS gap", { pairGapCss: NaN }],
    ["infinite CSS gap", { pairGapCss: Infinity }],
    ["physical radius overflow", { positions: [0, 0, 1e7, 1e7], diametersCss: [1e38, 1e38] }],
    ["physical radius underflow", { positions: [0, 0, 0, 0], diametersCss: [1e-40, 1e-40],
      pairGapCss: 0, width: 1e20, height: 1e20 }],
    ["Float32 clearance overflow", { pairGapCss: 1e100 }],
  ];

  it.each(malformed)("rejects %s", (_name, overrides) => {
    expect(() => buildWorkspaceForceAtlas2Units(scene(overrides))).toThrow();
  });
});
