import { Fragment, type ReactNode } from "react";

import { ListPanelHeader } from "./ListPanelHeader";

/** Minimal shape a recency row needs; surfaces extend it with their own fields. */
export type RecentItem = {
  id: string;
  title: string;
  subtitle?: string | null;
};

export type RecentItemsListProps<T extends RecentItem> = {
  /** Surface identity (drives nothing visual here; lets callers key state). */
  kind: "context" | "library";
  items: T[];
  /** Server total for the count badge; null/undefined falls back to items.length. */
  total: number | null;
  hasMore: boolean;
  /** True while a Load more request is in flight. */
  loading: boolean;
  onLoadMore: () => void | Promise<void>;
  onSelect: (id: string) => void;
  /** Id of the currently-selected row, highlighted via aria-pressed/.active. */
  activeId?: string | null;
  /** Section heading + the empty-state copy (presentational; no i18n inside). */
  headingLabel: ReactNode;
  emptyHeading: ReactNode;
  loadMoreLabel: ReactNode;
  loadingMoreLabel: ReactNode;
  /** Noun after the count in the header, e.g. "items" (already localized). */
  noun: string;
  /** Optional control anchored to the header's right edge (e.g. a list filter). */
  headerAction?: ReactNode;
  /** Leading glyph for a row (e.g. a FileText icon). */
  renderIcon?: (item: T) => ReactNode;
  /** Trailing meta cells (e.g. a type label + StatusPill). */
  renderMeta?: (item: T) => ReactNode;
  /** Full row override; when set it replaces the default icon/title/meta layout. */
  renderRow?: (item: T, isActive: boolean) => ReactNode;
};

/**
 * Shared recency list rendering BOTH the Context and Library overviews from one
 * source: a recency-ordered list, an honest count badge (via the shared
 * ListPanelHeader), and a Load more control. The badge reuses CountChip's
 * precedence (resolveDisplayCount): an authoritative `total` shows exactly, and
 * the "N+" lower bound appears only when no total is known but more rows remain.
 * Presentational only — the caller owns data fetching (see useLoadMore).
 */
export function RecentItemsList<T extends RecentItem>({
  items,
  total,
  hasMore,
  loading,
  onLoadMore,
  onSelect,
  activeId,
  headingLabel,
  emptyHeading,
  loadMoreLabel,
  loadingMoreLabel,
  noun,
  headerAction,
  renderIcon,
  renderMeta,
  renderRow,
}: RecentItemsListProps<T>) {
  return (
    <section className="feature-section recent-items-list">
      <ListPanelHeader
        title={headingLabel}
        count={items.length}
        unit={noun}
        total={total}
        approxMore={total == null && hasMore}
        action={headerAction}
      />
      {items.length === 0 ? (
        <p className="recent-items-empty">{emptyHeading}</p>
      ) : (
        <div className="entity-list">
          {items.map((item) => {
            const isActive = item.id === activeId;
            return renderRow ? (
              <Fragment key={item.id}>{renderRow(item, isActive)}</Fragment>
            ) : (
              <button
                aria-pressed={isActive}
                className={`entity-row selectable-row ${isActive ? "active" : ""}`}
                key={item.id}
                type="button"
                onClick={() => onSelect(item.id)}
              >
                {renderIcon?.(item)}
                <div>
                  <strong>{item.title}</strong>
                  {item.subtitle ? <p>{item.subtitle}</p> : null}
                </div>
                {renderMeta?.(item)}
              </button>
            );
          })}
        </div>
      )}
      {hasMore ? (
        <div className="load-more-row">
          <button
            className="icon-button text-button"
            disabled={loading}
            type="button"
            onClick={() => void onLoadMore()}
          >
            {loading ? loadingMoreLabel : loadMoreLabel}
          </button>
        </div>
      ) : null}
    </section>
  );
}
