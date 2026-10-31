import type { GraphRenderEdge } from "../shared/rendererTypes";

/** Presentation-only labels; geometry, original identities and input stay intact. */
export function localizeWorkspaceRenderEdges(
  edges: readonly GraphRenderEdge[],
  describeKind: (kind: string) => string,
): GraphRenderEdge[] {
  // One render owns this tiny vocabulary. Never reuse locale/viewer labels
  // between calls, and never run i18next for every edge of a dense scene.
  const labels = new Map<string, string>();
  return edges.map((edge) => {
    const kind = edge.edgeKind ?? edge.label;
    let label = labels.get(kind);
    if (label === undefined) {
      label = describeKind(kind);
      labels.set(kind, label);
    }
    return { ...edge, label };
  });
}
