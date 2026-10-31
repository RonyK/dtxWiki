import { afterEach, describe, expect, it, vi } from "vitest";

import { createLocalLayout, type LocalLayout } from "../layout";
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
});
