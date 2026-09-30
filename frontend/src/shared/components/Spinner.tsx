import type { CSSProperties } from "react";

type SpinnerProps = {
  /** Visible label announced to assistive tech. Defaults to "Loading". */
  "aria-label"?: string;
  /** Extra class names appended to the spinner element. */
  className?: string;
  /** Render as a decorative glyph inside another labelled status region. */
  decorative?: boolean;
  /** Optional inline size override (px). Defaults to CSS-driven size. */
  size?: number;
};

/**
 * Accessible, animated loading indicator.
 *
 * Renders a role="status" span with a spinning glyph (driven by the
 * `.spinner` CSS keyframes) plus a visually-hidden text label so screen
 * readers announce the loading state even though the glyph itself is
 * decorative.
 */
export function Spinner({
  "aria-label": ariaLabel = "Loading",
  className,
  decorative = false,
  size,
}: SpinnerProps) {
  const style: CSSProperties | undefined =
    typeof size === "number" ? { width: size, height: size } : undefined;
  return (
    <span
      aria-hidden={decorative ? "true" : undefined}
      aria-label={decorative ? undefined : ariaLabel}
      className={`spinner${className ? ` ${className}` : ""}`}
      role={decorative ? undefined : "status"}
    >
      <span aria-hidden="true" className="spinner-glyph" style={style} />
      {decorative ? null : <span className="visually-hidden">{ariaLabel}</span>}
    </span>
  );
}
