import type { ReactNode } from "react";

import { Spinner } from "./Spinner";

type LoadingIndicatorProps = {
  children: ReactNode;
  className?: string;
  size?: number;
  as?: "div" | "p" | "span";
  "aria-label"?: string;
};

/** A single announced loading message; callers own the request lifecycle. */
export function LoadingIndicator({
  children,
  className,
  size = 16,
  as: Element = "div",
  "aria-label": ariaLabel,
}: LoadingIndicatorProps) {
  return (
    <Element
      aria-atomic="true"
      aria-label={ariaLabel}
      aria-live="polite"
      className={`loading-indicator${className ? ` ${className}` : ""}`}
      role="status"
    >
      <Spinner decorative size={size} />
      <span className="loading-indicator-label">{children}</span>
    </Element>
  );
}
