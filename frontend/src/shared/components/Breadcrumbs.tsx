import { ChevronRight } from "lucide-react";
import type { MouseEvent } from "react";
import { Link } from "react-router-dom";

export type BreadcrumbItem = {
  label: string;
  href?: string;
  /**
   * Optional click handler. When provided (without an href) the crumb renders
   * as an in-page action — used for state-driven navigation such as returning
   * from a detail view to its overview without a route change.
   */
  onClick?: (event: MouseEvent) => void;
};

type BreadcrumbsProps = {
  items: BreadcrumbItem[];
  ariaLabel?: string;
};

export function Breadcrumbs({ ariaLabel = "Breadcrumb", items }: BreadcrumbsProps) {
  // A single crumb is always the last one, and the last crumb never renders as
  // a link — so even with an href it is dead text repeating the heading below.
  if (items.length <= 1 && !items[0]?.onClick) {
    return null;
  }
  return (
    <nav className="breadcrumbs" aria-label={ariaLabel}>
      {items.map((item, index) => {
        const isLast = index === items.length - 1;
        const interactive = (item.href || item.onClick) && !isLast;
        return (
          <span className="breadcrumb-item" key={`${item.label}-${index}`}>
            {index > 0 ? <ChevronRight size={14} aria-hidden="true" /> : null}
            {interactive ? (
              item.onClick ? (
                <a
                  className="breadcrumb-label"
                  href={item.href ?? "#"}
                  onClick={(event) => {
                    if (!item.href) {
                      event.preventDefault();
                    }
                    item.onClick?.(event);
                  }}
                  title={item.label}
                >
                  {item.label}
                </a>
              ) : (
                <Link className="breadcrumb-label" title={item.label} to={item.href as string}>
                  {item.label}
                </Link>
              )
            ) : (
              <span
                aria-current={isLast ? "page" : undefined}
                className="breadcrumb-label"
                title={item.label}
              >
                {item.label}
              </span>
            )}
          </span>
        );
      })}
    </nav>
  );
}
