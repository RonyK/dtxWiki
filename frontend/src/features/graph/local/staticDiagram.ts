import { stableEdges, stableNodes, type GraphScene } from "../graphModel";
import { GRAPH_CUE_SIZE, GRAPH_SEED_CUE, graphNodeCueParts, type GraphCuePart } from "../shared/GraphCues";
import {
  GRAPH_EDGE_PATTERN, GRAPH_EDGE_WIDTH, GRAPH_NODE_VISUALS,
  type GraphEdgeVisualKind, type GraphNodeVisualKind, type GraphVisualTokens,
} from "../shared/graphTokens";
import { LOCAL_EDGE_VISUAL_KIND_ORDER, localEdgeVisualKind } from "./edgeKinds";
import { localNodeVisualKind } from "./nodeVisual";

/** The static diagram reads the same tokens as the canvas and its legend. */
export type StaticDiagramVisuals = {
  tokens: GraphVisualTokens;
  /** Title ink and the seed cue's ring: the graph view's text colour. */
  ink: string;
  forcedColors: boolean;
};

export type StaticDiagramNode = { kind: GraphNodeVisualKind; seed: boolean };

export type StaticDiagram = {
  source: string;
  /** Mermaid node `n<i>` is `nodes[i]`. */
  nodes: StaticDiagramNode[];
};

// Mermaid trims and collapses whitespace in labels, so a word reserves the
// room for the legend glyph in front of each title; decorateStaticDiagramSvg
// removes it again. "Mm " is wider than the glyph and its gap in any text face.
const SLOT_WORD = "Mm";
// Mermaid's default flowchart wrapping width plus the slot word, so a title
// wraps where it would without the slot.
const WRAPPING_WIDTH_PX = 200 + 26;
const CARD_TINT = 0.1;
const GLYPH_GAP_PX = 6;
const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * A Local scene as a Mermaid flowchart in the legend's vocabulary: every node
 * is a card tinted with its kind's colour (a folder is a plain card in its
 * ring colour, a seed is filled), every line uses its kind's colour, dash and
 * weight. With `iconSlot`, each title is preceded by the slot word that
 * decorateStaticDiagramSvg turns into the node's legend glyph.
 */
export function graphSceneToMermaid(
  scene: GraphScene,
  visuals: StaticDiagramVisuals,
  { iconSlot }: { iconSlot: boolean },
): StaticDiagram {
  const sceneNodes = stableNodes(scene.nodes);
  const names = new Map(sceneNodes.map((node, index) => [node.id, `n${index}`]));
  const nodes = sceneNodes.map((node) => ({
    kind: localNodeVisualKind(node.entity_kind, node.family),
    seed: Boolean(node.is_seed),
  }));
  // A seed's bold title is a stylesheet rule, not a later change, so Mermaid
  // lays out and sizes its card at the weight it is drawn. (A classDef's
  // font-weight never reaches the words, which carry their own weight.)
  const seedClasses = [...new Set(nodes.filter((node) => node.seed).map((node) => `.${node.kind}Seed`))];
  const init = {
    ...(iconSlot ? { flowchart: { wrappingWidth: WRAPPING_WIDTH_PX } } : {}),
    ...(seedClasses.length
      ? { themeCSS: `${seedClasses.map((name) => `${name} .text-inner-tspan`).join(", ")} { font-weight: bold; }` }
      : {}),
  };
  const lines = Object.keys(init).length ? [`%%{init: ${JSON.stringify(init)}}%%`] : [];
  lines.push("flowchart LR");
  sceneNodes.forEach((node, index) => {
    lines.push(`  n${index}("${iconSlot ? `${SLOT_WORD} ` : ""}${mermaidText(node.label)}")`);
  });
  const linesByKind = new Map<GraphEdgeVisualKind, number[]>();
  let drawn = 0;
  for (const edge of stableEdges(scene.edges)) {
    const source = names.get(edge.source);
    const target = names.get(edge.target);
    if (!source || !target) {
      continue;
    }
    const connector = edge.direction === "undirected" ? "---" : "-->";
    lines.push(`  ${source} ${connector}|${mermaidText(edge.label)}| ${target}`);
    const kind = localEdgeVisualKind(edge.edge_kind);
    if (kind) {
      linesByKind.set(kind, [...(linesByKind.get(kind) ?? []), drawn]);
    }
    drawn += 1;
  }
  const classes = new Map<string, { style: string; members: string[] }>();
  nodes.forEach((node, index) => {
    const style = cardStyle(node, visuals);
    if (!style) {
      return;
    }
    const name = node.seed ? `${node.kind}Seed` : node.kind;
    const entry = classes.get(name) ?? { style, members: [] };
    entry.members.push(`n${index}`);
    classes.set(name, entry);
  });
  for (const [name, { style, members }] of classes) {
    lines.push(`  classDef ${name} ${style}`, `  class ${members.join(",")} ${name}`);
  }
  for (const kind of LOCAL_EDGE_VISUAL_KIND_ORDER) {
    const indices = linesByKind.get(kind);
    const style = indices ? lineStyle(kind, visuals) : null;
    if (indices && style) {
      lines.push(`  linkStyle ${indices.join(",")} ${style}`);
    }
  }
  return { source: lines.join("\n"), nodes };
}

/**
 * Draw each node's legend glyph into a sanitised static-diagram SVG. Mermaid
 * sized every card for "<slot word> <title>"; the word gives way to the
 * glyph, and the block of [glyph, gap, first line] over the wrapped lines is
 * centred in the card, left-aligned, with the glyph on the first line's
 * centre. That block is never wider than what Mermaid measured, so it stays
 * inside the card and the diagram's bounds. Without SVG text metrics the
 * slot word is only hidden.
 */
export function decorateStaticDiagramSvg(
  svg: string,
  nodes: StaticDiagramNode[],
  visuals: StaticDiagramVisuals,
): string {
  const host = document.createElement("div");
  host.setAttribute("aria-hidden", "true");
  host.style.cssText = "position:absolute;top:0;left:0;visibility:hidden;pointer-events:none";
  // Mounted only to measure text; the Mermaid pipeline already sanitised it.
  host.innerHTML = svg;
  document.body.append(host);
  try {
    for (const group of Array.from(host.querySelectorAll<SVGGElement>("svg g.node"))) {
      const index = /-flowchart-n(\d+)-/.exec(group.id)?.[1];
      const node = index === undefined ? undefined : nodes[Number(index)];
      if (node) {
        decorateNode(group, node, visuals);
      }
    }
    return host.innerHTML;
  } finally {
    host.remove();
  }
}

function decorateNode(group: SVGGElement, node: StaticDiagramNode, visuals: StaticDiagramVisuals) {
  const text = group.querySelector<SVGTextElement>("text");
  const card = group.querySelector<SVGRectElement>("rect.label-container");
  const slot = text?.querySelector<SVGTSpanElement>("tspan.text-inner-tspan");
  if (!text || !card || !slot || slot.textContent !== SLOT_WORD) {
    return;
  }
  const rows = Array.from(text.querySelectorAll<SVGTSpanElement>("tspan.text-outer-tspan"));
  const block = measureBlock(group, text, rows, card);
  if (!block) {
    // Keep Mermaid's layout; the word only stops showing.
    slot.style.setProperty("fill", "transparent", "important");
    slot.setAttribute("aria-hidden", "true");
    return;
  }
  slot.remove();
  const first = rows[0].querySelector("tspan.text-inner-tspan");
  if (first?.textContent) {
    first.textContent = first.textContent.trimStart();
  }
  rows.forEach((row, index) => {
    row.setAttribute("x", String(index === 0 ? block.left + GRAPH_CUE_SIZE + GLYPH_GAP_PX : block.left));
    row.style.setProperty("text-anchor", "start");
  });
  text.before(...glyphElements(node, block.left + GRAPH_CUE_SIZE / 2, block.firstLineCenter, visuals));
}

/** Where the decorated block starts, in the title's space; null when it cannot be measured or would not fit. */
function measureBlock(group: SVGGElement, text: SVGTextElement, rows: SVGTSpanElement[], card: SVGRectElement) {
  try {
    const groupMatrix = group.getCTM();
    const textMatrix = text.getCTM();
    if (!groupMatrix || !textMatrix || rows.length === 0) {
      return null;
    }
    const widths = rows.map((row) => row.getComputedTextLength());
    // The first line loses "<slot word> " and gains the glyph and its gap.
    const firstLine = widths[0] - text.getSubStringLength(0, SLOT_WORD.length + 1);
    const blockWidth = Math.max(GRAPH_CUE_SIZE + GLYPH_GAP_PX + firstLine, ...widths.slice(1));
    const cardWidth = card.width.baseVal.value;
    if (blockWidth > Math.max(...widths) + 0.5 || blockWidth > cardWidth) {
      return null;
    }
    // Card bounds are in the node's space, the title in the label's.
    const offset = groupMatrix.inverse().multiply(textMatrix);
    const line = text.getExtentOfChar(SLOT_WORD.length + 1);
    return {
      left: card.x.baseVal.value - offset.e + (cardWidth - blockWidth) / 2,
      firstLineCenter: line.y + line.height / 2,
    };
  } catch {
    return null;
  }
}

function glyphElements(node: StaticDiagramNode, cx: number, cy: number, visuals: StaticDiagramVisuals) {
  const color = visuals.tokens.node[GRAPH_NODE_VISUALS[node.kind].color];
  const surface = visuals.tokens.surface;
  if (node.seed) {
    // The legend's seed cue on a surface disc, so it reads on the filled card.
    return [
      svgElement("circle", { cx, cy, r: GRAPH_CUE_SIZE / 2 }, { fill: surface }),
      svgElement("circle", { cx, cy, r: GRAPH_SEED_CUE.radius, "stroke-width": GRAPH_SEED_CUE.ringWidth },
        { fill: color, stroke: visuals.ink }),
    ];
  }
  const { body, forcedOutline } = graphNodeCueParts(node.kind, cx, cy);
  return [body, ...(visuals.forcedColors ? forcedOutline : [])]
    .map((part) => cuePartElement(part, color, surface));
}

function cuePartElement(part: GraphCuePart, ink: string, surface: string) {
  return part.element === "circle"
    ? svgElement("circle", { cx: part.cx, cy: part.cy, r: part.r, "stroke-width": part.strokeWidth },
      { fill: "none", stroke: ink })
    : svgElement("path", { d: part.d }, { fill: part.tone === "ink" ? ink : surface });
}

function svgElement(
  name: "circle" | "path",
  attributes: Record<string, string | number>,
  paint: { fill: string; stroke?: string },
) {
  const element = document.createElementNS(SVG_NS, name);
  element.setAttribute("aria-hidden", "true");
  element.setAttribute("class", "graph-static-node-cue");
  for (const [key, value] of Object.entries(attributes)) {
    element.setAttribute(key, String(value));
  }
  // Paint through style: a token may be any CSS colour, var() included.
  element.style.setProperty("fill", paint.fill);
  if (paint.stroke) {
    element.style.setProperty("stroke", paint.stroke);
  }
  return element;
}

function cardStyle(node: StaticDiagramNode, visuals: StaticDiagramVisuals) {
  const color = hexColor(visuals.tokens.node[GRAPH_NODE_VISUALS[node.kind].color]);
  const surface = hexColor(visuals.tokens.surface);
  const ink = hexColor(visuals.ink);
  if (!color || !surface || !ink) {
    return null;
  }
  if (node.seed) {
    return `fill:${color},stroke:${color},stroke-width:1.5px,color:${surface}`;
  }
  return GRAPH_NODE_VISUALS[node.kind].shape === "ring"
    ? `fill:${surface},stroke:${color},stroke-width:2.5px,color:${ink}`
    : `fill:${mixHex(color, surface, CARD_TINT)},stroke:${color},stroke-width:1.5px,color:${ink}`;
}

function lineStyle(kind: GraphEdgeVisualKind, visuals: StaticDiagramVisuals) {
  const color = hexColor(visuals.tokens.edge[kind]);
  const dash = GRAPH_EDGE_PATTERN[kind];
  return color
    ? `stroke:${color},stroke-width:${GRAPH_EDGE_WIDTH[kind]}px${dash ? `,stroke-dasharray:${dash.join(" ")}` : ""}`
    : null;
}

// Mermaid style lists are comma separated, so colours go in as #rrggbb.
function hexColor(value: string): string | null {
  const color = value.trim().toLowerCase();
  const short = /^#([\da-f])([\da-f])([\da-f])$/.exec(color);
  if (short) {
    return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`;
  }
  if (/^#[\da-f]{6}$/.test(color)) {
    return color;
  }
  const rgb = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/.exec(color);
  return rgb
    ? `#${rgb.slice(1, 4).map((channel) => Math.round(Math.min(255, Number(channel))).toString(16).padStart(2, "0")).join("")}`
    : null;
}

function mixHex(color: string, base: string, alpha: number) {
  const channels = (hex: string) => [1, 3, 5].map((start) => Number.parseInt(hex.slice(start, start + 2), 16));
  const [over, under] = [channels(color), channels(base)];
  return `#${over.map((channel, index) =>
    Math.round(channel * alpha + under[index] * (1 - alpha)).toString(16).padStart(2, "0")).join("")}`;
}

function mermaidText(value: string) {
  return value
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/\|/g, "&#124;")
    .trim();
}
