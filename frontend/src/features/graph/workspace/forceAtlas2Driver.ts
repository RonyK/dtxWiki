import {
  workspaceForceAtlas2FragmentShader,
  workspaceForceAtlas2VertexShader,
} from "./forceAtlas2Shaders";

export interface WorkspaceForceAtlas2DriverInput {
  positions: Float32Array;
  radii: Float32Array;
  sources: Uint32Array;
  targets: Uint32Array;
  weights: Float64Array;
  pins?: readonly number[];
}

export interface WorkspaceForceAtlas2DriverSnapshot {
  positions: Float32Array;
  force: Float32Array;
  previousForce: Float32Array;
  masses: Float32Array;
  iteration: number;
}

export interface WorkspaceForceAtlas2Driver {
  /** RGBA32F [x, y, currentForceX, currentForceY], row-major node indices. */
  readonly currentTexture: WebGLTexture;
  /** Previous force history, also updated on an explicit GPU drag import. */
  readonly previousTexture: WebGLTexture;
  readonly textureSize: Readonly<{ width: number; height: number }>;
  readonly nodeCount: number;
  readonly iteration: number;
  /** A paused driver validates arguments, then leaves state/history unchanged. */
  step(iterations?: number): void;
  pause(): void;
  resume(): void;
  /** Atomic flag replacement; coordinates are not uploaded or read back. */
  setPins(indices: readonly number[]): void;
  /** Explicit diagnostic GPU readback; never called by step(). */
  snapshot(): WorkspaceForceAtlas2DriverSnapshot;
  destroy(): void;
}

interface DriverColumns {
  initialState: Float32Array;
  attributes: Float32Array;
  adjacency: Float32Array;
  pins: Float32Array;
  masses: Float32Array;
  nodeCount: number;
  adjacencyCount: number;
}

const MAX_EXACT_FLOAT_INTEGER = 2 ** 24;

/**
 * Mirrors graphology-layout-forceatlas2 0.10.1 helpers.graphToByteArrays:
 * mass starts at 1 and each original edge updates source, then target, through
 * Float32 storage using the original Float64 weight. A loop updates twice.
 * Attraction weights are separately rounded to Float32, as in EdgeMatrix.
 * See forceAtlas2Shaders.ts for upstream attribution and the retained MIT text.
 */
function buildColumns(input: WorkspaceForceAtlas2DriverInput): DriverColumns {
  if (!(input.positions instanceof Float32Array) || !(input.radii instanceof Float32Array)
    || !(input.sources instanceof Uint32Array) || !(input.targets instanceof Uint32Array)
    || !(input.weights instanceof Float64Array)) {
    throw new Error("ForceAtlas2 requires the declared typed position/radius/edge columns");
  }
  const nodeCount = input.radii.length;
  const edgeCount = input.weights.length;
  if (nodeCount < 1 || nodeCount > MAX_EXACT_FLOAT_INTEGER
    || input.positions.length !== nodeCount * 2
    || input.sources.length !== edgeCount || input.targets.length !== edgeCount
    || edgeCount * 2 > MAX_EXACT_FLOAT_INTEGER) {
    throw new Error("ForceAtlas2 indexed column lengths exceed exact Float32 index capacity or disagree");
  }
  const initialState = new Float32Array(nodeCount * 4);
  const masses = new Float32Array(nodeCount).fill(1);
  const counts = new Uint32Array(nodeCount);
  for (let node = 0; node < nodeCount; node++) {
    const x = input.positions[node * 2], y = input.positions[node * 2 + 1];
    if (!Number.isFinite(x) || !Number.isFinite(y)
      || !Number.isFinite(input.radii[node]) || input.radii[node] <= 0) {
      throw new Error(`ForceAtlas2 requires finite positions and positive radii: node ${node}`);
    }
    initialState[node * 4] = x;
    initialState[node * 4 + 1] = y;
  }
  for (let edge = 0; edge < edgeCount; edge++) {
    const source = input.sources[edge], target = input.targets[edge], weight = input.weights[edge];
    if (source >= nodeCount || target >= nodeCount || !Number.isFinite(weight) || weight <= 0
      || !Number.isFinite(Math.fround(weight)) || Math.fround(weight) <= 0) {
      throw new Error(`ForceAtlas2 requires valid endpoints and positive Float32 weights: edge ${edge}`);
    }
    masses[source] += weight;
    masses[target] += weight;
    if (!Number.isFinite(masses[source]) || !Number.isFinite(masses[target])) {
      throw new Error(`ForceAtlas2 weighted mass exceeds Float32 range: edge ${edge}`);
    }
    counts[source]++;
    counts[target]++;
  }
  const attributes = new Float32Array(nodeCount * 4);
  const offsets = new Uint32Array(nodeCount);
  let adjacencyCount = 0;
  for (let node = 0; node < nodeCount; node++) {
    offsets[node] = adjacencyCount;
    attributes.set([masses[node], input.radii[node], adjacencyCount, counts[node]], node * 4);
    adjacencyCount += counts[node];
  }
  const adjacency = new Float32Array(adjacencyCount * 4);
  const cursors = offsets.slice();
  for (let edge = 0; edge < edgeCount; edge++) {
    const source = input.sources[edge], target = input.targets[edge], weight = input.weights[edge];
    adjacency.set([target, weight, 0, 0], cursors[source]++ * 4);
    adjacency.set([source, weight, 0, 0], cursors[target]++ * 4);
  }
  const pins = new Float32Array(nodeCount);
  for (const node of input.pins ?? []) {
    if (!Number.isInteger(node) || node < 0 || node >= nodeCount) {
      throw new Error(`ForceAtlas2 pin index is outside the original node columns: ${node}`);
    }
    pins[node] = 1;
  }
  return { initialState, attributes, adjacency, pins, masses, nodeCount, adjacencyCount };
}

function textureDimensions(count: number, limit: number): { width: number; height: number } {
  const width = Math.min(limit, Math.ceil(Math.sqrt(Math.max(1, count))));
  const height = Math.ceil(Math.max(1, count) / width);
  if (height > limit) throw new Error("ForceAtlas2 columns exceed this context's texture capacity");
  return { width, height };
}

function createProgram(gl: WebGL2RenderingContext): WebGLProgram {
  const shaders: WebGLShader[] = [];
  let program: WebGLProgram | null = null;
  try {
    for (const [type, source] of [
      [gl.VERTEX_SHADER, workspaceForceAtlas2VertexShader],
      [gl.FRAGMENT_SHADER, workspaceForceAtlas2FragmentShader],
    ] as const) {
      const shader = gl.createShader(type);
      if (!shader) throw new Error("ForceAtlas2 shader allocation failed");
      shaders.push(shader);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error(`ForceAtlas2 shader compilation failed: ${gl.getShaderInfoLog(shader) ?? "unknown"}`);
      }
    }
    program = gl.createProgram();
    if (!program) throw new Error("ForceAtlas2 program allocation failed");
    for (const shader of shaders) gl.attachShader(program, shader);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(`ForceAtlas2 program link failed: ${gl.getProgramInfoLog(program) ?? "unknown"}`);
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
 * Owns an EXCLUSIVE, dedicated WebGL2 context. This factory and all methods
 * intentionally own its framebuffer/program/VAO/texture/viewport/pixel-store
 * state; they do not save/restore bindings for a shared Cosmos or renderer
 * context. The caller owns the canvas/context lifetime. No context recovery,
 * rendering, animation scheduler or CPU position setter is added. A shared
 * renderer MUST wrap calls with withWorkspaceForceAtlas2GlState.
 */
export function createWorkspaceForceAtlas2Driver(
  gl: WebGL2RenderingContext,
  input: WorkspaceForceAtlas2DriverInput,
): WorkspaceForceAtlas2Driver {
  const columns = buildColumns(input);
  if (gl.isContextLost()) throw new Error("ForceAtlas2 cannot initialize a lost WebGL2 context");
  if (!gl.getExtension("EXT_color_buffer_float")) {
    throw new Error("ForceAtlas2 requires EXT_color_buffer_float for RGBA32F state");
  }
  const limit = gl.getParameter(gl.MAX_TEXTURE_SIZE) as number;
  if (!Number.isInteger(limit) || limit < 1) throw new Error("ForceAtlas2 texture limit is unavailable");
  const size = Object.freeze(textureDimensions(columns.nodeCount, limit));
  const adjacencySize = textureDimensions(columns.adjacencyCount, limit);
  const textures: WebGLTexture[] = [];
  const framebuffers: WebGLFramebuffer[] = [];
  let program: WebGLProgram | null = null;
  let vertexArray: WebGLVertexArrayObject | null = null;
  let destroyed = false, paused = false, current = 0, iteration = 0;

  function cleanup(): void {
    for (const framebuffer of framebuffers) gl.deleteFramebuffer(framebuffer);
    for (const texture of textures) gl.deleteTexture(texture);
    if (vertexArray) gl.deleteVertexArray(vertexArray);
    if (program) gl.deleteProgram(program);
  }
  function requireAlive(): void {
    if (destroyed) throw new Error("ForceAtlas2 driver has been destroyed");
    if (gl.isContextLost()) throw new Error("ForceAtlas2 WebGL2 context is lost");
  }
  function requireNoGlError(operation: string): void {
    const error = gl.getError();
    if (error !== gl.NO_ERROR) throw new Error(`ForceAtlas2 ${operation} failed with WebGL error ${error}`);
  }
  function allocateTexture(
    data: Float32Array, dimensions: { width: number; height: number }, channels = 4,
  ): WebGLTexture {
    const texture = gl.createTexture();
    if (!texture) throw new Error("ForceAtlas2 texture allocation failed");
    textures.push(texture);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texStorage2D(gl.TEXTURE_2D, 1, channels === 4 ? gl.RGBA32F : gl.R32F,
      dimensions.width, dimensions.height);
    const padded = new Float32Array(dimensions.width * dimensions.height * channels);
    padded.set(data);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, dimensions.width, dimensions.height,
      channels === 4 ? gl.RGBA : gl.RED, gl.FLOAT, padded);
    return texture;
  }

  try {
    program = createProgram(gl);
    vertexArray = gl.createVertexArray();
    if (!vertexArray) throw new Error("ForceAtlas2 vertex array allocation failed");
    const stateTextures = [allocateTexture(columns.initialState, size),
      allocateTexture(columns.initialState, size)];
    const attributesTexture = allocateTexture(columns.attributes, size);
    const adjacencyTexture = allocateTexture(columns.adjacency, adjacencySize);
    const pinsTexture = allocateTexture(columns.pins, size, 1);
    for (const texture of stateTextures) {
      const framebuffer = gl.createFramebuffer();
      if (!framebuffer) throw new Error("ForceAtlas2 framebuffer allocation failed");
      framebuffers.push(framebuffer);
      gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
        throw new Error("ForceAtlas2 RGBA32F framebuffer is incomplete");
      }
    }
    gl.useProgram(program);
    const uniform = (name: string): WebGLUniformLocation => {
      const location = gl.getUniformLocation(program!, name);
      if (location === null) throw new Error(`ForceAtlas2 required uniform is missing: ${name}`);
      return location;
    };
    gl.uniform1i(uniform("stateTexture"), 0);
    gl.uniform1i(uniform("attributesTexture"), 1);
    gl.uniform1i(uniform("adjacencyTexture"), 2);
    gl.uniform1i(uniform("pinsTexture"), 3);
    gl.uniform1i(uniform("nodeCount"), columns.nodeCount);
    gl.uniform1i(uniform("stateWidth"), size.width);
    gl.uniform1i(uniform("adjacencyWidth"), adjacencySize.width);
    requireNoGlError("initialization");

    function readState(index: number): Float32Array {
      const data = new Float32Array(size.width * size.height * 4);
      gl.bindFramebuffer(gl.READ_FRAMEBUFFER, framebuffers[index]);
      gl.readBuffer(gl.COLOR_ATTACHMENT0);
      gl.pixelStorei(gl.PACK_ALIGNMENT, 4);
      gl.readPixels(0, 0, size.width, size.height, gl.RGBA, gl.FLOAT, data);
      requireNoGlError("diagnostic readback");
      return data;
    }

    return {
      get currentTexture() { requireAlive(); return stateTextures[current]; },
      get previousTexture() { requireAlive(); return stateTextures[1 - current]; },
      textureSize: size,
      nodeCount: columns.nodeCount,
      get iteration() { requireAlive(); return iteration; },
      step(iterations = 1): void {
        requireAlive();
        if (!Number.isSafeInteger(iterations) || iterations < 0) {
          throw new Error("ForceAtlas2 step requires a nonnegative safe integer iteration count");
        }
        if (paused || iterations === 0) return;
        if (!Number.isSafeInteger(iteration + iterations)) {
          throw new Error("ForceAtlas2 iteration counter exceeds exact integer capacity");
        }
        gl.useProgram(program);
        gl.bindVertexArray(vertexArray);
        gl.viewport(0, 0, size.width, size.height);
        for (const capability of [gl.BLEND, gl.DEPTH_TEST, gl.STENCIL_TEST,
          gl.SCISSOR_TEST, gl.CULL_FACE, gl.RASTERIZER_DISCARD]) gl.disable(capability);
        gl.colorMask(true, true, true, true);
        for (let pass = 0; pass < iterations; pass++) {
          const next = 1 - current;
          // The sampled state is never attached to the active draw framebuffer.
          gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, framebuffers[next]);
          const inputs = [stateTextures[current], attributesTexture, adjacencyTexture, pinsTexture];
          inputs.forEach((texture, unit) => {
            gl.activeTexture(gl.TEXTURE0 + unit);
            gl.bindTexture(gl.TEXTURE_2D, texture);
          });
          gl.drawArrays(gl.TRIANGLES, 0, 3);
          requireNoGlError("iteration");
          current = next;
          iteration++;
        }
      },
      pause(): void { requireAlive(); paused = true; },
      resume(): void { requireAlive(); paused = false; },
      setPins(indices): void {
        requireAlive();
        const flags = new Float32Array(size.width * size.height);
        for (const node of indices) {
          if (!Number.isInteger(node) || node < 0 || node >= columns.nodeCount) {
            throw new Error(`ForceAtlas2 pin index is outside the original node columns: ${node}`);
          }
          flags[node] = 1;
        }
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, pinsTexture);
        gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
        gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, size.width, size.height, gl.RED, gl.FLOAT, flags);
        requireNoGlError("pin replacement");
      },
      snapshot(): WorkspaceForceAtlas2DriverSnapshot {
        requireAlive();
        const state = readState(current), previous = readState(1 - current);
        const positions = new Float32Array(columns.nodeCount * 2);
        const force = new Float32Array(positions.length), previousForce = new Float32Array(positions.length);
        for (let node = 0; node < columns.nodeCount; node++) {
          positions.set(state.subarray(node * 4, node * 4 + 2), node * 2);
          force.set(state.subarray(node * 4 + 2, node * 4 + 4), node * 2);
          previousForce.set(previous.subarray(node * 4 + 2, node * 4 + 4), node * 2);
        }
        return { positions, force, previousForce, masses: columns.masses.slice(), iteration };
      },
      destroy(): void {
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
