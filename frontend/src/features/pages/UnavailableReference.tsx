import { CircleHelp, CircleSlash, Copy } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { copyTextWithReason, type CopyTextDetailedResult } from "../../shared/lib/copyText";

type UnavailableReferenceProps = {
  children: ReactNode;
  reference?: string | null;
  wikilink?: boolean;
  infoOnly?: boolean;
  infoMode?: "ask" | "public";
};

/** An unavailable authored reference with an accessible, non-modal explanation. */
export function UnavailableReference({
  children,
  reference,
  wikilink = false,
  infoOnly = false,
  infoMode = "public",
}: UnavailableReferenceProps) {
  const { t } = useTranslation("pages");
  const id = useId();
  const rootRef = useRef<HTMLSpanElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLSpanElement | null>(null);
  const referenceRef = useRef<HTMLElement | null>(null);
  const pointerInsideRef = useRef(false);
  const suppressFocusOpenRef = useRef(false);
  const hoverCloseTimerRef = useRef<number | null>(null);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [copyResult, setCopyResult] = useState<CopyTextDetailedResult | null>(null);
  const [panelPosition, setPanelPosition] = useState<CSSProperties | null>(null);
  const explanation = t("unavailableReference.explanation");
  const infoExplanation = infoOnly
    ? t(`unavailableReference.${infoMode}Explanation`)
    : explanation;
  const panelId = `unavailable-reference-${id}`;
  const descriptionId = `unavailable-reference-description-${id}`;
  const headingId = `unavailable-reference-heading-${id}`;

  useEffect(() => () => {
    if (hoverCloseTimerRef.current !== null) window.clearTimeout(hoverCloseTimerRef.current);
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setPanelPosition(null);
      return undefined;
    }
    const positionPanel = () => {
      const trigger = triggerRef.current;
      const panel = panelRef.current;
      if (!trigger || !panel) return;
      const triggerRect = trigger.getBoundingClientRect();
      const panelRect = panel.getBoundingClientRect();
      const margin = 12;
      const left = Math.max(margin, Math.min(
        triggerRect.left,
        window.innerWidth - panelRect.width - margin,
      ));
      const below = triggerRect.bottom + 8;
      const top = below + panelRect.height <= window.innerHeight - margin
        ? below
        : Math.max(margin, triggerRect.top - panelRect.height - 8);
      setPanelPosition({ left, top });
    };
    positionPanel();
    window.addEventListener("resize", positionPanel);
    window.addEventListener("scroll", positionPanel, true);
    window.visualViewport?.addEventListener("resize", positionPanel);
    window.visualViewport?.addEventListener("scroll", positionPanel);
    return () => {
      window.removeEventListener("resize", positionPanel);
      window.removeEventListener("scroll", positionPanel, true);
      window.visualViewport?.removeEventListener("resize", positionPanel);
      window.visualViewport?.removeEventListener("scroll", positionPanel);
    };
  }, [open, copyResult]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const activeElement = document.activeElement;
      if (!rootRef.current?.contains(activeElement)) {
        // A hover-only explanation must not hijack Escape or focus from an
        // unrelated control elsewhere on the page.
        setOpen(false);
        setPinned(false);
        return;
      }
      event.preventDefault();
      suppressFocusOpenRef.current = true;
      setOpen(false);
      setPinned(false);
      triggerRef.current?.focus();
    };
    const closeOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && rootRef.current?.contains(event.target)) return;
      setOpen(false);
      setPinned(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    document.addEventListener("pointerdown", closeOutside, true);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
      document.removeEventListener("pointerdown", closeOutside, true);
    };
  }, [open]);

  async function copyReference() {
    if (!reference) return;
    setCopyResult(await copyTextWithReason(reference, referenceRef.current));
  }

  function closeAndRestoreFocus() {
    suppressFocusOpenRef.current = true;
    setOpen(false);
    setPinned(false);
    triggerRef.current?.focus();
  }

  const copyStatus = copyResult?.status === "copied"
    ? t("unavailableReference.copied")
    : copyResult?.status === "selected"
      ? t("unavailableReference.selected")
      : copyResult
        ? t("unavailableReference.copyUnavailable")
        : null;

  return (
    <span
      className={`unavailable-reference${wikilink ? " unavailable-reference--wikilink" : ""}`}
      ref={rootRef}
      onPointerEnter={(event) => {
        if (hoverCloseTimerRef.current !== null) {
          window.clearTimeout(hoverCloseTimerRef.current);
          hoverCloseTimerRef.current = null;
        }
        pointerInsideRef.current = event.pointerType !== "touch";
        if (!pinned && event.pointerType !== "touch") {
          suppressFocusOpenRef.current = false;
          setOpen(true);
        }
      }}
      onPointerLeave={() => {
        pointerInsideRef.current = false;
        if (!pinned && !rootRef.current?.contains(document.activeElement)) {
          hoverCloseTimerRef.current = window.setTimeout(() => {
            hoverCloseTimerRef.current = null;
            if (!pinned && !rootRef.current?.contains(document.activeElement)) setOpen(false);
          }, 180);
        }
      }}
      onFocusCapture={() => {
        if (!suppressFocusOpenRef.current) setOpen(true);
      }}
      onBlurCapture={(event) => {
        if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
        suppressFocusOpenRef.current = false;
        if (!pointerInsideRef.current) {
          setOpen(false);
          setPinned(false);
        }
      }}
    >
      <span className="visually-hidden" id={descriptionId}>{infoExplanation}</span>
      <button
        aria-controls={panelId}
        aria-describedby={descriptionId}
        aria-expanded={open}
        aria-haspopup="dialog"
        className="unavailable-reference__trigger"
        onClick={() => {
          suppressFocusOpenRef.current = false;
          if (pinned) {
            setOpen(false);
            setPinned(false);
          } else {
            setOpen(true);
            setPinned(true);
          }
        }}
        ref={triggerRef}
        type="button"
      >
        <CircleSlash aria-hidden="true" className="unavailable-reference__icon" size={14} />
        <span className="unavailable-reference__label">{children}</span>
        <CircleHelp aria-hidden="true" className="unavailable-reference__help-icon" size={13} />
      </button>
      <span
        aria-labelledby={headingId}
        className="unavailable-reference__popover"
        hidden={!open}
        id={panelId}
        ref={panelRef}
        role="dialog"
        aria-modal="false"
        style={panelPosition ?? { visibility: "hidden" }}
      >
        <span className="unavailable-reference__popover-heading" id={headingId}>
          {infoExplanation}
        </span>
        {!infoOnly ? (
          <span className="unavailable-reference__guidance">
            {t("unavailableReference.guidance")}
          </span>
        ) : null}
        {reference && !infoOnly ? (
          <>
            <code className="unavailable-reference__source" ref={referenceRef}>
              {reference}
            </code>
            <button className="unavailable-reference__action" onClick={() => void copyReference()} type="button">
              <Copy aria-hidden="true" size={14} />
              <span>{t("unavailableReference.copy")}</span>
            </button>
          </>
        ) : null}
        {copyStatus ? <span className="unavailable-reference__status" role="status">{copyStatus}</span> : null}
        <button
          aria-label={t("unavailableReference.close")}
          className="unavailable-reference__close"
          onClick={closeAndRestoreFocus}
          type="button"
        >
          {t("unavailableReference.close")}
        </button>
      </span>
    </span>
  );
}
