import type { ReactNode } from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import { trapTabFocus } from "../lib/trapTabFocus";

type DialogProps = {
  children: ReactNode;
  className?: string;
  /**
   * Controls implicit dismissal (Escape and backdrop press). Explicit action
   * buttons may still call `onClose`, which lets mutation dialogs keep their
   * result surface mounted while a request is in flight without changing the
   * existing default behavior for every read-only dialog.
   */
  dismissible?: boolean;
  footer?: ReactNode;
  labelledBy: string;
  onClose: () => void;
  /**
   * Resolves a route-local focus target when the element that opened the
   * dialog was removed while the dialog was mounted (for example, deleting a
   * tree row). The resolved element is still required to be connected and
   * outside an inert subtree before it receives focus.
   */
  restoreFocusFallback?: () => HTMLElement | null;
};

export type DialogOverlayScope = {
  overlayRoot: HTMLDivElement | null;
  panel: HTMLDivElement | null;
  scrollRegion: HTMLDivElement | null;
};

const DialogOverlayContext = createContext<DialogOverlayScope | null>(null);

/**
 * Returns the overlay surface owned by the nearest dialog. Anchored popovers
 * can portal into this surface to escape the dialog's scroll clipping while
 * remaining a DOM descendant of the modal for focus containment and aria.
 */
export function useDialogOverlayScope() {
  return useContext(DialogOverlayContext);
}

export function DialogCloseButton({
  disabled = false,
  label,
  onClick,
}: {
  disabled?: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-label={label}
      className="dialog-close-button icon-button"
      disabled={disabled}
      onClick={onClick}
      title={label}
      type="button"
    >
      <X aria-hidden="true" size={18} />
    </button>
  );
}

let bodyScrollLockCount = 0;
let savedBodyOverflow = "";
let savedBodyPaddingInlineEnd = "";

function lockDocumentScroll() {
  if (typeof document === "undefined") {
    return () => {};
  }
  const body = document.body;
  if (bodyScrollLockCount === 0) {
    savedBodyOverflow = body.style.overflow;
    savedBodyPaddingInlineEnd = body.style.paddingInlineEnd;
    const rootWidth = document.documentElement.clientWidth;
    const scrollbarWidth = rootWidth > 0
      ? Math.max(0, window.innerWidth - rootWidth)
      : 0;
    body.style.overflow = "hidden";
    if (scrollbarWidth > 0) {
      body.style.paddingInlineEnd = `${scrollbarWidth}px`;
    }
  }
  bodyScrollLockCount += 1;
  return () => {
    bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1);
    if (bodyScrollLockCount === 0) {
      body.style.overflow = savedBodyOverflow;
      body.style.paddingInlineEnd = savedBodyPaddingInlineEnd;
    }
  };
}

export function Dialog({
  children,
  className = "",
  dismissible = true,
  footer,
  labelledBy,
  onClose,
  restoreFocusFallback,
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [panel, setPanel] = useState<HTMLDivElement | null>(null);
  const [scrollRegion, setScrollRegion] = useState<HTMLDivElement | null>(null);
  const [overlayRoot, setOverlayRoot] = useState<HTMLDivElement | null>(null);
  const setPanelRef = useCallback((element: HTMLDivElement | null) => {
    panelRef.current = element;
    setPanel(element);
  }, []);
  const previousFocusRef = useRef<Element | null>(
    typeof document === "undefined" ? null : document.activeElement,
  );
  const initialFocusedDescendantRef = useRef<HTMLElement | null>(null);
  // Call sites usually pass inline arrows, so onClose changes identity on
  // every parent re-render. Read it through a ref so the focus side effects
  // below stay mount/unmount-only and an open dialog never has the user's
  // caret yanked back to the panel mid-typing.
  const onCloseRef = useRef(onClose);
  const dismissibleRef = useRef(dismissible);
  const restoreFocusFallbackRef = useRef(restoreFocusFallback);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    dismissibleRef.current = dismissible;
  }, [dismissible]);

  useEffect(() => {
    restoreFocusFallbackRef.current = restoreFocusFallback;
  }, [restoreFocusFallback]);

  useEffect(() => {
    const previousFocus = previousFocusRef.current;
    const panel = panelRef.current;
    // React applies a descendant autoFocus during commit before passive
    // effects run. Remember it so development StrictMode's effect cleanup and
    // replay can restore that explicit caller intent instead of ending on the
    // panel. Use the panel itself only when no descendant requested focus.
    const activeElement = document.activeElement;
    if (
      panel
      && activeElement instanceof HTMLElement
      && panel.contains(activeElement)
    ) {
      initialFocusedDescendantRef.current = activeElement;
    } else if (panel) {
      const initialFocusedDescendant = initialFocusedDescendantRef.current;
      if (
        initialFocusedDescendant?.isConnected
        && panel.contains(initialFocusedDescendant)
      ) {
        initialFocusedDescendant.focus();
      } else {
        panel.focus();
      }
    }
    return () => {
      if (restoreFocus(previousFocus)) {
        return;
      }
      restoreFocus(restoreFocusFallbackRef.current?.() ?? null);
    };
  }, []);

  useEffect(() => lockDocumentScroll(), []);

  const overlayScope = useMemo<DialogOverlayScope>(
    () => ({ overlayRoot, panel, scrollRegion }),
    [overlayRoot, panel, scrollRegion],
  );

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      // An inner popup owns Escape when it has already handled the event.
      // This is important for listboxes such as TreePickerDropdown nested in
      // a mutation dialog: the first Escape closes only the popup.
      if (
        event.key === "Escape"
        && !event.defaultPrevented
        && dismissibleRef.current
      ) {
        event.preventDefault();
        onCloseRef.current();
      }
      if (event.key === "Tab") {
        trapTabFocus(event, panelRef.current);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  const dialog = (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (dismissible && event.target === event.currentTarget) {
          onClose();
        }
      }}
      role="presentation"
    >
      <div
        aria-labelledby={labelledBy}
        aria-modal="true"
        className={`dialog-panel ${className}`.trim()}
        ref={setPanelRef}
        role="dialog"
        tabIndex={-1}
      >
        <DialogOverlayContext.Provider value={overlayScope}>
          <div className="dialog-scroll-region" ref={setScrollRegion}>
            {children}
          </div>
          {footer == null ? null : (
            <div className="dialog-footer">{footer}</div>
          )}
          <div
            className="dialog-overlay-root"
            ref={setOverlayRoot}
          />
        </DialogOverlayContext.Provider>
      </div>
    </div>
  );

  // Render at the document root so a caller inside an off-canvas panel cannot
  // trap this fixed backdrop inside a transformed containing block. This also
  // gives every modal one predictable stacking context above drawers,
  // inspectors, popovers, and feature-local overflow clipping.
  return typeof document === "undefined"
    ? dialog
    : createPortal(dialog, document.body);
}

function restoreFocus(target: Element | null) {
  if (
    !(target instanceof HTMLElement)
    || !target.isConnected
    || target.closest('[hidden], [aria-hidden="true"], [inert]') !== null
    || !target.matches(
      'a[href], area[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, iframe, [contenteditable="true"], [tabindex]:not([tabindex="-1"])',
    )
  ) {
    return false;
  }
  target.focus();
  return true;
}
