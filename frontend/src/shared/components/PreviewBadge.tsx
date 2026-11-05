import { autoUpdate, computePosition, flip, offset, shift } from "@floating-ui/dom";
import { FlaskConical } from "lucide-react";
import { useId, useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import { createPortal } from "react-dom";

/**
 * The one Preview marker: the Settings > Labs flask in a round chip. The chip
 * is icon-only so it fits beside a navigation label without wrapping it; the
 * word stays in the accessibility tree and the description is the chip's own
 * hover tooltip.
 */
export function PreviewBadge({
  label,
  description,
  decorative = false,
}: {
  label: string;
  description: string;
  /** Set when the owner already names the badge (aria-label). */
  decorative?: boolean;
}) {
  const tooltipId = useId();
  const anchorRef = useRef<HTMLSpanElement | null>(null);
  const tooltipRef = useRef<HTMLSpanElement | null>(null);
  const [open, setOpen] = useState(false);

  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    const tooltip = tooltipRef.current;
    if (!open || !anchor || !tooltip) return;
    // Fixed and portalled: sidebars, tab strips and graph panels clip their
    // overflow, so an in-flow popup would be cut off at their edge.
    return autoUpdate(anchor, tooltip, () => {
      void computePosition(anchor, tooltip, {
        placement: "top",
        strategy: "fixed",
        middleware: [offset(6), flip({ padding: 8 }), shift({ padding: 8 })],
      }).then(({ x, y }) => {
        tooltip.style.left = `${x}px`;
        tooltip.style.top = `${y}px`;
      });
    });
  }, [open]);

  const show = (event: PointerEvent<HTMLSpanElement>) => {
    if (event.pointerType !== "touch") setOpen(true);
  };

  return (
    <span
      aria-describedby={open && !decorative ? tooltipId : undefined}
      aria-hidden={decorative ? "true" : undefined}
      className="nav-preview-badge"
      onPointerEnter={show}
      onPointerLeave={() => setOpen(false)}
      ref={anchorRef}
      // An empty title stops the owner's native tooltip (a nav link or tab
      // title) from appearing on top of this one while the chip is hovered.
      title=""
    >
      <FlaskConical aria-hidden="true" size={11} strokeWidth={2.4} />
      <span className="visually-hidden">{label}</span>
      {open
        ? createPortal(
            <span
              className="preview-badge-tooltip"
              id={tooltipId}
              ref={tooltipRef}
              role="tooltip"
            >
              {description}
            </span>,
            document.body,
          )
        : null}
    </span>
  );
}
