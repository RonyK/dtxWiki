import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sameGraphVisibleIdentity, type GraphRenderScene } from "../../shared/rendererTypes";
import type { CosmosRendererCallbacks } from "../../shared/cosmos/cosmosCore";
import type { CosmosSceneColumns } from "../../shared/cosmos/sceneTypes";
import { createLocalGraphAdapter, type LocalGraphAdapter } from "../cosmosAdapter";

const state = vi.hoisted(() => {
  const positions = new Map<string, { x: number; y: number }>();
  const core = {
    captureCoordinates: vi.fn(() => null),
    centerOn: vi.fn(), destroy: vi.fn(), fit: vi.fn(), focus: vi.fn(),
    getNodePosition: vi.fn((id: string) => positions.get(id) ?? null),
    getViewport: vi.fn(() => ({ zoom: 2, pan: { x: 3, y: 4 } })),
    getNodeDisplayData: vi.fn(() => ({ x: 30, y: 40, radius: 8, shape: 0 })),
    pin: vi.fn((id: string, pinned: boolean, point?: { x: number; y: number }) => {
      const next = point ?? positions.get(id) ?? { x: 4, y: 8 };
      if (pinned) positions.set(id, { ...next }); else positions.delete(id);
      return next;
    }),
    pinWhenSettled: vi.fn(), replaceColumns: vi.fn((_columns: CosmosSceneColumns) => undefined), reset: vi.fn(), resize: vi.fn(),
    resumeLayout: vi.fn(), select: vi.fn(), setLabelsVisible: vi.fn(),
    setViewport: vi.fn(), stopLayout: vi.fn(), whenSettled: vi.fn(() => Promise.resolve()), zoomBy: vi.fn(),
  };
  return { core, positions, createRenderer: vi.fn((_container: HTMLElement, _columns: CosmosSceneColumns, _options: unknown) => core), setTracing: vi.fn() };
});

vi.mock("../../shared/cosmos/cosmosCore", () => ({ createCosmosRenderer: state.createRenderer }));
vi.mock("../cosmosBinding", () => ({ createLocalCosmosBinding: () => ({ setTracing: state.setTracing }) }));
vi.mock("../cosmosLayout", () => ({ createLocalCosmosLayoutSolver: () => ({}) }));

const initialScene = (): GraphRenderScene => ({
  scopeKey: "same-query",
  currentId: "seed",
  nodes: ["seed", "neighbor", "other"].map(id => ({
    id, entity_id: id, entity_kind: "context_page", family: "document", label: id, is_seed: id === "seed",
  })),
  edges: [{ id: "relation", source: "seed", target: "neighbor", edge_kind: "wikilink", label: "link", layer: "knowledge" }],
});
const callbacks = () => ({ onHover: vi.fn(), onSelect: vi.fn(), onPin: vi.fn(), onViewport: vi.fn(), onContextStatus: vi.fn() });
let adapter: LocalGraphAdapter;
let host: HTMLDivElement;
let events: ReturnType<typeof callbacks>;
const rendererOptions = () => (state.createRenderer.mock.calls.at(-1) as unknown as [HTMLElement, unknown, { callbacks: CosmosRendererCallbacks }])[2];

beforeEach(async () => {
  vi.clearAllMocks(); state.positions.clear();
  state.core.whenSettled.mockImplementation(() => Promise.resolve());
  host = document.createElement("div"); host.append(document.createElement("canvas"));
  events = callbacks();
  adapter = createLocalGraphAdapter(host, initialScene(), {
    callbacks: events, forcedColors: false, positions: {}, reducedMotion: true, ownerKey: "authority-owner",
  });
  await adapter.whenSettled();
});
afterEach(() => { adapter.destroy(); vi.useRealTimers(); });

// Pointer picking, glyph pixels, label placement and GPU lifecycle are covered
// by the shared Cosmos tests and the migrated Local WebGL cases. These cases
// retain the Local orchestration failures that those renderer tests cannot see.
describe("Local Cosmos scene and interaction ownership", () => {
  it("keeps original relationship IDs, endpoint order and wire direction in the shared renderer", () => {
    const next = initialScene();
    next.edges[0] = { ...next.edges[0], source: "neighbor", target: "seed", direction: "undirected" };
    adapter.replaceScene(next);
    const columns = state.core.replaceColumns.mock.calls.at(-1)![0];
    expect(columns.edgeIds).toEqual(["relation"]);
    expect(columns.edgeDirections).toEqual(["undirected"]);
    expect(columns.nodeIds[columns.edgeSources[0]]).toBe("neighbor");
    expect(columns.nodeIds[columns.edgeTargets[0]]).toBe("seed");
  });

  it("retains live pins and camera ownership through metadata and same-query topology changes", async () => {
    adapter.pin("neighbor", true, { x: 31, y: 37 });
    const next = initialScene(); next.nodes[1].label = "Localized neighbor";
    state.core.pin.mockClear();
    expect(adapter.replaceScene(next)).toBe("metadata");
    expect(state.core.pin).not.toHaveBeenCalled();
    const arrival = { ...next, nodes: [...next.nodes, { ...next.nodes[1], id: "arrival", entity_id: "arrival" }] };
    expect(adapter.replaceScene(arrival)).toBe("topology");
    await adapter.whenSettled();
    expect(state.core.pin).not.toHaveBeenCalled();
    expect(state.positions.get("neighbor")).toEqual({ x: 31, y: 37 });
    expect(state.core.reset).not.toHaveBeenCalled();
    expect(state.core.setViewport).not.toHaveBeenCalled();
    expect(state.createRenderer).toHaveBeenCalledOnce();
  });

  it("rebuilds a changed selection or ambiguous semantic aliases instead of reusing a layout", () => {
    expect(adapter.replaceScene({ ...initialScene(), scopeKey: "new-query" })).toBe("rebuild");
    const ambiguous = initialScene();
    ambiguous.nodes.push({ ...ambiguous.nodes[1], id: "alias" });
    expect(adapter.replaceScene(ambiguous)).toBe("rebuild");
    const columns = state.core.replaceColumns.mock.calls.at(-1)![0];
    expect(columns.localGeometryKey).toBeNull();
    expect(columns.identity.selectionKey).not.toBe("same-query");
    // The same ambiguous wire/semantic aliases cannot retain renderer state
    // merely because the Context query scope itself has not changed.
    expect(adapter.replaceScene({ ...ambiguous, nodes: ambiguous.nodes.map(node => ({ ...node, label: `New ${node.label}` })) }))
      .toBe("rebuild");
    const repeated = state.core.replaceColumns.mock.calls.at(-1)![0];
    expect(repeated.localGeometryKey).toBeNull();
    expect(sameGraphVisibleIdentity(columns.identity, repeated.identity)).toBe(false);
    expect(repeated.identity.actorId).toBe(columns.identity.actorId);
    expect(repeated.identity.workspaceId).toBe(columns.identity.workspaceId);
  });

  it("uses the latest explicit pin and never revives a released restore coordinate", async () => {
    adapter.destroy();
    adapter = createLocalGraphAdapter(host, initialScene(), {
      callbacks: events, forcedColors: false, positions: { neighbor: { x: 1, y: 2 }, other: { x: 3, y: 4 } }, reducedMotion: true,
    });
    await adapter.whenSettled();
    adapter.pin("neighbor", false);
    adapter.pin("other", true, { x: 70, y: 80 });
    state.core.pin.mockClear();
    const next = initialScene(); next.edges[0].count = 2;
    adapter.replaceScene(next); await adapter.whenSettled();
    expect(state.core.pin).not.toHaveBeenCalled();
    expect(state.positions.has("neighbor")).toBe(false);
    expect(state.positions.get("other")).toEqual({ x: 70, y: 80 });
  });

  it("retains the final pointer-drag endpoint rather than a stale saved pin during subsequent arrivals", async () => {
    adapter.pin("neighbor", true, { x: 1, y: 2 });
    state.positions.set("neighbor", { x: 130, y: -45 });
    rendererOptions().callbacks.onPin!("neighbor", { x: 130, y: -45 });
    expect(events.onPin).toHaveBeenCalledExactlyOnceWith("neighbor", { x: 130, y: -45 });
    state.core.pin.mockClear();
    const next = initialScene(); next.nodes = next.nodes.slice(0, 2);
    adapter.replaceScene(next); await adapter.whenSettled();
    expect(state.core.pin).not.toHaveBeenCalled();
    // The core already owns the live drag row; no saved coordinate is replayed.
    expect(state.core.replaceColumns).toHaveBeenCalledOnce();
    expect(state.positions.get("neighbor")).toEqual({ x: 130, y: -45 });
  });

  it("queues an absent arrival once and reports only its settled position", async () => {
    expect(adapter.pin("arrival", true)).toBeNull();
    expect(adapter.pin("arrival", true)).toBeNull();
    expect(events.onPin).not.toHaveBeenCalled();
    const next = initialScene(); next.nodes.push({ ...next.nodes[1], id: "arrival", entity_id: "arrival" });
    adapter.replaceScene(next); await adapter.whenSettled();
    expect(state.core.pinWhenSettled).toHaveBeenCalledExactlyOnceWith("arrival");
    rendererOptions().callbacks.onPin!("arrival", { x: 21, y: 34 });
    expect(events.onPin).toHaveBeenCalledExactlyOnceWith("arrival", { x: 21, y: 34 });
  });

  it("forgets an explicitly unpinned absent arrival before it reappears", async () => {
    adapter.pin("arrival", true); adapter.pin("arrival", false);
    const next = initialScene(); next.nodes.push({ ...next.nodes[1], id: "arrival", entity_id: "arrival" });
    adapter.replaceScene(next); await adapter.whenSettled();
    expect(state.core.pinWhenSettled).not.toHaveBeenCalled();
    expect(events.onPin).not.toHaveBeenCalled();
  });

  it("reset clears all remembered and pending pins with one core reset", async () => {
    adapter.pin("neighbor", true, { x: 5, y: 6 }); adapter.pin("arrival", true);
    state.core.pin.mockClear(); adapter.reset();
    const next = initialScene(); next.nodes.push({ ...next.nodes[1], id: "arrival", entity_id: "arrival" });
    adapter.replaceScene(next); await adapter.whenSettled();
    expect(state.core.reset).toHaveBeenCalledOnce();
    expect(state.core.pin).not.toHaveBeenCalled();
    expect(state.core.pinWhenSettled).not.toHaveBeenCalled();
  });

  it("waits for current Worker readiness and retires deferred navigation superseded by later camera input", async () => {
    const pending: Array<() => void> = [];
    adapter.destroy();
    state.core.whenSettled.mockImplementation(() => new Promise<void>(resolve => pending.push(resolve)));
    adapter = createLocalGraphAdapter(host, initialScene(), {
      callbacks: events, forcedColors: false, positions: {}, reducedMotion: true, initialPaused: true,
    });
    let completed = false;
    const completion = adapter.whenSettled().then(() => { completed = true; });
    const next = initialScene(); next.nodes = next.nodes.slice(0, 2);
    adapter.replaceScene(next);
    pending[0](); await Promise.resolve(); await Promise.resolve();
    expect(completed).toBe(false);
    pending[1](); await completion;
    expect(completed).toBe(true);
    expect(host.querySelector("canvas")?.hasAttribute("data-local-graph-canvas")).toBe(true);
    state.core.whenSettled.mockImplementation(() => Promise.resolve());
    const deferred = () => {
      let finish!: () => void;
      state.core.whenSettled.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
      adapter.locate("seed");
      return async () => { finish(); await Promise.resolve(); await Promise.resolve(); };
    };
    // Positive control: the asynchronous command really centers once ready.
    const finish = deferred();
    expect(state.core.centerOn).not.toHaveBeenCalled();
    await finish();
    expect(state.core.centerOn).toHaveBeenCalledExactlyOnceWith("seed");
    state.core.centerOn.mockClear();
    // Five explicit input paths, one deferred-command failure mechanism.
    const supersede = [
      () => adapter.setViewport({ zoom: 3, pan: { x: 10, y: 20 } }),
      () => adapter.zoomBy(1.2),
      ...["pointerdown", "wheel", "touchstart"].map(type => () => host.dispatchEvent(new Event(type, { bubbles: true }))),
    ];
    for (const input of supersede) {
      const finish = deferred(); input(); await finish();
      expect(state.core.centerOn).not.toHaveBeenCalled();
    }
  });

  it("does not publish late hover, selection, camera or pin callbacks after destroy or loss", async () => {
    const old = rendererOptions().callbacks;
    old.onContextStatus!("lost");
    old.onPin!("neighbor", { x: 100, y: 200 }); old.onSelect("neighbor"); old.onHover("neighbor");
    old.onViewport!({ zoom: 9, pan: { x: 1, y: 2 } });
    expect(events.onPin).not.toHaveBeenCalled(); expect(events.onSelect).not.toHaveBeenCalled();
    expect(events.onHover).not.toHaveBeenCalled(); expect(events.onViewport).not.toHaveBeenCalled();
    expect(events.onContextStatus).toHaveBeenCalledExactlyOnceWith("lost");
    adapter.destroy(); old.onContextStatus!("restored");
    expect(events.onContextStatus).toHaveBeenCalledOnce();
    expect(adapter.captureCoordinates()).toBeNull();
  });

  it("keeps a stable authority owner for healthy remount snapshots and separates other owners", async () => {
    const columns = (state.createRenderer.mock.calls[0] as unknown as [HTMLElement, { identity: unknown }])[1];
    expect(columns.identity).toMatchObject({ workspaceId: "authority-owner", actorId: "authority-owner", selectionKey: "same-query" });
    adapter.destroy();
    adapter = createLocalGraphAdapter(host, initialScene(), { callbacks: events, forcedColors: false,
      positions: {}, reducedMotion: true, ownerKey: "different-owner" });
    await adapter.whenSettled();
    expect((state.createRenderer.mock.calls.at(-1) as unknown as [HTMLElement, { identity: unknown }])[1].identity)
      .toMatchObject({ workspaceId: "different-owner", actorId: "different-owner" });
  });

  it("traces only the current document when explicitly located and stops the trace on reset", async () => {
    adapter.destroy();
    adapter = createLocalGraphAdapter(host, initialScene(), { callbacks: events, forcedColors: false,
      positions: {}, reducedMotion: false });
    adapter.locate("neighbor");
    await adapter.whenSettled();
    expect(host.querySelector(".graph-locate-trace")).toBeNull();
    adapter.locate("seed");
    await adapter.whenSettled();
    expect(host.querySelector(".graph-locate-trace circle")).toHaveAttribute("cx", "30");
    expect(state.setTracing).toHaveBeenLastCalledWith("seed");
    adapter.reset(); expect(host.querySelector(".graph-locate-trace")).toBeNull();
    expect(state.core.centerOn).toHaveBeenCalledWith("seed");
  });
});
