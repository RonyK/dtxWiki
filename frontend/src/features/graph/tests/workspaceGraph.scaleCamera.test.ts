import { beforeEach, describe, expect, it } from "vitest";

import { workspaceGraphStorageScope } from "../../../shared/lib/workspaceGraphStorage";
import {
  freshWorkspaceGraphState,
  loadWorkspaceGraphState,
  saveWorkspaceGraphState,
  type WorkspaceGraphRestorableState,
} from "../workspaceGraphModel";

const WORKSPACE = "workspace-one";
const ACTOR = "actor-one";
const REVISION = "revision-0000000000000001";

// Real Cosmos fits: the 2K CPU layout spans ~10^5 units (zoom far below the old
// 0.05 floor) while the native 10K layout spans ~10^3 units around (2048, 2048).
const CAMERA_2K = { zoom: 0.004117, pan: { x: -1526.33, y: 5778.56 } };
const CAMERA_10K = { zoom: 0.55379, pan: { x: 2078.7, y: 2206.5 } };

type View = Pick<WorkspaceGraphRestorableState, "viewport" | "pinnedIds" | "pinnedPositions">;
const VIEW_2K: View = { viewport: CAMERA_2K, pinnedIds: ["context:one"], pinnedPositions: { "context:one": { x: -40_000, y: 61_000 } } };
const VIEW_10K: View = { viewport: CAMERA_10K, pinnedIds: ["library:two"], pinnedPositions: { "library:two": { x: 2100, y: 2300 } } };
const NO_VIEW: View = { viewport: null, pinnedIds: [], pinnedPositions: {} };

function stateWith(view: View): WorkspaceGraphRestorableState {
  return { ...freshWorkspaceGraphState(), selectedId: "context:one", viewportEngine: "cosmos", viewerId: ACTOR, ...view };
}

function viewOf(state: WorkspaceGraphRestorableState): View {
  return { viewport: state.viewport, pinnedIds: state.pinnedIds, pinnedPositions: state.pinnedPositions };
}

const save = (view: View, scale: "2k" | "10k") => saveWorkspaceGraphState(WORKSPACE, ACTOR, REVISION, stateWith(view), scale);
const load = (scale: "2k" | "10k") => loadWorkspaceGraphState(WORKSPACE, ACTOR, REVISION, undefined, scale);
const key = `${workspaceGraphStorageScope(WORKSPACE, ACTOR)}${encodeURIComponent(REVISION)}`;

describe("scale-keyed Workspace Map camera and pins", () => {
  beforeEach(() => window.sessionStorage.clear());

  it.each([["2k", "10k", VIEW_2K], ["10k", "2k", VIEW_10K]] as const)(
    "never applies a camera or pin captured under %s to the %s scene", (captured, other, view) => {
      save(view, captured);
      const restored = load(other);
      expect(viewOf(restored)).toEqual(NO_VIEW);
      // Selection is scale-neutral: it still follows the user.
      expect(restored.selectedId).toBe("context:one");
    },
  );

  it("restores the same-scale camera exactly, including a 2K fit zoom far below the old 0.05 floor", () => {
    save(VIEW_2K, "2k");
    expect(viewOf(load("2k"))).toEqual(VIEW_2K);
  });

  it("keeps each scale's view through a visit to the other (2K -> 10K -> 2K)", () => {
    save(VIEW_2K, "2k");
    // The 10K scene starts without a view, then frames itself and is saved.
    save(NO_VIEW, "10k");
    save(VIEW_10K, "10k");
    expect(viewOf(load("2k"))).toEqual(VIEW_2K);
    expect(viewOf(load("10k"))).toEqual(VIEW_10K);
    // Returning to 2K and saving there must not erase the parked 10K view either.
    save(VIEW_2K, "2k");
    expect(viewOf(load("10k"))).toEqual(VIEW_10K);
  });

  it("loads a record written before scale keying as 2K, never as 10K, and parks it when 10K saves over it", () => {
    // Exactly what the previous saveWorkspaceGraphState wrote: no scale anywhere.
    const serialized = JSON.stringify({ revision: REVISION, state: stateWith(VIEW_2K) });
    window.sessionStorage.setItem(key, serialized);
    window.sessionStorage.setItem(`${workspaceGraphStorageScope(WORKSPACE, ACTOR)}latest`, serialized);
    expect(viewOf(load("2k"))).toEqual(VIEW_2K);
    expect(viewOf(load("10k"))).toEqual(NO_VIEW);
    save(VIEW_10K, "10k");
    expect(viewOf(load("2k"))).toEqual(VIEW_2K);
    expect(viewOf(load("10k"))).toEqual(VIEW_10K);
  });

  it("ignores damaged or unknown parked scale data without losing the scale that wrote the record", () => {
    window.sessionStorage.setItem(key, JSON.stringify({
      revision: REVISION, mapScale: "2k", state: stateWith(VIEW_2K),
      scaleViews: {
        "10k": { viewport: { zoom: "bad", pan: { x: 1, y: 2 } }, pinnedIds: ["library:two", 7], pinnedPositions: { "library:two": { x: "bad", y: 1 } } },
        "50k": { viewport: CAMERA_10K, pinnedIds: ["x"], pinnedPositions: {} },
      },
    }));
    expect(load("10k").viewport).toBeNull();
    expect(viewOf(load("2k"))).toEqual(VIEW_2K);
  });
});
