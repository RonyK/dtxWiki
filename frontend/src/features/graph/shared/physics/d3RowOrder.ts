/**
 * Reorders D3 Worker frames into a renderer's row order. The order is computed
 * only when the Worker catalog or the renderer's keys change; when both orders
 * agree, the frame itself is returned without a copy.
 */
export function createD3RowOrder() {
  let workerCatalog: readonly string[] | null = null;
  let rendererKeys: readonly string[] | null = null;
  let order: Int32Array | null = null;
  return (rows: Float32Array, workerKeys: readonly string[], keys: readonly string[]): Float32Array => {
    if (workerKeys !== workerCatalog || keys !== rendererKeys) {
      const source = new Map(workerKeys.map((key, index) => [key, index]));
      const next = Int32Array.from(keys, key => source.get(key) ?? -1);
      if (next.some(index => index < 0)) throw new Error("D3 Worker returned a different visible catalog");
      workerCatalog = workerKeys; rendererKeys = keys;
      order = next.every((from, index) => from === index) ? null : next;
    }
    if (!order) return rows;
    const ordered = new Float32Array(order.length * 2);
    order.forEach((from, index) => {
      ordered[index * 2] = rows[from * 2]; ordered[index * 2 + 1] = rows[from * 2 + 1];
    });
    return ordered;
  };
}
