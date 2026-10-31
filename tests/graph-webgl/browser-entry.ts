import Graph from "graphology";
import Sigma from "sigma";

import { LOCAL_EDGE_PROGRAMS } from "../../frontend/src/features/graph/local/edgePrograms";
import { readGraphTokens } from "../../frontend/src/features/graph/shared/graphTokens";

const KINDS = ["link", "citation", "coAccess", "membership", "action"] as const;
type Kind = (typeof KINDS)[number];
type FixtureNode = { x: number; y: number; size: number; color: string };
type FixtureEdge = { type: Kind; size: number; color: string };

export type EdgePixels = {
  kind: Kind;
  lengthCssPx: number;
  startCssX: number;
  centerCssY: number;
  alpha: number[];
  peakRgb: [number, number, number];
};

export type ProbeResult = {
  webglVersion: string;
  pixelRatio: number;
  angleAvailable: boolean;
  forcedColorsActive: boolean;
  tokenInk: string;
  tokenSurface: string;
  diagnostics: {
    contextLost: boolean;
    defaultFramebuffer: boolean;
    framebufferComplete: boolean;
    drawingBuffer: [number, number];
    viewport: number[];
    colorMask: boolean[];
    blend: boolean;
    currentProgramLinked: boolean | null;
    shaders: Array<{ type: number; compiled: boolean }>;
  };
  edges: EdgePixels[];
};

const container = document.querySelector<HTMLElement>("#graph");
if (!container) throw new Error("Missing graph fixture container");
const forcedColorsActive = matchMedia("(forced-colors: active)").matches;
const tokens = readGraphTokens(container, forcedColorsActive ? "forced" : "light");
// Canvas pixels cannot be recolored by forced-colors CSS after they are drawn.
// Resolve the system ink/surface first, then retain those explicit colors.
container.style.setProperty("forced-color-adjust", "none");
container.style.backgroundColor = tokens.surface;

const graph = new Graph<FixtureNode, FixtureEdge>();
for (const [index, kind] of KINDS.entries()) {
  // Keep all rows in frame even when the camera is zoomed to ratio 0.5.
  const y = 0.37 + index * 0.065;
  graph.addNode(`${kind}:left`, { x: 0.08, y, size: 1, color: "#000000" });
  graph.addNode(`${kind}:right`, { x: 0.92, y, size: 1, color: "#000000" });
  graph.addEdgeWithKey(kind, `${kind}:left`, `${kind}:right`, {
    type: kind,
    size: 2,
    color: tokens.edge[kind],
  });
}

const renderer = new Sigma(graph, container, {
  edgeProgramClasses: LOCAL_EDGE_PROGRAMS,
  renderLabels: false,
  renderEdgeLabels: false,
  // Also compile and draw the PICKING_MODE variant of every edge shader.
  enableEdgeEvents: true,
  minEdgeThickness: 0,
});
const initialCamera = renderer.getCamera().getState();

function probe(ratio: number, membershipCenterPhase: 0 | 0.5 = 0): ProbeResult {
  const camera = renderer.getCamera();
  camera.setState({ ...initialCamera, ratio });
  renderer.refresh();

  // Exercise the rasterizer at both integer and half-integer CSS-pixel centers.
  // The old 0.9px membership line vanished at one of these alignments.
  const membership = graph.getNodeAttributes("membership:left");
  const firstY = renderer.graphToViewport(membership).y;
  const cameraStep = 0.01;
  camera.setState({ ...initialCamera, ratio, y: initialCamera.y + cameraStep });
  renderer.refresh();
  const shiftedY = renderer.graphToViewport(membership).y;
  const pixelsPerCameraY = (shiftedY - firstY) / cameraStep;
  if (Math.abs(pixelsPerCameraY) < 0.001) throw new Error("Camera phase calibration failed");
  const deltaPx = membershipCenterPhase - (firstY - Math.floor(firstY));
  camera.setState({
    ...initialCamera,
    ratio,
    y: initialCamera.y + deltaPx / pixelsPerCameraY,
  });
  renderer.refresh();

  const canvas = renderer.getCanvases().edges;
  const gl = canvas.getContext("webgl");
  if (!gl) throw new Error("The edge canvas did not use WebGL1");
  gl.finish();
  const program = gl.getParameter(gl.CURRENT_PROGRAM) as WebGLProgram | null;
  const diagnostics: ProbeResult["diagnostics"] = {
    contextLost: gl.isContextLost(),
    defaultFramebuffer: gl.getParameter(gl.FRAMEBUFFER_BINDING) === null,
    framebufferComplete: gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE,
    drawingBuffer: [gl.drawingBufferWidth, gl.drawingBufferHeight],
    viewport: Array.from(gl.getParameter(gl.VIEWPORT) as Int32Array),
    colorMask: gl.getParameter(gl.COLOR_WRITEMASK) as boolean[],
    blend: gl.isEnabled(gl.BLEND),
    currentProgramLinked: program ? Boolean(gl.getProgramParameter(program, gl.LINK_STATUS)) : null,
    shaders: program ? (gl.getAttachedShaders(program) ?? []).map((shader) => ({
      type: gl.getShaderParameter(shader, gl.SHADER_TYPE) as number,
      compiled: Boolean(gl.getShaderParameter(shader, gl.COMPILE_STATUS)),
    })) : [],
  };

  const { width, height } = renderer.getDimensions();
  const xScale = canvas.width / width;
  const yScale = canvas.height / height;

  const edges = KINDS.map((kind): EdgePixels => {
    const left = graph.getNodeAttributes(`${kind}:left`);
    const right = graph.getNodeAttributes(`${kind}:right`);
    const start = renderer.graphToViewport(left);
    const end = renderer.graphToViewport(right);
    const minX = Math.max(8, Math.ceil(Math.min(start.x, end.x) + 16));
    const maxX = Math.min(width - 8, Math.floor(Math.max(start.x, end.x) - 16));
    if (maxX - minX < 60) throw new Error(`Edge ${kind} is too short to sample`);

    const centerY = canvas.height - 1 - Math.floor(start.y * yScale);
    if (centerY < 2 || centerY > canvas.height - 3) {
      throw new Error(`Edge ${kind} left viewport at ratio ${ratio}: y=${start.y}, canvas=${canvas.height}, css=${height}`);
    }
    const bottomY = Math.max(0, centerY - 2);
    const rows = Math.min(5, canvas.height - bottomY);
    const rgba = new Uint8Array(canvas.width * rows * 4);
    gl.readPixels(0, bottomY, canvas.width, rows, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
    const error = gl.getError();
    if (error !== gl.NO_ERROR) {
      throw new Error(`WebGL readPixels failed for ${kind}: 0x${error.toString(16)}; canvas=${canvas.width}x${canvas.height}, y=${bottomY}, rows=${rows}, css=${width}x${height}, nodeY=${start.y}`);
    }

    const alpha: number[] = [];
    let peakAlpha = -1;
    let peakRgb: [number, number, number] = [0, 0, 0];
    for (let x = minX; x <= maxX; x++) {
      const physicalX = Math.min(canvas.width - 1, Math.floor((x + 0.5) * xScale));
      let strongest = 0;
      for (let row = 0; row < rows; row++) {
        const offset = (row * canvas.width + physicalX) * 4;
        const pixelAlpha = rgba[offset + 3];
        strongest = Math.max(strongest, pixelAlpha);
        if (pixelAlpha > peakAlpha) {
          peakAlpha = pixelAlpha;
          peakRgb = [rgba[offset], rgba[offset + 1], rgba[offset + 2]];
        }
      }
      alpha.push(strongest);
    }
    return {
      kind,
      lengthCssPx: alpha.length,
      startCssX: minX,
      centerCssY: start.y,
      alpha,
      peakRgb,
    };
  });

  return {
    webglVersion: gl.getParameter(gl.VERSION) as string,
    pixelRatio: xScale,
    angleAvailable: gl.getExtension("ANGLE_instanced_arrays") !== null,
    forcedColorsActive,
    tokenInk: tokens.edge.link,
    tokenSurface: tokens.surface,
    diagnostics,
    edges,
  };
}

// Sigma's own lookup samples its reduced-resolution edge-picking framebuffer.
// A gap should remain a usable pointer target even when the visible layer
// discards its fragments there.
function pick(x: number, y: number): string | null {
  return (renderer as unknown as {
    getEdgeAtPoint: (x: number, y: number) => string | null;
  }).getEdgeAtPoint(x, y);
}

Object.assign(window, { __graphWebglProbe: probe, __graphWebglPick: pick });
