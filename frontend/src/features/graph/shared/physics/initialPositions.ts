/** The deterministic phyllotaxis used by d3-force before any force tick. */
export function d3PhyllotaxisPositions(count: number): Float32Array {
  const positions = new Float32Array(count * 2);
  for (let index = 0; index < count; index++) {
    const radius = 10 * Math.sqrt(0.5 + index);
    const angle = index * Math.PI * (3 - Math.sqrt(5));
    positions[index * 2] = radius * Math.cos(angle);
    positions[index * 2 + 1] = radius * Math.sin(angle);
  }
  return positions;
}
