import { createSigmaGraphAdapter } from "../../frontend/src/features/graph/local/sigmaAdapter";
import type { GraphRenderScene } from "../../frontend/src/features/graph/shared/rendererTypes";
import Sigma from "sigma";

const container = document.querySelector<HTMLElement>("#graph");
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
const rawPicks: Array<{ kind: "node" | "edge" | "stage"; id: string | null }> = [];
// Capture the real renderer for read-only fixture observations, without adding
// test APIs to the production adapter or mocking Sigma's input/rendering paths.
let renderer: Sigma | undefined;
const originalRefresh = Sigma.prototype.refresh;
Sigma.prototype.refresh = function (...args: Parameters<Sigma["refresh"]>) {
  renderer = this;
  return originalRefresh.apply(this, args);
};
const adapter = createSigmaGraphAdapter(container, scene, {
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
Sigma.prototype.refresh = originalRefresh;

function sigma(): Sigma {
  if (!renderer) throw new Error("Local fixture did not observe the Sigma renderer");
  return renderer;
}

// Observe actual native 2D paint calls, not only Sigma's forceLabel flags.
const labelDraws: Array<{ text: string; x: number; y: number }> = [];
const labelContext = sigma().getCanvases().labels.getContext("2d")!;
const originalLabelFill = labelContext.fillText;
labelContext.fillText = function (...args: Parameters<CanvasRenderingContext2D["fillText"]>) {
  labelDraws.push({ text: args[0], x: args[1], y: args[2] });
  return originalLabelFill.apply(this, args);
};
sigma().on("beforeRender", () => { labelDraws.length = 0; });

function nodeKey(id: string): string {
  const node = scene.nodes.find((entry) => entry.id === id);
  if (!node) throw new Error(`Unknown Local fixture node: ${id}`);
  return `${node.entity_kind}:${node.entity_id}`;
}

for (const prefix of ["click", "doubleClick"] as const) {
  sigma().on(`${prefix}Node`, ({ node }) => rawPicks.push({ kind: "node", id: node }));
  sigma().on(`${prefix}Edge`, ({ edge }) => rawPicks.push({ kind: "edge", id: edge }));
  sigma().on(`${prefix}Stage`, () => rawPicks.push({ kind: "stage", id: null }));
}

function replaceScene(next: GraphRenderScene): void {
  scene = next;
  adapter.replaceScene(next);
}

function labelPixels(bounds = { x: 0, y: 0, width: container!.clientWidth, height: container!.clientHeight }) {
  const canvas = sigma().getCanvases().labels;
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
    rawPicks,
    camera: () => ({ ...sigma().getCamera().getState() }),
    counts: () => ({ nodes: sigma().getGraph().order, relations: sigma().getGraph().size }),
    edges: () => sigma().getGraph().edges().map((id) => ({ id,
      color: sigma().getEdgeDisplayData(id)?.color, hidden: sigma().getEdgeDisplayData(id)?.hidden })),
    labelPixels,
    labelDraws: () => labelDraws.map((draw) => ({ ...draw })),
    labels: (enabled: boolean) => adapter.setLabelsVisible(enabled),
    labelState: () => ({
      ...labelPixels(),
      displayed: Array.from(sigma().getNodeDisplayedLabels()).map((key) =>
        scene.nodes.find((node) => `${node.entity_kind}:${node.entity_id}` === key)?.id ?? key).sort(),
    }),
    detail: (id: string) => new Promise<void>((resolve) => {
      sigma().once("afterRender", () => resolve());
      adapter.setViewport({ zoom: 6, pan: { x: 0.5, y: 0.5 } });
      adapter.centerOn(id);
      const camera = sigma().getCamera().getState();
      if (!Number.isFinite(camera.x) || !Number.isFinite(camera.y)) {
        throw new Error("Reduced-motion centering emitted NaN camera coordinates");
      }
    }),
    kinds: () => {
      const kinds = [
        ["context_page", "Context"], ["context_page_version", "Context version"],
        ["library_item_version", "Library version"], ["library_artifact", "Library artifact"],
        ["workspace_tree_node", "Folder"], ["mcp_chat_session", "Conversation"],
        ["context_page", "Seed Context"],
      ];
      replaceScene({
        nodes: kinds.map(([kind, label], index) => ({
          id: `kind:${index}`, entity_kind: kind, entity_id: `kind-${index}`, label,
          family: kind === "workspace_tree_node" ? "structural" : kind === "mcp_chat_session" ? "chat" : "document", is_seed: index === 6,
        })),
        edges: [],
      });
      for (let index = 0; index < 6; index++) {
        adapter.pin(`kind:${index}`, true, { x: (index % 3 - 1) * 140, y: index < 3 ? 100 : -100 });
      }
      adapter.fit();
    },
    node: (id: string) => {
      const attributes = sigma().getGraph().getNodeAttributes(nodeKey(id));
      const display = sigma().getNodeDisplayData(nodeKey(id));
      return {
        point: sigma().graphToViewport({ x: Number(attributes.x), y: Number(attributes.y) }),
        graphPoint: { x: Number(attributes.x), y: Number(attributes.y) },
        color: display?.color,
        hidden: display?.hidden,
      };
    },
    focus: (id: string | null) => adapter.focus(id),
    pin: (id: string, pinned = true, position?: { x: number; y: number }) => adapter.pin(id, pinned, position),
    positions: () => Object.fromEntries(sigma().getGraph().nodes().map((key) => {
      const attributes = sigma().getGraph().getNodeAttributes(key);
      return [key, { x: Number(attributes.x), y: Number(attributes.y) }];
    })),
    replaceScene,
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
