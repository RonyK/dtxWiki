export type WorkspaceWebGLSupport = { supported: true } | {
  supported: false;
  reason: "webgl2" | "float-render-target";
};

/** Probe before importing the large Map engine; an accessible list remains usable. */
export function detectWorkspaceMapWebGL(): WorkspaceWebGLSupport {
  if (typeof document === "undefined") return { supported: false, reason: "webgl2" };
  const canvas = document.createElement("canvas");
  const gl = canvas.getContext("webgl2", { antialias: false });
  if (!gl) return { supported: false, reason: "webgl2" };
  const supported = Boolean(gl.getExtension("EXT_color_buffer_float"));
  gl.getExtension("WEBGL_lose_context")?.loseContext();
  return supported ? { supported: true } : { supported: false, reason: "float-render-target" };
}
