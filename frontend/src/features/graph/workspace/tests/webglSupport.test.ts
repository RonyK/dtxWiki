import { afterEach, describe, expect, it, vi } from "vitest";

import { detectWorkspaceMapWebGL } from "../webglSupport";

const originalGetContext = HTMLCanvasElement.prototype.getContext;

afterEach(() => { HTMLCanvasElement.prototype.getContext = originalGetContext; });

describe("Workspace Map WebGL fallback probe", () => {
  it("reports a missing WebGL2 context without constructing the graph", () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as typeof originalGetContext;
    expect(detectWorkspaceMapWebGL()).toEqual({ supported: false, reason: "webgl2" });
  });

  it("requires float render targets and releases the probe context", () => {
    const loseContext = vi.fn();
    HTMLCanvasElement.prototype.getContext = vi.fn(() => ({
      getExtension: (name: string) => name === "WEBGL_lose_context" ? { loseContext } : null,
    })) as unknown as typeof originalGetContext;
    expect(detectWorkspaceMapWebGL()).toEqual({ supported: false, reason: "float-render-target" });
    expect(loseContext).toHaveBeenCalledOnce();
  });
});
