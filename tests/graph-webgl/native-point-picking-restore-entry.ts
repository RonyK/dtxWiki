import { createWorkspaceNativePointPicker } from "../../frontend/src/features/graph/workspace/nativePointPicker";
import type { CosmosPointPickFrame } from "../../frontend/src/features/graph/workspace/cosmosPointPickingTypes";

// The picker alone on a bare WebGL2 context: a loss and restore of the SAME context object, with no renderer
// that would detach on loss and remount on restore. Three nodes on one row, 100 CSS pixels apart.
const canvas = document.querySelector("canvas")!;
const gl = canvas.getContext("webgl2")!;
const errors: string[] = [];
const sizes = Float32Array.from([6, 6, 6]), shapes = new Float32Array(3);
const picker = createWorkspaceNativePointPicker(sizes, shapes, error => errors.push(String(error)));
const nodes = [[2048, 2048], [2148, 2048], [1948, 2048]] as const;
let loseContext = gl.getExtension("WEBGL_lose_context")!;
let frame: CosmosPointPickFrame | null = null;
// Every program and framebuffer the picker makes, so a restore can be shown to have invalidated them.
const made = { programs: [] as WebGLProgram[], framebuffers: [] as WebGLFramebuffer[] };
const createProgram = gl.createProgram.bind(gl), createFramebuffer = gl.createFramebuffer.bind(gl);
gl.createProgram = () => { const handle = createProgram(); if (handle) made.programs.push(handle); return handle; };
gl.createFramebuffer = () => { const handle = createFramebuffer(); if (handle) made.framebuffers.push(handle); return handle; };

/** Resources the picker borrows belong to the host, so a restored context needs new ones; `floatTargets` is the host asking for EXT_color_buffer_float again. */
function fresh(floatTargets = true) {
  if (floatTargets) gl.getExtension("EXT_color_buffer_float");
  const texture = (width: number, height: number, data: Float32Array) => {
    const handle = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, handle);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, width, height, 0, gl.RGBA, gl.FLOAT, data);
    return handle;
  };
  const positions = new Float32Array(16);
  nodes.forEach((node, index) => positions.set([node[0], node[1], index, 0], index * 4));
  frame = { gl, positionTexture: texture(2, 2, positions), pointStatusTexture: texture(2, 2, new Float32Array(16)),
    exitTexture: texture(1, 1, new Float32Array(4)), textureSize: { width: 2, height: 2 }, nodeCount: 3,
    transformationMatrix: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], screenSize: [600, 400], pointer: [300, 200],
    spaceSize: 4096, pointSizeScale: 1, scalePointsOnZoom: false, maxPointSize: 64, pixelRatio: 1, pointOpacity: 1, pointGreyoutOpacity: null };
}
/** Top-left CSS pixels of node `index` on this canvas. */
const screenOf = (index: number): [number, number] => [300 + nodes[index][0] - 2048, 200 + nodes[index][1] - 2048];
const at = (x: number, y: number): CosmosPointPickFrame => ({ ...frame!, pointer: [x, 400 - y] });
/** GL errors raised since the last call. */
function drain() {
  const raised: number[] = [];
  for (let error = gl.getError(); error !== gl.NO_ERROR && raised.length < 16; error = gl.getError()) raised.push(error);
  return raised;
}

async function lose() {
  const lost = new Promise<void>(resolve => canvas.addEventListener("webglcontextlost", event => { event.preventDefault(); resolve(); }, { once: true }));
  loseContext.loseContext(); await lost;
  // restoreContext() is refused until the event dispatch that cancelled the loss has finished.
  await new Promise(resolve => setTimeout(resolve, 0));
  return gl.isContextLost();
}
async function restore() {
  const restored = new Promise<void>(resolve => canvas.addEventListener("webglcontextrestored", () => resolve(), { once: true }));
  loseContext.restoreContext(); await restored;
  loseContext = gl.getExtension("WEBGL_lose_context")!;
  return !gl.isContextLost();
}

declare global {
  interface Window { __nativeRestore: {
    fresh: typeof fresh;
    screenOf: typeof screenOf;
    lose: typeof lose;
    restore: typeof restore;
    drain: typeof drain;
    pick(x: number, y: number): number | null;
    request(x: number, y: number): boolean;
    take(x: number, y: number): { outcome: "pending-or-obsolete" | "no-point" | "hit"; index: number | null; hasPending: boolean };
    errors(): string[];
    alive(): { programs: boolean[]; framebuffers: boolean[] };
  }; }
}

fresh();
window.__nativeRestore = {
  fresh, screenOf, lose, restore, drain,
  pick: (x, y) => picker.pickSync(at(x, y))?.index ?? null,
  request: (x, y) => picker.request(at(x, y)),
  take(x, y) {
    const result = picker.take(at(x, y));
    return { outcome: result === undefined ? "pending-or-obsolete" : result === null ? "no-point" : "hit", index: result?.index ?? null, hasPending: picker.hasPending };
  },
  errors: () => errors.slice(),
  alive: () => ({ programs: made.programs.map(handle => gl.isProgram(handle)), framebuffers: made.framebuffers.map(handle => gl.isFramebuffer(handle)) }),
};
