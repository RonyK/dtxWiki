import {
  type ReactNode,
  type RefObject,
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";

import { BottomSheet } from "../../shared/components/BottomSheet";
import { useMobileModalFocus } from "../../shared/components/useMobileModalFocus";

/**
 * The single top row of the phone and tablet editor. Space runs out in one
 * order: the message gives up its text first (the state icon stays, and the
 * full text is in the overflow header), then request-review moves into the
 * overflow. Measuring instead of using a breakpoint keeps this correct for any
 * language, because label widths differ far more than screen widths do.
 */
export function EditorCompactBar({
  actions,
  close,
  fitKey,
  lead,
  message,
  onReviewInMenuChange,
  title,
}: {
  actions: ReactNode;
  close: ReactNode;
  fitKey: string;
  lead: ReactNode;
  message: ReactNode;
  onReviewInMenuChange: (reviewInMenu: boolean) => void;
  title: ReactNode;
}) {
  const barRef = useRef<HTMLDivElement | null>(null);

  useLayoutEffect(() => {
    const bar = barRef.current;
    if (!bar) {
      return undefined;
    }
    const element = bar;
    const overflows = () => element.scrollWidth > element.clientWidth;
    function placeTitle() {
      const slot = element.querySelector<HTMLElement>(".editor-compact-bar-title-slot");
      const panel = element.parentElement;
      if (!slot || !panel) return;
      const slotBox = slot.getBoundingClientRect();
      panel.style.setProperty("--editor-title-slot-left", `${slotBox.left - panel.getBoundingClientRect().left}px`);
      panel.style.setProperty("--editor-title-slot-width", `${slotBox.width}px`);
    }
    function fit() {
      element.dataset.message = "full";
      element.dataset.review = "bar";
      let reviewInMenu = false;
      if (overflows()) {
        element.dataset.message = "icon";
        reviewInMenu = overflows();
        element.dataset.review = reviewInMenu ? "menu" : "bar";
      }
      onReviewInMenuChange(reviewInMenu);
      placeTitle();
    }
    fit();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    observer?.observe(element);
    let disposed = false;
    void document.fonts?.ready.then(() => {
      if (!disposed) {
        fit();
      }
    });
    return () => {
      disposed = true;
      observer?.disconnect();
    };
  }, [fitKey, onReviewInMenuChange]);

  return (
    <div className="editor-compact-bar" ref={barRef}>
      <div className="editor-compact-bar-lead">{lead}</div>
      {title}
      {message}
      {actions}
      {close}
    </div>
  );
}

export type EditorOverflowEntry =
  | {
      ariaDisabled?: boolean;
      ariaLabel?: string;
      attention?: boolean;
      badge?: ReactNode;
      disabled?: boolean;
      icon: ReactNode;
      id: string;
      label: string;
      onSelect: () => void;
      pressed?: boolean;
      title?: string;
    }
  | { id: string; node: ReactNode };

/** A bottom sheet on phones, a menu under its button on wider screens. */
export function EditorOverflowPanel({
  anchorLeft,
  closeLabel,
  groups,
  header,
  label,
  onClose,
  presentation,
  triggerRef,
}: {
  anchorLeft: number;
  closeLabel: string;
  groups: readonly (readonly EditorOverflowEntry[])[];
  header: ReactNode;
  label: string;
  onClose: () => void;
  presentation: "menu" | "sheet";
  triggerRef: RefObject<HTMLElement | null>;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  useLayoutEffect(() => {
    restoreFocusRef.current = triggerRef.current;
  }, [triggerRef]);

  useMobileModalFocus({
    active: true,
    containerRef: panelRef,
    onClose,
    restoreFocusRef,
  });

  useEffect(() => {
    const openedAtWidth = window.innerWidth;
    function closeWhenWidthChanges() {
      if (window.innerWidth !== openedAtWidth) {
        onClose();
      }
    }
    window.addEventListener("resize", closeWhenWidthChanges);
    return () => window.removeEventListener("resize", closeWhenWidthChanges);
  }, [onClose]);

  function select(onSelect: () => void) {
    // The row is about to unmount. Focus returns to the button now, so a popover
    // keeps a focused anchor and a dialog records where to return; the delayed
    // restore is cancelled so it cannot pull focus out of what opens next.
    triggerRef.current?.focus();
    restoreFocusRef.current = null;
    onClose();
    onSelect();
  }

  return (
    <>
      <button
        aria-label={closeLabel}
        className="editor-overflow-scrim"
        data-presentation={presentation}
        onClick={onClose}
        tabIndex={-1}
        type="button"
      />
      <BottomSheet
        className="editor-overflow-panel"
        data-presentation={presentation}
        label={label}
        mobileLayout
        mobileOpen
        ref={panelRef}
        style={presentation === "menu" ? { left: anchorLeft } : undefined}
      >
        <div className="editor-overflow-header">{header}</div>
        {groups.map((group) => (
          <div className="editor-overflow-group" key={group[0]?.id}>
            {group.map((entry) =>
              "node" in entry ? (
                <div key={entry.id}>{entry.node}</div>
              ) : (
                <button
                  aria-disabled={entry.ariaDisabled || undefined}
                  aria-label={entry.ariaLabel}
                  aria-pressed={entry.pressed}
                  className="editor-overflow-row"
                  data-attention={entry.attention || undefined}
                  disabled={entry.disabled}
                  key={entry.id}
                  onClick={() => select(entry.onSelect)}
                  title={entry.title}
                  type="button"
                >
                  {entry.icon}
                  <span>{entry.label}</span>
                  {entry.badge === undefined ? null : (
                    <span aria-hidden="true" className="editor-overflow-row-badge">
                      {entry.badge}
                    </span>
                  )}
                </button>
              ),
            )}
          </div>
        ))}
      </BottomSheet>
    </>
  );
}
