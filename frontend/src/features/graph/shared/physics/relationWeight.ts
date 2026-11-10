/** Authored occurrences amplify degree-normalized D3 attraction up to 16x. */
export function boundedRelationWeight(occurrences: number): number {
  return Math.min(16, 1 + Math.log2(Math.max(1, Number.isFinite(occurrences) ? occurrences : 1)));
}
