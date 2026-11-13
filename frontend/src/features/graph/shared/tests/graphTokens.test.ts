import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  GRAPH_EDGE_ARROW, GRAPH_EDGE_PATTERN,
  GRAPH_NODE_RADIUS_PX,
  readGraphTokens,
} from "../graphTokens";

describe("Graph visual tokens", () => {
  it("uses the approved node palette when graph CSS variables are absent", () => {
    const element = document.createElement("div");
    expect(readGraphTokens(element, "light").node).toMatchObject({
      context: "#7c3aed",
      library: "#0f9f8f",
      agent: "#d97706",
      conversation: "#0284c7",
    });
    expect(readGraphTokens(element, "light").edge).toMatchObject({ action: "#94a3b8", actionWrite: "#94a3b8" });
    // Canvases, legends and list cues outside them all inherit the theme from
    // the document root, rather than from a canvas-only rule.
    const graphCss = readFileSync("src/styles/03-editor.css", "utf8");
    const darkTokens = graphCss.slice(graphCss.indexOf(':root[data-theme="dark"],\n.graph-canvas[data-graph-theme="dark"] {'));
    expect(darkTokens.slice(0, darkTokens.indexOf("}"))).toContain("--graph-node-agent: #f59e0b;");
    expect(readGraphTokens(element, "dark").node).toMatchObject({
      context: "#8b5cf6",
      library: "#0d9488",
      agent: "#f59e0b",
      conversation: "#38bdf8",
    });
    expect(readGraphTokens(element, "dark").edge).toMatchObject({ action: "#cbd5e1", actionWrite: "#cbd5e1" });
  });

  it("accepts explicit CSS graph tokens and falls back on blank values", () => {
    const element = document.createElement("div");
    element.style.setProperty("--graph-node-context", "#123456");
    element.style.setProperty("--graph-edge-citation", "#abcdef");
    element.style.setProperty("--graph-edge-action", "#654321");
    element.style.setProperty("--graph-edge-link", " ");
    const tokens = readGraphTokens(element);
    expect(tokens.node.context).toBe("#123456");
    expect(tokens.edge.citation).toBe("#abcdef");
    expect(tokens.edge.action).toBe("#654321");
    expect(tokens.edge.link).toBe("#64748b");
  });

  it.each(["document", "isolated"] as const)("keeps actual shipped %s-dark graph text legible without recoloring the app", (scope) => {
    const stylesheet = document.createElement("style");
    stylesheet.textContent = readFileSync("src/styles/03-editor.css", "utf8");
    const host = document.createElement("div");
    host.style.color = "rgb(15, 23, 42)";
    const canvas = document.createElement("div");
    canvas.className = "graph-canvas";
    const unrelated = document.createElement("span");
    const previousTheme = document.documentElement.getAttribute("data-theme");
    if (scope === "document") document.documentElement.setAttribute("data-theme", "dark");
    else {
      document.documentElement.removeAttribute("data-theme");
      canvas.dataset.graphTheme = "dark";
    }
    host.append(canvas, unrelated);
    document.head.append(stylesheet);
    document.body.append(host);
    try {
      // Cosmos snapshots this computed color for its document-label spans;
      // checking palette-token text alone would miss the dark foreground bug.
      expect(readGraphTokens(canvas, "dark").surface).toBe("#0f172a");
      expect(readGraphTokens(canvas, "dark").edge.action).toBe("#cbd5e1");
      // The static diagram writes its titles in this ink, not the page's --ink.
      expect(readGraphTokens(canvas, "dark").ink).toBe("#e5e7eb");
      expect(getComputedStyle(canvas).color).toBe("rgb(229, 231, 235)");
      expect(getComputedStyle(unrelated).color).toBe("rgb(15, 23, 42)");
      canvas.style.color = "rgb(255, 255, 255)";
      expect(getComputedStyle(canvas).color).toBe("rgb(255, 255, 255)");
      expect(new Set(Object.values(readGraphTokens(canvas, "forced").node)))
        .toEqual(new Set(["rgb(255, 255, 255)"]));
      delete canvas.dataset.graphTheme;
      document.documentElement.removeAttribute("data-theme");
      canvas.style.removeProperty("color");
      expect(getComputedStyle(canvas).color).toBe("rgb(15, 23, 42)");
      expect(readGraphTokens(canvas, "light").edge.action).toBe("#94a3b8");
    } finally {
      stylesheet.remove();
      host.remove();
      if (previousTheme === null) document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", previousTheme);
    }
  });

  it("keeps shape/dash cues when forced colors flatten the palette", () => {
    const frame = document.createElement("div");
    frame.style.backgroundColor = "rgb(0, 0, 0)";
    const element = document.createElement("div");
    element.style.color = "rgb(255, 255, 255)";
    frame.append(element);
    const tokens = readGraphTokens(element, "forced");
    expect(new Set(Object.values(tokens.node)).size).toBe(1);
    expect(new Set(Object.values(tokens.edge)).size).toBe(1);
    expect(tokens.surface).toBe("rgb(0, 0, 0)");
    expect(readGraphTokens(document.createElement("div"), "forced").surface)
      .toBe("#ffffff");
    const orphan = document.createElement("div");
    orphan.style.color = "rgb(255, 255, 255)";
    expect(readGraphTokens(orphan, "forced").surface).toBe("#000000");
    expect(GRAPH_EDGE_PATTERN).toEqual({
      link: null,
      citation: [8, 3, 1.5, 3],
      coAccess: [5, 3.5],
      membership: [10, 4],
      actionWrite: null,
      actionCite: [6, 3],
      actionRead: [10, 5],
      actionReference: [1.5, 3.5],
      action: [1.5, 3.5],
    });
    // Direction remains visible when colour is unavailable.
    expect(Object.entries(GRAPH_EDGE_ARROW).filter(([, arrow]) => arrow).map(([kind]) => kind))
      .toEqual(["link", "citation", "membership", "actionWrite", "actionCite", "actionRead", "actionReference", "action"]);
  });

  it("keeps separate Local and Workspace dot-size limits", () => {
    expect(GRAPH_NODE_RADIUS_PX.local).toEqual({ min: 3, max: 13 });
    expect(GRAPH_NODE_RADIUS_PX.workspace).toEqual({ min: 1.5, max: 8.5 });
  });
});
