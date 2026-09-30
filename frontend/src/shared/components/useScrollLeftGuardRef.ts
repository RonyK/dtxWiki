import { useCallback, useRef, type ForwardedRef } from "react";

import { assignRef } from "./assignRef";

/**
 * The shell containers a `scroll` event may clamp back to 0.
 *
 * An explicit allowlist, not "every `overflow-x: hidden` descendant". Two
 * reasons, both load-bearing:
 *
 * 1. A hidden axis does NOT mean a scrolled state is illegitimate. When focus
 *    moves to a control sitting past the edge of an `overflow: hidden` box,
 *    the browser scrolls that box to reveal it -- the only way a keyboard or
 *    screen-reader user can see it, since there is no scrollbar to drag.
 *    `.inspector-panel` and `.ask-session-rail` are `overflow: hidden` on BOTH
 *    axes (04-knowledge.css:557-563) and sit inside the guarded subtree, so a
 *    blanket rule would snap focus back out of sight the instant it landed.
 * 2. It keeps the hot path a cheap match instead of a `getComputedStyle` call.
 *    An element already scrolled horizontally -- a wide markdown table, a code
 *    block -- fires `scroll` on every frame of its own VERTICAL scrolling too,
 *    and resolving computed style inside that handler would force a style
 *    recalc on the shell's busiest path.
 *
 * `.feature-main` is deliberately NOT here (260912 review): reason 1 above
 * applies to it too -- a focus-driven horizontal scroll lands on this same
 * element (P3, `.feature-main` is `overflow-x: hidden`), and this container
 * DOES have things inside it a horizontal offset can usefully reveal, unlike
 * `.app-shell`/`.main-surface`. Clamping it on every `scroll` event cancelled
 * that focus reveal outright: measured side by side with an identical,
 * unguarded box, the guarded one landed back at `scrollLeft: 0` with the
 * revealed control still off-screen. See `RESIZE_GUARDED_CONTAINERS` below --
 * `.feature-main` is still covered, just on a different trigger.
 */
const SCROLL_GUARDED_CONTAINERS = ".app-shell, .main-surface";

/**
 * The shell containers a viewport `resize` may clamp back to 0.
 *
 * A superset of `SCROLL_GUARDED_CONTAINERS`: `.feature-main` belongs here so
 * that whatever offset a Fold-style viewport shrink leaves behind (P3, the
 * Fold report) is still zeroed, this being the only guard that catches an
 * offset a resize introduces without also firing `scroll` (see
 * `clampAfterViewportResize` below).
 *
 * A resize is NOT always safe to clamp, though: a phone's virtual keyboard
 * opens on focus and fires `visualViewport.resize`, right after the browser
 * scrolled to reveal the focused control. `clampGuardedShellContainer` holds
 * the offset in exactly that case -- see `revealsFocusedElement`.
 */
const RESIZE_GUARDED_CONTAINERS = ".app-shell, .main-surface, .feature-main";

/**
 * A ref for the `.app-shell` root that snaps a stray horizontal scroll offset
 * on the shell's own layout containers back to 0.
 *
 * Why this exists: an `overflow: hidden` element is still a SCROLL CONTAINER
 * -- it can carry a `scrollLeft` with no scrollbar to show or clear it. A
 * browser preserves that offset across a viewport-size change by CLAMPING it
 * to `scrollWidth - clientWidth` instead of zeroing it, so if anything inside
 * was even 1px wider than the new width, that many pixels survive the resize,
 * every child renders shifted left by that amount, and there is no scrollbar
 * to drag it back. Permanent until reload. That is what a Galaxy Fold folding
 * down (690px unfolded -> 344px folded) did to the whole app: reproduced on
 * /lint (260911 Fold report), where a `.visually-hidden` live region
 * overflowed `.feature-main` by 24px and 24px of scrollLeft was what stuck.
 *
 * `.app-shell` and `.main-surface` take `overflow: clip` where their CSS paths
 * can use it, which is not a scroll container at all and so cannot hold an
 * offset. The app shell retains `hidden` until dynamic viewport units are
 * available: Chrome/Edge 90-107 know `clip` but must keep programmatic focus
 * reveal for their 100vh fallback. This guard is the other half of that path,
 * so it is attached to `.app-shell` itself rather than to `<main>`. A `scroll`
 * event targeting `.app-shell` never passes through a listener on one of its
 * own descendants, in any phase, so a guard mounted lower would have left the
 * outermost -- and, on those engines, still scrollable -- container uncovered.
 *
 * `.feature-main` can never take `clip`: it needs `overflow-y: auto` for real
 * vertical scrolling, and per the CSS Overflow spec a `clip` on one axis
 * computes back to `hidden` whenever the other axis is `auto`/`scroll`. So it
 * relies on this guard on every browser, new or old -- but only on the
 * resize path (`RESIZE_GUARDED_CONTAINERS` above). It is deliberately absent
 * from the `scroll`-event path: `.feature-main` is where a focus-driven
 * horizontal reveal actually lands, and clamping it on `scroll` cancelled
 * that reveal outright (P3, 260912 review).
 *
 * A captured `scroll` event covers focus, user action, and most layout
 * changes -- for `.app-shell`/`.main-surface`, where there is nothing to
 * reveal and so no reason not to clamp immediately. A viewport resize needs
 * a second path regardless, since Chromium can retain an existing
 * `scrollLeft` while it shrinks the viewport without dispatching a `scroll`
 * event at all, and that second path is what also covers `.feature-main`.
 * The resize handler clears the offset immediately and once more on the next
 * frame, after resize restoration and layout have settled. Only `scrollLeft`
 * is ever written; vertical scrolling is untouched.
 *
 * This is a REF CALLBACK, not a `useEffect`, and that is deliberate too: the
 * guarded element is re-created whenever React tears it down and rebuilds it,
 * and a `useEffect(..., [])` scoped to the component would attach once, to the
 * first instance, and silently stop covering anything afterward. A ref
 * callback is invoked with `null` (detach) then the new node (attach) exactly
 * when that happens -- proven by `useScrollLeftGuardRef.test.tsx`, which
 * forces the remount and asserts the guard still works on the other side.
 */
export function useScrollLeftGuardRef<T extends HTMLElement>(
  forwardedRef: ForwardedRef<T> = null,
) {
  const nodeRef = useRef<T | null>(null);
  const visualViewportRef = useRef<VisualViewport | null>(null);
  const resizeFrameRef = useRef<number | null>(null);

  const clampAllGuardedShellContainers = useCallback(() => {
    const root = nodeRef.current;
    if (!root) {
      return;
    }

    clampGuardedShellContainer(root, RESIZE_GUARDED_CONTAINERS);
    for (const container of root.querySelectorAll<HTMLElement>(
      RESIZE_GUARDED_CONTAINERS,
    )) {
      clampGuardedShellContainer(container, RESIZE_GUARDED_CONTAINERS);
    }
  }, []);

  const clampAfterViewportResize = useCallback(() => {
    clampAllGuardedShellContainers();

    if (resizeFrameRef.current !== null) {
      return;
    }

    resizeFrameRef.current = window.requestAnimationFrame(() => {
      resizeFrameRef.current = null;
      clampAllGuardedShellContainers();
    });
  }, [clampAllGuardedShellContainers]);

  return useCallback(
    (node: T | null) => {
      assignRef(forwardedRef, node);
      // React calls a ref callback with `null` on detach without saying which
      // node was detached, so the OLD node has to come from our own bookkeeping
      // -- read it before the assignment at the end overwrites it.
      if (nodeRef.current && nodeRef.current !== node) {
        nodeRef.current.removeEventListener(
          "scroll",
          clampGuardedShellScrollLeft,
          { capture: true },
        );
        window.removeEventListener("resize", clampAfterViewportResize);
        visualViewportRef.current?.removeEventListener(
          "resize",
          clampAfterViewportResize,
        );
        visualViewportRef.current = null;
        if (resizeFrameRef.current !== null) {
          window.cancelAnimationFrame(resizeFrameRef.current);
          resizeFrameRef.current = null;
        }
      }
      nodeRef.current = node;
      if (node) {
        // Capture phase: `scroll` does not bubble, so a listener here would
        // never see it fire on a descendant. The capture phase intercepts it on
        // the way down instead. The node itself stays covered either way -- an
        // event targeting it is dispatched at the target in every phase.
        node.addEventListener("scroll", clampGuardedShellScrollLeft, {
          capture: true,
          passive: true,
        });
        window.addEventListener("resize", clampAfterViewportResize);
        visualViewportRef.current = window.visualViewport;
        visualViewportRef.current?.addEventListener(
          "resize",
          clampAfterViewportResize,
        );
        clampAfterViewportResize();
      }
    },
    [clampAfterViewportResize, forwardedRef],
  );
}

function clampGuardedShellScrollLeft(event: Event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  clampGuardedShellContainer(target, SCROLL_GUARDED_CONTAINERS);
}

function clampGuardedShellContainer(container: HTMLElement, selector: string) {
  if (container.scrollLeft === 0 || !container.matches(selector)) {
    return;
  }
  if (revealsFocusedElement(container)) {
    return;
  }
  container.scrollLeft = 0;
}

/**
 * Is this container's horizontal offset the only thing keeping the focused
 * control on screen?
 *
 * A resize is not always the viewport changing shape. On a phone, focusing an
 * input opens the virtual keyboard, which fires `visualViewport.resize` -- so
 * the clamp would run immediately after the browser scrolled horizontally to
 * reveal the very control the user just tapped, and undo it. The offset would
 * be rebuilt on the next keystroke and clamped again.
 *
 * "Contains the focused element" is too broad to use on its own: every screen
 * lives inside `.main-surface`, so focus is almost always somewhere below
 * these containers and the guard would never fire at all. The question is
 * narrower -- would the focused element still be visible at `scrollLeft: 0`?
 * Its distance from the container's content origin is its current offset from
 * the container's left edge plus whatever is already scrolled away; if that
 * reaches past the visible width, zeroing hides it and the offset is load
 * bearing.
 */
function revealsFocusedElement(container: HTMLElement) {
  const focused = container.ownerDocument.activeElement;
  if (
    !(focused instanceof HTMLElement) ||
    focused === container.ownerDocument.body ||
    !container.contains(focused) ||
    focused === container
  ) {
    return false;
  }
  const offsetFromContentOrigin =
    focused.getBoundingClientRect().left -
    container.getBoundingClientRect().left +
    container.scrollLeft;
  return offsetFromContentOrigin >= container.clientWidth;
}
