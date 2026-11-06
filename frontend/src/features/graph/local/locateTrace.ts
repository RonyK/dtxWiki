/** How long the ring takes to trace once around the current document. */
export const LOCATE_TRACE_MS = 800;

/** Where the ring is drawn (the centre line of its band), in container pixels; null when the point is not drawn. */
export type LocateTraceGeometry = { x: number; y: number; radius: number; width: number; color: string } | null;

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * A one-shot trace of the current document's ink ring after the reader asks
 * to locate it: the ring is drawn once, clockwise from the top, and the node's
 * own ring takes over where it ends. It is an SVG over the canvas because a
 * node program cannot draw part of a ring. `onEnd` runs once per play,
 * however it ends, so the node's ring is never left hidden.
 */
export function createLocateTrace(container: HTMLElement, onEnd: () => void) {
  let svg: SVGSVGElement | null = null;
  let circle: SVGCircleElement | null = null;
  let timer: number | undefined;

  const stop = (notify: boolean) => {
    if (timer !== undefined) window.clearTimeout(timer);
    timer = undefined;
    const playing = svg !== null;
    svg?.remove();
    svg = null;
    circle = null;
    if (notify && playing) onEnd();
  };

  return {
    get playing() { return svg !== null; },
    play() {
      stop(true);
      svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("class", "graph-locate-trace");
      svg.setAttribute("aria-hidden", "true");
      circle = document.createElementNS(SVG_NS, "circle");
      // A unit path length makes the dash animation independent of the radius,
      // which follows zoom while the trace plays.
      circle.setAttribute("pathLength", "1");
      circle.addEventListener("animationend", () => stop(true), { once: true });
      svg.appendChild(circle);
      container.appendChild(svg);
      // animationend never fires without animation support (or when the
      // element is not rendered); the node's ring must still come back.
      timer = window.setTimeout(() => stop(true), LOCATE_TRACE_MS + 200);
    },
    place(geometry: LocateTraceGeometry) {
      if (!circle) return;
      if (!geometry) { stop(true); return; }
      const { color, radius, width, x, y } = geometry;
      circle.setAttribute("cx", String(x));
      circle.setAttribute("cy", String(y));
      circle.setAttribute("r", String(radius));
      circle.setAttribute("stroke", color);
      circle.setAttribute("stroke-width", String(width));
      // Start at the top, as the static ring is read: clockwise from 12 o'clock.
      circle.setAttribute("transform", `rotate(-90 ${x} ${y})`);
    },
    cancel() { stop(true); },
    destroy() { stop(false); },
  };
}
