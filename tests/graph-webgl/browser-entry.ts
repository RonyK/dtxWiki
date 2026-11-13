import { NativeGraph } from "../../frontend/src/features/graph/workspace/cosmosNative";
import { buildLocalCosmosColumns } from "../../frontend/src/features/graph/shared/cosmos/localScene";
import { createLocalCosmosBinding } from "../../frontend/src/features/graph/local/cosmosBinding";
import { GRAPH_EDGE_PATTERN, GRAPH_EDGE_WIDTH_PX, GRAPH_EDGE_ENDPOINT_SIZE_PX, GRAPH_LINE_OPACITY, readGraphTokens } from "../../frontend/src/features/graph/shared/graphTokens";
import type { CosmosLayoutSolver } from "../../frontend/src/features/graph/shared/cosmos/layoutTypes";
import type { GraphRenderScene } from "../../frontend/src/features/graph/shared/rendererTypes";

const KINDS = ["link", "citation", "coAccess", "membership", "action"] as const;
type Kind = (typeof KINDS)[number];
export type EdgePixels = {
  kind: Kind;
  lengthCssPx: number;
  startCssX: number;
  centerCssY: number;
  alpha: number[];
  peakRgb: [number, number, number];
  endpoints: null | {
    source: { near: number; upper: number; lower: number; hollow: number; insideNode: number };
    target: { tip: number; upper: number; lower: number; hollow: number; outsideSmallHead: number; tipShoulder: number; insideNode: number };
  };
};

export type ProbeResult = {
  cameraRatio: number;
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
  geometry: {
    triangle: null | { sourceInterior: number; targetTip: number; targetInterior: number };
    shortAction: null | { freeCssPx: number; upper: number; lower: number };
  };
};

const container = document.querySelector<HTMLDivElement>("#graph");
if (!container) throw new Error("Missing graph fixture container");
const forcedColorsActive = matchMedia("(forced-colors: active)").matches;
const tokens = readGraphTokens(container, forcedColorsActive ? "forced" : "light");
container.style.setProperty("forced-color-adjust", "none");
container.style.backgroundColor = tokens.surface;

// Exercise the production Context buffer/style binding and pinned native shader.
// No layout is needed for a five-horizontal-line raster/picking fixture.
const rows = new Map<string, { x: number; y: number; size: number; color: string }>();
const wireKinds = ["wikilink", "citation_page", "co_access", "tree", "searched"];
const scene: GraphRenderScene = { nodes: [], edges: [] };
KINDS.forEach((kind, index) => {
  // Preserve the empty corridor sampled 30 CSS px below each line, including
  // at ratio 1, while all five rows still fit the ratio .5 viewport.
  const y = 128 - index * 64;
  for (const [side, x] of [["left", -250], ["right", 250]] as const) {
    const id = kind + ":" + side;
    // The action's final dash reaches its head at ratio 1, exposing a shaft
    // that incorrectly continues under the narrowing filled triangle.
    rows.set(id, { x: kind === "action" && side === "right" ? x + 4 : x, y, size: 1, color: "#000000" });
    scene.nodes.push({ id, entity_kind: "context_page", entity_id: id, family: "document", label: id });
  }
  scene.edges.push({ id: kind, source: kind + ":left", target: kind + ":right", edge_kind: wireKinds[index],
    layer: "knowledge", label: kind, direction: kind === "coAccess" ? "undirected" : "directed" });
});
// The horizontal circle rows cannot expose the triangle's asymmetric ray
// intersections or a filled head that already fits a short free corridor.
for (const [id, x, y, size, entity_kind] of [
  ["ray:source", -320, -70, 20, "context_page_version"],
  ["ray:target", -320, 70, 20, "context_page_version"],
  ["short:source", 280, 80, 1, "context_page"],
  ["short:target", 280 + 10 / .9, 80, 1, "context_page"],
] as const) {
  rows.set(id, { x, y, size, color: "#000000" });
  scene.nodes.push({ id, entity_kind, entity_id: id, family: "document", label: id });
}
scene.edges.push(
  { id: "ray", source: "ray:source", target: "ray:target", edge_kind: "wikilink", layer: "knowledge", label: "ray", direction: "directed" },
  { id: "short", source: "short:source", target: "short:target", edge_kind: "searched", layer: "knowledge", label: "short", direction: "directed" },
);
const columns = buildLocalCosmosColumns(scene, { workspaceId: "shader", actorId: "shader", surface: "local", selectionKey: "shader", visibleFingerprint: "shader" });
const binding = createLocalCosmosBinding({} as CosmosLayoutSolver, container);
const prepared = binding.prepareScene(columns, tokens, { forcedColors: forcedColorsActive, countText: String, measureCount: () => 0 });
const native = new NativeGraph(container, {
  backgroundColor: [0, 0, 0, 0], spaceSize: 4096, fitViewOnInit: false, rescalePositions: false,
  enableSimulation: false, transitionDuration: 0, pointSizeScale: 1, scalePointsOnZoom: false,
  // As in the production core, a link listener enables the SDK picking pass.
  onLinkClick: () => undefined,
  linkDefaultWidth: GRAPH_EDGE_WIDTH_PX, linkEndpointMarkers: true,
  linkDashLength: GRAPH_EDGE_PATTERN.citation[0], linkDashGap: GRAPH_EDGE_PATTERN.citation[1],
  linkDashDotLength: GRAPH_EDGE_PATTERN.citation[2], linkDashDotGap: GRAPH_EDGE_PATTERN.citation[3],
  linkDottedDashLength: GRAPH_EDGE_PATTERN.membership[0], linkDottedDashGap: GRAPH_EDGE_PATTERN.membership[1],
  linkEndpointArrowLength: GRAPH_EDGE_ENDPOINT_SIZE_PX.arrowLength,
  linkEndpointArrowHalfWidth: GRAPH_EDGE_ENDPOINT_SIZE_PX.arrowHalfWidth,
  linkEndpointDiamondLength: GRAPH_EDGE_ENDPOINT_SIZE_PX.diamondLength,
  linkEndpointDiamondHalfWidth: GRAPH_EDGE_ENDPOINT_SIZE_PX.diamondHalfWidth,
  linkEndpointNodeGap: GRAPH_EDGE_ENDPOINT_SIZE_PX.nodeGap,
  linkVisibilityDistanceRange: [40, 350], linkVisibilityMinTransparency: GRAPH_LINE_OPACITY.fade.small.floor,
});
const graph = { getNodeAttributes: (id: string) => rows.get(id)! };
const initialCamera = { x: 0, y: 0, ratio: 1 };
let cameraState = { ...initialCamera };
const renderer = {
  getCamera: () => ({ getState: () => cameraState, setState: (next: typeof cameraState) => {
    cameraState = { ...next };
    native.setZoomTransformByPointPositions(new Float32Array([next.x, next.y]), 0, .9 / next.ratio, 0, false);
  } }),
  refresh: () => native.render(undefined, 0),
  getCanvases: () => ({ edges: container!.querySelector<HTMLCanvasElement>("canvas")! }),
  getDimensions: () => ({ width: container!.clientWidth, height: container!.clientHeight }),
  graphToViewport: (point: { x: number; y: number }) => { const projected = native.spaceToScreenPosition([point.x, point.y]); return { x: projected[0], y: projected[1] }; },
  scaleSize: (size: number) => size,
};
async function probe(ratio: number, membershipCenterPhase: 0 | 0.5 = 0): Promise<ProbeResult> {
  const camera = renderer.getCamera();
  camera.setState({ ...initialCamera, ratio });
  renderer.refresh();
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));

  // Exercise the rasterizer at both integer and half-integer CSS-pixel centers.
  // The old 0.9px membership line vanished at one of these alignments.
  const membership = graph.getNodeAttributes("membership:left");
  const firstY = renderer.graphToViewport(membership).y;
  const cameraStep = 0.01;
  camera.setState({ ...initialCamera, ratio, y: initialCamera.y + cameraStep });
  renderer.refresh();
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
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
  await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));

  const canvas = renderer.getCanvases().edges;
  const gl = canvas.getContext("webgl2");
  if (!gl) throw new Error("The edge canvas did not use WebGL2");
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
          // The canvas holds premultiplied colour: divide by alpha to read the ink.
          peakRgb = [rgba[offset], rgba[offset + 1], rgba[offset + 2]].map((value) => Math.round(value * 255 / pixelAlpha)) as [number, number, number];
        }
      }
      alpha.push(strongest);
    }
    // Read native endpoint pixels at the expected CSS-pixel size. Action has
    // a smaller filled triangle; the other directed kinds have open wings.
    const sourceNear = start.x + renderer.scaleSize(left.size) + 1.5;
    const targetTip = end.x - renderer.scaleSize(right.size) - 1.5;
    let endpoints: EdgePixels["endpoints"] = null;
    if (sourceNear >= 8 && targetTip < width - 8 && start.y >= 8 && start.y < height - 8) {
      const readNear = (x: number, y: number, radius = 0.65, minimum = false) => {
        const x0 = Math.max(0, Math.floor((x - radius) * xScale));
        const x1 = Math.min(canvas.width - 1, Math.floor((x + radius) * xScale));
        const y0 = Math.max(0, Math.floor((height - y - radius) * yScale));
        const y1 = Math.min(canvas.height - 1, Math.floor((height - y + radius) * yScale));
        const pixels = new Uint8Array((x1 - x0 + 1) * (y1 - y0 + 1) * 4);
        gl.readPixels(x0, y0, x1 - x0 + 1, y1 - y0 + 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        let value = minimum ? 255 : 0;
        for (let index = 3; index < pixels.length; index += 4) value = minimum
          ? Math.min(value, pixels[index]) : Math.max(value, pixels[index]);
        return value;
      };
      endpoints = {
        source: {
          near: readNear(sourceNear, start.y),
          upper: readNear(sourceNear + 5, start.y - 4),
          lower: readNear(sourceNear + 5, start.y + 4),
          hollow: readNear(sourceNear + 5, start.y, 0.2),
          insideNode: readNear(start.x + renderer.scaleSize(left.size) - 0.75, start.y, 0.2),
        },
        target: {
          tip: readNear(targetTip, start.y),
          upper: kind === "action" ? readNear(targetTip - 3.2, start.y - 1.2)
            : readNear(targetTip - 4, start.y - 8 / 3),
          lower: kind === "action" ? readNear(targetTip - 3.2, start.y + 1.2)
            : readNear(targetTip - 4, start.y + 8 / 3),
          // Off the shaft, inside the triangle: filled for Action, clear
          // between the larger open wings on the other directed kinds.
          hollow: kind === "action" ? readNear(targetTip - 2.5, start.y + 0.4, 0.2)
            : readNear(targetTip - 5, start.y + 1.8, 0.45, true),
          outsideSmallHead: readNear(targetTip - 5, start.y + 3.5, 0.2),
          tipShoulder: Math.max(readNear(targetTip - 0.25, start.y - 0.75, 0.01),
            readNear(targetTip - 0.25, start.y + 0.75, 0.01)),
          insideNode: readNear(end.x - renderer.scaleSize(right.size) + 0.75, start.y, 0.2),
        },
      };
    }
    return {
      kind,
      lengthCssPx: alpha.length,
      startCssX: minX,
      centerCssY: start.y,
      alpha,
      peakRgb,
      endpoints,
    };
  });

  const readPoint = (x: number, y: number, radius = .2) => {
    const x0 = Math.max(0, Math.floor((x - radius) * xScale)), x1 = Math.min(canvas.width - 1, Math.floor((x + radius) * xScale));
    const y0 = Math.max(0, Math.floor((height - y - radius) * yScale)), y1 = Math.min(canvas.height - 1, Math.floor((height - y + radius) * yScale));
    const pixels = new Uint8Array((x1 - x0 + 1) * (y1 - y0 + 1) * 4);
    gl.readPixels(x0, y0, x1 - x0 + 1, y1 - y0 + 1, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    let alpha = 0;
    for (let index = 3; index < pixels.length; index += 4) alpha = Math.max(alpha, pixels[index]);
    return alpha;
  };
  const visible = (point: { x: number; y: number }) => point.x > 24 && point.x < width - 24 && point.y > 24 && point.y < height - 24;
  const raySource = renderer.graphToViewport(rows.get("ray:source")!), rayTarget = renderer.graphToViewport(rows.get("ray:target")!);
  const shortSource = renderer.graphToViewport(rows.get("short:source")!), shortTarget = renderer.graphToViewport(rows.get("short:target")!);
  const shortTip = shortTarget.x - 2.5;
  const freeCssPx = shortTarget.x - shortSource.x - 5;
  const geometry: ProbeResult["geometry"] = {
    triangle: visible(raySource) && visible(rayTarget) ? {
      sourceInterior: readPoint(raySource.x, raySource.y - 16),
      targetTip: readPoint(rayTarget.x, rayTarget.y + 11.5, .65),
      targetInterior: readPoint(rayTarget.x, rayTarget.y + 9.25),
    } : null,
    shortAction: visible(shortSource) && visible(shortTarget) && freeCssPx >= 4 ? {
      freeCssPx,
      upper: readPoint(shortTip - 3.25, shortSource.y - 1.5),
      lower: readPoint(shortTip - 3.25, shortSource.y + 1.5),
    } : null,
  };

  return {
    cameraRatio: ratio,
    webglVersion: gl.getParameter(gl.VERSION) as string,
    pixelRatio: xScale,
    angleAvailable: true,
    forcedColorsActive,
    tokenInk: tokens.edge.link,
    tokenSurface: tokens.surface,
    diagnostics,
    edges,
    geometry,
  };
}

// Read the native link picking framebuffer directly, as the prior shader
// fixture did. A visible dash gap must remain a usable native link target.
function pick(x: number, y: number): string | null {
  const engine = native as unknown as { store: { screenMousePosition: number[]; mousePosition: number[] }; lines: { pickLinkSync(): number | undefined } };
  // The SDK stores link-picking coordinates with a bottom-left origin.
  engine.store.screenMousePosition = [x, container!.clientHeight - y];
  engine.store.mousePosition = native.screenToSpacePosition([x, y]);
  const index = engine.lines.pickLinkSync();
  return index === undefined ? null : columns.edgeIds[index] ?? null;
}
void native.ready.then(() => {
  native.setPointPositions(Float32Array.from(columns.nodeIds.flatMap(id => { const p = rows.get(id)!; return [p.x, p.y]; })));
  // Keep node footprints for clipping but leave the combined framebuffer's
  // glyph pixels transparent, so samples isolate the relationship stroke.
  native.setPointSizes(Float32Array.from(columns.nodeIds, id => rows.get(id)!.size * 2));
  native.setPointShapes(prepared.pointShapes);
  native.setPointColors(new Float32Array(columns.nodeIds.length * 4));
  native.setLinks(prepared.links); native.setLinkColors(prepared.linkColors); native.setLinkStyles(prepared.linkStyles);
  renderer.getCamera().setState(initialCamera); native.render(undefined, 0);
  Object.assign(window, { __graphWebglProbe: probe, __graphWebglPick: pick });
});
