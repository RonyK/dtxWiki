import { Download, Pause, Play, RotateCcw, Scan, ZoomIn, ZoomOut } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

/** What the layout button can do: pause, resume, or nothing while motion is off or the layout is fixed. */
export type GraphLayoutControlState = "running" | "paused" | "reducedMotion" | "fixed";

const LAYOUT_LABEL = {
  running: "graph.layoutPause",
  paused: "graph.layoutResume",
  reducedMotion: "graph.layoutReducedMotion",
  fixed: "graph.layoutFixed",
} as const satisfies Record<GraphLayoutControlState, string>;

/**
 * The canvas controls of both graphs, in one order and with one set of names:
 * zoom in, zoom out, fit, pause or resume the layout, reset, export. A view
 * places the bar and may add its own controls after them.
 */
export function GraphCanvasControls({
  children, className, disabled = false, exportEnabled, layout,
  onExport, onFit, onReset, onToggleLayout, onZoomIn, onZoomOut,
}: {
  children?: ReactNode;
  className: string;
  /** The renderer is not ready: nothing on the canvas can move yet. */
  disabled?: boolean;
  exportEnabled: boolean;
  layout: GraphLayoutControlState;
  onExport: () => void;
  onFit: () => void;
  onReset: () => void;
  onToggleLayout: () => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
}) {
  const { t } = useTranslation("graph");
  return (
    <div aria-label={t("graph.canvasControls")} className={className} role="toolbar">
      <GraphCanvasButton disabled={disabled} label={t("graph.zoomIn")} onClick={onZoomIn}><ZoomIn aria-hidden="true" size={17} /></GraphCanvasButton>
      <GraphCanvasButton disabled={disabled} label={t("graph.zoomOut")} onClick={onZoomOut}><ZoomOut aria-hidden="true" size={17} /></GraphCanvasButton>
      <GraphCanvasButton disabled={disabled} label={t("graph.fit")} onClick={onFit}><Scan aria-hidden="true" size={17} /></GraphCanvasButton>
      <GraphCanvasButton disabled={disabled || layout === "reducedMotion" || layout === "fixed"} label={t(LAYOUT_LABEL[layout])}
        onClick={onToggleLayout}>
        {layout === "paused" ? <Play aria-hidden="true" size={17} /> : <Pause aria-hidden="true" size={17} />}
      </GraphCanvasButton>
      <GraphCanvasButton disabled={disabled} label={t("graph.resetView")} onClick={onReset}><RotateCcw aria-hidden="true" size={17} /></GraphCanvasButton>
      <GraphCanvasButton disabled={!exportEnabled} label={t(exportEnabled ? "graph.export" : "graph.exportUnavailable")}
        onClick={onExport}><Download aria-hidden="true" size={17} /></GraphCanvasButton>
      {children}
    </div>
  );
}

function GraphCanvasButton({ children, disabled, label, onClick }: {
  children: ReactNode;
  disabled: boolean;
  label: string;
  onClick: () => void;
}) {
  return <button aria-label={label} className="graph-icon-button" disabled={disabled} onClick={onClick} title={label} type="button">{children}</button>;
}
