import type { CosmosPointPickFrame, CosmosPointPicker, CosmosPointPickResult } from "./cosmosPointPickingTypes";
import { withWorkspaceForceAtlas2GlState } from "./forceAtlas2GlState";
import { createWorkspaceGpuReadback, type WorkspaceGpuReadback } from "./gpuReadback";

/** CSS pixels from a point's centre that still target it: the accessible hover and click target of the native map. */
const NATIVE_POINT_TARGET_PX = 12;
/** The shader's squared target, and the same number the CPU re-check applies. */
const TARGET_SQUARED = NATIVE_POINT_TARGET_PX ** 2;

/**
 * A pointer that moved since its query is answered at most this often. Every answer costs a synchronous GPU read and,
 * when the hovered node changes, a rebuild of the highlight on the main thread; a pointer at rest is never held back.
 */
export const NATIVE_MOVING_ANSWER_INTERVAL_MS = 50;
/** A poll at least this slow had the main thread wait on a busy GPU process. */
export const NATIVE_SLOW_READ_MS = 8;
/** After a slow poll, answers for a moved pointer wait this long from its end; a pointer at rest is still read at once. */
export const NATIVE_SLOW_READ_BACKOFF_MS = 300;

/** Vertex shader of the nearest-centre query for a target of `targetPx` CSS pixels; the picker passes NATIVE_POINT_TARGET_PX. */
const nativePointVertexShader = (targetPx: number) => `#version 300 es
precision highp float;
precision highp int;
uniform sampler2D positions;
uniform sampler2D status;
uniform sampler2D exits;
uniform sampler2D geometry;
uniform int textureWidth;
uniform mat4 transform;
uniform vec2 screenSize;
uniform vec2 pointer;
uniform float spaceSize;
uniform float sizeScale;
uniform float zoomSizes;
uniform float maxPointSize;
uniform float pointOpacity;
uniform float greyoutOpacity;
uniform float depthBound;
flat out vec4 candidate;
void main() {
  gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
  gl_PointSize = 1.0;
  candidate = vec4(-1.0);
  int index = gl_VertexID;
  ivec2 texel = ivec2(index % textureWidth, index / textureWidth);
  vec4 point = texelFetch(positions, texel, 0);
  vec2 body = texelFetch(geometry, texel, 0).rg;
  bool greyed = texelFetch(status, texel, 0).r != 0.0;
  // Native all-present scenes share a one-texel zero exit sentinel.
  ivec2 exitTexel = min(texel, textureSize(exits, 0) - 1);
  if (texelFetch(exits, exitTexel, 0).g > 0.5 || pointOpacity <= 0.0 || body.r <= 0.0
    || (greyed && greyoutOpacity == 0.0) || any(isnan(point.rg)) || any(isinf(point.rg))) return;
  vec2 normalized = (2.0 * point.rg / spaceSize - 1.0) * spaceSize / screenSize;
  vec2 ndc = (mat3(transform) * vec3(normalized, 1.0)).xy;
  vec2 center = (ndc + 1.0) * screenSize * 0.5;
  vec2 delta = center - pointer;
  float distanceSquared = dot(delta, delta);
  float zoom = zoomSizes > 0.0 ? transform[0][0] : min(5.0, max(1.0, transform[0][0] * 0.01));
  float diameter = min(body.r * sizeScale * zoom, maxPointSize);
  if (diameter <= 0.0) return;
  // Preserve the existing ${targetPx} CSS-pixel accessible target, plus the actual
  // rendered circle/square when zoom makes its body larger than that target.
  bool onBody = body.g == 1.0 ? all(lessThanEqual(abs(delta), vec2(diameter * 0.4)))
    : distanceSquared <= diameter * diameter * 0.25;
  if (distanceSquared > ${(targetPx ** 2).toFixed(1)} && !onBody) return;
  // All candidates rasterize the same pixel. Depth ranks absolute center
  // distance, never painted-pixel distance, sprite size or later draw order.
  // LESS keeps the first original index for exactly coincident centers.
  float depth = distanceSquared / depthBound;
  gl_Position = vec4(0.0, 0.0, depth * 2.0 - 1.0, 1.0);
  candidate = vec4(float(index), point.rg, 1.0);
}`;
const fragment = `#version 300 es
precision highp float;
flat in vec4 candidate;
out vec4 picked;
void main() { picked = candidate; }
`;

/** Additional state changed by the depth query, beyond the shared bounded guard. */
function withPickingState<T>(gl: WebGL2RenderingContext, action: () => T): T {
  return withWorkspaceForceAtlas2GlState(gl, () => {
    const func = gl.getParameter(gl.DEPTH_FUNC) as number;
    const mask = gl.getParameter(gl.DEPTH_WRITEMASK) as boolean;
    const range = Array.from(gl.getParameter(gl.DEPTH_RANGE) as Float32Array);
    try { return action(); }
    finally { gl.depthFunc(func); gl.depthMask(mask); gl.depthRange(range[0], range[1]); }
  });
}

/** Everything a query depends on except the pointer, which is judged separately in take(). */
function sceneKey(frame: CosmosPointPickFrame): string {
  return [...frame.screenSize, frame.spaceSize, frame.pixelRatio,
    frame.pointSizeScale, frame.scalePointsOnZoom, frame.maxPointSize, frame.pointOpacity,
    frame.pointGreyoutOpacity, ...frame.transformationMatrix].join(":");
}

/** Shader zoom: the camera itself, or a point size held between 1x and 5x. */
function zoomOf(frame: CosmosPointPickFrame): number {
  return frame.scalePointsOnZoom ? frame.transformationMatrix[0]
    : Math.min(5, Math.max(1, frame.transformationMatrix[0] * .01));
}

function renderedDiameter(frame: CosmosPointPickFrame, size: number): number {
  return Math.min(size * frame.pointSizeScale * zoomOf(frame), frame.maxPointSize);
}

/**
 * CSS pixels, bottom-left origin, where the vertex shader places a point of the
 * frame's scene. Same expression order as the shader: mat3(transform) is the
 * upper-left 3x3 of the column-major matrix, so its third column translates.
 * The device pixel ratio is deliberately absent; the shader never reads it.
 */
export function projectNativePoint(frame: CosmosPointPickFrame, position: readonly [number, number]): [number, number] {
  const matrix = frame.transformationMatrix, [width, height] = frame.screenSize, space = frame.spaceSize;
  const x = (2 * position[0] / space - 1) * space / width, y = (2 * position[1] / space - 1) * space / height;
  return [(matrix[0] * x + matrix[4] * y + matrix[8] + 1) * width * .5, (matrix[1] * x + matrix[5] * y + matrix[9] + 1) * height * .5];
}

/**
 * Whether the vertex shader would still keep this point as a candidate for the
 * frame's pointer: within the target, or on its rendered circle/square.
 * Matches the shader to float precision, not bit for bit.
 */
export function nativePointTargetsPointer(
  frame: CosmosPointPickFrame, position: readonly [number, number], size: number, shape: number,
): boolean {
  const [x, y] = projectNativePoint(frame, position), dx = x - frame.pointer[0], dy = y - frame.pointer[1];
  const squared = dx * dx + dy * dy, diameter = renderedDiameter(frame, size);
  if (!(diameter > 0)) return false;
  const onBody = shape === 1 ? Math.abs(dx) <= diameter * .4 && Math.abs(dy) <= diameter * .4
    : squared <= diameter * diameter * .25;
  return squared <= TARGET_SQUARED || onBody;
}

/**
 * Original-node nearest-center query, not a second renderer or layout engine.
 * Owns one result pixel, depth, metadata and a 16-byte async readback buffer.
 * Native textures, callbacks, the camera and animation scheduling remain borrowed.
 */
export function createWorkspaceNativePointPicker(
  sizes: Float32Array, shapes: Float32Array, onError?: (error: unknown) => void, now: () => number = () => performance.now(),
): CosmosPointPicker {
  if (!sizes.length || sizes.length !== shapes.length || sizes.length > 2 ** 24
    || !sizes.every(value => Number.isFinite(value) && value >= 0)
    || !shapes.every(value => value === 0 || value === 1)) throw new Error("Invalid native picking geometry");
  const diameters = sizes.slice(), kinds = shapes.slice();
  const maximumDiameter = diameters.reduce((maximum, diameter) => Math.max(maximum, diameter), 0);
  let gl: WebGL2RenderingContext | null = null, program: WebGLProgram | null = null;
  let vao: WebGLVertexArrayObject | null = null, framebuffer: WebGLFramebuffer | null = null;
  let readback: WorkspaceGpuReadback | null = null;
  const textures: WebGLTexture[] = [], shaders: WebGLShader[] = [];
  const locations = new Map<string, WebGLUniformLocation>();
  // The in-flight query: the scene it was asked in and the pointer it was asked at.
  let issued: { scene: string; pointer: readonly [number, number] } | null = null, destroyed = false;
  // No answer for a moved pointer is read before this time.
  let quietUntil = -Infinity;

  // The canvas whose context loss releases this picker's resources while it owns any.
  let watched: EventTarget | null = null;

  function discard() {
    readback?.discard(); issued = null;
  }
  function dispose() {
    watched?.removeEventListener("webglcontextlost", dispose); watched = null;
    discard();
    readback?.destroy(); readback = null;
    if (gl) {
      for (const texture of textures) gl.deleteTexture(texture);
      for (const shader of shaders) gl.deleteShader(shader);
      if (framebuffer) gl.deleteFramebuffer(framebuffer);
      if (vao) gl.deleteVertexArray(vao);
      if (program) gl.deleteProgram(program);
    }
    textures.length = 0; shaders.length = 0; locations.clear();
    framebuffer = null; vao = null; program = null; gl = null;
  }
  /**
   * A lost context takes everything made on it, and a restore hands the same context object back. Releasing at the
   * loss, when deleting is a no-op, makes the next query build fresh objects. Asking the GPU process whether each
   * object survived (isProgram and friends) would cost a synchronous round trip per query instead.
   */
  function watch(context: WebGL2RenderingContext) {
    const target: EventTarget | undefined = context.canvas;
    target?.addEventListener("webglcontextlost", dispose);
    watched = target ?? null;
  }
  function valid(frame: CosmosPointPickFrame | null): frame is CosmosPointPickFrame {
    return !destroyed && frame !== null && !frame.gl.isContextLost() && frame.nodeCount === diameters.length
      && frame.textureSize.width * frame.textureSize.height >= frame.nodeCount
      && Number.isInteger(frame.textureSize.width) && Number.isInteger(frame.textureSize.height)
      && frame.textureSize.width > 0 && frame.textureSize.height > 0
      && frame.transformationMatrix.length === 16 && frame.transformationMatrix.every(Number.isFinite)
      && [...frame.pointer, ...frame.screenSize, frame.spaceSize, frame.pixelRatio,
        frame.pointSizeScale, frame.maxPointSize, frame.pointOpacity].every(Number.isFinite)
      && frame.screenSize.every(value => value > 0) && frame.spaceSize > 0 && frame.pixelRatio > 0;
  }
  function ensure(frame: CosmosPointPickFrame) {
    if (gl && gl !== frame.gl) dispose();
    if (program) return;
    gl = frame.gl;
    try {
      // A restore resets extension state, and a RGBA32F attachment is incomplete without this one.
      gl.getExtension("EXT_color_buffer_float");
      for (const [type, source] of [[gl.VERTEX_SHADER, nativePointVertexShader(NATIVE_POINT_TARGET_PX)], [gl.FRAGMENT_SHADER, fragment]] as const) {
        const shader = gl.createShader(type);
        if (!shader) throw new Error("Native picker shader allocation failed");
        shaders.push(shader); gl.shaderSource(shader, source); gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(`Native picker shader failed: ${gl.getShaderInfoLog(shader)}`);
      }
      program = gl.createProgram(); vao = gl.createVertexArray(); framebuffer = gl.createFramebuffer();
      if (!program || !vao || !framebuffer) throw new Error("Native picker resource allocation failed");
      readback = createWorkspaceGpuReadback(gl, 1);
      for (const shader of shaders) gl.attachShader(program, shader);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(`Native picker program failed: ${gl.getProgramInfoLog(program)}`);
      for (const name of ["positions", "status", "exits", "geometry", "textureWidth", "transform", "screenSize", "pointer", "spaceSize",
        "sizeScale", "zoomSizes", "maxPointSize", "pointOpacity", "greyoutOpacity", "depthBound"]) {
        const location = gl.getUniformLocation(program, name);
        if (location === null) throw new Error(`Native picker uniform missing: ${name}`);
        locations.set(name, location);
      }
      gl.activeTexture(gl.TEXTURE3);
      for (const [format, width, height] of [[gl.RGBA32F, 1, 1], [gl.DEPTH_COMPONENT24, 1, 1],
        [gl.RGBA32F, frame.textureSize.width, frame.textureSize.height]]) {
        const texture = gl.createTexture();
        if (!texture) throw new Error("Native picker texture allocation failed");
        textures.push(texture); gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texStorage2D(gl.TEXTURE_2D, 1, format, width, height);
      }
      const metadata = new Float32Array(frame.textureSize.width * frame.textureSize.height * 4);
      diameters.forEach((diameter, index) => { metadata[index * 4] = diameter; metadata[index * 4 + 1] = kinds[index]; });
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, frame.textureSize.width, frame.textureSize.height, gl.RGBA, gl.FLOAT, metadata);
      gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, framebuffer);
      gl.framebufferTexture2D(gl.DRAW_FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, textures[0], 0);
      gl.framebufferTexture2D(gl.DRAW_FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.TEXTURE_2D, textures[1], 0);
      if (gl.checkFramebufferStatus(gl.DRAW_FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error("Native picker framebuffer is incomplete");
      watch(gl);
    } catch (error) { dispose(); throw error; }
  }
  function draw(frame: CosmosPointPickFrame) {
    ensure(frame);
    const context = frame.gl;
    context.useProgram(program); context.bindVertexArray(vao);
    context.bindFramebuffer(context.DRAW_FRAMEBUFFER, framebuffer);
    context.viewport(0, 0, 1, 1);
    for (const flag of [context.BLEND, context.STENCIL_TEST, context.SCISSOR_TEST, context.CULL_FACE, context.RASTERIZER_DISCARD]) context.disable(flag);
    context.enable(context.DEPTH_TEST); context.depthFunc(context.LESS); context.depthMask(true); context.depthRange(0, 1);
    context.colorMask(true, true, true, true);
    context.clearBufferfv(context.COLOR, 0, new Float32Array([-1, 0, 0, 0]));
    context.clearBufferfv(context.DEPTH, 0, new Float32Array([1]));
    [frame.positionTexture, frame.pointStatusTexture, frame.exitTexture, textures[2]].forEach((texture, unit) => {
      context.activeTexture(context.TEXTURE0 + unit); context.bindTexture(context.TEXTURE_2D, texture);
    });
    ["positions", "status", "exits", "geometry"].forEach((name, unit) => context.uniform1i(locations.get(name)!, unit));
    context.uniform1i(locations.get("textureWidth")!, frame.textureSize.width);
    context.uniformMatrix4fv(locations.get("transform")!, false, Float32Array.from(frame.transformationMatrix));
    context.uniform2f(locations.get("screenSize")!, ...frame.screenSize);
    context.uniform2f(locations.get("pointer")!, ...frame.pointer);
    const maximumRenderedDiameter = renderedDiameter(frame, maximumDiameter);
    // One shared dimensional bound keeps ranking absolute and linear, with
    // precision throughout the largest eligible circle/square, not near 0 only.
    context.uniform1f(locations.get("depthBound")!, 1 + Math.max(TARGET_SQUARED, maximumRenderedDiameter ** 2 / 2));
    for (const [name, value] of [["spaceSize", frame.spaceSize], ["sizeScale", frame.pointSizeScale],
      ["zoomSizes", Number(frame.scalePointsOnZoom)], ["maxPointSize", frame.maxPointSize],
      ["pointOpacity", frame.pointOpacity], ["greyoutOpacity", frame.pointGreyoutOpacity ?? -1]] as const) context.uniform1f(locations.get(name)!, value);
    context.drawArrays(context.POINTS, 0, frame.nodeCount);
    context.bindFramebuffer(context.READ_FRAMEBUFFER, framebuffer); context.readBuffer(context.COLOR_ATTACHMENT0);
  }
  /** Farthest a point's centre can sit from a pointer that still targets it: the target or a body corner. */
  function reach(frame: CosmosPointPickFrame): number {
    return Math.max(Math.sqrt(TARGET_SQUARED), renderedDiameter(frame, maximumDiameter) * .4 * Math.SQRT2);
  }
  function decode(result: Float32Array): CosmosPointPickResult | null {
    const index = result[0];
    return Number.isInteger(index) && index >= 0 && index < diameters.length && result[3] === 1
      && Number.isFinite(result[1]) && Number.isFinite(result[2]) ? { index, position: [result[1], result[2]] } : null;
  }
  function guarded<T>(action: () => T, fallback: T): T {
    try { return action(); }
    catch (error) {
      destroyed = true; dispose();
      if (!onError) throw error;
      onError(error);
      return fallback;
    }
  }
  return {
    get hasPending() { return readback?.pending() ?? false; },
    pickSync(frame) {
      discard();
      if (!valid(frame)) return null;
      return guarded(() => withPickingState(frame.gl, () => {
        draw(frame);
        return decode(readback!.readSync(framebuffer!, 1, 1));
      }), null);
    },
    request(frame) {
      if (readback?.pending()) return false;
      if (!valid(frame)) return true;
      return guarded(() => withPickingState(frame.gl, () => {
        draw(frame);
        const queued = readback!.request(framebuffer!, 1, 1);
        issued = queued ? { scene: sceneKey(frame), pointer: [frame.pointer[0], frame.pointer[1]] } : null;
        return queued;
      }), true);
    },
    take(frame) {
      if (!readback?.pending()) return undefined;
      if (!valid(frame) || frame.gl !== gl) { discard(); return undefined; }
      // A fence resolves a frame after its query at the earliest, so a moving pointer has always moved on by now.
      // Such an answer can still serve it only while some point may target both pointers: never across another
      // scene, and never beyond twice the farthest a point can sit from a pointer that targets it.
      if (!issued || sceneKey(frame) !== issued.scene
        || Math.hypot(frame.pointer[0] - issued.pointer[0], frame.pointer[1] - issued.pointer[1]) > 2 * reach(frame)) {
        discard(); return undefined;
      }
      const asked = issued.pointer, moved = frame.pointer[0] !== asked[0] || frame.pointer[1] !== asked[1];
      // A moved pointer is answered at a bounded rate: the skipped query is dropped unread and the caller asks again.
      if (moved && now() < quietUntil) { discard(); return undefined; }
      return guarded(() => {
        const started = now(), answer = readback!.poll(), finished = now();
        if (finished - started >= NATIVE_SLOW_READ_MS) quietUntil = finished + NATIVE_SLOW_READ_BACKOFF_MS;
        if (!answer) return undefined;
        issued = null;
        if (moved) quietUntil = Math.max(quietUntil, finished + NATIVE_MOVING_ANSWER_INTERVAL_MS);
        const result = decode(answer);
        if (!moved) return result;
        // The one-frame-old answer is served only while its node still targets the pointer now. "No point" cannot be
        // shown to hold for a pointer that moved on, so it is obsolete like any answer for a position already left.
        return result && nativePointTargetsPointer(frame, result.position, diameters[result.index], kinds[result.index])
          ? result : undefined;
      }, null);
    },
    discard,
    destroy() { if (destroyed) return; destroyed = true; dispose(); },
  };
}
