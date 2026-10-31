import { createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { createInstance } from "i18next";
import { initReactI18next } from "react-i18next";
import Sigma from "sigma";

import { GraphCanvas } from "../../frontend/src/features/graph/GraphCanvas";
import type { GraphRenderScene } from "../../frontend/src/features/graph/shared/rendererTypes";
import graphEn from "../../frontend/src/shared/i18n/locales/en/graph.json";

const scene: GraphRenderScene = {
  nodes: [
    { id: "seed", entity_kind: "context_page", entity_id: "seed", family: "document", label: "Seed Context", is_seed: true },
    { id: "note", entity_kind: "context_page", entity_id: "note", family: "document", label: "Related note" },
    { id: "source", entity_kind: "library_item_version", entity_id: "source", family: "document", label: "Library source" },
  ],
  edges: [
    { id: "link", source: "seed", target: "note", edge_kind: "wikilink", layer: "knowledge", label: "Wiki link" },
    { id: "citation", source: "seed", target: "source", edge_kind: "citation_source", layer: "evidence", label: "Source citation" },
  ],
};
const renderers: Sigma[] = [];
const selections: Array<string | null> = [];
let lostContext: WEBGL_lose_context | null = null;
const originalRefresh = Sigma.prototype.refresh;
// Observe actual engine identity without exposing a production-only test API.
Sigma.prototype.refresh = function (...args: Parameters<Sigma["refresh"]>) {
  if (!renderers.includes(this)) renderers.push(this);
  return originalRefresh.apply(this, args);
};
function current(): Sigma {
  const renderer = renderers.at(-1);
  if (!renderer) throw new Error("No Local GraphCanvas renderer observed");
  return renderer;
}
function Fixture() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  return createElement(GraphCanvas, {
    active: true, focusedId: null, forcedColors: false, reducedMotion: true,
    scene, selectedId, pinnedIds: ["note", "source"],
    pinnedPositions: { note: { x: -120, y: 80 }, source: { x: 120, y: 80 } },
    onHover: () => undefined, onPin: () => undefined, onRendererStatusChange: () => undefined,
    onSelect: (id) => { selections.push(id); setSelectedId(id); },
  });
}
Object.assign(window, {
  __graphCanvasFixture: {
    selections,
    instances: () => renderers.length,
    node: (id: string) => {
      const node = scene.nodes.find((entry) => entry.id === id)!;
      const key = `${node.entity_kind}:${node.entity_id}`;
      const attributes = current().getGraph().getNodeAttributes(key);
      return current().graphToViewport({ x: Number(attributes.x), y: Number(attributes.y) });
    },
    lose: () => {
      const canvas = current().getCanvases().nodes;
      const context = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
      lostContext = context?.getExtension("WEBGL_lose_context") ?? null;
      if (!lostContext) throw new Error("WEBGL_lose_context is unavailable");
      lostContext.loseContext();
    },
    restore: () => {
      if (!lostContext) throw new Error("No lost WebGL context to restore");
      lostContext.restoreContext();
    },
  },
});
void createInstance().use(initReactI18next).init({
  lng: "en", resources: { en: { graph: graphEn } }, ns: ["graph"], defaultNS: "graph",
  interpolation: { escapeValue: false },
}).then(() => {
  const host = document.querySelector("#graph");
  if (!host) throw new Error("Missing GraphCanvas fixture host");
  createRoot(host).render(createElement(Fixture));
});
