import { Info, Link2 } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { formatVersionTimestamp } from "../lib/datetime";

export type ContentProperty = {
  label: string;
  value: ReactNode;
};

export type ContentBacklinkItem = {
  id: string;
  slug?: string;
  href?: string;
  title: string;
  occurrenceCount?: number;
  updatedAt?: string;
  meta?: ReactNode;
  snippet?: string | null;
};

export function ContentSummaryPanel({
  badge,
  badgeTone,
  description,
  heading,
  title,
  warnings = [],
}: {
  badge: ReactNode;
  badgeTone: "ok" | "warning";
  description: ReactNode;
  heading: string;
  title: string;
  warnings?: string[];
}) {
  return (
    <div className="inspector-section">
      <header>
        <span>{heading}</span>
      </header>
      <div className="warning-anchor">
        <span
          className={`warning-badge ${
            badgeTone === "warning" ? "warning-amber" : "warning-ok"
          }`}
        >
          {badge}
        </span>
        <div>
          <strong>{title}</strong>
          <p>{description}</p>
        </div>
      </div>
      {warnings.length > 0 ? (
        <ul className="inspector-warning-list">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function ContentPropertiesPanel({
  ariaLabel,
  children,
  heading,
  properties,
}: {
  ariaLabel: string;
  children?: ReactNode;
  heading: string;
  properties: ContentProperty[];
}) {
  return (
    <aside className="page-properties" aria-label={ariaLabel}>
      <div className="page-properties-heading">
        <Info size={16} aria-hidden="true" />
        <h2>{heading}</h2>
      </div>
      <dl className="page-properties-list">
        {properties.map((property) => (
          <div key={property.label}>
            <dt>{property.label}</dt>
            <dd>{property.value}</dd>
          </div>
        ))}
      </dl>
      {children}
    </aside>
  );
}

export function ContentBacklinksPanel({
  ariaLabel,
  backlinks = [],
  count,
  emptyLabel,
  heading,
  linkLabel,
  metrics = [],
}: {
  ariaLabel: string;
  backlinks?: ContentBacklinkItem[];
  count?: number;
  emptyLabel: string;
  heading: string;
  linkLabel?: (count: number) => string;
  metrics?: ContentProperty[];
}) {
  return (
    <aside className="page-backlinks" aria-label={ariaLabel}>
      <div className="page-backlinks-heading">
        <Link2 size={16} aria-hidden="true" />
        <h2>{heading}</h2>
        {count !== undefined ? <span>{count}</span> : null}
      </div>
      {metrics.length > 0 ? (
        <dl className="page-properties-list">
          {metrics.map((metric) => (
            <div key={metric.label}>
              <dt>{metric.label}</dt>
              <dd>{metric.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {backlinks.length === 0 ? (
        <p className="page-backlinks-empty">{emptyLabel}</p>
      ) : (
        <div className="page-backlink-list">
          {backlinks.map((backlink) => {
            const href = backlink.href ?? null;
            const content = (
              <>
                <span className="page-backlink-title">{backlink.title}</span>
                {backlink.meta ? (
                  <span className="page-backlink-meta">{backlink.meta}</span>
                ) : backlink.occurrenceCount !== undefined && backlink.updatedAt ? (
                  <span className="page-backlink-meta">
                    {linkLabel
                      ? linkLabel(backlink.occurrenceCount)
                      : `${backlink.occurrenceCount} link${
                          backlink.occurrenceCount === 1 ? "" : "s"
                        }`}{" "}
                    - {formatVersionTimestamp(backlink.updatedAt)}
                  </span>
                ) : null}
                {backlink.snippet ? (
                  <span className="page-backlink-snippet">{backlink.snippet}</span>
                ) : null}
              </>
            );
            return href ? (
              <Link className="page-backlink-item" key={backlink.id} to={href}>
                {content}
              </Link>
            ) : (
              <div className="page-backlink-item" key={backlink.id}>
                {content}
              </div>
            );
          })}
        </div>
      )}
    </aside>
  );
}
