import { AlertTriangle, Info, X } from "lucide-react";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

/** A one-line notice over the canvas: expandable when it overflows, dismissible per scene. */
export function WorkspaceGraphNotice({ children, dismissed, noticeKey, onDismiss, tone }: {
  children: ReactNode;
  dismissed: ReadonlySet<string>;
  noticeKey: string;
  onDismiss: (key: string) => void;
  tone: "info" | "warning";
}) {
  const { t } = useTranslation("graph");
  const textRef = useRef<HTMLParagraphElement | null>(null);
  const measuredTextRef = useRef<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const hidden = dismissed.has(noticeKey);
  // One observer per shown notice; a resize re-measures the clamped line.
  useLayoutEffect(() => {
    const text = textRef.current;
    if (!text || expanded || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => setOverflows(text.scrollWidth > text.clientWidth + 1));
    observer.observe(text);
    return () => observer.disconnect();
  }, [expanded, hidden]);
  // New text (another message or language) can overflow without a resize;
  // unchanged text skips the layout read on every parent render.
  useLayoutEffect(() => {
    const text = textRef.current;
    if (!text || expanded || measuredTextRef.current === text.textContent) return;
    measuredTextRef.current = text.textContent;
    setOverflows(text.scrollWidth > text.clientWidth + 1);
  }, [children, expanded]);
  if (hidden) return null;
  const Icon = tone === "warning" ? AlertTriangle : Info;
  return (
    <div aria-atomic="true" className={`workspace-graph-notice is-${tone}`} data-expanded={expanded ? "true" : "false"}
      role={tone === "warning" ? "alert" : "status"}>
      <Icon aria-hidden="true" size={15} />
      <p ref={textRef}>{children}</p>
      {expanded || overflows ? (
        <button aria-expanded={expanded} className="workspace-graph-notice-more"
          onClick={() => setExpanded((value) => !value)} type="button">
          {t(expanded ? "workspaceGraph.ui.noticeLess" : "workspaceGraph.ui.noticeMore")}
        </button>
      ) : null}
      <button aria-label={t("workspaceGraph.ui.noticeDismiss")} className="workspace-graph-notice-dismiss"
        onClick={() => onDismiss(noticeKey)} title={t("workspaceGraph.ui.noticeDismiss")} type="button">
        <X aria-hidden="true" size={15} />
      </button>
    </div>
  );
}
