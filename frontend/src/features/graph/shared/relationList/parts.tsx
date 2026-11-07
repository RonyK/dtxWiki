import { ArrowUpRight, ChevronDown, ChevronRight, FileText } from "lucide-react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import { flushSync } from "react-dom";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { i18n } from "../../../../shared/i18n";
import { GraphEdgeCue } from "../GraphCues";
import type { RelationListChipView, RelationListEdgeView, RelationListNodeView, RelationListStroke } from "./types";

/** Result lists grow by this many rows; a selected node's relations by the flow page. */
export const RELATION_LIST_PAGE = 10;
export const RELATION_FLOW_PAGE = 5;

export function formatRelationListCount(value: number) {
  return value.toLocaleString(i18n.resolvedLanguage || "en");
}

/** A short stroke in the map's line style. */
/** A short sample of the relation's line: the graph legend's dash, colour and weight. */
export function RelationListStrokeCue({ stroke }: { stroke: RelationListStroke }) {
  return <GraphEdgeCue className="relation-list-stroke-cue" kind={stroke} width={16} />;
}

/** Map symbol + short name. The full kind name stays in the tooltip. */
export function RelationListChip({ chip }: { chip: RelationListChipView }) {
  return <span className="relation-list-chip" data-tone={chip.tone} title={chip.title}>{chip.cue}{chip.label}</span>;
}

/** Document icon + arrow: a link, not a button. The row itself still selects. */
export function RelationListOpenLink({ href, label, onNavigate, tabIndex }: {
  href: string;
  label: string;
  onNavigate?: (event: ReactMouseEvent<HTMLAnchorElement>) => void;
  tabIndex?: number;
}) {
  const { t } = useTranslation("graph");
  const name = t("relationList.openDocument", { label });
  return <Link aria-label={name} className="relation-list-open-link" onClick={onNavigate} tabIndex={tabIndex} title={name} to={href}>
    <FileText aria-hidden="true" size={15} /><ArrowUpRight aria-hidden="true" className="relation-list-open-arrow" size={11} strokeWidth={2.6} />
  </Link>;
}

/** Folded headers keep the count and the composition visible. */
export function RelationListFoldHeader({ controls, count, level = "section", note, onToggle, open, summary, title }: {
  controls?: string;
  count: number;
  level?: "section" | "group";
  note?: string;
  onToggle: () => void;
  open: boolean;
  summary?: ReactNode;
  title: string;
}) {
  const Chevron = open ? ChevronDown : ChevronRight;
  return <button aria-controls={controls} aria-expanded={open} className="relation-list-fold-head"
    data-level={level} onClick={onToggle} type="button">
    <Chevron aria-hidden="true" className="relation-list-fold-chevron" size={15} />
    <span className="relation-list-fold-title">{title}</span>
    <span className="relation-list-fold-count">{formatRelationListCount(count)}</span>
    {note ? <span className="relation-list-fold-note">{note}</span> : null}
    {summary ? <span className="relation-list-fold-summary">{summary}</span> : null}
  </button>;
}

export type RelationListSummaryItem = { cue: ReactNode; count: number; key: string; label: string };

/** Counts per symbol or line style; the label is for screen readers. */
export function RelationListSummary({ items }: { items: readonly RelationListSummaryItem[] }) {
  return <>{items.map((item) => (
    <span className="relation-list-fold-summary-item" key={item.key}>
      {item.cue}<span className="visually-hidden">{item.label}</span>{formatRelationListCount(item.count)}
    </span>
  ))}</>;
}

/** One entry per map line style, in the given order. */
export function relationListStrokeSummary<E>(
  edges: readonly E[],
  strokeOf: (edge: E) => RelationListStroke,
  order: readonly RelationListStroke[],
  label: (stroke: RelationListStroke) => string,
): RelationListSummaryItem[] {
  const counts = new Map<RelationListStroke, number>();
  for (const edge of edges) {
    const stroke = strokeOf(edge);
    counts.set(stroke, (counts.get(stroke) ?? 0) + 1);
  }
  return order.filter((stroke) => counts.has(stroke)).map((stroke) => ({
    count: counts.get(stroke)!, cue: <RelationListStrokeCue stroke={stroke} />, key: stroke, label: label(stroke),
  }));
}

export function RelationListMoreButton({ controls, onClick, remaining, step }: {
  /** The id of the list this button pages. */
  controls: string;
  onClick: () => void;
  remaining: number;
  step: number;
}) {
  const { t } = useTranslation("graph");
  if (remaining <= 0) return null;
  return <button aria-controls={controls} className="relation-list-more" onClick={(event) => {
    const button = event.currentTarget;
    const shown = document.getElementById(controls)?.children.length ?? 0;
    // The last page removes this button. Commit now and hand focus to the
    // first row it revealed, so focus never falls to the document body.
    flushSync(onClick);
    if (button.isConnected) return;
    document.getElementById(controls)?.children[shown]?.querySelector<HTMLElement>("button, a[href]")?.focus();
  }} type="button">
    {t("relationList.more", { count: Math.min(step, remaining), remaining: formatRelationListCount(remaining) })}
  </button>;
}

/** A relation drawn like the map: source on top, the map's stroke and arrow, target below. */
export function RelationListMiniMap({ edge, source, target }: {
  edge: RelationListEdgeView;
  source: RelationListNodeView | undefined;
  target: RelationListNodeView | undefined;
}) {
  return <span className="relation-list-map">
    <span className="relation-list-map-cue">{source?.cue ?? null}</span>
    <strong className="relation-list-map-end">{source?.label ?? "—"}{source?.badge ?? null}</strong>
    <span aria-hidden="true" className="relation-list-map-stroke" data-directed={edge.directed} data-visual={edge.stroke} />
    <span className="relation-list-map-kind"><RelationListChip chip={edge.chip} /></span>
    <span className="relation-list-map-cue">{target?.cue ?? null}</span>
    <strong className="relation-list-map-end">{target?.label ?? "—"}{target?.badge ?? null}</strong>
  </span>;
}
