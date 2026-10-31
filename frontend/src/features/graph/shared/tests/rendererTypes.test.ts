import { describe, expect, it } from "vitest";

import {
  sameGraphVisibleIdentity,
  type GraphRendererAdapter,
  type GraphRendererEvents,
  type GraphVisibleIdentity,
} from "../rendererTypes";

const visibleIdentity: GraphVisibleIdentity = {
  workspaceId: "workspace-a",
  actorId: "actor-a",
  surface: "local",
  selectionKey: "seed-1:depth-2",
  visibleFingerprint: "visible-input-hash",
};

describe("Graph renderer contract", () => {
  it("requires actor, visibility, surface and selection identity to match", () => {
    expect(sameGraphVisibleIdentity(visibleIdentity, { ...visibleIdentity })).toBe(true);
    for (const changed of [
      { workspaceId: "workspace-b" },
      { actorId: "actor-b" },
      { surface: "workspace" as const },
      { selectionKey: "seed-1:depth-1" },
      { visibleFingerprint: "different-visible-input" },
    ]) {
      expect(sameGraphVisibleIdentity(visibleIdentity, { ...visibleIdentity, ...changed }))
        .toBe(false);
    }
  });

  it("types a cancellable renderer lifecycle without a Cytoscape camera", async () => {
    type Camera = { x: number; y: number; ratio: number };
    type Scene = { columns: Float32Array };
    const observed: string[] = [];
    let emitSelect: GraphRendererEvents<Camera>["onSelect"] = () => {};
    const adapter: GraphRendererAdapter<Scene, Camera> = {
      ready: async (signal) => {
        if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      },
      load: (_scene, identity) => { observed.push(identity.actorId); },
      update: (_scene, identity) => { observed.push(identity.visibleFingerprint); },
      destroy: () => { observed.push("destroy"); },
      pause: () => { observed.push("pause"); },
      resume: () => { observed.push("resume"); },
      fit: () => { observed.push("fit"); },
      zoomBy: () => { observed.push("zoom"); },
      reset: () => { observed.push("reset"); },
      pin: (_id, position) => position ?? null,
      unpin: () => { observed.push("unpin"); },
      getCamera: () => ({ x: 0, y: 0, ratio: 1 }),
      setCamera: () => { observed.push("camera"); },
      setReducedMotion: () => { observed.push("motion"); },
      subscribe: (events) => {
        emitSelect = events.onSelect;
        return () => { emitSelect = () => {}; };
      },
    };
    const controller = new AbortController();
    await adapter.ready(controller.signal);
    adapter.load({ columns: new Float32Array(0) }, visibleIdentity);
    adapter.subscribe({
      onHover: (id) => { observed.push(`hover:${id}`); },
      onSelect: (id) => { observed.push(`select:${id}`); },
      onCameraChange: () => {},
      onContextStatus: () => {},
    });
    emitSelect(null);
    adapter.destroy();
    expect(observed).toEqual(["actor-a", "select:null", "destroy"]);
    controller.abort();
    await expect(adapter.ready(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
  });
});
