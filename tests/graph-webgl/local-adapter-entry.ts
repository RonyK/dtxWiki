import { createLocalGraphAdapter } from "../../frontend/src/features/graph/local/cosmosAdapter";
import type { GraphRenderScene } from "../../frontend/src/features/graph/shared/rendererTypes";
import { observeLocalNative } from "./local-native-observer";

const container = document.querySelector<HTMLDivElement>("#graph");
if (!container) throw new Error("Missing Local Graph fixture container");

let scene: GraphRenderScene = {
  nodes: [
    { id: "context:seed", entity_kind: "context_page", entity_id: "seed", family: "document", label: "Current Context", is_seed: true },
    { id: "context:linked", entity_kind: "context_page", entity_id: "linked", family: "document", label: "Linked note" },
    { id: "library:cited", entity_kind: "library_item_version", entity_id: "cited", family: "document", label: "Cited source" },
    { id: "context:co-read", entity_kind: "context_page", entity_id: "co-read", family: "document", label: "Read together" },
    { id: "context:child", entity_kind: "context_page", entity_id: "child", family: "document", label: "Folder member" },
    { id: "context:unrelated", entity_kind: "context_page", entity_id: "unrelated", family: "document", label: "Unrelated point" },
  ],
  edges: [
    { id: "edge:link", source: "context:seed", target: "context:linked", edge_kind: "wikilink", layer: "knowledge", label: "linked" },
    { id: "edge:citation", source: "context:seed", target: "library:cited", edge_kind: "citation_source", layer: "evidence", label: "cited" },
    { id: "edge:co-access", source: "context:seed", target: "context:co-read", edge_kind: "co_access", layer: "activity", label: "read together" },
    { id: "edge:membership", source: "context:seed", target: "context:child", edge_kind: "tree", layer: "knowledge", label: "in folder" },
  ],
};

const events: Array<{ type: string; id: string | null }> = [];
const observation = observeLocalNative(() => scene);
const adapter = createLocalGraphAdapter(container, scene, {
  forcedColors: matchMedia("(forced-colors: active)").matches,
  positions: {
    "context:seed": { x: 0, y: 0 },
    "context:linked": { x: -120, y: 100 },
    "library:cited": { x: 120, y: 100 },
    "context:co-read": { x: -120, y: -100 },
    "context:child": { x: 120, y: -100 },
    "context:unrelated": { x: 240, y: -100 },
  },
  reducedMotion: true,
  callbacks: {
    onHover: (id) => events.push({ type: "hover", id }),
    onSelect: (id) => {
      events.push({ type: "select", id });
      // Mirror GraphCanvas's controlled selection feedback after real input.
      adapter.select(id);
    },
    onPin: (id) => events.push({ type: "pin", id }),
    onViewport: () => undefined,
    onContextStatus: (status) => events.push({ type: `context:${status}`, id: null }),
  },
});
function native() { return observation.current(); }

function nodeKey(id: string): string {
  const node = scene.nodes.find((entry) => entry.id === id);
  if (!node) throw new Error(`Unknown Local fixture node: ${id}`);
  return `${node.entity_kind}:${node.entity_id}`;
}

function replaceScene(next: GraphRenderScene): Promise<void> {
  scene = next;
  adapter.replaceScene(next);
  return adapter.whenSettled();
}

function labelPixels(bounds = { x: 0, y: 0, width: container!.clientWidth, height: container!.clientHeight }) {
  const canvas = native().getCanvases().labels;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Local fixture label canvas unavailable");
  const scale = canvas.width / container!.clientWidth;
  const pixels = context.getImageData(Math.round(bounds.x * scale), Math.round(bounds.y * scale),
    Math.round(bounds.width * scale), Math.round(bounds.height * scale)).data;
  let alpha = 0;
  let ink = 0;
  for (let index = 0; index < pixels.length; index += 4) {
    if (!pixels[index + 3]) continue;
    alpha++;
    if (pixels[index] < 100 && pixels[index + 1] < 100 && pixels[index + 2] < 100) ink++;
  }
  return { alpha, ink };
}

Object.assign(window, {
  __graphLocalFixture: {
    events,
    get rawPicks() { return native().picks; },
    camera: () => ({ ...native().getCamera().getState() }),
    counts: () => ({ nodes: native().getGraph().order, relations: native().getGraph().size }),
    edges: () => native().getGraph().edges().map((id) => ({ id,
      color: native().getEdgeDisplayData(id)?.color, hidden: native().getEdgeDisplayData(id)?.hidden })),
    labelPixels,
    labelDraws: () => native().labelDraws(),
    labels: (enabled: boolean) => adapter.setLabelsVisible(enabled),
    labelState: () => ({
      ...labelPixels(),
      displayed: Array.from(native().getNodeDisplayedLabels()).map((key) =>
        scene.nodes.find((node) => `${node.entity_kind}:${node.entity_id}` === key)?.id ?? key).sort(),
    }),
    detail: async (id: string) => {
      await adapter.whenSettled();
      adapter.setLabelsVisible(false);
      // A detail zoom large enough to distinguish a balanced square corner
      // from a circle in actual framebuffer pixels, independent of Fit units.
      adapter.setViewport({ zoom: 64, pan: { x: 0, y: 0 } });
      adapter.centerOn(id);
      await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
      const camera = native().getCamera().getState();
      if (!Number.isFinite(camera.x) || !Number.isFinite(camera.y)) {
        throw new Error("Reduced-motion centering emitted NaN camera coordinates");
      }
    },
    rightBoundary: async (id: string) => {
      await adapter.whenSettled();
      const camera = native().getCamera().getState();
      const point = native().graphToViewport(native().getGraph().getNodeAttributes(nodeKey(id)));
      // Keep the original boundary regression at x=825 CSS px despite the
      // renderer's graph-space camera replacing Sigma's normalized Fit ratio.
      adapter.setViewport({ zoom: 1 / camera.ratio,
        pan: { x: camera.x + (point.x - 825) * camera.ratio, y: camera.y } });
    },
    kinds: () => {
      const kinds = [
        ["context_page", "Context"], ["context_page_version", "Context version"],
        ["library_item_version", "Library version"], ["library_artifact", "Library artifact"],
        ["workspace_tree_node", "Folder"], ["mcp_chat_session", "Conversation"],
        ["context_page", "Seed Context"], ["agent_page", "Agent"],
      ];
      replaceScene({
        nodes: kinds.map(([kind, label], index) => ({
          id: `kind:${index}`, entity_kind: kind, entity_id: `kind-${index}`, label,
          family: kind === "workspace_tree_node" ? "structural" : kind === "mcp_chat_session" ? "chat"
            : kind === "agent_page" ? "agent" : "document", is_seed: index === 6,
        })),
        edges: [],
      });
      for (let index = 0; index < 6; index++) {
        adapter.pin(`kind:${index}`, true, { x: (index % 3 - 1) * 140, y: index < 3 ? 100 : -100 });
      }
      // Inside the existing pinned extent, so fitting the scene keeps its camera.
      adapter.pin("kind:7", true, { x: -140, y: 0 });
      adapter.fit();
    },
    node: (id: string) => {
      const attributes = native().getGraph().getNodeAttributes(nodeKey(id));
      const display = native().getNodeDisplayData(nodeKey(id));
      return {
        point: native().graphToViewport({ x: Number(attributes.x), y: Number(attributes.y) }),
        graphPoint: { x: Number(attributes.x), y: Number(attributes.y) },
        color: display?.color,
        hidden: display?.hidden,
        radius: display?.size,
      };
    },
    focus: (id: string | null) => adapter.focus(id),
    pin: (id: string, pinned = true, position?: { x: number; y: number }) => adapter.pin(id, pinned, position),
    positions: () => Object.fromEntries(native().getGraph().nodes().map((key) => {
      const attributes = native().getGraph().getNodeAttributes(key);
      return [key, { x: Number(attributes.x), y: Number(attributes.y) }];
    })),
    replaceScene,
    settled: () => adapter.whenSettled(),
    reset: () => adapter.reset(),
    get scene() { return scene; },
    select: (id: string | null) => adapter.select(id),
    stop: () => adapter.stopLayout(),
    stress: () => {
      replaceScene({
        nodes: Array.from({ length: 250 }, (_, index) => ({
          id: `stress:${index}`, entity_kind: "context_page", entity_id: `stress-${index}`,
          family: "document", label: `Stress point ${index}`, is_seed: index === 0,
        })),
        edges: Array.from({ length: 750 }, (_, index) => ({
          id: `stress-edge:${index}`, source: `stress:${index % 250}`,
          target: `stress:${(index % 250 + [1, 7, 17][Math.floor(index / 250)]) % 250}`,
          edge_kind: "wikilink", layer: "knowledge", label: "linked",
        })),
      });
    },
    crowdedLabels: () => {
      replaceScene({
        nodes: Array.from({ length: 10 }, (_, index) => ({
          id: `priority:${index}`, entity_kind: "context_page", entity_id: `priority-${index}`,
          family: "document", label: `Priority title ${index}`, is_seed: true,
        })),
        edges: [],
      });
      for (let index = 0; index < 10; index++) adapter.pin(`priority:${index}`, true, { x: 0, y: 0 });
      adapter.fit();
    },
    zoom: () => adapter.zoomBy(1.2),
  },
});
