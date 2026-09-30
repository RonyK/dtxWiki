// Focus-trap helper extracted from Dialog so it can be reused by any
// modal-like surface (Dialog, MobileDrawer, BottomSheet, etc.) that
// needs to keep keyboard focus inside a container while open.

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Collect the visible, tab-reachable elements inside `container`.
 *
 * Mirrors the previous in-line Dialog logic: invisible nodes
 * (`offsetParent === null`) are filtered out unless they are the
 * currently focused element (which legitimately may not have a parent
 * offset in jsdom but should still participate in the cycle).
 */
export function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  ).filter(
    (element) =>
      element.offsetParent !== null || element === document.activeElement,
  );
}

/**
 * Handle a Tab/Shift+Tab keydown so that focus stays within `container`.
 *
 * Behaviour:
 * - If the container has no focusable descendants, focus is forced back
 *   onto the container itself.
 * - If the current focus is outside the container, focus jumps to the
 *   first focusable element inside.
 * - Tab from the last focusable wraps to the first; Shift+Tab from the
 *   first wraps to the last.
 */
export function trapTabFocus(
  event: KeyboardEvent,
  container: HTMLElement | null,
): void {
  if (!container) {
    return;
  }
  const focusable = focusableElements(container);
  if (focusable.length === 0) {
    event.preventDefault();
    container.focus();
    return;
  }
  if (
    document.activeElement instanceof Node &&
    !container.contains(document.activeElement)
  ) {
    event.preventDefault();
    focusable[0].focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
    return;
  }
  if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
