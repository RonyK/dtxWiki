export const GRAPH_CANVAS_RATIOS = ["16:11", "16:9", "4:3", "1:1"] as const;
export type GraphCanvasRatio = typeof GRAPH_CANVAS_RATIOS[number];

const STORAGE_KEY = "dtxwiki.contextGraph.aspectRatio";

export function readCanvasRatio(): GraphCanvasRatio {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    const ratio = GRAPH_CANVAS_RATIOS.find((value) => value === stored);
    if (ratio) return ratio;
  } catch {
    // A blocked browser store must not prevent the graph from opening.
  }
  return "16:11";
}

export function storeCanvasRatio(ratio: GraphCanvasRatio): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, ratio);
  } catch {
    // The selected ratio still works for the current mounted graph.
  }
}
