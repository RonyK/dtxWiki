import { workspaceForceAtlas2VertexShader } from "./forceAtlas2Shaders";

type Dimensions = Readonly<{ width: number; height: number }>;

export interface WorkspaceForceAtlas2GpuTransfer {
  /** Copy contact output into numerical state, retaining its force channels. */
  adopt(source: WebGLTexture, sourceSize: Dimensions, target: WebGLTexture, targetSize: Dimensions): void;
  /** Native RGBA is [x,y,index,0], never the numerical force channels. */
  publish(source: WebGLTexture, sourceSize: Dimensions, target: WebGLFramebuffer,
    nativeSize: Dimensions, previousNative: WebGLTexture, nativePins: WebGLTexture, draggingIndex?: number | null): void;
  /** Initial upload / explicit drag: reset only imported nodes' force history. */
  importNative(source: WebGLTexture, nativeSize: Dimensions, target: WebGLTexture,
    modelSize: Dimensions, index?: number): void;
  destroy(): void;
}

const fragment = `#version 300 es
precision highp float;
precision highp int;
uniform sampler2D sourceTexture;
uniform sampler2D previousNativeTexture;
uniform sampler2D nativePinsTexture;
uniform int sourceWidth;
uniform int targetWidth;
uniform int nodeCount;
uniform int mode;
uniform int draggingIndex;
uniform vec2 origin;
out vec4 outputState;
void main() {
  ivec2 target = ivec2(gl_FragCoord.xy);
  int index = target.y * targetWidth + target.x;
  if (index >= nodeCount) { outputState = vec4(0.0); return; }
  ivec2 source = ivec2(index % sourceWidth, index / sourceWidth);
  vec4 value = texelFetch(sourceTexture, source, 0);
  if (mode == 0) outputState = value;
  else if (mode == 1) {
    bool pinned = index == draggingIndex || texelFetch(nativePinsTexture, target, 0).r > 0.5;
    vec2 position = pinned ? texelFetch(previousNativeTexture, target, 0).xy : value.xy + origin;
    outputState = vec4(position, float(index), 0.0);
  } else outputState = vec4(value.xy - origin, 0.0, 0.0);
}`;

/**
 * GPU-only format conversion. Borrows external textures/FBOs and owns only its
 * program, VAO and attachment FBO. Caller wraps creation/calls with the shared
 * GL guard. Does not change the camera, create a scheduler or read coordinates.
 */
export function createWorkspaceForceAtlas2GpuTransfer(
  gl: WebGL2RenderingContext, nodeCount: number, origin: readonly [number, number],
): WorkspaceForceAtlas2GpuTransfer {
  if (!Number.isSafeInteger(nodeCount) || nodeCount <= 0 || nodeCount > 2 ** 24
    || origin.length !== 2 || !origin.every(value => Number.isFinite(value) && Number.isFinite(Math.fround(value)))) {
    throw new Error("Invalid ForceAtlas2 GPU transfer columns or origin");
  }
  const shaders: WebGLShader[] = [];
  let program: WebGLProgram | null = null, vao: WebGLVertexArrayObject | null = null;
  let framebuffer: WebGLFramebuffer | null = null, destroyed = false;
  const locations = new Map<string, WebGLUniformLocation>();
  function cleanup() {
    for (const shader of shaders) gl.deleteShader(shader);
    if (program) gl.deleteProgram(program);
    if (vao) gl.deleteVertexArray(vao);
    if (framebuffer) gl.deleteFramebuffer(framebuffer);
  }
  try {
    for (const [type, source] of [[gl.VERTEX_SHADER, workspaceForceAtlas2VertexShader],
      [gl.FRAGMENT_SHADER, fragment]] as const) {
      const shader = gl.createShader(type);
      if (!shader) throw new Error("ForceAtlas2 transfer shader allocation failed");
      shaders.push(shader); gl.shaderSource(shader, source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error(`ForceAtlas2 transfer compilation failed: ${gl.getShaderInfoLog(shader)}`);
      }
    }
    program = gl.createProgram(); vao = gl.createVertexArray(); framebuffer = gl.createFramebuffer();
    if (!program || !vao || !framebuffer) throw new Error("ForceAtlas2 transfer allocation failed");
    for (const shader of shaders) gl.attachShader(program, shader);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(`ForceAtlas2 transfer link failed: ${gl.getProgramInfoLog(program)}`);
    for (const name of ["sourceTexture", "previousNativeTexture", "nativePinsTexture", "sourceWidth", "targetWidth", "nodeCount", "mode", "draggingIndex", "origin"]) {
      const location = gl.getUniformLocation(program, name);
      if (location === null) throw new Error(`ForceAtlas2 transfer uniform missing: ${name}`);
      locations.set(name, location);
    }
  } catch (error) { cleanup(); throw error; }

  function requireDimensions(size: Dimensions) {
    if (!Number.isInteger(size.width) || !Number.isInteger(size.height)
      || size.width <= 0 || size.height <= 0 || size.width * size.height < nodeCount) {
      throw new Error("ForceAtlas2 transfer texture dimensions disagree with original rows");
    }
  }
  function copy(source: WebGLTexture, sourceSize: Dimensions, target: WebGLFramebuffer,
    targetSize: Dimensions, mode: number, previous?: WebGLTexture, pins?: WebGLTexture, index?: number, draggingIndex = -1) {
    if (destroyed || gl.isContextLost()) throw new Error("ForceAtlas2 transfer is destroyed or context lost");
    requireDimensions(sourceSize); requireDimensions(targetSize);
    if (index !== undefined && (!Number.isInteger(index) || index < 0 || index >= nodeCount)) {
      throw new Error("ForceAtlas2 drag row is outside the original columns");
    }
    gl.useProgram(program); gl.bindVertexArray(vao);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, target);
    if (gl.checkFramebufferStatus(gl.DRAW_FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error("ForceAtlas2 transfer target is incomplete");
    gl.viewport(0, 0, targetSize.width, targetSize.height);
    for (const flag of [gl.BLEND, gl.DEPTH_TEST, gl.STENCIL_TEST, gl.SCISSOR_TEST, gl.CULL_FACE, gl.RASTERIZER_DISCARD]) gl.disable(flag);
    gl.colorMask(true, true, true, true);
    gl.uniform1i(locations.get("sourceTexture")!, 0);
    gl.uniform1i(locations.get("previousNativeTexture")!, 1);
    gl.uniform1i(locations.get("nativePinsTexture")!, 2);
    gl.uniform1i(locations.get("sourceWidth")!, sourceSize.width);
    gl.uniform1i(locations.get("targetWidth")!, targetSize.width);
    gl.uniform1i(locations.get("nodeCount")!, nodeCount);
    gl.uniform1i(locations.get("mode")!, mode);
    gl.uniform1i(locations.get("draggingIndex")!, draggingIndex);
    gl.uniform2f(locations.get("origin")!, origin[0], origin[1]);
    [source, previous ?? source, pins ?? source].forEach((texture, unit) => {
      gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, texture);
    });
    // A one-row drag writes a single texel in both histories. It never samples
    // the attached destination texture, so there is no feedback loop.
    if (index !== undefined) {
      gl.enable(gl.SCISSOR_TEST);
      gl.scissor(index % targetSize.width, Math.floor(index / targetSize.width), 1, 1);
    }
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.disable(gl.SCISSOR_TEST);
    const error = gl.getError();
    if (error !== gl.NO_ERROR) throw new Error(`ForceAtlas2 GPU transfer error ${error}`);
  }
  function attach(texture: WebGLTexture): WebGLFramebuffer {
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, framebuffer);
    gl.framebufferTexture2D(gl.DRAW_FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
    return framebuffer!;
  }
  return {
    adopt(source, sourceSize, target, targetSize) {
      if (source === target) throw new Error("ForceAtlas2 transfer must not sample its target");
      copy(source, sourceSize, attach(target), targetSize, 0);
    },
    publish(source, sourceSize, target, nativeSize, previous, pins, draggingIndex) {
      copy(source, sourceSize, target, nativeSize, 1, previous, pins, undefined, draggingIndex ?? -1);
    },
    importNative(source, nativeSize, target, modelSize, index) {
      if (source === target) throw new Error("ForceAtlas2 native import must not sample its target");
      // The guard owns scissor enable state; this helper also preserves its box.
      const box = gl.getParameter(gl.SCISSOR_BOX) as Int32Array;
      try { copy(source, nativeSize, attach(target), modelSize, 2, undefined, undefined, index); }
      finally { gl.scissor(box[0], box[1], box[2], box[3]); }
    },
    destroy() { if (destroyed) return; destroyed = true; cleanup(); },
  };
}
