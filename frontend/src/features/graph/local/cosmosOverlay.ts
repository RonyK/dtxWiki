import { GRAPH_NODE_VISUALS, GRAPH_REMOVED_EDGE_COLOR, type GraphNodeShape } from "../shared/graphTokens";
import { GRAPH_RING_GAP_PX, GRAPH_RING_WIDTH_PX, traceOutline } from "../shared/nodeShapes";
import { nativeGlyphOutline, nativeGlyphShape } from "../shared/workspaceGlyphGeometry";
import type { CosmosOverlayFrame } from "../shared/cosmos/renderBinding";

type Bounds = { left: number; right: number; top: number; bottom: number };
const overlaps = (a: Bounds, b: Bounds) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

/** Local's priority labels and history rings use Cosmos's displayed CSS geometry. */
export function createLocalCosmosOverlay(container: HTMLDivElement) {
  let tracingId: string | null = null;
  let lastFrame: CosmosOverlayFrame | null = null;
  const rings = document.createElement("canvas"), labels = document.createElement("canvas");
  rings.dataset.localGraphRings = "";
  labels.dataset.localGraphLabels = "";
  for (const canvas of [rings, labels]) {
    canvas.setAttribute("aria-hidden", "true");
    Object.assign(canvas.style, { position: "absolute", inset: "0", pointerEvents: "none", width: "100%", height: "100%",
      zIndex: canvas === rings ? "2" : "3" });
  }
  const prepare = (canvas: HTMLCanvasElement, width: number, height: number) => {
    const ratio = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
    if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);
    const context = canvas.getContext("2d");
    if (!context) return null;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    return context;
  };
  const overlay = {
    render(frame: CosmosOverlayFrame) {
      lastFrame = frame;
      // NativeGraph owns the first canvas: its GL lookup and context events
      // must never bind one of the 2D overlays during renderer construction.
      for (const canvas of [rings, labels]) if (!canvas.parentNode) container.appendChild(canvas);
      const { columns: c, points, diameters, width, height, tokens } = frame;
      const ring = prepare(rings, width, height), context = prepare(labels, width, height);
      if (!context || !ring || width <= 0 || height <= 0) return;
      const activeId = frame.focusedId ?? frame.selectedId;
      const active = activeId ? c.nodeIds.indexOf(activeId) : -1;
      const activeEdge = activeId ? c.edgeIds.indexOf(activeId) : -1;
      const relatedNodes = activeId ? new Set<number>() : null;
      if (active >= 0) relatedNodes!.add(active);
      c.edgeIds.forEach((_, index) => {
        if (index === activeEdge || active >= 0 && (c.edgeSources[index] === active || c.edgeTargets[index] === active)) {
          relatedNodes!.add(c.edgeSources[index]); relatedNodes!.add(c.edgeTargets[index]);
        }
      });
      const shapeFor = (index: number): GraphNodeShape => c.nodeSeedGlyph?.[index] ? "circle"
        : GRAPH_NODE_VISUALS[c.nodeVisualKinds![index]].shape;
      const band = (index: number, ink: string, offset = 0) => {
        const x = points[index * 2], y = points[index * 2 + 1], diameter = diameters[index];
        const shape = nativeGlyphShape(shapeFor(index));
        ring.save();
        ring.lineJoin = "miter";
        ring.strokeStyle = tokens.surface; ring.lineWidth = GRAPH_RING_GAP_PX;
        traceOutline(ring, nativeGlyphOutline(shape, x, y, diameter, offset + GRAPH_RING_GAP_PX / 2)); ring.stroke();
        ring.strokeStyle = ink; ring.lineWidth = GRAPH_RING_WIDTH_PX;
        traceOutline(ring, nativeGlyphOutline(shape, x, y, diameter,
          offset + GRAPH_RING_GAP_PX + GRAPH_RING_WIDTH_PX / 2)); ring.stroke();
        ring.restore();
      };
      c.nodeIds.forEach((_, index) => {
        const x = points[index * 2], y = points[index * 2 + 1], diameter = diameters[index];
        if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(diameter) || diameter <= 0) return;
        if (c.nodeSeedGlyph?.[index] && c.nodeIds[index] !== tracingId) {
          // The seed's ink ring occupies the same 24% of its disc as before.
          const radius = diameter / 2, stroke = radius * .24;
          ring.save(); ring.strokeStyle = tokens.ink; ring.lineWidth = stroke;
          ring.beginPath(); ring.arc(x, y, radius - stroke / 2, 0, 2 * Math.PI); ring.stroke(); ring.restore();
        }
        if (c.nodeHighlighted?.[index]) band(index, tokens.highlight);
        if (index === active) band(index, tokens.selection,
          c.nodeHighlighted?.[index] ? GRAPH_RING_GAP_PX + GRAPH_RING_WIDTH_PX : 0);
      });
      const bounds: Bounds[] = [];
      const rows = Array.from({ length: c.nodeIds.length }, (_, index) => index);
      const priority = (index: number) => c.nodeIsCurrent?.[index] ? 0 : c.nodeIsSeed?.[index] ? 1
        : c.nodeIds[index] === frame.selectedId ? 2 : c.nodeIds[index] === frame.focusedId ? 3 : 4;
      rows.sort((a, b) => priority(a) - priority(b) || c.nodeKeys[a].localeCompare(c.nodeKeys[b]));
      for (const index of rows) {
        const x = points[index * 2], y = points[index * 2 + 1], radius = diameters[index] / 2;
        if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || x > width || y < 0 || y > height) continue;
        const forced = priority(index) < 4;
        const current = Boolean(c.nodeIsCurrent?.[index]);
        const quiet = !current && Boolean(c.nodeMuted?.[index] || relatedNodes !== null && !relatedNodes.has(index));
        if (!forced && (!frame.showLabels || quiet || c.nodeSessions?.[index] && width <= 440)) continue;
        const text = c.nodeLabels[index];
        if (!text) continue;
        const cap = current ? 40 : 24;
        const letters = Array.from(text);
        const shortened = letters.length > cap ? `${letters.slice(0, cap).join("")}…` : text;
        const offset = radius + (current ? 6 : 5), margin = Math.min(8, width / 4);
        const roomRight = width - x - offset - 8, roomLeft = x - offset - 8;
        const right = roomRight >= roomLeft || roomLeft < 48;
        const room = Math.min(180, forced ? Math.max(right ? roomRight : roomLeft, width - 2 * margin)
          : right ? roomRight : roomLeft);
        if (room < 18 && !forced) continue;
        const fontSize = forced ? Math.min(12, Math.max(1, height - 4)) : 12;
        context.save();
        const badge = c.nodeBadges?.[index];
        const badgeFont = `600 ${Math.max(8, fontSize - 2)}px sans-serif`;
        context.font = badgeFont;
        const badgeWidth = badge && room - context.measureText(badge).width - 12 >= 24
          ? context.measureText(badge).width + 12 : 0;
        context.font = `${current ? 700 : c.nodeVisualKinds?.[index] === "folder" ? 600 : 400} ${fontSize}px sans-serif`;
        context.textAlign = right ? "left" : "right"; context.textBaseline = "middle";
        const textRoom = room - badgeWidth, graphemes = Array.from(shortened);
        let visible = shortened;
        while (graphemes.length > 1 && context.measureText(visible).width > textRoom) {
          graphemes.pop(); visible = `${graphemes.join("")}…`;
        }
        const textWidth = Math.min(textRoom, context.measureText(visible).width), labelWidth = textWidth + badgeWidth;
        const anchorX = x + (right ? offset : -offset);
        const labelX = forced ? Math.max(right ? margin : margin + labelWidth,
          Math.min(right ? width - margin - labelWidth : width - margin, anchorX)) : anchorX;
        const insetY = Math.min(fontSize / 2 + 2, height / 2);
        const anchorY = forced ? Math.max(insetY, Math.min(height - insetY, y)) : y;
        const lineStep = fontSize + 6;
        const offsets = forced ? [0, ...Array.from({ length: Math.min(31, Math.ceil(height / lineStep)) },
          (_, row) => [-(row + 1), row + 1]).flat()] : [0, -1, 1];
        const candidates = offsets.map(step => {
          const candidateY = anchorY + step * lineStep;
          const y = forced ? Math.max(insetY, Math.min(height - insetY, candidateY)) : candidateY;
          return { y, left: right ? labelX - 2 : labelX - labelWidth - 2,
            right: right ? labelX + labelWidth + 2 : labelX + 2, top: y - insetY, bottom: y + insetY };
        });
        const chosen = candidates.find(candidate => candidate.top >= 0 && candidate.bottom <= height
          && !bounds.some(bound => overlaps(candidate, bound))) ?? (forced ? candidates[0] : undefined);
        if (!chosen) { context.restore(); continue; }
        if (current) {
          const left = right ? labelX - 7 : labelX - labelWidth - 7, half = fontSize / 2 + 5;
          bounds.push({ left: left - 2, right: left + labelWidth + 16,
            top: chosen.y - half - 2, bottom: chosen.y + half + 2 });
          context.fillStyle = tokens.ink; context.beginPath();
          context.roundRect(left, chosen.y - half, labelWidth + 14, half * 2, 6); context.fill();
          context.fillStyle = tokens.surface; context.fillText(visible, labelX, chosen.y, room);
          context.restore(); continue;
        }
        bounds.push(chosen);
        const textX = right ? labelX + badgeWidth : labelX;
        if (badgeWidth && badge) {
          const pillX = right ? labelX : labelX - textWidth - badgeWidth, pillHeight = Math.max(10, fontSize + 1);
          context.save(); context.font = badgeFont; context.fillStyle = tokens.surface; context.beginPath();
          context.roundRect(pillX, chosen.y - pillHeight / 2, badgeWidth - 4, pillHeight, pillHeight / 2); context.fill();
          context.globalAlpha = .14; context.fillStyle = tokens.ink; context.fill(); context.globalAlpha = 1;
          context.textAlign = "center"; context.fillText(badge, pillX + (badgeWidth - 4) / 2, chosen.y + .5); context.restore();
        }
        context.lineWidth = 3; context.strokeStyle = tokens.surface; context.strokeText(visible, textX, chosen.y, textRoom);
        context.fillStyle = tokens.ink; context.fillText(visible, textX, chosen.y, textRoom); context.restore();
      }
      c.edgeIds.forEach((_, index) => {
        const caption = c.edgeCaptions?.[index];
        if (!caption) return;
        const from = c.edgeSources[index], to = c.edgeTargets[index];
        const x = (points[from * 2] + points[to * 2]) / 2, y = (points[from * 2 + 1] + points[to * 2 + 1]) / 2;
        if (!Number.isFinite(x) || !Number.isFinite(y)) return;
        context.save(); context.font = "600 11px sans-serif";
        const width = Math.min(180, context.measureText(caption).width + 16), chars = Array.from(caption);
        let visible = caption;
        while (chars.length && context.measureText(visible).width > width - 12) { chars.pop(); visible = `${chars.join("")}…`; }
        context.fillStyle = c.edgeRemoved?.[index] ? GRAPH_REMOVED_EDGE_COLOR : tokens.highlight;
        context.beginPath(); context.roundRect(x - width / 2, y - 11, width, 22, 7); context.fill();
        context.fillStyle = tokens.surface; context.textAlign = "center"; context.textBaseline = "middle";
        context.fillText(visible, x, y); context.restore();
      });
    },
    setTracing(id: string | null) { tracingId = id; if (lastFrame) overlay.render(lastFrame); },
    destroy() { rings.remove(); labels.remove(); },
  };
  return overlay;
}
