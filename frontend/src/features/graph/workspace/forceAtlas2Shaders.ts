/*!
 * Size-aware ForceAtlas2/LinLog equations adapted from graphology-layout-
 * forceatlas2 0.10.1, iterate.js, upstream commit
 * ee71891109dfa22394eedb7cbeb4e143625b9bb6.
 * https://github.com/graphology/graphology/tree/ee71891109dfa22394eedb7cbeb4e143625b9bb6/src/layout-forceatlas2
 *
 * The MIT License (MIT)
 * Copyright (c) 2016-2021 Guillaume Plique (Yomguithereal)
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 */

export const workspaceForceAtlas2VertexShader = `#version 300 es
precision highp float;

void main() {
  vec2 point = gl_VertexID == 0 ? vec2(-1.0, -1.0)
    : gl_VertexID == 1 ? vec2(3.0, -1.0) : vec2(-1.0, 3.0);
  gl_Position = vec4(point, 0.0, 1.0);
}
`;

/**
 * Fixed profile: adjustSizes/linLog true; scalingRatio, gravity, slowDown and
 * edgeWeightInfluence 1; strongGravity/outbound distribution/Barnes-Hut false.
 * Radii and positions share the caller's unchanged scene units. This is not a
 * collision constraint: coincident centers receive no artificial random kick.
 */
export const workspaceForceAtlas2FragmentShader = `#version 300 es
precision highp float;
precision highp int;

uniform sampler2D stateTexture;
uniform sampler2D attributesTexture;
uniform sampler2D adjacencyTexture;
uniform sampler2D pinsTexture;
uniform int nodeCount;
uniform int stateWidth;
uniform int adjacencyWidth;

out vec4 nextState;

// The upstream oracle computes each term in JS doubles, then rounds on every
// Float32 node-matrix write. Plain GLSL rounds each intermediate: a one-ulp
// force difference changes the next adaptive swinging term near equilibrium.
// A scalar vec2 below holds high/low parts until the same matrix-write boundary;
// textures, force history, traversal order and the equations remain unchanged.
vec2 preciseSum(float a, float b) {
  float sum = a + b;
  float bv = sum - a;
  return vec2(sum, (a - (sum - bv)) + (b - bv));
}
vec2 splitValue(float value) {
  float high = uintBitsToFloat(floatBitsToUint(value) & 0xfffff000u);
  return vec2(high, value - high);
}
vec2 preciseProduct(float a, float b) {
  float product = a * b;
  vec2 x = splitValue(a), y = splitValue(b);
  float error = ((x.x * y.x - product) + x.x * y.y + x.y * y.x) + x.y * y.y;
  return vec2(product, error);
}
vec2 wideAdd(vec2 a, vec2 b) {
  vec2 s = preciseSum(a.x, b.x);
  return preciseSum(s.x, s.y + a.y + b.y);
}
vec2 wideSub(vec2 a, vec2 b) { return wideAdd(a, -b); }
vec2 wideMul(vec2 a, vec2 b) {
  vec2 p = preciseProduct(a.x, b.x);
  return preciseSum(p.x, p.y + a.x * b.y + a.y * b.x);
}
vec2 wideDiv(vec2 a, vec2 b) {
  float q = a.x / b.x;
  vec2 residual = wideSub(a, wideMul(b, vec2(q, 0.0)));
  return preciseSum(q, (residual.x + residual.y) / b.x);
}
vec2 wideSqrt(vec2 a) {
  float root = sqrt(a.x);
  if (root == 0.0) return vec2(0.0);
  vec2 residual = wideSub(a, preciseProduct(root, root));
  return preciseSum(root, (residual.x + residual.y) / (2.0 * root));
}
vec2 wideNorm(vec2 x, vec2 y) {
  return wideSqrt(wideAdd(wideMul(x, x), wideMul(y, y)));
}
vec2 wideLog(vec2 a) {
  int exponent = int((floatBitsToUint(a.x) >> 23u) & 255u) - 127;
  vec2 mantissa = wideMul(a, vec2(exp2(float(-exponent)), 0.0));
  if (mantissa.x > 1.4142135623730951) { mantissa *= 0.5; exponent++; }
  // Range reduction keeps |z| <= (sqrt(2)-1)/(sqrt(2)+1). Odd powers through17
  // reproduce log in the scalar high/low format without native-log rounding.
  vec2 z = wideDiv(wideSub(mantissa, vec2(1.0, 0.0)), wideAdd(mantissa, vec2(1.0, 0.0)));
  vec2 square = wideMul(z, z), power = z, sum = z;
  for (int term = 3; term <= 17; term += 2) {
    power = wideMul(power, square);
    sum = wideAdd(sum, wideDiv(power, vec2(float(term), 0.0)));
  }
  return wideAdd(sum * 2.0, wideMul(vec2(0.6931471824645996, -1.904654323148236e-9), vec2(float(exponent), 0.0)));
}
float forceAdd(float force, vec2 delta, vec2 factor) {
  vec2 result = wideAdd(vec2(force, 0.0), wideMul(delta, factor));
  return result.x + result.y;
}

ivec2 nodeTexel(int index) {
  return ivec2(index % stateWidth, index / stateWidth);
}

void main() {
  ivec2 texel = ivec2(gl_FragCoord.xy);
  int index = texel.y * stateWidth + texel.x;
  if (index >= nodeCount) {
    nextState = vec4(0.0);
    return;
  }

  vec4 state = texelFetch(stateTexture, texel, 0);
  vec4 attributes = texelFetch(attributesTexture, texel, 0);
  vec2 position = state.xy;
  float mass = attributes.x;
  float radius = attributes.y;
  vec2 force = vec2(0.0);

  // Gathering in ascending node order preserves upstream pair traversal order
  // for each node. No regions, sampling, alpha or degree-normalized charge.
  for (int other = 0; other < nodeCount; other++) {
    if (other == index) continue;
    ivec2 otherTexel = nodeTexel(other);
    vec2 otherPosition = texelFetch(stateTexture, otherTexel, 0).xy;
    vec2 dx = preciseSum(position.x, -otherPosition.x), dy = preciseSum(position.y, -otherPosition.y);
    vec4 otherAttributes = texelFetch(attributesTexture, otherTexel, 0);
    vec2 gap = wideSub(wideSub(wideNorm(dx, dy), vec2(radius, 0.0)), vec2(otherAttributes.y, 0.0));
    if (gap.x > 0.0) {
      vec2 factor = wideDiv(wideDiv(preciseProduct(mass, otherAttributes.x), gap), gap);
      force = vec2(forceAdd(force.x, dx, factor), forceAdd(force.y, dy, factor));
    } else if (gap.x < 0.0) {
      vec2 factor = wideMul(preciseProduct(100.0, mass), vec2(otherAttributes.x, 0.0));
      force = vec2(forceAdd(force.x, dx, factor), forceAdd(force.y, dy, factor));
    }
  }

  vec2 distanceFromOrigin = wideNorm(vec2(position.x, 0.0), vec2(position.y, 0.0));
  if (distanceFromOrigin.x > 0.0) {
    vec2 factor = wideDiv(vec2(-mass, 0.0), distanceFromOrigin);
    force = vec2(forceAdd(force.x, vec2(position.x, 0.0), factor), forceAdd(force.y, vec2(position.y, 0.0), factor));
  }

  // Each directed/parallel row contributes once at each endpoint, in original
  // global edge order. Self-loops retain two incidences and their mass, but
  // their negative clearance contributes no attraction.
  int offset = int(attributes.z);
  int count = int(attributes.w);
  for (int entry = 0; entry < count; entry++) {
    int adjacencyIndex = offset + entry;
    ivec2 edgeTexel = ivec2(adjacencyIndex % adjacencyWidth,
      adjacencyIndex / adjacencyWidth);
    vec2 edge = texelFetch(adjacencyTexture, edgeTexel, 0).xy;
    int other = int(edge.x);
    ivec2 otherTexel = nodeTexel(other);
    vec2 otherPosition = texelFetch(stateTexture, otherTexel, 0).xy;
    vec2 dx = preciseSum(position.x, -otherPosition.x), dy = preciseSum(position.y, -otherPosition.y);
    float otherRadius = texelFetch(attributesTexture, otherTexel, 0).y;
    vec2 gap = wideSub(wideSub(wideNorm(dx, dy), vec2(radius, 0.0)), vec2(otherRadius, 0.0));
    if (gap.x > 0.0) {
      vec2 factor = wideDiv(wideMul(vec2(-edge.y, 0.0), wideLog(wideAdd(vec2(1.0, 0.0), gap))), gap);
      force = vec2(forceAdd(force.x, dx, factor), forceAdd(force.y, dy, factor));
    }
  }

  // Upstream fixed nodes retain their unbounded force history, but do not move.
  // Free nodes clamp the complete force, before swinging/traction are evaluated.
  if (texelFetch(pinsTexture, texel, 0).r > 0.5) {
    nextState = vec4(position, force);
    return;
  }
  vec2 magnitude = wideNorm(vec2(force.x, 0.0), vec2(force.y, 0.0));
  if (magnitude.x > 10.0) {
    vec2 fx = wideDiv(preciseProduct(force.x, 10.0), magnitude), fy = wideDiv(preciseProduct(force.y, 10.0), magnitude);
    force = vec2(fx.x + fx.y, fy.x + fy.y);
  }
  vec2 swinging = wideMul(vec2(mass, 0.0), wideNorm(preciseSum(state.z, -force.x), preciseSum(state.w, -force.y)));
  vec2 traction = wideNorm(preciseSum(state.z, force.x), preciseSum(state.w, force.y)) * 0.5;
  vec2 speed = wideDiv(wideMul(vec2(0.10000000149011612, -1.4901161138336505e-9), wideLog(wideAdd(vec2(1.0, 0.0), traction))), wideAdd(vec2(1.0, 0.0), wideSqrt(swinging)));
  nextState = vec4(forceAdd(position.x, vec2(force.x, 0.0), speed), forceAdd(position.y, vec2(force.y, 0.0), speed), force);
}
`;
