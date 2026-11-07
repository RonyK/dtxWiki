import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
} from "d3-force";
import type { SimulationLinkDatum, SimulationNodeDatum } from "d3-force";

import type { GraphPoint } from "../shared/rendererTypes";
import {
  LOCAL_ARRIVAL_PIN,
  LOCAL_LAYOUT_PARAMS,
  type LocalLayoutInput,
  type LocalLayoutPositions,
} from "./layoutTypes";

type LayoutNode = SimulationNodeDatum & {
  key: string;
  radiusPx: number;
  x: number;
  y: number;
};

type LayoutLink = SimulationLinkDatum<LayoutNode> & {
  kind: LocalLayoutInput["edges"][number]["kind"];
};

export type LocalLayoutOptions = {
  /** Caller must scope these positions to this actor, lens and visible-input fingerprint. */
  cachedPositions?: LocalLayoutPositions | null;
  fixedPositions?: LocalLayoutPositions | null;
  onTick?: (positions: LocalLayoutPositions) => void;
  /** A node waiting through pinWhenSettled() has been fixed where it came to rest. */
  onSettledPin?: (key: string, position: GraphPoint) => void;
  /** Millisecond clock for arrival settling; defaults to performance.now(). */
  now?: () => number;
  reducedMotion?: boolean;
  autoStart?: boolean;
};

export type LocalLayout = {
  getPositions(): LocalLayoutPositions;
  /** Update the current scope without resetting existing positions or velocities. */
  update(input: LocalLayoutInput, fixedPositions?: LocalLayoutPositions): void;
  pause(): void;
  resume(): void;
  destroy(): void;
  pin(key: string, position?: GraphPoint): GraphPoint | null;
  /**
   * Fix an arriving node once its motion calms down (LOCAL_ARRIVAL_PIN), or
   * at once when nothing moves. Repeating it never restarts a running wait;
   * pin, unpin, clearPins, a departure or destroy ends the wait.
   */
  pinWhenSettled(key: string): void;
  unpin(key: string): void;
  clearPins(): void;
  beginDrag(key: string, position?: GraphPoint): GraphPoint | null;
  moveDrag(key: string, position: GraphPoint): GraphPoint | null;
  endDrag(key: string): void;
  setReducedMotion(enabled: boolean): void;
};

function compareKeys(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function finitePoint(point: GraphPoint | undefined | null): point is GraphPoint {
  return point !== undefined && point !== null
    && Number.isFinite(point.x) && Number.isFinite(point.y);
}

function safeRadius(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 3;
}

/**
 * Build force inputs only from the already visibility-filtered Local scene.
 * Stable ordering makes the d3 phyllotaxis start and force application
 * independent of response row order. Never add a missing endpoint here.
 */
function normalizeInput(input: LocalLayoutInput): {
  nodes: LayoutNode[];
  links: LayoutLink[];
  seedKey: string | null;
} {
  const byKey = new Map<string, { radiusPx: number; isSeed: boolean }>();
  for (const node of input.nodes) {
    if (typeof node.key !== "string" || node.key.length === 0) continue;
    const old = byKey.get(node.key);
    const radiusPx = safeRadius(node.radiusPx);
    byKey.set(node.key, {
      radiusPx: Math.max(old?.radiusPx ?? 0, radiusPx),
      isSeed: Boolean(old?.isSeed || node.isSeed),
    });
  }

  const keys = [...byKey.keys()].sort(compareKeys);
  const seedKey = keys.find((key) => byKey.get(key)?.isSeed) ?? null;
  const nodes: LayoutNode[] = keys.map((key) => ({
    key,
    radiusPx: byKey.get(key)!.radiusPx,
    x: Number.NaN,
    y: Number.NaN,
  }));

  const byEdge = new Map<string, LayoutLink>();
  for (const edge of input.edges) {
    if (!byKey.has(edge.sourceKey) || !byKey.has(edge.targetKey)) continue;
    if (edge.sourceKey === edge.targetKey) continue;
    if (!Number.isFinite(edge.count) || edge.count <= 0) continue;
    const [source, target] = compareKeys(edge.sourceKey, edge.targetKey) <= 0
      ? [edge.sourceKey, edge.targetKey]
      : [edge.targetKey, edge.sourceKey];
    const edgeKey = JSON.stringify([source, target, edge.kind]);
    if (!byEdge.has(edgeKey)) byEdge.set(edgeKey, { source, target, kind: edge.kind });
  }
  const links = [...byEdge.entries()]
    .sort(([left], [right]) => compareKeys(left, right))
    .map(([, link]) => link);

  return { nodes, links, seedKey };
}

function topologyKey(input: ReturnType<typeof normalizeInput>): string {
  return JSON.stringify([
    input.seedKey,
    input.nodes.map((node) => [node.key, node.radiusPx]),
    input.links.map((link) => [link.source, link.target, link.kind]),
  ]);
}

export type ArrivalWatch = {
  activeMs: number;
  recent: Array<{ ms: number; distance: number }>;
};

/**
 * Advance one arriving node by one live frame and say whether to fix it now.
 * Time, not tick count: d3 ticks once per display frame (60 or 120 Hz). The
 * still window sums the path travelled, so an oscillating node is not still.
 */
export function advanceArrival(watch: ArrivalWatch, elapsedMs: number | null, distance: number): boolean {
  if (elapsedMs === null || !(elapsedMs >= 0)) {
    watch.recent.length = 0;
    return false;
  }
  watch.activeMs += Math.min(elapsedMs, LOCAL_ARRIVAL_PIN.maxFrameGapMs);
  if (elapsedMs > LOCAL_ARRIVAL_PIN.maxFrameGapMs) {
    watch.recent.length = 0;
    return watch.activeMs >= LOCAL_ARRIVAL_PIN.maxMs;
  }
  watch.recent.push({ ms: elapsedMs, distance });
  let span = watch.recent.reduce((sum, sample) => sum + sample.ms, 0);
  while (watch.recent.length > 1 && span - watch.recent[0].ms >= LOCAL_ARRIVAL_PIN.stillWindowMs) {
    span -= watch.recent.shift()!.ms;
  }
  if (watch.activeMs >= LOCAL_ARRIVAL_PIN.maxMs) return true;
  const path = watch.recent.reduce((sum, sample) => sum + sample.distance, 0);
  return watch.activeMs >= LOCAL_ARRIVAL_PIN.minMs
    && span >= LOCAL_ARRIVAL_PIN.stillWindowMs && path <= LOCAL_ARRIVAL_PIN.stillDistance;
}

function spawnOffset(key: string): GraphPoint {
  let hash = 0;
  for (const character of key) hash = (Math.imul(hash, 31) + character.charCodeAt(0)) >>> 0;
  const angle = hash / 0x100000000 * Math.PI * 2;
  return { x: Math.cos(angle) * 16, y: Math.sin(angle) * 16 };
}

/**
 * A browser-only Local Graph force layout. Initial 300 ticks are synchronous
 * so the first renderer frame receives settled, deterministic coordinates;
 * subsequent d3 timer ticks keep the graph alive until paused or cooled.
 */
export function createLocalLayout(
  input: LocalLayoutInput,
  options: LocalLayoutOptions = {},
): LocalLayout {
  const normalized = normalizeInput(input);
  let { nodes, seedKey } = normalized;
  let currentTopologyKey = topologyKey(normalized);
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const persistentPins = new Set<string>();
  let draggingKey: string | null = null;
  let reducedMotion = Boolean(options.reducedMotion);
  let paused = options.autoStart === false;
  let pendingTopologySettle = false;
  let destroyed = false;
  const now = options.now ?? (() => performance.now());
  // Arriving nodes waiting to be pinned, with the point seen on the last frame.
  const arrivals = new Map<string, ArrivalWatch & GraphPoint>();
  let lastFrameAt: number | null = null;

  for (const node of nodes) {
    const cached = options.cachedPositions?.[node.key];
    if (finitePoint(cached)) {
      node.x = cached.x;
      node.y = cached.y;
    }
    const fixed = options.fixedPositions?.[node.key];
    if (finitePoint(fixed)) {
      node.x = node.fx = fixed.x;
      node.y = node.fy = fixed.y;
      persistentPins.add(node.key);
    }
  }

  if (seedKey !== null) {
    const seed = byKey.get(seedKey)!;
    seed.x = seed.fx = 0;
    seed.y = seed.fy = 0;
    persistentPins.add(seedKey);
  }

  const linkForce = forceLink<LayoutNode, LayoutLink>(normalized.links)
    .id((node) => node.key)
    .distance((link) => link.kind === "membership"
      ? LOCAL_LAYOUT_PARAMS.membershipDistance
      : LOCAL_LAYOUT_PARAMS.relationDistance);
  const simulation = forceSimulation<LayoutNode>(nodes).stop()
    .force("link", linkForce)
    .force("charge", forceManyBody<LayoutNode>()
      .strength(LOCAL_LAYOUT_PARAMS.chargeStrength)
      .distanceMax(LOCAL_LAYOUT_PARAMS.chargeDistanceMax))
    .force("x", forceX<LayoutNode>(0).strength(LOCAL_LAYOUT_PARAMS.axisStrength))
    .force("y", forceY<LayoutNode>(0).strength(LOCAL_LAYOUT_PARAMS.axisStrength))
    .force("collide", forceCollide<LayoutNode>()
      .radius((node) => node.radiusPx + LOCAL_LAYOUT_PARAMS.collidePadding));

  const getPositions = (): LocalLayoutPositions => Object.fromEntries(
    nodes.map((node) => [node.key, { x: node.x, y: node.y }]),
  );
  const notify = (): void => {
    if (!destroyed) options.onTick?.(getPositions());
  };
  const settleDragWithoutMotion = (): void => {
    // The initial scene is already settled. One small relaxation after an
    // actual drag is enough; pin reconciliation/reset must never run this.
    simulation.alphaTarget(0).alpha(0.08).tick(24);
    notify();
  };
  const startLive = (alpha = 0.15): void => {
    if (destroyed || paused || reducedMotion || nodes.length === 0) return;
    simulation.alpha(Math.max(alpha, simulation.alpha())).restart();
  };
  const setPosition = (node: LayoutNode, point: GraphPoint): void => {
    node.x = node.fx = point.x;
    node.y = node.fy = point.y;
    node.vx = node.vy = 0;
  };
  const afterChange = (): void => {
    notify();
    startLive();
  };
  const settleArrival = (key: string): void => {
    // Only a wait still in place settles, and it ends first: a callback may
    // pin, unpin or reset re-entrantly, even in the middle of a batch.
    if (!arrivals.delete(key)) return;
    const node = byKey.get(key);
    if (destroyed || !node) return;
    // Fix the point where it stands. Unlike pin(), this neither moves it nor
    // reheats the simulation, so every other node keeps settling smoothly.
    node.fx = node.x;
    node.fy = node.y;
    node.vx = node.vy = 0;
    persistentPins.add(key);
    options.onSettledPin?.(key, { x: node.x, y: node.y });
  };
  const settleArrivals = (): void => {
    for (const key of [...arrivals.keys()]) {
      if (key !== draggingKey) settleArrival(key);
    }
  };
  const observeArrivals = (): void => {
    if (arrivals.size === 0) {
      lastFrameAt = null;
      return;
    }
    const at = now();
    const elapsed = lastFrameAt === null ? null : at - lastFrameAt;
    lastFrameAt = at;
    for (const key of [...arrivals.keys()]) {
      const watch = arrivals.get(key);
      const node = byKey.get(key);
      if (!watch || !node) continue;
      const distance = Math.hypot(node.x - watch.x, node.y - watch.y);
      watch.x = node.x;
      watch.y = node.y;
      // A drag is the user's motion, not arrival time.
      if (key === draggingKey) watch.recent.length = 0;
      else if (advanceArrival(watch, elapsed, distance)) settleArrival(key);
    }
  };

  simulation.on("tick.local-layout", () => {
    notify();
    observeArrivals();
  });
  // A cooled simulation moves nothing further: arrivals are at rest.
  simulation.on("end.local-layout", () => {
    lastFrameAt = null;
    settleArrivals();
  });
  simulation.tick(LOCAL_LAYOUT_PARAMS.preTicks);
  notify();
  startLive();

  return {
    getPositions,
    update(nextInput, fixedPositions) {
      if (destroyed) return;
      const next = normalizeInput(nextInput);
      const nextTopologyKey = topologyKey(next);
      if (nextTopologyKey === currentTopologyKey) return;

      const retained = new Map(byKey);
      const nextKeys = new Set(next.nodes.map((node) => node.key));
      for (const key of persistentPins) {
        if (!nextKeys.has(key)) persistentPins.delete(key);
      }
      for (const key of [...arrivals.keys()]) {
        if (!nextKeys.has(key)) arrivals.delete(key);
      }
      if (draggingKey !== null && !nextKeys.has(draggingKey)) {
        draggingKey = null;
        simulation.alphaTarget(0);
      }
      if (seedKey !== null && seedKey !== next.seedKey && retained.has(seedKey)) {
        const previousSeed = retained.get(seedKey)!;
        persistentPins.delete(seedKey);
        previousSeed.fx = previousSeed.fy = null;
      }

      nodes = next.nodes.map((node) => {
        const existing = retained.get(node.key);
        if (existing) {
          existing.radiusPx = node.radiusPx;
          return existing;
        }
        const fixed = fixedPositions?.[node.key];
        if (finitePoint(fixed)) {
          setPosition(node, fixed);
          persistentPins.add(node.key);
          return node;
        }
        const relation = next.links.find((link) =>
          link.source === node.key && retained.has(String(link.target))
          || link.target === node.key && retained.has(String(link.source)));
        const neighbor = relation && retained.get(String(relation.source === node.key ? relation.target : relation.source));
        if (neighbor) {
          const offset = spawnOffset(node.key);
          node.x = neighbor.x + offset.x;
          node.y = neighbor.y + offset.y;
        }
        return node;
      });
      byKey.clear();
      nodes.forEach((node) => byKey.set(node.key, node));
      seedKey = next.seedKey;
      if (seedKey !== null) {
        setPosition(byKey.get(seedKey)!, { x: 0, y: 0 });
        persistentPins.add(seedKey);
      }
      // Remove old endpoint references before d3 initializes forces on the
      // new membership; a departed endpoint must never remain in a force.
      linkForce.links([]);
      simulation.nodes(nodes);
      linkForce.links(next.links);
      currentTopologyKey = nextTopologyKey;
      pendingTopologySettle = paused;
      if (reducedMotion && !paused) {
        simulation.alpha(0.3).tick(LOCAL_LAYOUT_PARAMS.preTicks);
      }
      notify();
      startLive(0.3);
    },
    pause() {
      if (destroyed || paused) return;
      paused = true;
      lastFrameAt = null;
      simulation.stop().alphaTarget(0);
    },
    resume() {
      if (destroyed || !paused) return;
      paused = false;
      lastFrameAt = null;
      if (pendingTopologySettle && reducedMotion) {
        simulation.alpha(0.3).tick(LOCAL_LAYOUT_PARAMS.preTicks);
        notify();
      }
      pendingTopologySettle = false;
      // Manual ticks dispatch no events; settle reduced-motion arrivals here.
      if (reducedMotion) settleArrivals();
      startLive();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      simulation.stop().on("tick.local-layout", null).on("end.local-layout", null);
      arrivals.clear();
      draggingKey = null;
    },
    pinWhenSettled(key) {
      if (destroyed || arrivals.has(key)) return;
      const node = byKey.get(key);
      if (!node) return;
      arrivals.set(key, { activeMs: 0, recent: [], x: node.x, y: node.y });
      // Nothing will move it: already fixed, reduced motion, or a cooled
      // simulation. A paused layout decides on resume instead.
      if (persistentPins.has(key)
        || !paused && (reducedMotion || simulation.alpha() < simulation.alphaMin())) {
        settleArrival(key);
      }
    },
    pin(key, position) {
      if (destroyed) return null;
      const node = byKey.get(key);
      if (!node) return null;
      if (key === seedKey) return { x: 0, y: 0 };
      if (position !== undefined && !finitePoint(position)) return null;
      // An explicit pin ends any arrival wait.
      arrivals.delete(key);
      if (key === draggingKey) {
        // The drag owns its point until release (as for unpin and clearPins):
        // record the pin, never pull the dragged node back to another point.
        persistentPins.add(key);
        return { x: node.x, y: node.y };
      }
      const point = position ?? { x: node.x, y: node.y };
      if (persistentPins.has(key) && node.fx === point.x && node.fy === point.y) return { ...point };
      setPosition(node, point);
      persistentPins.add(key);
      afterChange();
      return { ...point };
    },
    unpin(key) {
      if (destroyed || key === seedKey) return;
      arrivals.delete(key);
      const node = byKey.get(key);
      if (!node || !persistentPins.delete(key)) return;
      if (draggingKey !== key) {
        node.fx = node.fy = null;
        afterChange();
      }
    },
    clearPins() {
      if (destroyed) return;
      arrivals.clear();
      let changed = false;
      for (const key of persistentPins) {
        if (key === seedKey) continue;
        persistentPins.delete(key);
        const node = byKey.get(key)!;
        if (draggingKey !== key) node.fx = node.fy = null;
        changed = true;
      }
      if (changed) afterChange();
    },
    beginDrag(key, position) {
      if (destroyed) return null;
      const node = byKey.get(key);
      if (!node) return null;
      if (key === seedKey) return { x: 0, y: 0 };
      if (position !== undefined && !finitePoint(position)) return null;
      if (draggingKey !== null && draggingKey !== key) this.endDrag(draggingKey);
      draggingKey = key;
      const point = position ?? { x: node.x, y: node.y };
      setPosition(node, point);
      simulation.alphaTarget(LOCAL_LAYOUT_PARAMS.dragAlphaTarget);
      if (reducedMotion || paused) notify();
      else startLive(LOCAL_LAYOUT_PARAMS.dragAlphaTarget);
      return { ...point };
    },
    moveDrag(key, position) {
      if (destroyed || draggingKey !== key || !finitePoint(position)) return null;
      const node = byKey.get(key)!;
      setPosition(node, position);
      notify();
      return { ...position };
    },
    endDrag(key) {
      if (destroyed || draggingKey !== key) return;
      draggingKey = null;
      const node = byKey.get(key)!;
      if (!persistentPins.has(key)) node.fx = node.fy = null;
      simulation.alphaTarget(0);
      if (reducedMotion && !paused) settleDragWithoutMotion();
      else afterChange();
    },
    setReducedMotion(enabled) {
      if (destroyed || reducedMotion === enabled) return;
      reducedMotion = enabled;
      lastFrameAt = null;
      if (enabled) {
        simulation.stop().alphaTarget(0);
        notify();
        if (!paused) settleArrivals();
      } else {
        startLive();
      }
    },
  };
}
