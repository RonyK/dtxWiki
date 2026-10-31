import { expect, test } from "@playwright/test";
import { sameWorkspaceRemountCamera } from "../e2e/workspace_graph_camera";

// Pure Node-side oracle cases: no browser, GPU, app server or database fixture.
const before = Object.freeze({ x: 160.31897255360647, y: 215.80099556992369, k: .525060457364594 });

test("remount camera oracle accepts the native Float32 center roundtrip, not bitwise equality", () => {
  const width = 727, height = 608, space = 4096;
  const panX = (width / 2 - before.x) / before.k - (width - space) / 2;
  const panY = height - (height / 2 - before.y) / before.k - (height - space) / 2;
  // Exact adapter setViewport Float32 conversion and native getTransform
  // inverse, evaluated outside any browser/performance measurement.
  const after = {
    x: width / 2 - (Math.fround(panX) + (width - space) / 2) * before.k,
    y: height / 2 - (height - Math.fround(panY) - (height - space) / 2) * before.k,
    k: before.k,
  };
  expect(after).not.toEqual(before);
  expect(Math.abs(after.x - before.x)).toBeGreaterThan(0);
  expect(sameWorkspaceRemountCamera(before, after)).toBe(true);
  expect(sameWorkspaceRemountCamera(before, before)).toBe(true);
});

for (const axis of ["x", "y"] as const) for (const distance of [-1, 1]) {
  test(`remount camera oracle rejects a real ${distance} CSS-pixel ${axis} pan`, () => {
    expect(sameWorkspaceRemountCamera(before, { ...before, [axis]: before[axis] + distance })).toBe(false);
  });
}

for (const zoom of [.99, 1.01]) test(`remount camera oracle rejects a ${zoom} zoom multiplier`, () => {
  expect(sameWorkspaceRemountCamera(before, { ...before, k: before.k * zoom })).toBe(false);
});

for (const missing of [null, undefined, {}, { x: before.x, y: before.y }, { x: before.x, k: before.k },
  { y: before.y, k: before.k }]) test(`remount camera oracle rejects missing camera ${JSON.stringify(missing)}`, () => {
  expect(sameWorkspaceRemountCamera(before, missing)).toBe(false);
  expect(sameWorkspaceRemountCamera(missing, before)).toBe(false);
});

for (const axis of ["x", "y", "k"] as const) for (const invalid of [NaN, Infinity, -Infinity]) {
  test(`remount camera oracle rejects nonfinite ${axis} ${String(invalid)}`, () => {
    expect(sameWorkspaceRemountCamera(before, { ...before, [axis]: invalid })).toBe(false);
    expect(sameWorkspaceRemountCamera({ ...before, [axis]: invalid }, before)).toBe(false);
  });
}

for (const k of [0, -1]) test(`remount camera oracle rejects nonpositive zoom ${k}`, () => {
  expect(sameWorkspaceRemountCamera(before, { ...before, k })).toBe(false);
  expect(sameWorkspaceRemountCamera({ ...before, k }, { ...before, k })).toBe(false);
});

test("remount camera oracle retains the accepted zoom5 and CSS pan2 precision boundaries", () => {
  expect(sameWorkspaceRemountCamera(before, { ...before, x: before.x + .0049, y: before.y - .0049, k: before.k + .0000049 })).toBe(true);
  for (const after of [{ ...before, x: before.x + .0051 }, { ...before, y: before.y + .0051 },
    { ...before, k: before.k + .0000051 }]) expect(sameWorkspaceRemountCamera(before, after)).toBe(false);
});
