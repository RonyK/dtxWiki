import { forceCollide, forceSimulation, type SimulationNodeDatum } from "d3-force";

export interface WorkspaceForceAtlas2CollisionInput {
  radii: Float32Array;
  pins?: readonly number[];
}

export type WorkspaceForceAtlas2CollisionRequest =
  | { type: "init"; generation: number; radii: ArrayBuffer; pins: readonly number[] }
  | { type: "project"; generation: number; requestId: number; positions: ArrayBuffer };

export type WorkspaceForceAtlas2CollisionResponse =
  | { type: "projected"; generation: number; requestId: number; positions: ArrayBuffer; durationMs: number }
  | { type: "error"; generation: number; requestId: number | null; message: string };

const MAX_NODES = 2000;
const FORCE_ITERATIONS = 4;
const MANUAL_TICKS = 4;

export function snapshotWorkspaceForceAtlas2CollisionInput(input: WorkspaceForceAtlas2CollisionInput) {
  if (!(input.radii instanceof Float32Array) || input.radii.length < 1 || input.radii.length > MAX_NODES
    || !input.radii.every(radius => Number.isFinite(radius) && radius > 0
      && Number.isFinite(Math.fround(radius * 2)))
    || (input.pins !== undefined && (!Array.isArray(input.pins)
      || !input.pins.every(pin => Number.isInteger(pin) && pin >= 0 && pin < input.radii.length)))) {
    throw new Error("Collision requires 1–2000 finite positive Float32 radii and original pin indices");
  }
  return { radii: input.radii.slice(), pins: Object.freeze([...(input.pins ?? [])]) };
}

export function snapshotWorkspaceForceAtlas2CollisionPositions(positions: Float32Array, count: number): Float32Array {
  if (!(positions instanceof Float32Array) || positions.length !== count * 2
    || !positions.every(Number.isFinite)) throw new Error("Collision requires finite indexed Float32 xy rows");
  return positions.slice();
}

interface CollisionNode extends SimulationNodeDatum {
  x: number;
  y: number;
  vx: number;
  vy: number;
  radius: number;
}

/**
 * Mature quadtree-based, bounded soft collision projection, not a hard
 * nonpenetration certificate. No topology, attraction, camera or timer owns
 * this operation. d3's default random source is deterministic; only its
 * coincidence jiggle uses randomness. Pins keep exact incoming Float32 rows.
 * In particular, overlapping fixed/fixed glyphs cannot be separated without
 * violating the user's pins; fixed/free correction can need later requests.
 */
export function projectWorkspaceForceAtlas2Collision(
  positions: Float32Array,
  input: WorkspaceForceAtlas2CollisionInput,
): Float32Array {
  const { radii, pins } = snapshotWorkspaceForceAtlas2CollisionInput(input);
  const rows = snapshotWorkspaceForceAtlas2CollisionPositions(positions, radii.length);
  const fixed = new Set(pins);
  const nodes: CollisionNode[] = Array.from(radii, (radius, index) => ({
    x: rows[index * 2], y: rows[index * 2 + 1], vx: 0, vy: 0, radius,
    ...(fixed.has(index) ? { fx: rows[index * 2], fy: rows[index * 2 + 1] } : {}),
  }));
  // Stop the timer immediately; exactly four manual ticks each run four
  // upstream forceCollide passes. No copied collision/force implementation.
  const simulation = forceSimulation(nodes).stop().velocityDecay(0).alphaDecay(0)
    .force("collision", forceCollide<CollisionNode>(node => node.radius).strength(1).iterations(FORCE_ITERATIONS));
  try {
    for (let tick = 0; tick < MANUAL_TICKS; tick++) {
      // Each tick projects this position snapshot. Do not carry an inertial
      // impulse into the next pass or add another integrator beside FA2.
      for (const node of nodes) { node.vx = 0; node.vy = 0; }
      simulation.tick();
      for (const pin of pins) {
        nodes[pin].x = rows[pin * 2]; nodes[pin].y = rows[pin * 2 + 1];
        nodes[pin].vx = 0; nodes[pin].vy = 0;
      }
    }
    const result = Float32Array.from(nodes.flatMap(node => [node.x, node.y]));
    if (!result.every(Number.isFinite)) throw new Error("Collision projection exceeded finite Float32 coordinates");
    return result;
  } finally { simulation.stop(); }
}
