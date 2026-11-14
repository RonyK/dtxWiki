import { describe, expect, it } from "vitest";

import {
  sameGraphVisibleIdentity,
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
});
