import {
  createEdgeArrowHeadProgram,
  createEdgeCompoundProgram,
  EdgeProgram,
  type ProgramDefinition,
  type ProgramInfo,
} from "sigma/rendering";
import type Sigma from "sigma";
import type { EdgeDisplayData, NodeDisplayData, RenderParams } from "sigma/types";
import { parseColor, rgbaToFloat } from "sigma/utils";

import {
  GRAPH_EDGE_ARROW, GRAPH_EDGE_PATTERN, GRAPH_EDGE_WIDTH_PX, GRAPH_LINE_INK, graphLineFade, type GraphEdgeVisualKind,
} from "../shared/graphTokens";

// Every line has the legend's one CSS-pixel width; half of it on each side.
export const LOCAL_EDGE_HALF_WIDTH_PX = GRAPH_EDGE_WIDTH_PX / 2;

/**
 * The edge reducer's line state, as on the Map: "selected" while a selection
 * or history step is emphasised (opaque), "forced" in forced colours (ink, no fade).
 */
export type LocalLineState = "rest" | "selected" | "forced";

// Lines are measured in the camera of the renderer's last Fit, so the reader's
// zoom never changes a line's opacity. Before any Fit: Sigma's home view.
const HOME_CAMERA = { x: 0.5, y: 0.5, angle: 0, ratio: 1 };
const fitCameras = new WeakMap<Sigma, typeof HOME_CAMERA>();

/** Records the camera ratio a Fit used: GRAPH_LINE_FADE measures lines in that view. */
export function setLocalFitRatio(renderer: Sigma, ratio: number): void {
  fitCameras.set(renderer, { ...HOME_CAMERA, ratio });
}

/** The Map's line opacity rules (GRAPH_LINE_INK, GRAPH_LINE_FADE) for one Context line. */
function lineOpacity(renderer: Sigma, source: NodeDisplayData, target: NodeDisplayData, data: EdgeDisplayData): number {
  const state = (data as EdgeDisplayData & { lineState?: LocalLineState }).lineState;
  if (state === "selected") return 1;
  const ink = data.type === "citation" ? GRAPH_LINE_INK.citation : GRAPH_LINE_INK.line;
  if (state === "forced") return ink;
  const cameraState = fitCameras.get(renderer) ?? HOME_CAMERA;
  const start = renderer.framedGraphToViewport(source, { cameraState });
  const end = renderer.framedGraphToViewport(target, { cameraState });
  return ink * graphLineFade(Math.hypot(end.x - start.x, end.y - start.y), renderer.getGraph().size);
}

const inks = new Map<string, ReturnType<typeof parseColor>>();
/** The colour's alpha times the line opacity, in whole 1/255 steps (Sigma's packing). */
function fadedAlpha(color: string, opacity: number): { ink: ReturnType<typeof parseColor>; alpha: number } {
  let ink = inks.get(color);
  if (ink === undefined) { ink = parseColor(color); inks.set(color, ink); }
  return { ink, alpha: (ink.a * opacity * 255) | 0 };
}

// Sigma sizes an arrowhead from the edge's `size`, which hover and history
// emphasis change. The head keeps one size; only its line is emphasised.
const ARROW_HEAD_THICKNESS = 1.6;
const LocalArrowHeadProgram = createEdgeArrowHeadProgram({
  extremity: "target",
  lengthToThicknessRatio: 3,
  widenessToThicknessRatio: 2.2,
});
type ProcessVisibleItem = (
  this: unknown,
  edgeIndex: number,
  startIndex: number,
  sourceData: NodeDisplayData,
  targetData: NodeDisplayData,
  data: EdgeDisplayData,
) => void;
// This class is created above for Local lines only; Sigma's default arrow
// program is a different class and keeps its own behaviour.
const arrowHead = LocalArrowHeadProgram.prototype as unknown as { processVisibleItem: ProcessVisibleItem };
const processArrowHead = arrowHead.processVisibleItem;
arrowHead.processVisibleItem = function (edgeIndex, startIndex, sourceData, targetData, data) {
  // The head fades with its line. Sigma packs the head's colour from a string.
  const { ink, alpha } = fadedAlpha(data.color,
    lineOpacity((this as { renderer: Sigma }).renderer, sourceData, targetData, data));
  processArrowHead.call(this, edgeIndex, startIndex, sourceData, targetData, {
    ...data,
    color: `rgba(${ink.r}, ${ink.g}, ${ink.b}, ${alpha / 255})`,
    size: ARROW_HEAD_THICKNESS,
  });
};

// WebGL1 enum values. Keeping these as literals also avoids reading a browser
// global while this module is imported by build-time tooling.
const GL_FLOAT = 0x1406;
const GL_UNSIGNED_BYTE = 0x1401;
const GL_TRIANGLES = 0x0004;
const PICK_HALF_WIDTH_PX = 4;

const UNIFORMS = [
  "u_matrix",
  "u_viewportPx",
  "u_halfWidthPx",
  "u_dashOnPx",
  "u_dashOffPx",
  "u_rampPx",
] as const;
type EdgeUniform = (typeof UNIFORMS)[number];

// The quad is expanded per edge, not instanced. Sigma otherwise skips WebGL1
// drawing when ANGLE_instanced_arrays is absent. Local graphs are small enough
// that six vertices per relation are preferable to an invisible fallback.
const QUAD_CORNERS = [
  [0, 1], [0, -1], [1, 1],
  [1, 1], [0, -1], [1, -1],
] as const;

// Adapted from Sigma 3's MIT-licensed edge rectangle geometry. Expansion in
// clip space keeps both thickness and dash periods fixed in CSS pixels across
// camera zoom and device-pixel ratios.
const VERTEX_SHADER_SOURCE = /* glsl */ `
attribute vec2 a_positionStart;
attribute vec2 a_positionEnd;
attribute vec4 a_color;
attribute vec4 a_id;
attribute float a_positionCoef;
attribute float a_normalCoef;

uniform mat3 u_matrix;
uniform vec2 u_viewportPx;
uniform float u_halfWidthPx;
uniform float u_rampPx;

varying vec4 v_color;
varying float v_alongPx;
varying float v_across;

void main(void) {
  vec2 startClip = (u_matrix * vec3(a_positionStart, 1.0)).xy;
  vec2 endClip = (u_matrix * vec3(a_positionEnd, 1.0)).xy;
  vec2 edgePx = (endClip - startClip) * (u_viewportPx * 0.5);
  float edgeLengthPx = length(edgePx);
  vec2 perpendicularPx = edgeLengthPx > 0.0001
    ? vec2(-edgePx.y, edgePx.x) / edgeLengthPx
    : vec2(0.0, 1.0);
  // The quad also holds half a ramp outside the stroke, as the Map's line does.
  vec2 offsetClip = perpendicularPx * a_normalCoef
    * (u_halfWidthPx + 0.5 * u_rampPx) * (2.0 / u_viewportPx);

  gl_Position = vec4(mix(startClip, endClip, a_positionCoef) + offsetClip, 0.0, 1.0);
  v_alongPx = a_positionCoef * edgeLengthPx;
  v_across = a_normalCoef;

#ifdef PICKING_MODE
  v_color = a_id;
#else
  v_color = a_color;
#endif
  v_color.a *= 255.0 / 254.0;
}
`;

const FRAGMENT_SHADER_SOURCE = /* glsl */ `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

uniform float u_halfWidthPx;
uniform float u_dashOnPx;
uniform float u_dashOffPx;
uniform float u_rampPx;

varying vec4 v_color;
varying float v_alongPx;
varying float v_across;

void main(void) {
#ifdef PICKING_MODE
  // A continuous hit corridor makes short dots usable with Sigma's reduced
  // resolution picking buffer. Visible gaps remain gaps on the drawing layer.
  gl_FragColor = v_color;
#else
  if (u_dashOffPx > 0.0 && mod(v_alongPx, u_dashOnPx + u_dashOffPx) >= u_dashOnPx) {
    discard;
  }
  // The Map line's anti-aliasing: a one-device-pixel ramp centred on the stroke edge.
  float distancePx = abs(v_across) * (u_halfWidthPx + 0.5 * u_rampPx);
  float alpha = 1.0 - smoothstep(u_halfWidthPx - 0.5 * u_rampPx, u_halfWidthPx + 0.5 * u_rampPx, distancePx);
  gl_FragColor = vec4(v_color.rgb, v_color.a * alpha);
#endif
}
`;

function createLocalEdgeProgram(kind: GraphEdgeVisualKind) {
  const dash = GRAPH_EDGE_PATTERN[kind];
  const halfWidthPx = LOCAL_EDGE_HALF_WIDTH_PX;

  return class LocalEdgeProgram extends EdgeProgram<EdgeUniform> {
    getDefinition(): ProgramDefinition<EdgeUniform> {
      return {
        VERTICES: QUAD_CORNERS.length,
        VERTEX_SHADER_SOURCE,
        FRAGMENT_SHADER_SOURCE,
        UNIFORMS,
        METHOD: GL_TRIANGLES,
        ATTRIBUTES: [
          { name: "a_positionStart", size: 2, type: GL_FLOAT },
          { name: "a_positionEnd", size: 2, type: GL_FLOAT },
          { name: "a_color", size: 4, type: GL_UNSIGNED_BYTE, normalized: true },
          { name: "a_id", size: 4, type: GL_UNSIGNED_BYTE, normalized: true },
          { name: "a_positionCoef", size: 1, type: GL_FLOAT },
          { name: "a_normalCoef", size: 1, type: GL_FLOAT },
        ],
      };
    }

    processVisibleItem(
      edgeIndex: number,
      startIndex: number,
      sourceData: NodeDisplayData,
      targetData: NodeDisplayData,
      data: EdgeDisplayData,
    ): void {
      const { ink, alpha } = fadedAlpha(data.color, lineOpacity(this.renderer, sourceData, targetData, data));
      const color = rgbaToFloat(ink.r, ink.g, ink.b, alpha, true);
      for (const [along, across] of QUAD_CORNERS) {
        this.array[startIndex++] = sourceData.x;
        this.array[startIndex++] = sourceData.y;
        this.array[startIndex++] = targetData.x;
        this.array[startIndex++] = targetData.y;
        this.array[startIndex++] = color;
        this.array[startIndex++] = edgeIndex;
        this.array[startIndex++] = along;
        this.array[startIndex++] = across;
      }
    }

    setUniforms(params: RenderParams, { gl, uniformLocations, isPicking }: ProgramInfo<EdgeUniform>): void {
      gl.uniformMatrix3fv(uniformLocations.u_matrix, false, params.matrix);
      gl.uniform2f(uniformLocations.u_viewportPx, params.width, params.height);
      // Sigma downsizes its picking framebuffer, so visual-width hit targets
      // can disappear even while the visible edge is present.
      gl.uniform1f(uniformLocations.u_halfWidthPx, isPicking
        ? Math.max(halfWidthPx, PICK_HALF_WIDTH_PX)
        : halfWidthPx);
      // One device pixel in CSS px: the same ramp as the Map's line shader.
      gl.uniform1f(uniformLocations.u_rampPx, isPicking ? 0 : 1 / Math.max(1, params.pixelRatio));
      // The picking shader removes dash uniforms at compile time.
      if (uniformLocations.u_dashOnPx) {
        gl.uniform1f(uniformLocations.u_dashOnPx, dash?.[0] ?? 0);
      }
      if (uniformLocations.u_dashOffPx) {
        gl.uniform1f(uniformLocations.u_dashOffPx, dash?.[1] ?? 0);
      }
    }
  };
}

function localEdgeProgram(kind: GraphEdgeVisualKind) {
  const line = createLocalEdgeProgram(kind);
  return GRAPH_EDGE_ARROW[kind] ? createEdgeCompoundProgram([line, LocalArrowHeadProgram]) : line;
}

export const LOCAL_EDGE_PROGRAMS = {
  link: localEdgeProgram("link"),
  citation: localEdgeProgram("citation"),
  coAccess: localEdgeProgram("coAccess"),
  membership: localEdgeProgram("membership"),
  actionWrite: localEdgeProgram("actionWrite"),
  actionCite: localEdgeProgram("actionCite"),
  actionRead: localEdgeProgram("actionRead"),
  actionReference: localEdgeProgram("actionReference"),
  action: localEdgeProgram("action"),
} as const satisfies Record<GraphEdgeVisualKind, ReturnType<typeof localEdgeProgram>>;
