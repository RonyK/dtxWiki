import { afterEach, describe, expect, it, vi } from "vitest";

import { advanceArrival, createLocalLayout, type ArrivalWatch, type LocalLayout } from "../layout";
import type { LocalLayoutInput } from "../layoutTypes";

const layouts: LocalLayout[] = [];

function create(input: LocalLayoutInput, options?: Parameters<typeof createLocalLayout>[1]): LocalLayout {
  const layout = createLocalLayout(input, options);
  layouts.push(layout);
  return layout;
}

afterEach(() => {
  for (const layout of layouts) layout.destroy();
  layouts.length = 0;
});

const seedAndNeighbors: LocalLayoutInput = {
  nodes: [
    { key: "page:seed", radiusPx: 5, isSeed: true },
    { key: "page:a", radiusPx: 7, isSeed: false },
    { key: "page:b", radiusPx: 8, isSeed: false },
  ],
  edges: [
    { sourceKey: "page:seed", targetKey: "page:a", kind: "link", count: 1 },
    { sourceKey: "page:b", targetKey: "page:seed", kind: "membership", count: 1 },
  ],
};

describe("Local d3-force layout", () => {
  it("delivers a finite 300-tick first frame with the seed fixed at the origin", () => {
    const frames: number[] = [];
    const layout = create(seedAndNeighbors, {
      autoStart: false,
      onTick: (positions) => frames.push(Object.keys(positions).length),
    });
    const positions = layout.getPositions();

    expect(frames).toEqual([3]);
    expect(positions["page:seed"]).toEqual({ x: 0, y: 0 });
    expect(Object.values(positions).every(({ x, y }) => Number.isFinite(x) && Number.isFinite(y))).toBe(true);
  });

  it("sorts and deduplicates nodes and links so row order cannot change layout", () => {
    const original = create(seedAndNeighbors, { autoStart: false }).getPositions();
    const reordered = create({
      nodes: [
        seedAndNeighbors.nodes[2],
        seedAndNeighbors.nodes[0],
        seedAndNeighbors.nodes[1],
        { ...seedAndNeighbors.nodes[1], radiusPx: 5 },
      ],
      edges: [
        seedAndNeighbors.edges[1],
        { ...seedAndNeighbors.edges[0], sourceKey: "page:a", targetKey: "page:seed" },
        seedAndNeighbors.edges[0],
      ],
    }, { autoStart: false }).getPositions();

    expect(reordered).toEqual(original);
  });

  it("does not synthesize or lay out a hidden endpoint from a dangling relation", () => {
    const visible: LocalLayoutInput = {
      nodes: seedAndNeighbors.nodes,
      edges: seedAndNeighbors.edges,
    };
    const withDangling: LocalLayoutInput = {
      ...visible,
      edges: [
        ...visible.edges,
        { sourceKey: "page:seed", targetKey: "page:hidden", kind: "citation", count: 1 },
        { sourceKey: "page:hidden", targetKey: "page:seed", kind: "coAccess", count: 1 },
        { sourceKey: "page:a", targetKey: "page:a", kind: "link", count: 1 },
        { sourceKey: "page:a", targetKey: "page:b", kind: "link", count: 0 },
      ],
    };

    expect(create(withDangling, { autoStart: false }).getPositions())
      .toEqual(create(visible, { autoStart: false }).getPositions());
    expect(create(withDangling, { autoStart: false }).getPositions()["page:hidden"])
      .toBeUndefined();
  });

  it("validates cached and fixed coordinates, and never lets either move the seed", () => {
    const layout = create(seedAndNeighbors, {
      autoStart: false,
      cachedPositions: {
        "page:seed": { x: 100, y: 200 },
        "page:a": { x: Number.POSITIVE_INFINITY, y: 4 },
      },
      fixedPositions: {
        "page:seed": { x: 10, y: 20 },
        "page:b": { x: 80, y: -30 },
      },
    });

    expect(layout.getPositions()["page:seed"]).toEqual({ x: 0, y: 0 });
    expect(layout.getPositions()["page:b"]).toEqual({ x: 80, y: -30 });
    expect(Object.values(layout.getPositions()).every(({ x, y }) => Number.isFinite(x) && Number.isFinite(y))).toBe(true);
    expect(layout.pin("page:seed", { x: 9, y: 9 })).toEqual({ x: 0, y: 0 });
    layout.unpin("page:seed");
    expect(layout.getPositions()["page:seed"]).toEqual({ x: 0, y: 0 });
  });

  it("pins and drags without accepting invalid or absent points", () => {
    const layout = create(seedAndNeighbors, { autoStart: false });
    expect(layout.pin("page:absent", { x: 1, y: 2 })).toBeNull();
    expect(layout.pin("page:a", { x: Number.NaN, y: 1 })).toBeNull();
    expect(layout.beginDrag("page:a", { x: 10, y: 20 })).toEqual({ x: 10, y: 20 });
    expect(layout.moveDrag("page:a", { x: 30, y: -5 })).toEqual({ x: 30, y: -5 });
    expect(layout.getPositions()["page:a"]).toEqual({ x: 30, y: -5 });
    expect(layout.moveDrag("page:a", { x: Number.POSITIVE_INFINITY, y: 0 })).toBeNull();
    // A pin arriving mid-drag records the pin but never pulls the point back.
    expect(layout.pin("page:a", { x: 8, y: 9 })).toEqual({ x: 30, y: -5 });
    expect(layout.getPositions()["page:a"]).toEqual({ x: 30, y: -5 });
    layout.endDrag("page:a");
    expect(layout.getPositions()["page:a"]).toEqual({ x: 30, y: -5 });
    expect(layout.pin("page:a", { x: 8, y: 9 })).toEqual({ x: 8, y: 9 });
    expect(layout.getPositions()["page:a"]).toEqual({ x: 8, y: 9 });
  });

  it("settles reduced motion without a live tick timer and honors destroy", async () => {
    const frames: number[] = [];
    const layout = create(seedAndNeighbors, {
      reducedMotion: true,
      onTick: () => frames.push(1),
    });
    expect(frames).toHaveLength(1);
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(frames).toHaveLength(1);

    layout.setReducedMotion(false);
    layout.pause();
    const before = frames.length;
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(frames).toHaveLength(before);

    layout.resume();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(frames.length).toBeGreaterThan(before);

    layout.destroy();
    const atDestroy = frames.length;
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(frames).toHaveLength(atDestroy);
    expect(layout.pin("page:a", { x: 1, y: 2 })).toBeNull();
  });

  it("unpinning an unpinned node is a no-op, not a new 300-tick layout", () => {
    const onTick = vi.fn();
    const layout = create(seedAndNeighbors, { reducedMotion: true, onTick });
    const before = layout.getPositions();
    onTick.mockClear();
    layout.unpin("page:a");
    layout.unpin("page:b");
    expect(onTick).not.toHaveBeenCalled();
    expect(layout.getPositions()).toEqual(before);
  });

  it("reconciling unchanged pins does not settle or move another point", () => {
    const onTick = vi.fn();
    const layout = create(seedAndNeighbors, {
      reducedMotion: true, onTick, fixedPositions: { "page:a": { x: 80, y: 40 } },
    });
    const before = layout.getPositions();
    onTick.mockClear();
    layout.pin("page:a", { x: 80, y: 40 });
    layout.pin("page:a");
    expect(onTick).not.toHaveBeenCalled();
    expect(layout.getPositions()).toEqual(before);
  });

  it("clears all pins as one update and keeps reduced-motion positions stable", () => {
    const onTick = vi.fn();
    const layout = create(seedAndNeighbors, {
      reducedMotion: true, onTick,
      fixedPositions: { "page:a": { x: 80, y: 40 }, "page:b": { x: -80, y: -40 } },
    });
    const before = layout.getPositions();
    onTick.mockClear();
    layout.clearPins();
    expect(onTick).toHaveBeenCalledOnce();
    expect(layout.getPositions()).toEqual(before);
    layout.clearPins();
    expect(onTick).toHaveBeenCalledOnce();
    layout.pin("page:seed", { x: 99, y: 99 });
    expect(layout.getPositions()["page:seed"]).toEqual({ x: 0, y: 0 });
  });

  it("pinning and releasing a point does not relayout other reduced-motion nodes", () => {
    const layout = create(seedAndNeighbors, { reducedMotion: true });
    const before = layout.getPositions();
    layout.pin("page:a", { x: 200, y: 300 });
    expect(layout.getPositions()["page:b"]).toEqual(before["page:b"]);
    layout.unpin("page:a");
    expect(layout.getPositions()["page:b"]).toEqual(before["page:b"]);
    expect(layout.getPositions()["page:a"]).toEqual({ x: 200, y: 300 });
  });

  it("lets arriving nodes converge from their neighbor while retaining live pins and releasing departed nodes", async () => {
    // Natural cooling ends a wait even before 2 s: nothing moves any more. A
    // frozen clock leaves the d3 end event as the only way to settle here.
    const cooledPin = vi.fn();
    const cooling = create(seedAndNeighbors, { autoStart: false, onSettledPin: cooledPin, now: () => 0 });
    cooling.update({
      nodes: [...seedAndNeighbors.nodes, { key: "page:late", radiusPx: 7, isSeed: false }],
      edges: [...seedAndNeighbors.edges, { sourceKey: "page:a", targetKey: "page:late", kind: "link", count: 1 }],
    });
    cooling.pinWhenSettled("page:late");
    cooling.resume();
    const onTick = vi.fn();
    // Each live frame advances the injected clock by 99 ms (within the frame-gap limit).
    let clock = 0;
    let settledAt = 0;
    const onSettledPin = vi.fn(() => { settledAt = clock; });
    const layout = create(seedAndNeighbors, { autoStart: false, onTick, onSettledPin, now: () => (clock += 99) });
    layout.pin("page:a", { x: 80, y: 40 });
    layout.beginDrag("page:b", { x: -70, y: 30 });
    const before = layout.getPositions();
    const arrived: LocalLayoutInput = {
      nodes: [...seedAndNeighbors.nodes, { key: "page:new", radiusPx: 7, isSeed: false }],
      edges: [...seedAndNeighbors.edges, { sourceKey: "page:b", targetKey: "page:new", kind: "link", count: 1 }],
    };
    layout.update(arrived);
    const joined = layout.getPositions();
    expect(joined["page:a"]).toEqual(before["page:a"]);
    expect(joined["page:b"]).toEqual(before["page:b"]);
    const arrivalDistance = Math.hypot(joined["page:new"].x - before["page:b"].x,
      joined["page:new"].y - before["page:b"].y);
    expect(arrivalDistance).toBeGreaterThan(0);
    expect(arrivalDistance).toBeLessThan(30);

    const departed: LocalLayoutInput = {
      nodes: arrived.nodes.filter((node) => node.key !== "page:b"),
      edges: [seedAndNeighbors.edges[0], { sourceKey: "page:a", targetKey: "page:new", kind: "link", count: 1 }],
    };
    layout.update(departed);
    expect(layout.getPositions()["page:b"]).toBeUndefined();
    expect(layout.pin("page:b")).toBeNull();
    expect(() => layout.endDrag("page:b")).not.toThrow();
    expect(layout.getPositions()["page:new"]).toEqual(joined["page:new"]);
    // A waiting pin lets the arrival move first and fixes it only after 2 s.
    layout.pinWhenSettled("page:new");
    layout.resume();
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(layout.getPositions()["page:new"]).not.toEqual(joined["page:new"]);
    expect(layout.getPositions()["page:a"]).toEqual({ x: 80, y: 40 });
    expect(layout.getPositions()["page:seed"]).toEqual({ x: 0, y: 0 });
    await vi.waitFor(() => expect(onSettledPin).toHaveBeenCalledOnce(), { timeout: 5_000, interval: 20 });
    expect(settledAt).toBeGreaterThanOrEqual(2_000);
    expect(settledAt).toBeLessThanOrEqual(5_000 + 2 * 99);
    const fixedAt = layout.getPositions()["page:new"];
    expect(onSettledPin).toHaveBeenCalledWith("page:new", fixedAt);
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(layout.getPositions()["page:new"]).toEqual(fixedAt);
    await vi.waitFor(() => expect(cooledPin).toHaveBeenCalledOnce(), { timeout: 10_000, interval: 50 });
    expect(cooledPin).toHaveBeenCalledWith("page:late", cooling.getPositions()["page:late"]);

    layout.pause();
    const settled = layout.getPositions();
    onTick.mockClear();
    layout.update({ nodes: [...departed.nodes].reverse(), edges: [...departed.edges].reverse() });
    expect(onTick).not.toHaveBeenCalled();
    expect(layout.getPositions()).toEqual(settled);
  }, 15_000);

  it("settles topology changes without animation in reduced motion and does not advance paused updates", async () => {
    const onTick = vi.fn();
    const onSettledPin = vi.fn();
    const layout = create(seedAndNeighbors, { reducedMotion: true, onTick, onSettledPin });
    const next: LocalLayoutInput = {
      nodes: [seedAndNeighbors.nodes[0], { key: "page:new", radiusPx: 7, isSeed: false }],
      edges: [{ sourceKey: "page:seed", targetKey: "page:new", kind: "link", count: 1 }],
    };
    onTick.mockClear();
    layout.update(next, { "page:new": { x: 90, y: 20 } });
    expect(layout.getPositions()).toEqual({ "page:seed": { x: 0, y: 0 }, "page:new": { x: 90, y: 20 } });
    expect(onTick).toHaveBeenCalledOnce();
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(onTick).toHaveBeenCalledOnce();

    layout.pause();
    layout.update({
      nodes: [...next.nodes, { key: "page:later", radiusPx: 7, isSeed: false }],
      edges: [...next.edges, { sourceKey: "page:new", targetKey: "page:later", kind: "link", count: 1 }],
    });
    // A release ends an arrival wait: resuming must not fix the point later.
    layout.pinWhenSettled("page:later");
    layout.unpin("page:later");
    const paused = layout.getPositions();
    expect(Math.hypot(paused["page:later"].x - 90, paused["page:later"].y - 20)).toBeLessThan(30);
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(layout.getPositions()).toEqual(paused);
    layout.resume();
    expect(layout.getPositions()["page:later"]).not.toEqual(paused["page:later"]);
    const resumed = layout.getPositions();
    const atResume = onTick.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(layout.getPositions()).toEqual(resumed);
    expect(onTick).toHaveBeenCalledTimes(atResume);
    layout.pause();
    layout.resume();
    expect(onTick).toHaveBeenCalledTimes(atResume);
    expect(onSettledPin).not.toHaveBeenCalled();
    // Reduced motion moves nothing further, so a new wait fixes the point at once.
    layout.pinWhenSettled("page:later");
    expect(onSettledPin).toHaveBeenCalledExactlyOnceWith("page:later", resumed["page:later"]);
    layout.unpin("page:later");
    layout.setReducedMotion(false);
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(layout.getPositions()["page:later"]).not.toEqual(resumed["page:later"]);
    expect(layout.getPositions()["page:new"]).toEqual({ x: 90, y: 20 });
  });

  it("fixes an arrival by elapsed time and stillness, not by frame count, and skips frame gaps", () => {
    const fixedAt = (frameMs: number, distance: (activeMs: number) => number, gapAtMs?: number): number => {
      const watch: ArrivalWatch = { activeMs: 0, recent: [] };
      expect(advanceArrival(watch, null, 0)).toBe(false);
      let gapped = gapAtMs === undefined;
      for (let frame = 0; frame < 10_000; frame += 1) {
        if (!gapped && watch.activeMs >= gapAtMs!) {
          gapped = true;
          // A hidden tab or long task is neither elapsed time nor stillness.
          expect(advanceArrival(watch, 10_000, 0)).toBe(false);
        }
        if (advanceArrival(watch, frameMs, distance(watch.activeMs))) return watch.activeMs;
      }
      return Number.POSITIVE_INFINITY;
    };
    // Still from its zero-velocity spawn, a point still waits 2 s, at 60 and 120 Hz alike.
    for (const frameMs of [1000 / 60, 1000 / 120]) {
      const still = fixedAt(frameMs, () => 0);
      expect(still).toBeGreaterThanOrEqual(2_000);
      expect(still).toBeLessThan(2_000 + frameMs);
    }
    const frameMs = 1000 / 60;
    // Moving until 3 s, it is fixed once a full 250 ms window has been still.
    const calmed = fixedAt(frameMs, (ms) => ms < 3_000 ? 2 : 0);
    expect(calmed).toBeGreaterThanOrEqual(3_250 - 1);
    expect(calmed).toBeLessThan(3_250 + 2 * frameMs);
    // Small but continuous motion never reads as still: the 5 s ceiling fixes it.
    const ceiling = fixedAt(frameMs, () => 0.5);
    expect(ceiling).toBeGreaterThanOrEqual(5_000);
    expect(ceiling).toBeLessThan(5_000 + frameMs);
    // A gap at 1.9 s adds at most 250 ms and restarts the still window.
    const gapped = fixedAt(frameMs, () => 0, 1_900);
    expect(gapped).toBeGreaterThanOrEqual(2_400 - 1);
    expect(gapped).toBeLessThan(2_400 + 2 * frameMs);
    // Steadily slow frames are never stillness but still reach the ceiling.
    expect(fixedAt(300, () => 0)).toBe(5_000);
  });
});
