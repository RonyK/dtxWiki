import {
  workspaceForceAtlas2ContactFragmentShader,
  workspaceForceAtlas2ContactVertexShader,
} from "./forceAtlas2ContactShaders";

export const WORKSPACE_FORCEATLAS2_CONTACT_ITERATIONS = 4;

export interface WorkspaceForceAtlas2ContactInput {
  /** Immutable conservative circle radii in the same world units as positions. */
  radii: Float32Array;
  pins?: readonly number[];
}

export interface WorkspaceForceAtlas2ContactDimensions {
  readonly width: number;
  readonly height: number;
}

export interface WorkspaceForceAtlas2Contact {
  readonly nodeCount: number;
  readonly textureSize: WorkspaceForceAtlas2ContactDimensions;
  /**
   * RGBA32F source [x, y, force x, force y]. Returns an owned RGBA32F texture.
   * One through four Jacobi passes are permitted; four is the fixed default.
   * This is a bounded projection, not a guarantee of converged nonpenetration
   * in an arbitrary jammed scene. Both-pinned overlaps cannot be resolved.
   */
  project(sourceTexture: WebGLTexture, sourceDimensions: WorkspaceForceAtlas2ContactDimensions,
    iterations?: number): WebGLTexture;
  /** Validate the entire list before replacing pin flags; radii stay immutable. */
  setPins(indices: readonly number[]): void;
  destroy(): void;
}

const MAX_EXACT_FLOAT_INTEGER = 2 ** 24;
const MAX_PAIR_RADIUS = Math.fround((2 - 2 ** -23) * 2 ** 127 / 2);

function validatePinIndices(indices: readonly number[], nodeCount: number): void {
  for (const index of indices) {
    if (!Number.isInteger(index) || index < 0 || index >= nodeCount) {
      throw new Error(`ForceAtlas2 contact pin is outside the original node rows: ${index}`);
    }
  }
}

function createContactProgram(gl: WebGL2RenderingContext): WebGLProgram {
  const shaders: WebGLShader[] = [];
  let program: WebGLProgram | null = null;
  try {
    for (const [type, source] of [
      [gl.VERTEX_SHADER, workspaceForceAtlas2ContactVertexShader],
      [gl.FRAGMENT_SHADER, workspaceForceAtlas2ContactFragmentShader],
    ] as const) {
      const shader = gl.createShader(type);
      if (!shader) throw new Error("ForceAtlas2 contact shader allocation failed");
      shaders.push(shader);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error(`ForceAtlas2 contact shader compilation failed: ${gl.getShaderInfoLog(shader) ?? "unknown"}`);
      }
    }
    program = gl.createProgram();
    if (!program) throw new Error("ForceAtlas2 contact program allocation failed");
    for (const shader of shaders) gl.attachShader(program, shader);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`ForceAtlas2 contact program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
    }
    return program;
  } catch (error) {
    if (program) gl.deleteProgram(program);
    throw error;
  } finally {
    for (const shader of shaders) gl.deleteShader(shader);
  }
}

/**
 * Separate contact augmentation, not part of the exact upstream FA2 model.
 * Owns only its textures/framebuffers/program/VAO, never the supplied source
 * texture or WebGL context. It mutates GL bindings, viewport and raster state;
 * a shared-context caller must handle state ownership/restoration explicitly.
 * project() has no CPU position readback, renderer, scheduler or box clamp.
 */
export function createWorkspaceForceAtlas2Contact(
  gl: WebGL2RenderingContext,
  input: WorkspaceForceAtlas2ContactInput,
): WorkspaceForceAtlas2Contact {
  if (!(input.radii instanceof Float32Array) || input.radii.length < 1
    || input.radii.length > MAX_EXACT_FLOAT_INTEGER) {
    throw new Error("ForceAtlas2 contact requires nonempty indexed Float32 radii");
  }
  const radii = input.radii.slice(), nodeCount = radii.length;
  for (const radius of radii) {
    if (!Number.isFinite(radius) || radius <= 0 || radius > MAX_PAIR_RADIUS) {
      throw new Error("ForceAtlas2 contact requires positive finite radii with finite pair sums");
    }
  }
  validatePinIndices(input.pins ?? [], nodeCount);
  if (gl.isContextLost()) throw new Error("ForceAtlas2 contact cannot initialize a lost context");
  if (!gl.getExtension("EXT_color_buffer_float")) {
    throw new Error("ForceAtlas2 contact requires EXT_color_buffer_float");
  }
  const limit = gl.getParameter(gl.MAX_TEXTURE_SIZE) as number;
  if (!Number.isInteger(limit) || limit < 1) throw new Error("ForceAtlas2 contact texture limit is unavailable");
  const width = Math.min(limit, Math.ceil(Math.sqrt(nodeCount))), height = Math.ceil(nodeCount / width);
  if (height > limit) throw new Error("ForceAtlas2 contact rows exceed the texture capacity");
  const textureSize = Object.freeze({ width, height });
  const textures: WebGLTexture[] = [], framebuffers: WebGLFramebuffer[] = [];
  let program: WebGLProgram | null = null, vertexArray: WebGLVertexArrayObject | null = null;
  let destroyed = false;

  function requireAlive(): void {
    if (destroyed) throw new Error("ForceAtlas2 contact has been destroyed");
    if (gl.isContextLost()) throw new Error("ForceAtlas2 contact WebGL2 context is lost");
  }
  function requireNoGlError(operation: string): void {
    const error = gl.getError();
    if (error !== gl.NO_ERROR) throw new Error(`ForceAtlas2 contact ${operation} failed with WebGL error ${error}`);
  }
  function cleanup(): void {
    for (const framebuffer of framebuffers) gl.deleteFramebuffer(framebuffer);
    for (const texture of textures) gl.deleteTexture(texture);
    if (vertexArray) gl.deleteVertexArray(vertexArray);
    if (program) gl.deleteProgram(program);
  }
  function pinData(indices: readonly number[]): Float32Array {
    const data = new Float32Array(width * height);
    for (const index of indices) data[index] = 1;
    return data;
  }
  function allocateTexture(data?: Float32Array): WebGLTexture {
    const texture = gl.createTexture();
    if (!texture) throw new Error("ForceAtlas2 contact texture allocation failed");
    textures.push(texture);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texStorage2D(gl.TEXTURE_2D, 1, data ? gl.R32F : gl.RGBA32F, width, height);
    if (data) {
      const padded = new Float32Array(width * height);
      padded.set(data);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, width, height, gl.RED, gl.FLOAT, padded);
    }
    return texture;
  }

  try {
    program = createContactProgram(gl);
    vertexArray = gl.createVertexArray();
    if (!vertexArray) throw new Error("ForceAtlas2 contact vertex array allocation failed");
    const outputs = [allocateTexture(), allocateTexture()];
    const radiiTexture = allocateTexture(radii), pinsTexture = allocateTexture(pinData(input.pins ?? []));
    for (const texture of outputs) {
      const framebuffer = gl.createFramebuffer();
      if (!framebuffer) throw new Error("ForceAtlas2 contact framebuffer allocation failed");
      framebuffers.push(framebuffer);
      gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
        throw new Error("ForceAtlas2 contact RGBA32F framebuffer is incomplete");
      }
    }
    gl.useProgram(program);
    const uniform = (name: string): WebGLUniformLocation => {
      const location = gl.getUniformLocation(program!, name);
      if (location === null) throw new Error(`ForceAtlas2 contact required uniform is missing: ${name}`);
      return location;
    };
    gl.uniform1i(uniform("stateTexture"), 0);
    gl.uniform1i(uniform("radiiTexture"), 1);
    gl.uniform1i(uniform("pinsTexture"), 2);
    gl.uniform1i(uniform("nodeCount"), nodeCount);
    gl.uniform1i(uniform("outputWidth"), width);
    const sourceWidthUniform = uniform("sourceWidth");
    requireNoGlError("initialization");

    return {
      nodeCount, textureSize,
      project(sourceTexture, sourceDimensions, iterations = WORKSPACE_FORCEATLAS2_CONTACT_ITERATIONS) {
        requireAlive();
        if (!Number.isInteger(iterations) || iterations < 1 || iterations > WORKSPACE_FORCEATLAS2_CONTACT_ITERATIONS) {
          throw new Error("ForceAtlas2 contact permits one through four bounded projection passes");
        }
        const sourceWidth = sourceDimensions.width, sourceHeight = sourceDimensions.height;
        if (!Number.isInteger(sourceWidth) || !Number.isInteger(sourceHeight)
          || sourceWidth < 1 || sourceHeight < 1 || sourceWidth > limit || sourceHeight > limit
          || sourceWidth * sourceHeight < nodeCount || !gl.isTexture(sourceTexture)) {
          throw new Error("ForceAtlas2 contact source texture/dimensions do not cover the original rows");
        }
        if (outputs.includes(sourceTexture) && (sourceWidth !== width || sourceHeight !== height)) {
          throw new Error("ForceAtlas2 contact owned source dimensions disagree");
        }
        gl.useProgram(program);
        gl.bindVertexArray(vertexArray);
        gl.viewport(0, 0, width, height);
        for (const capability of [gl.BLEND, gl.DEPTH_TEST, gl.STENCIL_TEST,
          gl.SCISSOR_TEST, gl.CULL_FACE, gl.RASTERIZER_DISCARD]) gl.disable(capability);
        gl.colorMask(true, true, true, true);
        let source = sourceTexture, packedWidth = sourceWidth;
        for (let pass = 0; pass < iterations; pass++) {
          const next = source === outputs[0] ? 1 : 0;
          // Source is never the active draw attachment, including a previous
          // project() result supplied as input for another bounded projection.
          gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, framebuffers[next]);
          for (const [unit, texture] of [source, radiiTexture, pinsTexture].entries()) {
            gl.activeTexture(gl.TEXTURE0 + unit);
            gl.bindSampler(unit, null);
            gl.bindTexture(gl.TEXTURE_2D, texture);
          }
          gl.uniform1i(sourceWidthUniform, packedWidth);
          gl.drawArrays(gl.TRIANGLES, 0, 3);
          requireNoGlError("projection");
          source = outputs[next]; packedWidth = width;
        }
        return source;
      },
      setPins(indices) {
        requireAlive();
        validatePinIndices(indices, nodeCount);
        const data = pinData(indices);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, pinsTexture);
        gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
        gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, width, height, gl.RED, gl.FLOAT, data);
        requireNoGlError("pin update");
      },
      destroy() {
        if (destroyed) return;
        destroyed = true;
        cleanup();
      },
    };
  } catch (error) {
    cleanup();
    throw error;
  }
}
