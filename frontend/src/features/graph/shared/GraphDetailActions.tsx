import { FileText, LocateFixed, Network, Pin, PinOff } from "lucide-react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Spinner } from "../../../shared/components/Spinner";

/** A pin requested before its node is laid out waits, then fixes; both stay cancellable. */
export type GraphPinState = "unpinned" | "waiting" | "pinning" | "pinned";

/**
 * The selected node's action row in both graphs: open, center, pin and the
 * other graph's view, each an icon with its name, in that order. A view adds
 * its own node actions (expand) after them.
 */
export function GraphDetailActions({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`graph-detail-actions ${className}`.trim()}>{children}</div>;
}

export function GraphDetailOpenLink({ href, onNavigate }: {
  href: string;
  onNavigate?: (event: ReactMouseEvent<HTMLAnchorElement>) => void;
}) {
  const { t } = useTranslation("graph");
  return <Link className="graph-detail-open" onClick={onNavigate} to={href}>
    <FileText aria-hidden="true" size={16} />{t("graph.open")}
  </Link>;
}

export function GraphDetailCenterButton({ onCenter }: { onCenter: () => void }) {
  const { t } = useTranslation("graph");
  return <button className="graph-detail-center" onClick={onCenter} type="button">
    <LocateFixed aria-hidden="true" size={16} />{t("graph.details.center")}
  </button>;
}

export function GraphDetailPinButton({ onToggle, state }: { onToggle: () => void; state: GraphPinState }) {
  const { t } = useTranslation("graph");
  const pending = state === "waiting" || state === "pinning";
  const label = state === "unpinned" ? t("graph.details.pin") : state === "pinned" ? t("graph.details.unpin")
    : state === "pinning" ? t("graph.details.pinning") : t("graph.details.pinWaiting");
  const cancel = state === "pinning" ? t("graph.details.pinningCancel") : t("graph.details.pinWaitingCancel");
  const Icon = state === "unpinned" ? Pin : PinOff;
  return (
    <button aria-label={pending ? cancel : undefined} aria-pressed={state === "unpinned" ? false : true}
      className={pending ? "graph-detail-pin graph-details-pin-pending" : "graph-detail-pin"} onClick={onToggle} type="button">
      <Icon aria-hidden="true" size={16} />{label}
      {state === "pinning" ? <Spinner decorative size={12} /> : null}
    </button>
  );
}

/** The same node in the other graph: the Knowledge Map from Context, the Context graph from the Map. */
export function GraphDetailOtherGraphLink({ href, label, onNavigate }: {
  href: string;
  label: string;
  onNavigate?: (event: ReactMouseEvent<HTMLAnchorElement>) => void;
}) {
  return <Link className="graph-detail-other-graph" onClick={onNavigate} to={href}>
    <Network aria-hidden="true" size={16} />{label}
  </Link>;
}
