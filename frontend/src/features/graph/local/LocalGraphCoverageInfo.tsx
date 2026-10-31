import { Info } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

/** Disclosure state only: coverage classification remains owned by GraphView. */
export function LocalGraphCoverageInfo({
  active, descriptions, heading, messages, payload, workspaceId,
}: {
  active: boolean;
  descriptions: readonly string[];
  heading: string;
  messages: readonly string[];
  payload: object;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("graph");
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const panelId = useId();

  useLayoutEffect(() => {
    if (detailsRef.current) detailsRef.current.open = false;
  }, [active, payload, workspaceId]);

  useEffect(() => {
    const details = detailsRef.current;
    const positionPanel = () => {
      if (!details?.open) return;
      const summary = details.querySelector("summary");
      if (!summary) return;
      const bounds = summary.getBoundingClientRect();
      // The reader can be much narrower than the browser. GraphView clips
      // overflow, so its visible intersection, not window size alone, owns
      // both the popup's width and its above/below space.
      const owner = details.closest(".graph-view")?.getBoundingClientRect();
      const left = Math.max(12, (owner?.left ?? 0) + 12);
      const right = Math.min(window.innerWidth - 12, (owner?.right ?? window.innerWidth) - 12);
      const top = Math.max(12, (owner?.top ?? 0) + 12);
      const bottom = Math.min(window.innerHeight - 12, (owner?.bottom ?? window.innerHeight) - 12);
      const width = Math.max(1, Math.min(360, right - left));
      const panelRight = Math.min(right, Math.max(left + width, bounds.right));
      const below = bottom - bounds.bottom - 8;
      const above = bounds.top - top - 8;
      const placeAbove = below < 120 && above > below;
      details.dataset.side = placeAbove ? "above" : "below";
      details.style.setProperty("--graph-coverage-width", `${width}px`);
      details.style.setProperty("--graph-coverage-right", `${bounds.right - panelRight}px`);
      details.style.setProperty("--graph-coverage-available-height", `${Math.max(1, placeAbove ? above : below)}px`);
    };
    const closeOutside = (event: PointerEvent) => {
      if (details?.open && event.target instanceof Node && !details.contains(event.target)) {
        details.open = false;
      }
    };
    const closeEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !details?.open) return;
      // GraphView owns a native bubbling Escape handler. A delegated React
      // handler would run too late to keep the current graph selection.
      event.preventDefault();
      event.stopPropagation();
      details.open = false;
      details.querySelector("summary")?.focus({ preventScroll: true });
    };
    document.addEventListener("pointerdown", closeOutside, true);
    document.addEventListener("scroll", positionPanel, true);
    window.addEventListener("resize", positionPanel);
    details?.addEventListener("keydown", closeEscape);
    details?.addEventListener("toggle", positionPanel);
    return () => {
      document.removeEventListener("pointerdown", closeOutside, true);
      document.removeEventListener("scroll", positionPanel, true);
      window.removeEventListener("resize", positionPanel);
      details?.removeEventListener("keydown", closeEscape);
      details?.removeEventListener("toggle", positionPanel);
    };
  }, []);

  return <>
    <div aria-atomic="true" className="visually-hidden" role="status">
      {heading}{messages.length ? ` ${messages.join(" ")}` : ""}
    </div>
    <details className="graph-local-coverage-info" ref={detailsRef}>
      <summary aria-controls={panelId} tabIndex={0}>
        <Info aria-hidden="true" size={16} />
        {t("graph.omissions.folderChip")}
      </summary>
      {/* Keyboard users must be able to scroll the bounded explanation region. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <div aria-label={t("graph.omissions.heading")} className="graph-local-coverage-panel" id={panelId} role="region" tabIndex={0}>
        <strong>{heading}</strong>
        {messages.length ? <p>{messages.join(" ")}</p> : null}
        <ul>{descriptions.map((description) => <li key={description}>{description}</li>)}</ul>
        <p>{t("graph.omissions.noCounts")}</p>
      </div>
    </details>
  </>;
}
