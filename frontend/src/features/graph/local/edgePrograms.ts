import { EdgeProgram, type ProgramDefinition, type ProgramInfo } from "sigma/rendering";
import type { EdgeDisplayData, NodeDisplayData, RenderParams } from "sigma/types";
import { floatColor } from "sigma/utils";

import { GRAPH_EDGE_PATTERN, type GraphEdgeVisualKind } from "../shared/graphTokens";

// Local strokes deliberately use CSS-pixel widths. The Workspace Map has a
// separate renderer and should not inherit these values or this edge buffer.
export const LOCAL_EDGE_HALF_WIDTH_PX = {
  link: 0.75,
  citation: 0.75,
  coAccess: 0.8,
  membership: 0.65,
  action: 0.85,
} as const satisfies Record<GraphEdgeVisualKind, number>;

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
  vec2 offsetClip = perpendicularPx * a_normalCoef
    * u_halfWidthPx * (2.0 / u_viewportPx);

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
  // A quarter CSS pixel of feather preserves thin membership strokes at DPR 1.
  float inner = max(0.0, u_halfWidthPx - 0.25);
  float alpha = 1.0 - smoothstep(inner, u_halfWidthPx, abs(v_across) * u_halfWidthPx);
  gl_FragColor = vec4(v_color.rgb, v_color.a * alpha);
#endif
}
`;

function createLocalEdgeProgram(kind: GraphEdgeVisualKind) {
  const dash = GRAPH_EDGE_PATTERN[kind];
  const halfWidthPx = LOCAL_EDGE_HALF_WIDTH_PX[kind];

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
      const color = floatColor(data.color);
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

export const LOCAL_EDGE_PROGRAMS = {
  link: createLocalEdgeProgram("link"),
  citation: createLocalEdgeProgram("citation"),
  coAccess: createLocalEdgeProgram("coAccess"),
  membership: createLocalEdgeProgram("membership"),
  action: createLocalEdgeProgram("action"),
} as const satisfies Record<GraphEdgeVisualKind, ReturnType<typeof createLocalEdgeProgram>>;
