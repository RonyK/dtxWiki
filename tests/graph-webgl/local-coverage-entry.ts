import { createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import Sigma from "sigma";

import { GraphView } from "../../frontend/src/features/graph/GraphView";
import { buildLocalScene } from "../../frontend/src/features/graph/local/sceneInput";
import { graphFixture } from "../../frontend/src/features/graph/tests/graphFixture";
import { setLocale } from "../../frontend/src/shared/i18n";
import type { GraphNode, GraphQueryPayload } from "../../frontend/src/shared/api/graph";
import { Dialog } from "../../frontend/src/shared/components/Dialog";
import "../../frontend/src/styles/index.css";

// Actual GraphView, translations, styles and Sigma renderer, with a synthetic
// public response. No authentication, database or application API is involved.
const parameters = new URLSearchParams(window.location.search);
const payload = graphFixture();
payload.history.mode = "current";
payload.query.time = { mode: "current" };
const scenario = parameters.get("scenario") ?? "folder";
if (!["complete", "node-cues", "details-long", "details-actions-long", "keyboard-boundary", "camera-rebuild"].includes(scenario)) {
  payload.truncated = true;
  payload.completeness = "capped";
}
if (scenario === "folder" || scenario === "folder-long") {
  payload.warnings = ["graph_budget_applied"];
  payload.omissions = [{ scope: "node", reason: "folder_node", hop: 1 }];
  if (scenario === "folder-long") {
    // Valid distinct distance/class combinations exercise bounded explanation
    // scrolling without inventing excluded counts or unrecognized reasons.
    // The Korean reason rows alone fit the 640px viewport. Include the real
    // informational setting message the folder-only classifier already allows.
    payload.warnings.push("usage_relation_signals_disabled");
    payload.omissions = ([null, 1, 2] as const).flatMap((hop) =>
      (["folder", "seed", "h1", "h12", "h2"] as const).map((relation_class) => ({
        scope: "node" as const, reason: "folder_node" as const, hop, relation_class,
      })),
    );
  }
} else if (scenario === "quota") {
  payload.warnings = ["graph_budget_applied"];
  payload.omissions = [
    { scope: "node", reason: "node_quota", hop: 2 },
    { scope: "relation", reason: "relation_quota", relation_class: "h12" },
  ];
} else if (scenario === "source") {
  payload.completeness = "bounded_sample";
  payload.warnings = ["graph_budget_applied", "graph_projection_source_bounded"];
  payload.omissions = [
    { scope: "node", reason: "folder_node", hop: 1 },
    { scope: "node", reason: "frontier_cap", hop: 2 },
    { scope: "relation", reason: "relation_source_cap", relation_class: "seed" },
  ];
} else if (scenario === "history") {
  // Metadata-only history restriction around an explicitly authoritative
  // current scene; source-bounded observations belong to the case below.
  payload.completeness = "complete";
  payload.truncated = false;
  payload.history.status = "partial";
  payload.warnings = [];
  payload.history.warnings = ["history_coverage_partial"];
} else if (scenario === "observation-source") {
  // Matches the current API shape: observations also contribute current edges,
  // while history.status itself remains complete.
  payload.completeness = "bounded_sample";
  payload.history.status = "complete";
  payload.warnings = ["graph_history_source_bounded"];
  payload.omissions = [];
} else if (scenario === "privacy" || scenario === "unknown") {
  payload.completeness = scenario === "privacy" ? "privacy_suppressed" : "capped";
  payload.warnings = scenario === "unknown" ? ["internal_future_warning_code"] : [];
  payload.omissions = [{ scope: "node", reason: "folder_node", hop: 1 }];
} else if (scenario === "details-long") {
  payload.nodes[0].label = `Decision Log ${"full title stays available ".repeat(24)}`;
  payload.nodes[0].evidence = {
    available: true, kind: "projection", id: "long-visible-evidence",
    label: `Visible evidence ${"complete provenance explanation ".repeat(40)}end of evidence`,
  };
} else if (scenario === "details-actions-long") {
  // A non-primary Context page keeps expansion enabled while its full title
  // and evidence overflow the bounded reader. Existing long-seed stays intact.
  payload.nodes[1].label = `Architecture Notes ${"full title stays available ".repeat(24)}end of full title`;
  payload.nodes[1].evidence = {
    available: true, kind: "projection", id: "long-related-evidence",
    label: `Visible evidence ${"complete provenance explanation ".repeat(40)}end of evidence`,
  };
} else if (scenario === "keyboard-boundary") {
  // Exact Local API ceiling, not a 2k Map/performance proxy. Distinct wire
  // identities and original membership/authored edges exercise every list.
  const documentTemplate = payload.nodes[0];
  const edgeTemplate = payload.edges[0];
  const documents = Array.from({ length: 248 }, (_, index) => ({
    ...documentTemplate,
    id: index === 0 ? "node:seed" : `node:boundary:${String(index).padStart(3, "0")}`,
    entity_id: index === 0 ? "page-seed" : `boundary-${index}`,
    entity_kind: index % 31 === 30 ? "library_item_version"
      : index % 31 === 29 ? "mcp_chat_session" : "context_page",
    family: index % 31 === 29 ? "chat" as const : "document" as const,
    label: `Boundary document ${String(index).padStart(3, "0")}`,
    is_seed: index === 0,
    path: `/context/boundary-${index}.md`,
    evidence: null,
  }));
  const folders = ["a", "b"].map((name) => ({
    ...documentTemplate, id: `node:folder:${name}`, entity_id: `boundary-folder-${name}`,
    entity_kind: "workspace_tree_node", family: "structural" as const,
    label: `Boundary folder ${name.toUpperCase()}`, path: null,
    is_seed: false, evidence: null,
  }));
  const relation = (id: string, source: string, target: string, edge_kind: string) => ({
    ...edgeTemplate, id, source, target, edge_kind, label: id,
  });
  payload.nodes = [...documents, ...folders];
  payload.edges = documents.map((node, index) =>
    relation(`boundary:membership:${index}`, folders[index % 2].id, node.id, "tree"));
  for (const distance of [1, 2]) {
    payload.edges.push(...documents.map((node, index) => relation(
      `boundary:link:${distance}:${index}`, node.id,
      documents[(index + distance) % documents.length].id, "wikilink",
    )));
  }
  payload.edges.push(...[5, 6, 7, 8, 9, 10].map((index) => relation(
    `boundary:extra:${index}`, documents[0].id, documents[index].id, "wikilink",
  )));
  payload.query.budget = { nodes: 250, edges: 750 };
  payload.budget = { requested: { nodes: 250, edges: 750 }, applied: { nodes: 250, edges: 750 } };
  payload.timeline = [];
  payload.facets = {
    ...payload.facets,
    node_families: [...new Set(payload.nodes.map((node) => node.family))],
    node_kinds: [...new Set(payload.nodes.map((node) => node.entity_kind))],
    edge_kinds: ["tree", "wikilink"], action_kinds: [], layers: ["knowledge"],
  };
  if (payload.nodes.length !== 250 || payload.edges.length !== 750
    || new Set(payload.nodes.map((node) => node.id)).size !== 250
    || new Set(payload.edges.map((edge) => edge.id)).size !== 750) {
    throw new Error("Local keyboard fixture must preserve exactly 250/750 original identities");
  }
} else if (scenario === "node-cues") {
  // Real wire identities, not a library_item-only proxy. The shared title
  // intentionally belongs to two different visual kinds.
  const node = (id: string, entity_kind: string, family: typeof payload.nodes[number]["family"], label: string) => ({
    ...payload.nodes[1], id, entity_id: id, entity_kind, family, label,
    is_seed: false, status: null, path: null, evidence: null,
  });
  payload.nodes.push(
    node("node:context-version", "context_page_version", "document", "Shared source title"),
    node("node:library-version", "library_item_version", "document", "Shared source title"),
    node("node:library-artifact", "library_artifact", "document", "Source Artifact"),
    node("node:chat", "mcp_chat_session", "chat", "MCP Conversation"),
    node("node:folder", "workspace_tree_node", "structural", "Visible Source Folder"),
  );
  const relation = (id: string, source: string, target: string, edge_kind: string) => ({
    ...payload.edges[0], id, source, target, edge_kind, label: edge_kind,
  });
  const members = payload.nodes.filter((member) => member.id !== "node:folder");
  payload.edges.push(...members.map((member) =>
    relation(`membership:${member.id}`, "node:folder", member.id, "tree")));
  payload.edges.push(...members.filter((member) => !member.is_seed && member.id !== "node:related" && member.id !== "node:ask")
    .map((member) => relation(`link:${member.id}`, "node:seed", member.id, "wikilink")));
  payload.timeline = [];
  payload.facets = {
    ...payload.facets,
    node_families: ["document", "ask", "chat", "structural"],
    node_kinds: [...new Set(payload.nodes.map((member) => member.entity_kind))],
    edge_kinds: ["wikilink", "ask_citation", "tree"],
  };
}
setLocale(parameters.get("locale") === "ko" ? "ko" : "en", { persist: false });
const host = document.querySelector("#fixture");
if (!host) throw new Error("Missing GraphView coverage fixture host");
const renderers: Sigma[] = [];
let lostContext: WEBGL_lose_context | null = null;
let hoveredKey: string | null = null;
const originalRefresh = Sigma.prototype.refresh;
Sigma.prototype.refresh = function (...args: Parameters<Sigma["refresh"]>) {
  if (!renderers.includes(this)) {
    renderers.push(this);
    // Observe real native picking; never synthesize renderer input events.
    this.on("enterNode", ({ node }) => { hoveredKey = node; });
    this.on("leaveNode", ({ node }) => { if (hoveredKey === node) hoveredKey = null; });
  }
  return originalRefresh.apply(this, args);
};
let graphActions = 0;
const graphAction = async () => { graphActions += 1; };
const expandedNodes: Array<{ id: string; entityKind: string; entityId: string }> = [];
const expandNode = async (node: GraphNode) => {
  expandedNodes.push({ id: node.id, entityKind: node.entity_kind, entityId: node.entity_id });
  await graphAction();
};
declare global {
  interface Window {
    __localCoverageFixture: {
      activate(active: boolean): void;
      replacePayload(): void;
      projectionRevision(): number;
      cameraAnimated(): boolean;
      loseContext(): void;
      restoreContext(): void;
      nativeNode(id: string): {
        key: string; raw: { x: number; y: number }; displayed: { x: number; y: number };
        point: { x: number; y: number }; paintedPoint: { x: number; y: number };
        hoveredId: string | null;
        camera: { x: number; y: number; angle: number; ratio: number };
        bbox: { x: [number, number]; y: [number, number] };
        customBBox: { x: [number, number]; y: [number, number] } | null;
      };
      point(id: string): { x: number; y: number };
      display(id: string): { color: string; hidden: boolean; forceLabel: boolean };
      read(): { instances: number; graphActions: number;
        camera: { x: number; y: number; angle: number; ratio: number };
        positions: Array<{ key: string; x: number; y: number }>;
        expandedNodes: Array<{ id: string; entityKind: string; entityId: string }> };
    };
  }
}

function Fixture() {
  const [active, setActive] = useState(true);
  const [currentPayload, setPayload] = useState(payload);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  Object.assign(window, { __localCoverageFixture: {
    activate: setActive,
    replacePayload: () => setPayload((current) => ({ ...current, projection_revision: current.projection_revision + 1 })),
    projectionRevision: () => currentPayload.projection_revision,
    cameraAnimated: () => renderers.at(-1)?.getCamera().isAnimated() ?? false,
    loseContext: () => {
      const canvas = renderers.at(-1)?.getCanvases().nodes;
      const context = canvas?.getContext("webgl2") ?? canvas?.getContext("webgl");
      lostContext = context?.getExtension("WEBGL_lose_context") ?? null;
      if (!lostContext) throw new Error("WEBGL_lose_context is unavailable");
      lostContext.loseContext();
    },
    restoreContext: () => {
      if (!lostContext) throw new Error("No lost Local Graph context to restore");
      lostContext.restoreContext();
    },
    nativeNode: (id: string) => {
      const renderer = renderers.at(-1);
      const localScene = buildLocalScene(currentPayload);
      const key = localScene.keyByGraphId.get(id);
      const displayed = key && renderer?.getNodeDisplayData(key);
      if (!renderer || !key || !displayed || !renderer.getGraph().hasNode(key)) {
        throw new Error(`Visible native observation unavailable: ${id}`);
      }
      const attributes = renderer.getGraph().getNodeAttributes(key);
      const raw = { x: Number(attributes.x), y: Number(attributes.y) };
      const bounds = renderer.getContainer().getBoundingClientRect();
      const viewportPoint = renderer.graphToViewport(raw);
      const paintedPoint = renderer.framedGraphToViewport(displayed);
      return { key, raw, displayed: { x: displayed.x, y: displayed.y },
        point: { x: bounds.left + viewportPoint.x, y: bounds.top + viewportPoint.y },
        paintedPoint: { x: bounds.left + paintedPoint.x, y: bounds.top + paintedPoint.y },
        hoveredId: hoveredKey ? localScene.graphIdByKey.get(hoveredKey) ?? null : null,
        camera: renderer.getCamera().getState(), bbox: renderer.getBBox(), customBBox: renderer.getCustomBBox() };
    },
    // Read-only native coordinates for real mouse/touch input. Do not call
    // selection, pin, Fit or camera methods on behalf of the browser tests.
    point: (id: string) => {
      const renderer = renderers.at(-1);
      const key = buildLocalScene(currentPayload).keyByGraphId.get(id);
      if (!renderer || !key || !renderer.getGraph().hasNode(key)) {
        throw new Error(`Visible native point unavailable: ${id}`);
      }
      const attributes = renderer.getGraph().getNodeAttributes(key);
      const point = renderer.graphToViewport({ x: Number(attributes.x), y: Number(attributes.y) });
      const bounds = renderer.getContainer().getBoundingClientRect();
      return { x: bounds.left + point.x, y: bounds.top + point.y };
    },
    // Public processed display data observes the real reducer/native renderer;
    // this helper cannot focus, select, pin, resize, or move the camera.
    display: (id: string) => {
      const renderer = renderers.at(-1);
      const key = buildLocalScene(currentPayload).keyByGraphId.get(id);
      const data = key && renderer?.getGraph().hasNode(key)
        ? renderer.getNodeDisplayData(key) : renderer?.getEdgeDisplayData(id);
      if (!data) throw new Error(`Visible native display unavailable: ${id}`);
      return { color: data.color, hidden: data.hidden, forceLabel: "forceLabel" in data && Boolean(data.forceLabel) };
    },
    read: () => {
      const renderer = renderers.at(-1);
      if (!renderer) throw new Error("Native Local coverage renderer not observed");
      return { instances: renderers.length, graphActions, expandedNodes: expandedNodes.map((node) => ({ ...node })), camera: renderer.getCamera().getState(),
        positions: renderer.getGraph().nodes().map((key) => {
          const attributes = renderer.getGraph().getNodeAttributes(key);
          return { key, x: Number(attributes.x), y: Number(attributes.y) };
        }) };
    },
  } });
  const graph = createElement(GraphView, {
    active, payload: currentPayload, onCollapse: graphAction, onDepthChange: graphAction,
    onExpand: expandNode, onHistoryModeChange: graphAction, workspaceId,
    workspaceGraphHref: scenario === "details-actions-long"
      ? (node: GraphNode) => `/workspaces/native-fixture/graph?focused_doc_id=${encodeURIComponent(node.entity_id)}`
      : undefined,
    key: workspaceId,
  });
  if (scenario === "camera-rebuild") {
    return createElement("div", {},
      createElement("button", { type: "button", onClick: () => setDialogOpen(true) }, "Open camera fixture dialog"),
      graph,
      dialogOpen ? createElement(Dialog, {
        labelledBy: "camera-fixture-dialog-title", onClose: () => setDialogOpen(false),
        children: createElement("div", {},
          createElement("h2", { id: "camera-fixture-dialog-title" }, "Camera fixture dialog"),
          createElement("button", { type: "button", onClick: () => setDialogOpen(false) }, "Close camera fixture dialog")),
      }) : null,
    );
  }
  if (scenario !== "keyboard-boundary") return graph;
  const replace = (next: GraphQueryPayload) => setPayload(next);
  return createElement("div", {},
    createElement("nav", { "aria-label": "Native fixture controls" },
      createElement("button", { type: "button", onClick: () => setActive((value) => !value) },
        active ? "Hide graph tab" : "Show graph tab"),
      createElement("button", { type: "button", onClick: () => replace({
        ...currentPayload, projection_revision: currentPayload.projection_revision + 1,
        nodes: currentPayload.nodes.filter((node) => node.id !== "node:boundary:001"),
        edges: currentPayload.edges.filter((edge) => edge.source !== "node:boundary:001" && edge.target !== "node:boundary:001"),
      }) }, "Remove boundary row 001"),
      createElement("button", { type: "button", onClick: () => replace({
        ...currentPayload, projection_revision: currentPayload.projection_revision + 1,
        nodes: [], edges: [],
      }) }, "Empty graph scene"),
      createElement("button", { type: "button", onClick: () => replace(payload) }, "Restore graph scene"),
      createElement("button", { type: "button", onClick: () => setWorkspaceId("native-workspace-next") }, "Change workspace scope"),
    ),
    createElement("div", { hidden: !active }, graph),
  );
}
createRoot(host).render(createElement(MemoryRouter, {}, createElement(Fixture)));
