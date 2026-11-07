import { useEffect, useLayoutEffect, useRef } from "react";

/**
 * A re-render can unmount the focused control without the reader doing
 * anything to it: the list switching between bow-tie and flow, a timeline
 * step or refresh dropping a row, a group emptying. Focus would fall to the
 * document body, where the graph's own keys (Escape) and the reader's place
 * are lost.
 *
 * After every commit of the owner, if the control focused last inside `root`
 * is gone and focus is on the body, focus the same item (the same
 * `data-local-graph-id`, its pressed copy first) inside `root`, else
 * `fallback()`. A commit that changes the selection is left to the
 * selection's own focus hand-off, and focus the reader moved anywhere else is
 * never taken.
 */
export function useFocusRecovery(
  rootRef: { current: HTMLElement | null },
  fallback: () => HTMLElement | null,
  selectionKey: string | null,
) {
  const lastRef = useRef<HTMLElement | null>(null);
  const selectionRef = useRef(selectionKey);
  const fallbackRef = useRef(fallback);
  useLayoutEffect(() => {
    fallbackRef.current = fallback;
  });
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return undefined;
    const onFocusIn = (event: FocusEvent) => {
      if (event.target instanceof HTMLElement) lastRef.current = event.target;
    };
    root.addEventListener("focusin", onFocusIn);
    return () => root.removeEventListener("focusin", onFocusIn);
  }, [rootRef]);
  useLayoutEffect(() => {
    const selectionChanged = selectionRef.current !== selectionKey;
    selectionRef.current = selectionKey;
    const last = lastRef.current;
    const root = rootRef.current;
    if (!last || last.isConnected || !root) return;
    lastRef.current = null;
    if (selectionChanged) return;
    const active = document.activeElement;
    if (active && active !== document.body) return;
    const id = last.dataset.localGraphId;
    const same = id === undefined ? undefined : Array.from(root.querySelectorAll<HTMLElement>("[data-local-graph-id]"))
      .filter((candidate) => candidate.dataset.localGraphId === id && !candidate.closest("[hidden]"))
      .sort((left, right) => Number(right.getAttribute("aria-pressed") === "true") - Number(left.getAttribute("aria-pressed") === "true"))[0];
    if (same) {
      same.focus({ preventScroll: true });
      same.scrollIntoView?.({ block: "nearest" });
      return;
    }
    fallbackRef.current()?.focus({ preventScroll: true });
  });
}
