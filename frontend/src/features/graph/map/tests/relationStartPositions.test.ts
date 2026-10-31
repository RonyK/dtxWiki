import { describe, expect, it } from "vitest";

import {
  buildWorkspaceRelationStartPositions,
  type WorkspaceRelationStartEdge,
  type WorkspaceRelationStartInput,
} from "../relationStartPositions";

const seed = 0x5eeda11;

function input(
  nodeCount: number,
  edges: readonly WorkspaceRelationStartEdge[],
): WorkspaceRelationStartInput {
  return { nodeCount, workspaceSeed: seed, edges };
}

function positions(value: WorkspaceRelationStartInput): Float32Array {
  const result = buildWorkspaceRelationStartPositions(value);
  expect(result).toBeInstanceOf(Float32Array);
  return result as Float32Array;
}

function distance(value: Float32Array, source: number, target: number): number {
  return Math.hypot(
    value[source * 2] - value[target * 2],
    value[source * 2 + 1] - value[target * 2 + 1],
  );
}

function expectBounds(value: Float32Array, nodeCount: number): void {
  expect(value).toHaveLength(nodeCount * 2);
  for (const coordinate of value) {
    expect(Number.isFinite(coordinate)).toBe(true);
    expect(coordinate).toBeGreaterThanOrEqual(248);
    expect(coordinate).toBeLessThanOrEqual(3848);
  }
}

describe("Workspace Map relation-only worker warm start", () => {
  const chain = [
    { source: 0, target: 1, weight: 3 },
    { source: 1, target: 2, weight: 1 },
    { source: 2, target: 3, weight: 2 },
  ];

  it("returns deterministic full-node positions, including visible isolates", () => {
    const first = positions(input(7, chain));
    const second = positions(input(7, chain));
    expect(first).toEqual(second);
    expectBounds(first, 7);
    expect(new Set(Array.from(first)).size).toBeGreaterThan(1);
    for (const axis of [0, 1]) {
      const mean = Array.from({ length: 7 }, (_, index) => first[index * 2 + axis])
        .reduce((total, coordinate) => total + coordinate, 0) / 7;
      expect(mean).toBeCloseTo(2048, 3);
    }
  });

  it("canonicalizes edge row order and direction without modifying input", () => {
    const original = Object.freeze({
      nodeCount: 4,
      workspaceSeed: seed,
      edges: Object.freeze(chain.map((edge) => Object.freeze({ ...edge }))),
    });
    const snapshot = JSON.stringify(original);
    const reversed = input(4, [...chain].reverse().map((edge) => ({
      source: edge.target, target: edge.source, weight: edge.weight,
    })));
    expect(positions(original)).toEqual(positions(reversed));
    expect(JSON.stringify(original)).toBe(snapshot);
    expect(original.edges[0].source).toBe(0);
    expect(original.edges[0].target).toBe(1);
  });

  it("does not use folder or community metadata as layout constraints", () => {
    const first = {
      ...input(4, chain),
      folderKeys: ["one", "one", "two", "two"],
      communityIds: [0, 0, 1, 1],
    };
    const second = {
      ...input(4, chain),
      folderKeys: ["different", "third", "different", "fourth"],
      communityIds: [3, 2, 1, 0],
    };
    expect(positions(first)).toEqual(positions(second));
  });

  it.each([
    { pairs: [[0, 1], [2, 3], [4, 5], [6, 7]] },
    { pairs: [[0, 2], [1, 3], [4, 6], [5, 7]] },
  ])("moves stronger direct relations closer than the matched unweighted baseline ($pairs)", ({ pairs }) => {
    const strongPairs = new Set(pairs.map(([source, target]) => `${source}:${target}`));
    const baseline: WorkspaceRelationStartEdge[] = [];
    for (let source = 0; source < 8; source += 1) {
      for (let target = source + 1; target < 8; target += 1) {
        baseline.push({ source, target, weight: 1 });
      }
    }
    // Degree stays seven for every node; only authored occurrences change.
    const weighted = baseline.map((edge) => ({
      ...edge,
      weight: strongPairs.has(`${edge.source}:${edge.target}`) ? 64 : 1,
    }));
    const before = positions(input(8, baseline));
    const after = positions(input(8, weighted));
    const mean = (value: Float32Array) => pairs.reduce(
      (total, [source, target]) => total + distance(value, source, target), 0,
    ) / pairs.length;
    expect(mean(after)).toBeLessThan(mean(before));
    expectBounds(after, 8);
  });

  it("caps occurrence amplification without changing the rendered relation input", () => {
    const edges: WorkspaceRelationStartEdge[] = [];
    for (let source = 0; source < 20; source += 1) {
      for (let target = source + 1; target < 20; target += 1) {
        edges.push({ source, target, weight: source === 0 && target === 1 ? 32768 : 1 });
      }
    }
    const maximum = edges.map((edge) => ({
      ...edge, weight: edge.weight === 32768 ? Number.MAX_SAFE_INTEGER : edge.weight,
    }));
    expect(positions(input(20, edges))).toEqual(positions(input(20, maximum)));
    expect(edges[0].weight).toBe(32768);
    expect(maximum[0].weight).toBe(Number.MAX_SAFE_INTEGER);
  });

  it("keeps degree-one high-occurrence links finite", () => {
    const result = positions(input(2, [{
      source: 0, target: 1, weight: Number.MAX_SAFE_INTEGER,
    }]));
    expectBounds(result, 2);
    expect(distance(result, 0, 1)).toBeGreaterThan(0);
  });

  it.each([0, 1, 2, 2000, 2001, 20000])(
    "defers a valid all-isolate %i-node input to the existing packed path",
    (nodeCount) => expect(buildWorkspaceRelationStartPositions(input(nodeCount, []))).toBeNull(),
  );

  it("defers graphs above 2K without silently truncating visible nodes", () => {
    expect(buildWorkspaceRelationStartPositions(input(2001, chain))).toBeNull();
    expect(buildWorkspaceRelationStartPositions(input(20000, chain))).toBeNull();
  });

  it.each([-1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])(
    "rejects invalid visible node count %s",
    (nodeCount) => expect(() => buildWorkspaceRelationStartPositions(input(nodeCount, [])))
      .toThrow(/node count/),
  );

  it.each([-1, 1.5, NaN, Infinity, 0x100000000])(
    "rejects invalid workspace seed %s",
    (workspaceSeed) => expect(() => buildWorkspaceRelationStartPositions({
      ...input(2, chain.slice(0, 1)), workspaceSeed,
    })).toThrow(/workspace seed/),
  );

  it("accepts a zero uint32 seed without losing deterministic random initialization", () => {
    const value = { ...input(4, chain), workspaceSeed: 0 };
    expect(positions(value)).toEqual(positions(value));
  });

  it.each([-1, 0.5, NaN, Infinity, 4])(
    "rejects endpoints outside the actor-visible catalog (%s)",
    (endpoint) => {
      for (const edge of [
        { source: endpoint, target: 1, weight: 1 },
        { source: 1, target: endpoint, weight: 1 },
      ]) {
        expect(() => buildWorkspaceRelationStartPositions(input(4, [edge])))
          .toThrow(/visible endpoint/);
      }
    },
  );

  it.each([-1, 0, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])(
    "rejects invalid authored occurrence weight %s",
    (weight) => expect(() => buildWorkspaceRelationStartPositions(input(2, [
      { source: 0, target: 1, weight },
    ]))).toThrow(/occurrence weight/),
  );

  it("rejects self edges and repeated undirected pairs rather than multiplying their force", () => {
    expect(() => buildWorkspaceRelationStartPositions(input(2, [
      { source: 1, target: 1, weight: 1 },
    ]))).toThrow(/self edge/);
    expect(() => buildWorkspaceRelationStartPositions(input(2, [
      { source: 0, target: 1, weight: 1 },
      { source: 1, target: 0, weight: 3 },
    ]))).toThrow(/duplicate pair/);
  });

  it("validates endpoints and weights before taking the above-2K fallback", () => {
    expect(() => buildWorkspaceRelationStartPositions(input(20000, [
      { source: 0, target: 20000, weight: 1 },
    ]))).toThrow(/visible endpoint/);
    expect(() => buildWorkspaceRelationStartPositions(input(20000, [
      { source: 0, target: 1, weight: NaN },
    ]))).toThrow(/occurrence weight/);
  });

  it("computes one bounded full-node 2K / 6K-relation fixture", () => {
    const edges: WorkspaceRelationStartEdge[] = [];
    for (let source = 0; source < 2000; source += 1) {
      for (const offset of [1, 5, 31]) {
        edges.push({ source, target: (source + offset) % 2000, weight: 1 + source % 4 });
      }
    }
    const started = performance.now();
    const result = positions(input(2000, edges));
    const elapsed = performance.now() - started;
    expectBounds(result, 2000);
    console.info(`Relation warm-start 2000 nodes / ${edges.length} edges: ${elapsed.toFixed(1)} ms`);
  }, 30000);
});
