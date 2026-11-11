import { describe, expect, it } from "vitest";

import { placeWorkspaceMapLabels, WORKSPACE_MAP_LABEL_LIMIT, type WorkspaceMapLabelCandidate } from "../labelPlacement";

const label = (overrides: Partial<WorkspaceMapLabelCandidate> = {}): WorkspaceMapLabelCandidate => ({
  screenX: 100, screenY: 100, width: 150, height: 36, priority: "ordinary", ...overrides,
});
const viewport = { width: 390, height: 600 };

describe("bounded Workspace Map label placement", () => {
  it("reserves active intent before explicit and ordinary titles, independent of input order", () => {
    const result = placeWorkspaceMapLabels([
      label(), label({ priority: "explicit" }), label({ priority: "active" }),
    ], viewport);
    expect(result[2]).toEqual({ x: 108, y: 91, visible: true, reason: "placed" });
    // A wide glyph (a cluster's count disc) pushes its title clear of the disc, never over it.
    expect(placeWorkspaceMapLabels([label({ clearance: 24 })], viewport)[0]).toMatchObject({ x: 124, visible: true });
    expect(result[1].visible).toBe(true);
    expect(result[0]).toMatchObject({ visible: false, reason: "collision" });
    expect(result[1].y + 36 + 5 <= result[2].y || result[2].y + 36 + 5 <= result[1].y
      || result[1].x + 150 + 5 <= result[2].x || result[2].x + 150 + 5 <= result[1].x).toBe(true);
  });

  it.each([390, 734])("keeps three individually fitting full titles non-overlapping near a %ipx stage bottom", (width) => {
    const candidates = [0, 1, 2].map((index) => label({ priority: index === 0 ? "active" : "explicit",
      screenX: width - 12, screenY: 600 - 130 - index * 15, width: Math.min(374, width - 16), height: 58 }));
    const result = placeWorkspaceMapLabels(candidates, { width, height: 600 });
    expect(result.every((placement) => placement.visible && placement.reason === "placed")).toBe(true);
    for (let index = 0; index < result.length; index += 1) {
      const current = result[index];
      expect(current.x).toBeGreaterThanOrEqual(0); expect(current.y).toBeGreaterThanOrEqual(0);
      expect(current.x + candidates[index].width).toBeLessThanOrEqual(width);
      expect(current.y + candidates[index].height).toBeLessThanOrEqual(600);
      for (const other of result.slice(index + 1)) {
        expect(current.y + 58 + 5 <= other.y || other.y + 58 + 5 <= current.y
          || current.x + candidates[index].width + 5 <= other.x || other.x + candidates[index].width + 5 <= current.x).toBe(true);
      }
    }
  });

  it("does not overprint mandatory titles when the viewport cannot hold both", () => {
    const result = placeWorkspaceMapLabels([
      label({ priority: "explicit", width: 300, height: 100 }),
      label({ priority: "active", width: 300, height: 100 }),
    ], { width: 300, height: 100 });
    expect(result[1]).toEqual({ x: 0, y: 0, visible: true, reason: "placed" });
    expect(result[0]).toMatchObject({ visible: false, reason: "collision" });
  });

  it("keeps shown ordinary titles against newcomers and holds the shown set during a gesture", () => {
    const crowd = [label(), label({ screenX: 120 })];
    expect(placeWorkspaceMapLabels(crowd, viewport).map((placement) => placement.visible)).toEqual([true, false]);
    expect(placeWorkspaceMapLabels([{ ...crowd[0], wasVisible: false }, { ...crowd[1], wasVisible: true }], viewport)
      .map((placement) => placement.visible)).toEqual([false, true]);
    // 7 px apart: a shown title may stay at the 5 px gap; a hidden one needs 9 px to appear.
    const below = (wasVisible?: boolean) => placeWorkspaceMapLabels(
      [label({ wasVisible: true }), label({ screenY: 100 + 36 + 7, wasVisible })], viewport)[1].visible;
    expect([below(true), below(undefined), below(false)]).toEqual([true, true, false]);
    // A gesture: shown titles stay even overlapping each other, none appears, and
    // a mandatory title still displaces an overlapping ordinary one.
    expect(placeWorkspaceMapLabels([
      label({ wasVisible: true }), label({ screenX: 110, wasVisible: true }), label({ screenY: 250 }),
      label({ priority: "active", screenY: 400 }), label({ screenX: 110, screenY: 400, wasVisible: true }),
    ], viewport, { freeze: true }).map((placement) => placement.reason))
      .toEqual(["placed", "placed", "deferred", "placed", "collision"]);
  });

  it("retains oversized active intent with an explicit constrained reason", () => {
    expect(placeWorkspaceMapLabels([label({ priority: "active", width: 350, height: 200 })], { width: 300, height: 100 }))
      .toEqual([{ x: 0, y: 0, visible: true, reason: "viewport-constrained" }]);
  });

  it("keeps ordinary labels local rather than packing offscreen anchors elsewhere", () => {
    expect(placeWorkspaceMapLabels([label({ screenX: -200 })], viewport)[0]).toMatchObject({ visible: false, reason: "outside-viewport" });
    expect(placeWorkspaceMapLabels([label({ priority: "active", screenX: -200 })], viewport)[0])
      .toEqual({ x: 0, y: 91, visible: true, reason: "placed" });
  });

  it.each([NaN, Infinity, -1, 0])("rejects invalid viewport width %s without inventing positions", (width) => {
    expect(placeWorkspaceMapLabels([label({ priority: "active" })], { width, height: 600 })[0])
      .toEqual({ x: 0, y: 0, visible: false, reason: "invalid" });
  });

  it.each([NaN, Infinity, -1, 0])("rejects invalid measured height %s", (height) => {
    expect(placeWorkspaceMapLabels([label({ priority: "active", height })], viewport)[0])
      .toMatchObject({ visible: false, reason: "invalid" });
  });

  it("enforces the existing 40-title bound without changing or retaining caller geometry", () => {
    const candidates = Array.from({ length: WORKSPACE_MAP_LABEL_LIMIT }, () => label());
    const before = structuredClone(candidates);
    expect(placeWorkspaceMapLabels(candidates, viewport)).toHaveLength(40);
    expect(candidates).toEqual(before);
    expect(() => placeWorkspaceMapLabels([...candidates, label()], viewport)).toThrow(RangeError);
  });
});
