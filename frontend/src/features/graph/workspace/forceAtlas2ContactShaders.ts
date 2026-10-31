export {
  workspaceForceAtlas2VertexShader as workspaceForceAtlas2ContactVertexShader,
} from "./forceAtlas2Shaders";

/**
 * Original bounded Jacobi contact projection, separate from the upstream FA2
 * force model. Input/output RGBA is [world x, world y, force x, force y].
 * Circles are conservative caller-authored radii, not rendered CSS diameters.
 */
export const workspaceForceAtlas2ContactFragmentShader = `#version 300 es
precision highp float;
precision highp int;

uniform sampler2D stateTexture;
uniform sampler2D radiiTexture;
uniform sampler2D pinsTexture;
uniform int nodeCount;
uniform int sourceWidth;
uniform int outputWidth;

out vec4 nextState;

ivec2 outputTexel(int index) {
  return ivec2(index % outputWidth, index / outputWidth);
}

ivec2 sourceTexel(int index) {
  return ivec2(index % sourceWidth, index / sourceWidth);
}

void main() {
  ivec2 texel = ivec2(gl_FragCoord.xy);
  int index = texel.y * outputWidth + texel.x;
  if (index >= nodeCount) {
    nextState = vec4(0.0);
    return;
  }

  vec4 state = texelFetch(stateTexture, sourceTexel(index), 0);
  if (texelFetch(pinsTexture, texel, 0).r > 0.5) {
    nextState = state;
    return;
  }

  float radius = texelFetch(radiiTexture, texel, 0).r;
  vec2 correction = vec2(0.0);
  int contacts = 0;
  for (int other = 0; other < nodeCount; other++) {
    if (other == index) continue;
    ivec2 otherTexel = outputTexel(other);
    vec2 delta = state.xy - texelFetch(stateTexture, sourceTexel(other), 0).xy;
    float distance = length(delta);
    float clearance = radius + texelFetch(radiiTexture, otherTexel, 0).r;
    if (distance >= clearance) continue;

    // Opposite deterministic directions for an exactly coincident index pair.
    // There is no random kick, folder/edge input, or global box constraint.
    vec2 direction = distance > 0.0 ? delta / distance
      : vec2(index < other ? -1.0 : 1.0, 0.0);
    bool otherPinned = texelFetch(pinsTexture, otherTexel, 0).r > 0.5;
    float share = otherPinned ? 1.0 : 0.5;
    correction += direction * (clearance - distance) * share;
    contacts++;
  }

  vec2 position = state.xy;
  if (contacts > 0) position += correction / float(contacts);
  // Preserve the upstream force history exactly. Contacts are geometric,
  // not extra attraction/repulsion, and do not overwrite adaptive state.
  nextState = vec4(position, state.zw);
}
`;
