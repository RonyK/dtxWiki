export type WorkspaceWebGLSupport = { supported: true; floatBlend: boolean } | {
  supported: false;
  reason: "webgl2" | "float-render-target";
};

/** Probe before importing the large Map engine; an accessible list remains usable. */
export function detectWorkspaceMapWebGL(): WorkspaceWebGLSupport {
  if (typeof document === "undefined") return { supported: false, reason: "webgl2" };
  const canvas = document.createElement("canvas");
  const gl = canvas.getContext("webgl2", { antialias: false });
  if (!gl) return { supported: false, reason: "webgl2" };
  try {
    if (!gl.getExtension("EXT_color_buffer_float")) return { supported: false, reason: "float-render-target" };
    // Rendering a CPU layout needs float targets; native collision also needs
    // float blending. Its absence must not reject the render-only 2K view.
    return { supported: true, floatBlend: Boolean(gl.getExtension("EXT_float_blend")) };
  } finally {
    gl.getExtension("WEBGL_lose_context")?.loseContext();
  }
}
