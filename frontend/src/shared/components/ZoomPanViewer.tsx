import { Minus, Plus, RotateCcw, X } from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type TouchEvent as ReactTouchEvent,
} from "react";
import { useTranslation } from "react-i18next";

const MIN_SCALE = 0.25;
const MAX_SCALE = 8;
const SCALE_STEP = 1.2;

type ZoomPanViewerProps = {
  /** Fitted to the viewport at 100%; the toolbar, wheel, keys and pinch scale it. */
  children: ReactNode;
  closeLabel: string;
  /** Names the viewport as one image when the content has no accessible name of its own. */
  contentLabel?: string;
  controlsLabel?: string;
  onClose: () => void;
};

type Pan = {
  x: number;
  y: number;
};

type DragState = {
  baseX: number;
  baseY: number;
  pointerId: number;
  startX: number;
  startY: number;
};

type PinchState = {
  baseScale: number;
  distance: number;
};

function clampScale(value: number) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value));
}

/**
 * A zoom and pan surface for one piece of content: a toolbar (zoom out, the
 * current percentage that resets on click, zoom in, reset, close), wheel zoom
 * around the pointer, +/-/0 keys, drag to pan and two-finger pinch. Mermaid
 * diagrams and enlarged images share it so both behave the same way.
 */
export function ZoomPanViewer({
  children,
  closeLabel,
  contentLabel,
  controlsLabel,
  onClose,
}: ZoomPanViewerProps) {
  const { t } = useTranslation("shell");
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const pinchRef = useRef<PinchState | null>(null);
  const [scale, setScale] = useState(1);
  const [pan, setPan] = useState<Pan>({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const zoomPercent = Math.round(scale * 100);

  const zoomBy = useCallback((factor: number, anchor?: { x: number; y: number }) => {
    setScale((current) => {
      const next = clampScale(current * factor);
      if (next === current) {
        return current;
      }
      if (anchor && viewportRef.current) {
        const rect = viewportRef.current.getBoundingClientRect();
        const anchorX = anchor.x - rect.left - rect.width / 2;
        const anchorY = anchor.y - rect.top - rect.height / 2;
        setPan((currentPan) => ({
          x: anchorX - (anchorX - currentPan.x) * (next / current),
          y: anchorY - (anchorY - currentPan.y) * (next / current),
        }));
      }
      return next;
    });
  }, []);

  const resetView = useCallback(() => {
    setScale(1);
    setPan({ x: 0, y: 0 });
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) {
      return undefined;
    }
    function handleWheel(event: WheelEvent) {
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? SCALE_STEP : 1 / SCALE_STEP, {
        x: event.clientX,
        y: event.clientY,
      });
    }
    viewport.addEventListener("wheel", handleWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", handleWheel);
  }, [zoomBy]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        zoomBy(SCALE_STEP);
      } else if (event.key === "-") {
        event.preventDefault();
        zoomBy(1 / SCALE_STEP);
      } else if (event.key === "0") {
        event.preventDefault();
        resetView();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [resetView, zoomBy]);

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      baseX: pan.x,
      baseY: pan.y,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
    };
    setDragging(true);
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) {
      return;
    }
    setPan({
      x: drag.baseX + event.clientX - drag.startX,
      y: drag.baseY + event.clientY - drag.startY,
    });
  }

  function stopDragging(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (dragRef.current?.pointerId === event.pointerId) {
      dragRef.current = null;
      setDragging(false);
    }
  }

  function handleTouchStart(event: ReactTouchEvent<HTMLDivElement>) {
    if (event.touches.length !== 2) {
      pinchRef.current = null;
      return;
    }
    pinchRef.current = {
      baseScale: scale,
      distance: touchDistance(event.touches),
    };
  }

  function handleTouchMove(event: ReactTouchEvent<HTMLDivElement>) {
    const pinch = pinchRef.current;
    if (!pinch || event.touches.length !== 2) {
      return;
    }
    event.preventDefault();
    const nextDistance = touchDistance(event.touches);
    setScale(clampScale(pinch.baseScale * (nextDistance / pinch.distance)));
  }

  function clearPinch() {
    pinchRef.current = null;
  }

  return (
    <div className="zoom-viewer">
      <div
        aria-label={controlsLabel ?? t("zoomViewer.controls")}
        className="zoom-viewer-toolbar"
        role="toolbar"
      >
        <button
          aria-label={t("zoomViewer.out")}
          className="zoom-viewer-toolbar-button"
          type="button"
          onClick={() => zoomBy(1 / SCALE_STEP)}
        >
          <Minus size={14} aria-hidden="true" />
        </button>
        <button
          aria-label={t("zoomViewer.current", { percent: zoomPercent })}
          aria-live="polite"
          className="zoom-viewer-readout"
          type="button"
          onClick={resetView}
        >
          {zoomPercent}%
        </button>
        <button
          aria-label={t("zoomViewer.in")}
          className="zoom-viewer-toolbar-button"
          type="button"
          onClick={() => zoomBy(SCALE_STEP)}
        >
          <Plus size={14} aria-hidden="true" />
        </button>
        <button
          aria-label={t("zoomViewer.reset")}
          className="zoom-viewer-toolbar-button"
          type="button"
          onClick={resetView}
        >
          <RotateCcw size={14} aria-hidden="true" />
        </button>
        <button
          aria-label={closeLabel}
          className="zoom-viewer-toolbar-button zoom-viewer-close"
          type="button"
          onClick={onClose}
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>
      <div
        aria-label={contentLabel}
        className={`zoom-viewer-viewport ${dragging ? "is-dragging" : ""}`}
        ref={viewportRef}
        role={contentLabel ? "img" : undefined}
        onPointerCancel={stopDragging}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={stopDragging}
        onTouchCancel={clearPinch}
        onTouchEnd={clearPinch}
        onTouchMove={handleTouchMove}
        onTouchStart={handleTouchStart}
      >
        <div
          className="zoom-viewer-content"
          style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${scale})` }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}

function touchDistance(touches: ReactTouchEvent<HTMLDivElement>["touches"]) {
  const first = touches[0];
  const second = touches[1];
  if (!first || !second) {
    return 1;
  }
  return Math.hypot(first.clientX - second.clientX, first.clientY - second.clientY);
}
