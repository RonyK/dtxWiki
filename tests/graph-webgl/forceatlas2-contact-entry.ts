import { createWorkspaceForceAtlas2Contact, type WorkspaceForceAtlas2Contact } from "../../frontend/src/features/graph/workspace/forceAtlas2Contact";
import { withWorkspaceForceAtlas2GlState } from "../../frontend/src/features/graph/workspace/forceAtlas2GlState";

export type ContactFixture = {
  state: number[];
  radii: number[];
  pins?: number[];
  iterations?: number;
  sourceDimensions?: { width: number; height: number };
};

type Dimensions = { width: number; height: number };
type Sample = { state: number[]; positions: number[]; force: number[] };

function context() {
  const canvas = document.createElement("canvas"), gl = canvas.getContext("webgl2", { antialias: false });
  if (!gl || !gl.getExtension("EXT_color_buffer_float")) throw new Error("Float32 WebGL2 contact targets required");
  return gl;
}

function source(gl: WebGL2RenderingContext, fixture: ContactFixture) {
  const dimensions = fixture.sourceDimensions ?? { width: fixture.radii.length, height: 1 };
  if (fixture.state.length !== fixture.radii.length * 4) throw new Error("Fixture RGBA rows disagree");
  const texture = gl.createTexture();
  if (!texture) throw new Error("Contact fixture source allocation failed");
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA32F, dimensions.width, dimensions.height);
  const data = new Float32Array(dimensions.width * dimensions.height * 4);
  data.set(fixture.state);
  gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, dimensions.width, dimensions.height, gl.RGBA, gl.FLOAT, data);
  return { texture, dimensions };
}

function read(gl: WebGL2RenderingContext, texture: WebGLTexture, dimensions: Dimensions, count: number): Sample {
  return withWorkspaceForceAtlas2GlState(gl, () => {
    const framebuffer = gl.createFramebuffer();
    if (!framebuffer) throw new Error("Contact diagnostic framebuffer allocation failed");
    try {
      gl.bindFramebuffer(gl.READ_FRAMEBUFFER, framebuffer);
      gl.framebufferTexture2D(gl.READ_FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
      if (gl.checkFramebufferStatus(gl.READ_FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error("Contact diagnostic framebuffer incomplete");
      gl.readBuffer(gl.COLOR_ATTACHMENT0);
      const data = new Float32Array(dimensions.width * dimensions.height * 4);
      gl.readPixels(0, 0, dimensions.width, dimensions.height, gl.RGBA, gl.FLOAT, data);
      const state = Array.from(data.slice(0, count * 4)), positions: number[] = [], force: number[] = [];
      for (let index = 0; index < count; index++) {
        positions.push(state[index * 4], state[index * 4 + 1]);
        force.push(state[index * 4 + 2], state[index * 4 + 3]);
      }
      return { state, positions, force };
    } finally { gl.deleteFramebuffer(framebuffer); }
  });
}

function run(fixture: ContactFixture) {
  const gl = context(), original = source(gl, fixture), errors: number[] = [gl.getError()];
  const contact = withWorkspaceForceAtlas2GlState(gl, () => createWorkspaceForceAtlas2Contact(gl,
    { radii: Float32Array.from(fixture.radii), pins: fixture.pins }));
  try {
    const before = read(gl, original.texture, original.dimensions, contact.nodeCount);
    const output = withWorkspaceForceAtlas2GlState(gl, () => contact.project(original.texture, original.dimensions, fixture.iterations));
    const result = read(gl, output, contact.textureSize, contact.nodeCount);
    // Reusing an owned output must choose the opposite draw attachment.
    const reused = withWorkspaceForceAtlas2GlState(gl, () => contact.project(output, contact.textureSize, 1));
    const reprojected = read(gl, reused, contact.textureSize, contact.nodeCount);
    const after = read(gl, original.texture, original.dimensions, contact.nodeCount);
    errors.push(gl.getError());
    contact.destroy(); contact.destroy();
    let destroyedProject = false, destroyedPins = false;
    try { contact.project(original.texture, original.dimensions); } catch { destroyedProject = true; }
    try { contact.setPins([]); } catch { destroyedPins = true; }
    errors.push(gl.getError());
    return { input: fixture, before, result, reprojected, after, dimensions: contact.textureSize,
      errors, destroyedProject, destroyedPins };
  } finally { contact.destroy(); gl.deleteTexture(original.texture); gl.getExtension("WEBGL_lose_context")?.loseContext(); }
}

function pinMutation() {
  const fixture = { state: [-.5, 0, 3, -4, .5, 0, 5, -6], radii: [1, 1] };
  const gl = context(), original = source(gl, fixture), errors: number[] = [gl.getError()];
  const contact = withWorkspaceForceAtlas2GlState(gl, () => createWorkspaceForceAtlas2Contact(gl, { radii: Float32Array.from(fixture.radii) }));
  const samples: Array<{ pins: number[]; error: string | null; sample: Sample }> = [];
  try {
    for (const pins of [[0], [1], [], [0, 1], [0, 2], []]) {
      let error: string | null = null;
      try { withWorkspaceForceAtlas2GlState(gl, () => contact.setPins(pins)); }
      catch (failure) { error = failure instanceof Error ? failure.message : String(failure); }
      const output = withWorkspaceForceAtlas2GlState(gl, () => contact.project(original.texture, original.dimensions));
      samples.push({ pins, error, sample: read(gl, output, contact.textureSize, 2) }); errors.push(gl.getError());
    }
    return { input: fixture, samples, errors, sourceAfter: read(gl, original.texture, original.dimensions, 2) };
  } finally { contact.destroy(); gl.deleteTexture(original.texture); gl.getExtension("WEBGL_lose_context")?.loseContext(); }
}

function invalid(kind: string) {
  const gl = context();
  let contact: WorkspaceForceAtlas2Contact | null = null;
  try {
    const radii = kind === "wrong-type" ? new Float64Array([1, 1])
      : Float32Array.from(kind === "empty" ? [] : kind === "zero" ? [0, 1]
        : kind === "nonfinite" ? [Infinity, 1] : kind === "underflow" ? [1e-50, 1] : [1, 1]);
    contact = withWorkspaceForceAtlas2GlState(gl, () => createWorkspaceForceAtlas2Contact(gl,
      { radii: radii as Float32Array, pins: kind === "invalid-pin" ? [2] : [] }));
    return { error: null, gl: gl.getError() };
  } catch (failure) {
    return { error: failure instanceof Error ? failure.message : String(failure), gl: gl.getError() };
  } finally { contact?.destroy(); gl.getExtension("WEBGL_lose_context")?.loseContext(); }
}

const PIXEL_PARAMETERS = ["PACK_ALIGNMENT", "UNPACK_ALIGNMENT", "PACK_ROW_LENGTH", "PACK_SKIP_PIXELS", "PACK_SKIP_ROWS",
  "UNPACK_ROW_LENGTH", "UNPACK_IMAGE_HEIGHT", "UNPACK_SKIP_PIXELS", "UNPACK_SKIP_ROWS", "UNPACK_SKIP_IMAGES"] as const;
const CAPABILITIES = ["BLEND", "DEPTH_TEST", "STENCIL_TEST", "SCISSOR_TEST", "CULL_FACE", "RASTERIZER_DISCARD"] as const;

function captureState(gl: WebGL2RenderingContext) {
  const activeTexture = gl.getParameter(gl.ACTIVE_TEXTURE) as number;
  const units = Array.from({ length: 4 }, (_, unit) => {
    gl.activeTexture(gl.TEXTURE0 + unit);
    return { texture: gl.getParameter(gl.TEXTURE_BINDING_2D) as WebGLTexture | null,
      sampler: gl.getParameter(gl.SAMPLER_BINDING) as WebGLSampler | null };
  });
  gl.activeTexture(activeTexture);
  return {
    program: gl.getParameter(gl.CURRENT_PROGRAM) as WebGLProgram | null,
    vao: gl.getParameter(gl.VERTEX_ARRAY_BINDING) as WebGLVertexArrayObject | null,
    draw: gl.getParameter(gl.DRAW_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null,
    read: gl.getParameter(gl.READ_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null,
    readBuffer: gl.getParameter(gl.READ_BUFFER) as number,
    pack: gl.getParameter(gl.PIXEL_PACK_BUFFER_BINDING) as WebGLBuffer | null,
    unpack: gl.getParameter(gl.PIXEL_UNPACK_BUFFER_BINDING) as WebGLBuffer | null,
    viewport: Array.from(gl.getParameter(gl.VIEWPORT) as Int32Array), activeTexture, units,
    colorMask: Array.from(gl.getParameter(gl.COLOR_WRITEMASK) as boolean[]),
    flags: CAPABILITIES.map(name => gl.isEnabled(gl[name])),
    pixel: PIXEL_PARAMETERS.map(name => gl.getParameter(gl[name]) as number),
  };
}

function compareState(before: ReturnType<typeof captureState>, after: ReturnType<typeof captureState>) {
  return { program: before.program === after.program, vao: before.vao === after.vao,
    draw: before.draw === after.draw, read: before.read === after.read, readBuffer: before.readBuffer === after.readBuffer,
    pack: before.pack === after.pack, unpack: before.unpack === after.unpack, activeTexture: before.activeTexture === after.activeTexture,
    viewport: JSON.stringify(before.viewport) === JSON.stringify(after.viewport),
    colorMask: JSON.stringify(before.colorMask) === JSON.stringify(after.colorMask),
    flags: JSON.stringify(before.flags) === JSON.stringify(after.flags), pixel: JSON.stringify(before.pixel) === JSON.stringify(after.pixel),
    units: before.units.map((unit, index) => ({ texture: unit.texture === after.units[index].texture, sampler: unit.sampler === after.units[index].sampler })) };
}

function stateGuard() {
  const gl = context(), fixture = { state: [-.5, 0, 3, -4, .5, 0, 5, -6], radii: [1, 1] };
  const original = source(gl, fixture), shaders: WebGLShader[] = [], textures: WebGLTexture[] = [], samplers: WebGLSampler[] = [];
  const framebuffers: WebGLFramebuffer[] = [], buffers: WebGLBuffer[] = [], errors: number[] = [];
  const program = gl.createProgram(), vao = gl.createVertexArray();
  if (!program || !vao) throw new Error("Caller sentinel allocation failed");
  let contact: WorkspaceForceAtlas2Contact | null = null;
  const ownedContacts: WorkspaceForceAtlas2Contact[] = [];
  try {
    for (const [type, code] of [[gl.VERTEX_SHADER, "#version 300 es\nvoid main(){gl_Position=vec4(0.0,0.0,0.0,1.0);}"],
      [gl.FRAGMENT_SHADER, "#version 300 es\nprecision highp float;out vec4 color;void main(){color=vec4(1.0);}"]] as const) {
      const shader = gl.createShader(type); if (!shader) throw new Error("Caller shader allocation failed");
      shaders.push(shader); gl.shaderSource(shader, code); gl.compileShader(shader); gl.attachShader(program, shader);
    }
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error("Caller sentinel program link failed");
    for (let unit = 0; unit < 4; unit++) {
      const texture = gl.createTexture(), sampler = gl.createSampler();
      if (!texture || !sampler) throw new Error("Caller texture/sampler allocation failed");
      textures.push(texture); samplers.push(sampler); gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, texture); gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA8, 2, 2);
      gl.samplerParameteri(sampler, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.samplerParameteri(sampler, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.bindSampler(unit, sampler);
    }
    for (const target of [gl.DRAW_FRAMEBUFFER, gl.READ_FRAMEBUFFER]) {
      const framebuffer = gl.createFramebuffer(), texture = gl.createTexture();
      if (!framebuffer || !texture) throw new Error("Caller framebuffer allocation failed");
      framebuffers.push(framebuffer); textures.push(texture); gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA8, 2, 2); gl.bindFramebuffer(target, framebuffer);
      gl.framebufferTexture2D(target, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);
      if (gl.checkFramebufferStatus(target) !== gl.FRAMEBUFFER_COMPLETE) throw new Error("Caller framebuffer incomplete");
    }
    gl.bindTexture(gl.TEXTURE_2D, textures[3]); gl.readBuffer(gl.COLOR_ATTACHMENT0);
    for (const target of [gl.PIXEL_PACK_BUFFER, gl.PIXEL_UNPACK_BUFFER]) {
      const buffer = gl.createBuffer(); if (!buffer) throw new Error("Caller PBO allocation failed");
      buffers.push(buffer); gl.bindBuffer(target, buffer); gl.bufferData(target, 4096, gl.DYNAMIC_COPY);
    }
    PIXEL_PARAMETERS.forEach(name => gl.pixelStorei(gl[name], name.endsWith("ALIGNMENT") ? 8 : 1));
    CAPABILITIES.forEach(name => gl.enable(gl[name])); gl.colorMask(false, true, false, true);
    gl.viewport(3, 4, 17, 19); gl.useProgram(program); gl.bindVertexArray(vao); gl.activeTexture(gl.TEXTURE2);
    const before = captureState(gl);
    const observations: Array<{ name: string; threw: boolean; error: string | null; restored: ReturnType<typeof compareState> }> = [];
    function exercise(name: string, action: () => void) {
      let error: string | null = null;
      try { withWorkspaceForceAtlas2GlState(gl, action); }
      catch (failure) { error = failure instanceof Error ? failure.message : String(failure); }
      observations.push({ name, threw: error !== null, error, restored: compareState(before, captureState(gl)) }); errors.push(gl.getError());
    }
    exercise("factory", () => { contact = createWorkspaceForceAtlas2Contact(gl, { radii: Float32Array.from(fixture.radii) }); ownedContacts.push(contact); });
    exercise("project", () => { contact!.project(original.texture, original.dimensions); });
    exercise("project-then-throw", () => { contact!.project(original.texture, original.dimensions); throw new Error("Deliberate contact project throw"); });
    exercise("factory-then-throw", () => { ownedContacts.push(createWorkspaceForceAtlas2Contact(gl, { radii: Float32Array.from(fixture.radii) })); throw new Error("Deliberate contact factory throw"); });
    exercise("invalid-factory", () => { createWorkspaceForceAtlas2Contact(gl, { radii: new Float64Array([1, 1]) as unknown as Float32Array }); });
    exercise("invalid-project", () => { contact!.project(original.texture, original.dimensions, 0); });
    const projected = withWorkspaceForceAtlas2GlState(gl, () => contact!.project(original.texture, original.dimensions));
    const result = read(gl, projected, contact!.textureSize, 2); errors.push(gl.getError());
    return { observations, result, errors, caller: { viewport: before.viewport, activeUnit: before.activeTexture - gl.TEXTURE0,
      flags: before.flags, pixel: before.pixel, colorMask: before.colorMask,
      programPresent: before.program !== null, vaoPresent: before.vao !== null,
      distinctFramebuffers: before.draw !== null && before.read !== null && before.draw !== before.read,
      packPresent: before.pack !== null, unpackPresent: before.unpack !== null,
      allTexturesAndSamplersPresent: before.units.every(unit => unit.texture !== null && unit.sampler !== null) } };
  } finally {
    for (const item of ownedContacts) item.destroy();
    gl.useProgram(null); gl.bindVertexArray(null);
    for (const shader of shaders) gl.deleteShader(shader);
    for (const texture of textures) gl.deleteTexture(texture);
    for (const sampler of samplers) gl.deleteSampler(sampler);
    for (const framebuffer of framebuffers) gl.deleteFramebuffer(framebuffer);
    for (const buffer of buffers) gl.deleteBuffer(buffer);
    gl.deleteProgram(program); gl.deleteVertexArray(vao); gl.deleteTexture(original.texture);
    gl.getExtension("WEBGL_lose_context")?.loseContext();
  }
}

declare global {
  interface Window {
    __forceAtlas2Contact: { run: typeof run; pinMutation: typeof pinMutation; invalid: typeof invalid; stateGuard: typeof stateGuard };
  }
}
window.__forceAtlas2Contact = { run, pinMutation, invalid, stateGuard };
