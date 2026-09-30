import type { RefObject } from "react";
import { useCallback, useEffect } from "react";

type MobileModalFocusOptions = {
  active: boolean;
  containerRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  restoreFocusRef: RefObject<HTMLElement | null>;
};

/**
 * Shares the keyboard-focus lifecycle used by mobile workspace drawers and
 * sheets: focus enters the surface, Tab stays trapped, Escape closes it, and
 * focus returns to the trigger after the surface is dismissed.
 */
export function useMobileModalFocus({
  active,
  containerRef,
  onClose,
  restoreFocusRef,
}: MobileModalFocusOptions) {
  const restoreFocus = useCallback(() => {
    restoreFocusRef.current?.focus();
  }, [restoreFocusRef]);

  useEffect(() => {
    if (!active) {
      return;
    }
    const container = containerRef.current;
    if (!container) {
      return;
    }
    const modalContainer = container;
    const focusTimer = window.setTimeout(
      () => focusFirstModalElement(modalContainer),
      0,
    );

    function handleKeyDown(event: KeyboardEvent) {
      const eventTarget = event.target;
      // Portaled dialogs and popups render outside the drawer/sheet DOM even
      // when the component that opened them lives inside it. Their key events
      // belong to that outer surface; closing this container as well would
      // collapse two modal layers on a single Escape.
      if (!(eventTarget instanceof Node) || !modalContainer.contains(eventTarget)) {
        return;
      }
      // Nested popups (for example a TreeRail row menu) own their dismissal
      // key by preventing its default action. Handle the modal surface in the
      // bubble phase so that ownership is visible before Escape can close the
      // surrounding drawer or sheet.
      if (event.defaultPrevented) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key === "Tab") {
        trapModalTabFocus(event, modalContainer);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("keydown", handleKeyDown);
      // The trigger can be conditionally remounted after the modal closes, so
      // read the ref when this callback runs rather than when the modal opens.
      window.setTimeout(restoreFocus, 0);
    };
  }, [active, containerRef, onClose, restoreFocus]);
}

function focusFirstModalElement(container: HTMLElement) {
  const focusable = focusableElements(container);
  (focusable[0] ?? container).focus();
}

function trapModalTabFocus(event: KeyboardEvent, container: HTMLElement) {
  const focusable = focusableElements(container);
  if (focusable.length === 0) {
    event.preventDefault();
    container.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (!container.contains(document.activeElement)) {
    event.preventDefault();
    first.focus();
    return;
  }
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

function focusableElements(container: HTMLElement) {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute("aria-hidden"));
}
