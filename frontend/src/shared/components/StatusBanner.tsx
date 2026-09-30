import { AlertTriangle, Info } from "lucide-react";
import type { ReactNode } from "react";

type StatusBannerProps = {
  tone: "info" | "warning";
  children: ReactNode;
  action?: ReactNode;
};

export function StatusBanner({ tone, children, action }: StatusBannerProps) {
  const Icon = tone === "warning" ? AlertTriangle : Info;
  const role = tone === "warning" ? "alert" : "status";
  // aria-atomic ensures that when the status text changes, screen readers
  // announce the full banner content rather than only the diff. WCAG 4.1.3.
  return (
    <div
      className={`status-banner status-banner-${tone}`}
      role={role}
      aria-atomic="true"
    >
      <Icon size={16} aria-hidden="true" />
      <div>{children}</div>
      {action ? <div className="status-banner-action">{action}</div> : null}
    </div>
  );
}
