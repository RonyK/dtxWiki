import { autoUpdate, computePosition, flip, hide, offset, shift, size } from "@floating-ui/dom";
import type { MouseEvent } from "react";
import { useCallback, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

import type { TreeNodePayload } from "../api/workspace";
import { treeNodeStatusDescription } from "./treeNodeStatus";

type TooltipOwner = { element: HTMLElement; nodeId: string; direction: "ltr" | "rtl" };

// A focused row in another rail must not leave a second popup behind when a
// pointer or keyboard user starts reading a different row.
let activeTooltip: { dismiss: () => void } | null = null;
let suppressedPointer: { x: number; y: number; release: () => void } | null = null;

function suppressPointerReentry(x: number, y: number) {
  suppressedPointer?.release();
  const release = () => {
    document.removeEventListener("pointermove", handleMove, true);
    if (suppressedPointer?.release === release) suppressedPointer = null;
  };
  const handleMove = (event: globalThis.PointerEvent) => {
    if (event.clientX !== x || event.clientY !== y) release();
  };
  suppressedPointer = { x, y, release };
  document.addEventListener("pointermove", handleMove, true);
  return release;
}

function hasCoveringOverlay(owner: HTMLElement) {
  return Array.from(document.querySelectorAll(".tree-node-menu, .dialog-backdrop, [aria-modal='true']"))
    .some((overlay) => !overlay.contains(owner));
}

export function useTreeRowTooltip({
  nodes,
  now,
  disabled,
  scopeKey,
}: {
  nodes: ReadonlyMap<string, TreeNodePayload>;
  now: number;
  disabled: boolean;
  scopeKey: string;
}) {
  const { t, i18n } = useTranslation("tree");
  const id = useId();
  const [owner, setOwner] = useState<TooltipOwner | null>(null);
  const ownerRef = useRef<TooltipOwner | null>(null);
  const popupRef = useRef<HTMLDivElement | null>(null);
  const pointerWithinRef = useRef(false);
  const pointerPositionRef = useRef({ x: 0, y: 0 });
  const suppressionReleaseRef = useRef<(() => void) | null>(null);
  const dismiss = useCallback(() => {
    ownerRef.current = null;
    pointerWithinRef.current = false;
    setOwner(null);
    if (activeTooltip?.dismiss === dismiss) activeTooltip = null;
  }, []);
  const dismissForActivation = useCallback((event?: { clientX: number; clientY: number; detail: number }) => {
    if (event && event.detail > 0) {
      pointerPositionRef.current = { x: event.clientX, y: event.clientY };
    }
    suppressionReleaseRef.current = suppressPointerReentry(pointerPositionRef.current.x, pointerPositionRef.current.y);
    dismiss();
  }, [dismiss]);

  function show(nodeId: string, element: HTMLElement, pointer: boolean) {
    if (disabled || hasCoveringOverlay(element) || (pointer && suppressedPointer)) return;
    if (!pointer) suppressedPointer?.release();
    if (activeTooltip?.dismiss !== dismiss) activeTooltip?.dismiss();
    activeTooltip = { dismiss };
    const next: TooltipOwner = { nodeId, element,
      direction: getComputedStyle(element).direction === "rtl" ? "rtl" : "ltr" };
    if (ownerRef.current?.element !== element) pointerWithinRef.current = pointer;
    ownerRef.current = next;
    if (pointer) pointerWithinRef.current = true;
    setOwner((current) => current?.element === element ? current : next);
  }

  function pointerWithin(target: EventTarget | null) {
    return target instanceof Node && Boolean(
      ownerRef.current?.element.contains(target) || popupRef.current?.contains(target),
    );
  }

  function leave(event: MouseEvent<HTMLElement>) {
    pointerWithinRef.current = pointerWithin(event.relatedTarget);
    if (!pointerWithinRef.current && !ownerRef.current?.element.contains(document.activeElement)) dismiss();
  }

  useLayoutEffect(() => {
    if (disabled) dismiss();
  }, [disabled, dismiss]);
  useLayoutEffect(() => () => {
    dismiss();
    suppressionReleaseRef.current?.();
  }, [dismiss, scopeKey]);

  const node = owner ? nodes.get(owner.nodeId) : undefined;
  const status = node ? treeNodeStatusDescription(node, now, i18n.resolvedLanguage ?? i18n.language, t) : "";
  useLayoutEffect(() => {
    if (!owner) return;
    const popup = popupRef.current;
    if (!node || !popup || !owner.element.isConnected) {
      dismiss();
      return;
    }
    let disposed = false;
    let revision = 0;
    const update = () => {
      const currentRevision = ++revision;
      if (!owner.element.isConnected) {
        dismiss();
        return;
      }
      const reference = owner.element.getBoundingClientRect();
      if (!reference.width || !reference.height) {
        dismiss();
        return;
      }
      void computePosition(owner.element, popup, {
        placement: "bottom-start",
        strategy: "fixed",
        middleware: [
          offset(6), flip({ padding: 8 }),
          size({ padding: 8, apply: ({ availableHeight, elements }) => {
            // A full title can be taller than either side of its row. Keep
            // the popup beside its owner and let its content scroll to fit.
            const content = elements.floating.querySelector<HTMLElement>(".tree-row-tooltip-content");
            if (content) content.style.maxHeight = `${Math.max(0, availableHeight - 2)}px`;
          } }),
          shift({ padding: 8 }), hide({ strategy: "referenceHidden" }),
        ],
      }).then(({ x, y, placement, middlewareData }) => {
        if (disposed || currentRevision !== revision) return;
        if (middlewareData.hide?.referenceHidden) {
          dismiss();
          return;
        }
        popup.style.left = `${x}px`;
        popup.style.top = `${y}px`;
        popup.dataset.placement = placement;
      });
    };
    // Tracks scrollable ancestors, viewport resize, row/content resize, layout
    // shifts, and transformed drawer movement while this one popup is active.
    const cleanup = autoUpdate(owner.element, popup, update, { animationFrame: true });
    const viewport = window.visualViewport;
    viewport?.addEventListener("resize", update);
    viewport?.addEventListener("scroll", update);
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        dismissForActivation();
      }
    };
    const handlePointerMove = (event: globalThis.PointerEvent) => {
      pointerPositionRef.current = { x: event.clientX, y: event.clientY };
      // WebKit can omit mouseleave when expanding a row replaces its hovered
      // chevron SVG. The next real target reconciles row and popup ownership.
      pointerWithinRef.current = pointerWithin(event.target);
      if (!pointerWithinRef.current && !owner.element.contains(document.activeElement)) dismiss();
    };
    const dismissForOverlay = () => {
      if (hasCoveringOverlay(owner.element)) dismiss();
    };
    const overlays = new MutationObserver(dismissForOverlay);
    overlays.observe(document.body, { childList: true, subtree: true });
    document.addEventListener("keydown", handleKeyDown, true);
    document.addEventListener("pointermove", handlePointerMove, true);
    document.addEventListener("dragstart", dismiss, true);
    return () => {
      disposed = true;
      cleanup();
      overlays.disconnect();
      viewport?.removeEventListener("resize", update);
      viewport?.removeEventListener("scroll", update);
      document.removeEventListener("keydown", handleKeyDown, true);
      document.removeEventListener("pointermove", handlePointerMove, true);
      document.removeEventListener("dragstart", dismiss, true);
    };
  }, [owner, node, status, dismiss, dismissForActivation]);

  const rect = owner?.element.getBoundingClientRect();
  return {
    dismissForActivation,
    focus: (nodeId: string, element: HTMLElement) => show(nodeId, element, false),
    blur: (relatedTarget: EventTarget | null) => {
      if (!ownerRef.current?.element.contains(relatedTarget as Node | null) && !pointerWithinRef.current) dismiss();
    },
    enter: (nodeId: string, element: HTMLElement, event: { clientX: number; clientY: number }) => {
      pointerPositionRef.current = { x: event.clientX, y: event.clientY };
      show(nodeId, element, true);
    },
    move: (nodeId: string, element: HTMLElement, event: { clientX: number; clientY: number; pointerType?: string }) => {
      // A stationary overlay removal can synthesize mouseenter with rounded
      // MouseEvent coordinates. Only native pointer movement releases the
      // suppression; its capture listener runs before this row handler.
      if (event.pointerType === "touch") return;
      pointerPositionRef.current = { x: event.clientX, y: event.clientY };
      if (ownerRef.current?.element !== element) show(nodeId, element, true);
    },
    leave,
    popup: owner && node && rect && !disabled ? createPortal(
      <div
        id={id}
        ref={popupRef}
        role="tooltip"
        dir={owner.direction}
        className="tree-row-tooltip"
        data-placement="bottom-start"
        style={{ left: Math.max(8, rect.left), top: rect.bottom + 6 }}
        onMouseEnter={() => { pointerWithinRef.current = true; }}
        onMouseLeave={leave}
      >
        <div className="tree-row-tooltip-content">
          <strong>{node.title}</strong>
          {status ? <span>{status}</span> : null}
        </div>
      </div>, document.body,
    ) : null,
  };
}

export type TreeRowTooltip = ReturnType<typeof useTreeRowTooltip>;
