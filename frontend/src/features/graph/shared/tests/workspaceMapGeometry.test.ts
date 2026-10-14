import { describe, expect, it } from "vitest";

import type { WorkspaceMapColumns } from "../../map/sceneColumns";
import { sameWorkspaceMapIndexedGeometry } from "../workspaceMapGeometry";

function columns(): WorkspaceMapColumns {
  return {
    identity: { workspaceId: "w", actorId: "a", surface: "workspace", selectionKey: "links", visibleFingerprint: "visible" },
    nodeIds: ["first", "second"], nodeKeys: ["context:1", "context:2"], nodeLabels: ["First", "Second"],
    nodeFolderKeys: [null, "primary"], nodeKinds: new Uint8Array([0, 0]), nodeSizes: new Float32Array([3, 4]),
    edgeIds: ["edge"], edgeSources: new Uint32Array([0]), edgeTargets: new Uint32Array([1]),
    edgeKinds: new Uint8Array([0]), edgeCounts: new Uint32Array([1]), edgeWeights: new Float64Array([2]),
    edgeCrossFolder: new Uint8Array([1]),
  };
}

describe("Workspace Map indexed geometry policy", () => {
  it("matches cloned rows and ignores display metadata and projection IDs", () => {
    const first = columns(), next = columns();
    expect(next).not.toBe(first);
    next.nodeIds = ["new-first-projection", "new-second-projection"];
    next.edgeIds = ["new-edge-projection"];
    next.nodeLabels = ["Renamed", "Also renamed"];
    next.nodeFolderKeys = ["different-primary", null];
    next.edgeCrossFolder[0] = 0;
    expect(sameWorkspaceMapIndexedGeometry(first, next)).toBe(true);
  });

  it.each(["nodeKeys", "nodeKinds", "nodeSizes", "edgeSources", "edgeTargets",
    "edgeKinds", "edgeCounts", "edgeWeights"] as const)("rejects changed indexed %s independently of the fingerprint", (field) => {
    const first = columns(), next = columns();
    const values = next[field] as { [index: number]: string | number };
    values[0] = typeof values[0] === "string" ? `${values[0]}:changed` : values[0] + 1;
    expect(next.identity).toEqual(first.identity);
    expect(sameWorkspaceMapIndexedGeometry(first, next)).toBe(false);
  });

  it("rejects changed row lengths and semantic row order", () => {
    const first = columns(), next = columns();
    next.nodeKeys.push("context:3");
    expect(sameWorkspaceMapIndexedGeometry(first, next)).toBe(false);
    next.nodeKeys = [...first.nodeKeys].reverse();
    expect(sameWorkspaceMapIndexedGeometry(first, next)).toBe(false);
  });
});
