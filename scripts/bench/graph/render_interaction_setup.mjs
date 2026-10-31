/** DOM-only event-based setup, separate from measured pointer input. */
export function waitForNeutralNodePublication(container, deadlineMs) {
  const startedAt = performance.now();
  const state = window.__dtxGraphRenderBench;
  const scene = state.requests.find(request => request.budget?.documents === 10_000)?.scene;
  const nodes = new Map((scene?.nodes ?? []).map(node => [node.id, node]));
  let mutations = 0;
  const inspect = () => {
    const labels = [...container.querySelectorAll('[data-workspace-map-labels] span[data-node-id]')];
    const canvas = container.querySelector("canvas"), bounds = container.getBoundingClientRect();
    const reasons = { unpublished: 0, identity: 0, duplicate: 0, outside: 0, blocked: 0 };
    const full = [], candidates = [], ids = new Set();
    let published = 0;
    for (const label of labels.slice(0, 40)) {
      const id = label.dataset.nodeId, node = nodes.get(id);
      if (label.dataset.fullTitle !== "false") full.push(id ?? null);
      if (!id || !node?.path) { reasons.identity++; continue; }
      if (ids.has(id)) { reasons.duplicate++; continue; }
      ids.add(id);
      const rawX = label.dataset.pointX ?? "", rawY = label.dataset.pointY ?? "";
      const x = Number(rawX), y = Number(rawY);
      if (!rawX.trim() || !rawY.trim() || !Number.isFinite(x) || !Number.isFinite(y)
        || state.pointPublications.get(label)?.node_id !== id) { reasons.unpublished++; continue; }
      published++;
      if (![bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)
        || x < 12 || y < 12 || x > bounds.width - 12 || y > bounds.height - 12) { reasons.outside++; continue; }
      if (!canvas || document.elementFromPoint(bounds.x + x, bounds.y + y) !== canvas) { reasons.blocked++; continue; }
      candidates.push({ node_id: id, node_label: node.label, path: node.path });
    }
    const cursor = canvas?.style.cursor ?? "";
    const selected = Boolean(document.querySelector(".workspace-graph-selection-clear"));
    const sameScene = state.requests.find(request => request.budget?.documents === 10_000)?.scene === scene;
    const at = performance.now();
    const ready = at - startedAt <= deadlineMs && sameScene && Boolean(canvas)
      && !selected && full.length === 0 && cursor !== "grab" && cursor !== "grabbing"
      && labels.length > 0 && labels.length <= 40 && published === labels.length
      && reasons.identity === 0 && reasons.duplicate === 0;
    const camera = canvas?.__zoom;
    return { ready, candidates, diagnostics: { at_ms: at, started_at_ms: startedAt, elapsed_ms: at - startedAt,
      deadline_ms: deadlineMs, mutation_events: mutations,
      row_count: labels.length, published_rows: published, eligible_count: candidates.length, full_priority_ids: full,
      has_selection: selected, native_cursor: cursor, same_scene: sameScene, rejections: reasons,
      camera: camera ? { x: camera.x, y: camera.y, k: camera.k } : null,
      bounds: { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height } } };
  };
  return new Promise(resolve => {
    let timer = null, done = false;
    const observer = new MutationObserver(() => { mutations++; check(); });
    const finish = result => {
      if (done) return;
      done = true; observer.disconnect();
      if (timer !== null) clearTimeout(timer);
      document.removeEventListener("focusin", check, true);
      resolve(result);
    };
    const check = () => {
      const result = inspect();
      if (result.ready || !result.diagnostics.same_scene || result.diagnostics.elapsed_ms > deadlineMs) finish(result);
    };
    observer.observe(container.closest?.(".workspace-graph") ?? container, { subtree: true, childList: true, attributes: true });
    document.addEventListener("focusin", check, true);
    timer = setTimeout(() => finish(inspect()), deadlineMs);
    check();
  });
}

/** Clear's public RAF focus handoff must finish before moving focus outside the list. */
export async function neutralizeNodeInteraction(page, selector) {
  const clear = page.locator(".workspace-graph-selection-clear");
  if (await clear.count()) {
    await clear.click();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => resolve())));
  }
  await page.getByRole("searchbox", { name: "Find in visible map", exact: true }).focus();
  await page.mouse.move(0, 0);
  return page.locator(selector).evaluate(waitForNeutralNodePublication, 1_000);
}
