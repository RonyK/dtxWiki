import { describe, expect, it } from "vitest";
import { directoryStartIsStructured, workspaceDirectoryStartPositions } from "../directoryStartPositions";

const radius = (rows: Float32Array, index: number) => Math.hypot(rows[index * 2], rows[index * 2 + 1]);
const angle = (rows: Float32Array, index: number) => (Math.atan2(rows[index * 2 + 1], rows[index * 2]) + Math.PI * 2) % (Math.PI * 2);

describe("Map TreeRail start positions", () => {
  it("places deeper containment further out, keeps each branch in its sector and survives deep chains", () => {
    // 0 wraps everything; 1 and 2 are folders; 3 is a document holding 4; 5 and 6 are leaves.
    const keys = ["folder:w", "folder:a", "folder:b", "context:parent", "context:child", "context:x", "context:y"];
    const containers = Int32Array.from([-1, 0, 0, 1, 3, 2, 2]);
    const rows = workspaceDirectoryStartPositions(containers, keys);
    expect(radius(rows, 1)).toBeLessThan(radius(rows, 3));
    expect(radius(rows, 3)).toBeLessThan(radius(rows, 4));
    expect(radius(rows, 2)).toBeLessThan(radius(rows, 5));
    // Branch "a" (folder a, parent, child) ends before branch "b" starts.
    expect(Math.max(angle(rows, 1), angle(rows, 3), angle(rows, 4))).toBeLessThan(Math.min(angle(rows, 2), angle(rows, 5), angle(rows, 6)));
    const chain = Int32Array.from({ length: 30_000 }, (_, index) => index - 1);
    expect(workspaceDirectoryStartPositions(chain, Array.from(chain, String)).every(Number.isFinite)).toBe(true);
  });

  it("calls a start structured only when nodes split into branches, past a dominant container", () => {
    const keys = (count: number) => Array.from({ length: count }, (_, index) => String(index));
    expect(directoryStartIsStructured(Int32Array.from([-1, -1, -1]), keys(3))).toBe(false);
    expect(directoryStartIsStructured(Int32Array.from([-1, 0, 0, 0, 0, -1]), keys(6))).toBe(false);
    expect(directoryStartIsStructured(Int32Array.from([-1, -1, 0, 0, 1, 1]), keys(6))).toBe(true);
    // The same split one level below a container holding most nodes still counts.
    expect(directoryStartIsStructured(Int32Array.from([-1, 0, 0, 1, 1, 2, 2, -1]), keys(8))).toBe(true);
  });
});
