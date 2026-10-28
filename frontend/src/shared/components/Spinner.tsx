import type { CSSProperties } from "react";

type SpinnerProps = {
  /** Accessible label for an icon-only status. Defaults to "Loading". */
  "aria-label"?: string;
  /** Extra class names appended to the spinner element. */
  className?: string;
  /** Render as a decorative glyph inside another labelled status region. */
  decorative?: boolean;
  /** Optional inline size override (px). Defaults to CSS-driven size. */
  size?: number;
};

/**
 * Shared glyph. Use decorative inside an already-labelled loading status.
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
      aria-live={decorative ? undefined : "polite"}
      className={`spinner${className ? ` ${className}` : ""}`}
      role={decorative ? undefined : "status"}
    >
      <span aria-hidden="true" className="spinner-glyph" style={style} />
      {decorative ? null : <span className="visually-hidden">{ariaLabel}</span>}
    </span>
  );
}
