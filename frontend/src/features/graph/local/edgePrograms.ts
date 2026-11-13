import {
  EdgeProgram,
  type ProgramDefinition,
  type ProgramInfo,
} from "sigma/rendering";
import type Sigma from "sigma";
import type { EdgeDisplayData, NodeDisplayData, RenderParams } from "sigma/types";
import { parseColor, rgbaToFloat } from "sigma/utils";

import {
  GRAPH_EDGE_ENDPOINTS, GRAPH_EDGE_ENDPOINT_SIZE_PX, GRAPH_EDGE_PATTERN, GRAPH_EDGE_TRIANGLE_SIZE_PX, GRAPH_EDGE_WIDTH_PX, GRAPH_LINE_OPACITY, GRAPH_REMOVED_EDGE_PATTERN,
  graphLineFade, type GraphEdgeVisualKind,
} from "../shared/graphTokens";
import { TRIANGLE_RADIUS_FACTOR } from "../shared/nodeShapes";

// Every line has the legend's one CSS-pixel width; half of it on each side.
export const LOCAL_EDGE_HALF_WIDTH_PX = GRAPH_EDGE_WIDTH_PX / 2;

/**
 * The edge reducer's line state, as on the Map: "selected" while a selection
 * or history step is emphasised (ink x emphasis, no fade), "forced" in forced
 * colours (ink, no fade).
 */
export type LocalLineState = "rest" | "selected" | "forced";

// Lines are measured in the camera of the renderer's last Fit, so the reader's
// zoom never changes a line's opacity. Before any Fit: Sigma's home view.
const HOME_CAMERA = { x: 0.5, y: 0.5, angle: 0, ratio: 1 };
const fitCameras = new WeakMap<Sigma, typeof HOME_CAMERA>();

/** Records the camera ratio a Fit used: the long-line fade measures lines in that view. */
export function setLocalFitRatio(renderer: Sigma, ratio: number): void {
  fitCameras.set(renderer, { ...HOME_CAMERA, ratio });
}

/** The Map's line opacity rules (GRAPH_LINE_OPACITY) for one Context line. */
function lineOpacity(renderer: Sigma, source: NodeDisplayData, target: NodeDisplayData, data: EdgeDisplayData): number {
  const state = (data as EdgeDisplayData & { lineState?: LocalLineState }).lineState;
  const ink = data.type === "citation" ? GRAPH_LINE_OPACITY.citation : GRAPH_LINE_OPACITY.line;
  if (state === "selected") return Math.min(1, ink * GRAPH_LINE_OPACITY.emphasis);
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
  "u_dashPatternPx",
  "u_endpoints",
  "u_endpointGeometryPx",
  "u_nodeGapPx",
  "u_sizeRatio",
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
// Raw Sigma radii, followed by source/target shape codes.
attribute vec4 a_nodeGeometry;

uniform mat3 u_matrix;
uniform vec2 u_viewportPx;
uniform float u_halfWidthPx;
uniform float u_rampPx;
uniform float u_sizeRatio;
uniform vec2 u_endpoints;
uniform vec4 u_endpointGeometryPx;
uniform float u_nodeGapPx;

varying vec4 v_color;
varying vec2 v_pointPx;
varying vec3 v_endpointFrame;

// Ray/shape intersections match the native node glyphs. Squares and diamonds
// keep their screen orientation, as Sigma's node programs do.
float boundaryRadius(float radius, float shape, vec2 direction) {
  if (shape < 0.5) return radius;
  if (shape < 1.5) return radius / max(max(abs(direction.x), abs(direction.y)), 0.0001);
  if (shape < 2.5) return 1.41421356237 * radius / max(abs(direction.x) + abs(direction.y), 0.0001);
  float normal = max(-direction.y, max(0.86602540378 * direction.x + 0.5 * direction.y,
    -0.86602540378 * direction.x + 0.5 * direction.y));
  return ${TRIANGLE_RADIUS_FACTOR / 2} * radius / max(normal, 0.0001);
}

void main(void) {
  vec2 startClip = (u_matrix * vec3(a_positionStart, 1.0)).xy;
  vec2 endClip = (u_matrix * vec3(a_positionEnd, 1.0)).xy;
  vec2 edgePx = (endClip - startClip) * (u_viewportPx * 0.5);
  float edgeLengthPx = length(edgePx);
  vec2 direction = edgeLengthPx > 0.0001 ? edgePx / edgeLengthPx : vec2(1.0, 0.0);
  vec2 perpendicularPx = vec2(-direction.y, direction.x);
  float sizeRatio = max(u_sizeRatio, 0.0001);
  float sourceRadius = boundaryRadius(a_nodeGeometry.x / sizeRatio, a_nodeGeometry.z, direction);
  float targetRadius = boundaryRadius(a_nodeGeometry.y / sizeRatio, a_nodeGeometry.w, -direction);
  float startPx = min(sourceRadius + u_nodeGapPx, edgeLengthPx);
  float endPx = max(startPx, edgeLengthPx - targetRadius - u_nodeGapPx);
  float room = max(edgeLengthPx - sourceRadius - targetRadius - 2.0 * u_nodeGapPx, 0.0);
  float targetPresent = min(u_endpoints.y, 1.0);
  float requested = u_endpoints.x * u_endpointGeometryPx.z + targetPresent * u_endpointGeometryPx.x;
  // Very short relations retain their direction without letting heads cross
  // each other or cover either node.
  float headScale = min(1.0, room / max(requested, 0.0001));
  float endpointExtent = max(u_endpoints.x * u_endpointGeometryPx.w,
    targetPresent * u_endpointGeometryPx.y) * headScale;
  float halfExtent = max(u_halfWidthPx, endpointExtent);
#ifndef PICKING_MODE
  // The path's extreme vertex also needs its half-stroke and antialias ramp.
  halfExtent = max(u_halfWidthPx, endpointExtent + u_halfWidthPx) + 0.5 * u_rampPx;
#endif
  float acrossPx = a_normalCoef * halfExtent;
  vec2 offsetClip = perpendicularPx * acrossPx * (2.0 / u_viewportPx);
  gl_Position = vec4(mix(startClip, endClip, a_positionCoef) + offsetClip, 0.0, 1.0);
  v_pointPx = vec2(a_positionCoef * edgeLengthPx, acrossPx);
  v_endpointFrame = vec3(startPx, endPx, headScale);
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
uniform vec4 u_dashPatternPx;
uniform float u_rampPx;
uniform vec2 u_endpoints;
uniform vec4 u_endpointGeometryPx;

varying vec4 v_color;
varying vec2 v_pointPx;
varying vec3 v_endpointFrame;

float segmentDistance(vec2 point, vec2 start, vec2 end) {
  vec2 segment = end - start;
  float t = clamp(dot(point - start, segment) / max(dot(segment, segment), 0.0001), 0.0, 1.0);
  return length(point - start - t * segment);
}

float strokeCoverage(float distancePx) {
  return 1.0 - smoothstep(u_halfWidthPx - 0.5 * u_rampPx,
    u_halfWidthPx + 0.5 * u_rampPx, distancePx);
}

void main(void) {
#ifdef PICKING_MODE
  // Keep the whole corridor selectable, including dash and hollow-head gaps.
  gl_FragColor = v_color;
#else
  float scale = v_endpointFrame.z;
  float startPx = v_endpointFrame.x;
  float endPx = v_endpointFrame.y;
  float sourceLength = u_endpoints.x * u_endpointGeometryPx.z * scale;
  float lineStart = startPx + sourceLength;
  // A filled arrow's shaft stops at its base so the stroke cannot blunt its tip.
  float lineEnd = endPx - (u_endpoints.y > 1.5 ? u_endpointGeometryPx.x * scale : 0.0);
  float coverage = 0.0;
  if (lineEnd > lineStart && v_pointPx.x >= lineStart && v_pointPx.x <= lineEnd) {
    float dashPeriod = dot(u_dashPatternPx, vec4(1.0));
    float phase = dashPeriod > 0.0 ? mod(v_pointPx.x, dashPeriod) : 0.0;
    float secondStroke = u_dashPatternPx.x + u_dashPatternPx.y;
    bool firstInk = phase < u_dashPatternPx.x;
    bool secondInk = phase >= secondStroke && phase < secondStroke + u_dashPatternPx.z;
    if (dashPeriod <= 0.0 || firstInk || secondInk) coverage = strokeCoverage(abs(v_pointPx.y));
  }
  if (scale > 0.0 && u_endpoints.y > 0.5) {
    vec2 tip = vec2(endPx, 0.0);
    vec2 upper = tip + vec2(-u_endpointGeometryPx.x, u_endpointGeometryPx.y) * scale;
    vec2 lower = tip + vec2(-u_endpointGeometryPx.x, -u_endpointGeometryPx.y) * scale;
    float distancePx = min(segmentDistance(v_pointPx, tip, upper), segmentDistance(v_pointPx, tip, lower));
    if (u_endpoints.y > 1.5) {
      distancePx = min(distancePx, segmentDistance(v_pointPx, upper, lower));
      float along = (endPx - v_pointPx.x) / max(u_endpointGeometryPx.x * scale, 0.0001);
      bool inside = along >= 0.0 && along <= 1.0 && abs(v_pointPx.y) <= along * u_endpointGeometryPx.y * scale;
      float signedDistance = inside ? -distancePx : distancePx;
      coverage = max(coverage, 1.0 - smoothstep(-0.5 * u_rampPx, 0.5 * u_rampPx, signedDistance));
    } else {
      coverage = max(coverage, strokeCoverage(distancePx));
    }
  }
  if (scale > 0.0 && u_endpoints.x > 0.5) {
    vec2 near = vec2(startPx, 0.0);
    vec2 far = near + vec2(sourceLength, 0.0);
    vec2 upper = near + vec2(sourceLength * 0.5, u_endpointGeometryPx.w * scale);
    vec2 lower = near + vec2(sourceLength * 0.5, -u_endpointGeometryPx.w * scale);
    float distancePx = min(min(segmentDistance(v_pointPx, near, upper), segmentDistance(v_pointPx, upper, far)),
      min(segmentDistance(v_pointPx, far, lower), segmentDistance(v_pointPx, lower, near)));
    coverage = max(coverage, strokeCoverage(distancePx));
  }
  // Union the line and endpoint masks before premultiplication: their joins
  // have exactly the line's opacity rather than accumulating a second stroke.
  float opacity = v_color.a * coverage;
  gl_FragColor = vec4(v_color.rgb * opacity, opacity);
#endif
}
`;

function nodeShapeCode(data: NodeDisplayData): number {
  const shape = (data as NodeDisplayData & { shape?: string }).shape ?? data.type;
  return shape === "square" ? 1 : shape === "diamond" ? 2
    : shape === "triangle" || shape === "outlinedTriangle" ? 3 : 0;
}

function createLocalEdgeProgram(kind: GraphEdgeVisualKind, dash: readonly number[] | null = GRAPH_EDGE_PATTERN[kind], removed = false) {
  const halfWidthPx = LOCAL_EDGE_HALF_WIDTH_PX;
  const endpoints = GRAPH_EDGE_ENDPOINTS[kind];
  const filledTriangle = endpoints.target === "filled-triangle";

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
          { name: "a_nodeGeometry", size: 4, type: GL_FLOAT },
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
        this.array[startIndex++] = sourceData.size;
        this.array[startIndex++] = targetData.size;
        this.array[startIndex++] = nodeShapeCode(sourceData);
        this.array[startIndex++] = nodeShapeCode(targetData);
      }
    }

    setUniforms(params: RenderParams, { gl, uniformLocations, isPicking }: ProgramInfo<EdgeUniform>): void {
      gl.uniformMatrix3fv(uniformLocations.u_matrix, false, params.matrix);
      gl.uniform2f(uniformLocations.u_viewportPx, params.width, params.height);
      gl.uniform1f(uniformLocations.u_sizeRatio, params.sizeRatio);
      gl.uniform2f(uniformLocations.u_endpoints, !removed && endpoints.source === "open-diamond" ? 1 : 0,
        removed || endpoints.target === "none" ? 0 : filledTriangle ? 2 : 1);
      gl.uniform4f(uniformLocations.u_endpointGeometryPx,
        filledTriangle ? GRAPH_EDGE_TRIANGLE_SIZE_PX.length : GRAPH_EDGE_ENDPOINT_SIZE_PX.arrowLength,
        filledTriangle ? GRAPH_EDGE_TRIANGLE_SIZE_PX.halfWidth : GRAPH_EDGE_ENDPOINT_SIZE_PX.arrowHalfWidth, GRAPH_EDGE_ENDPOINT_SIZE_PX.diamondLength,
        GRAPH_EDGE_ENDPOINT_SIZE_PX.diamondHalfWidth);
      gl.uniform1f(uniformLocations.u_nodeGapPx, GRAPH_EDGE_ENDPOINT_SIZE_PX.nodeGap);
      // Sigma downsizes its picking framebuffer, so visual-width hit targets
      // can disappear even while the visible edge is present.
      gl.uniform1f(uniformLocations.u_halfWidthPx, isPicking
        ? Math.max(halfWidthPx, PICK_HALF_WIDTH_PX)
        : halfWidthPx);
      // One device pixel in CSS px: the same ramp as the Map's line shader.
      gl.uniform1f(uniformLocations.u_rampPx, isPicking ? 0 : 1 / Math.max(1, params.pixelRatio));
      // The picking shader removes dash uniforms at compile time.
      if (uniformLocations.u_dashPatternPx) {
        gl.uniform4f(uniformLocations.u_dashPatternPx, dash?.[0] ?? 0, dash?.[1] ?? 0,
          dash?.[2] ?? 0, dash?.[3] ?? 0);
      }
    }
  };
}

export const LOCAL_EDGE_PROGRAMS = {
  link: createLocalEdgeProgram("link"),
  citation: createLocalEdgeProgram("citation"),
  coAccess: createLocalEdgeProgram("coAccess"),
  membership: createLocalEdgeProgram("membership"),
  actionWrite: createLocalEdgeProgram("actionWrite"),
  actionCite: createLocalEdgeProgram("actionCite"),
  actionRead: createLocalEdgeProgram("actionRead"),
  actionReference: createLocalEdgeProgram("actionReference"),
  action: createLocalEdgeProgram("action"),
  removed: createLocalEdgeProgram("action", GRAPH_REMOVED_EDGE_PATTERN, true),
} as const satisfies Record<GraphEdgeVisualKind | "removed", ReturnType<typeof createLocalEdgeProgram>>;
