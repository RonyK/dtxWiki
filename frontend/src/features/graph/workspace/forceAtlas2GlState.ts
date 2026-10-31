interface TextureUnitState {
  unit: number;
  texture: WebGLTexture | null;
  sampler: WebGLSampler | null;
}

/**
 * Synchronous binding guard for the FA2 numerical core and its GPU position
 * copy. Restoring the actual GL state keeps an enclosing luma state cache
 * truthful; this does not access or invalidate private luma/Cosmos state.
 *
 * Scope is deliberately bounded: program, VAO, framebuffers/read buffer,
 * viewport, texture-2D/sampler bindings on units 0..3, the six core enable flags,
 * color mask, and typed-array pixel transfer bindings/layout. It does not guard
 * arbitrary GL state, texture/VAO contents, uniforms of caller-owned programs,
 * or resources deleted by the action. The action must not mutate those caller
 * resources or use other texture units. Only typed-array 2D transfers are used;
 * DOM-image flip/premultiplication/colorspace state is outside this scope.
 *
 * The action MUST finish synchronously. Returning a thenable is rejected; this
 * cannot make an already-started asynchronous GL action safe after an await.
 * No coordinates, texture contents, or framebuffer pixels are read back here.
 */
export function withWorkspaceForceAtlas2GlState<T>(
  gl: WebGL2RenderingContext,
  action: () => T,
): T {
  if (gl.isContextLost()) throw new Error("ForceAtlas2 GL state guard requires a live context");
  const program = gl.getParameter(gl.CURRENT_PROGRAM) as WebGLProgram | null;
  const vertexArray = gl.getParameter(gl.VERTEX_ARRAY_BINDING) as WebGLVertexArrayObject | null;
  const drawFramebuffer = gl.getParameter(gl.DRAW_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
  const readFramebuffer = gl.getParameter(gl.READ_FRAMEBUFFER_BINDING) as WebGLFramebuffer | null;
  const readBuffer = gl.getParameter(gl.READ_BUFFER) as number;
  const viewport = Array.from(gl.getParameter(gl.VIEWPORT) as Int32Array);
  const activeTexture = gl.getParameter(gl.ACTIVE_TEXTURE) as number;
  const colorMask = Array.from(gl.getParameter(gl.COLOR_WRITEMASK) as boolean[]);
  const pixelPackBuffer = gl.getParameter(gl.PIXEL_PACK_BUFFER_BINDING) as WebGLBuffer | null;
  const pixelUnpackBuffer = gl.getParameter(gl.PIXEL_UNPACK_BUFFER_BINDING) as WebGLBuffer | null;
  const capabilities = [gl.BLEND, gl.DEPTH_TEST, gl.STENCIL_TEST, gl.SCISSOR_TEST,
    gl.CULL_FACE, gl.RASTERIZER_DISCARD].map((capability) => ({ capability, enabled: gl.isEnabled(capability) }));
  const alignments = [gl.PACK_ALIGNMENT, gl.UNPACK_ALIGNMENT]
    .map((parameter) => ({ parameter, value: gl.getParameter(parameter) as number }));
  const transferOffsets = [gl.PACK_ROW_LENGTH, gl.PACK_SKIP_PIXELS, gl.PACK_SKIP_ROWS,
    gl.UNPACK_ROW_LENGTH, gl.UNPACK_IMAGE_HEIGHT, gl.UNPACK_SKIP_PIXELS,
    gl.UNPACK_SKIP_ROWS, gl.UNPACK_SKIP_IMAGES]
    .map((parameter) => ({ parameter, value: gl.getParameter(parameter) as number }));
  const textureUnits: TextureUnitState[] = [];

  try {
    for (let unit = 0; unit < 4; unit++) {
      gl.activeTexture(gl.TEXTURE0 + unit);
      textureUnits.push({ unit,
        texture: gl.getParameter(gl.TEXTURE_BINDING_2D) as WebGLTexture | null,
        sampler: gl.getParameter(gl.SAMPLER_BINDING) as WebGLSampler | null });
    }
    // Existing sampler objects can override the core textures' NEAREST filters.
    // PBO bindings/strides would change or invalidate its typed-array transfers.
    for (const { unit } of textureUnits) gl.bindSampler(unit, null);
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
    gl.bindBuffer(gl.PIXEL_UNPACK_BUFFER, null);
    for (const { parameter } of alignments) gl.pixelStorei(parameter, 4);
    for (const { parameter } of transferOffsets) gl.pixelStorei(parameter, 0);
    gl.activeTexture(activeTexture);

    const result = action();
    if (result !== null && (typeof result === "object" || typeof result === "function")
      && typeof (result as { then?: unknown }).then === "function") {
      throw new Error("ForceAtlas2 GL state guard action must be synchronous, not a thenable");
    }
    return result;
  } finally {
    gl.useProgram(program);
    gl.bindVertexArray(vertexArray);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, drawFramebuffer);
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, readFramebuffer);
    gl.readBuffer(readBuffer);
    gl.viewport(viewport[0], viewport[1], viewport[2], viewport[3]);
    for (const { unit, texture, sampler } of textureUnits) {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.bindSampler(unit, sampler);
    }
    gl.activeTexture(activeTexture);
    for (const { capability, enabled } of capabilities) {
      if (enabled) gl.enable(capability);
      else gl.disable(capability);
    }
    gl.colorMask(colorMask[0], colorMask[1], colorMask[2], colorMask[3]);
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, pixelPackBuffer);
    gl.bindBuffer(gl.PIXEL_UNPACK_BUFFER, pixelUnpackBuffer);
    for (const { parameter, value } of [...alignments, ...transferOffsets]) gl.pixelStorei(parameter, value);
  }
}
