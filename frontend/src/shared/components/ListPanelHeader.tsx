import type { ReactNode, Ref } from "react";

import { CountChip } from "./CountChip";

export type ListPanelHeaderProps = {
  /** Section/list heading (already localized). */
  title: ReactNode;
  /** Rows loaded so far. */
  count: number;
  /** Localized unit noun, e.g. "items" / "turns". */
  unit?: string;
  /** Pre-composed count text, for copy that fuses number and unit ("3개"). */
  countText?: string;
  /** Authoritative backend total when known; overrides `count` in the chip. */
  total?: number | null;
  /** Render the count as an "N+" lower bound when more rows remain. */
  approxMore?: boolean;
  /**
   * Optional control anchored to the header's right edge (e.g. a list filter).
   * Not a page-size control — that was removed workspace-wide.
   */
  action?: ReactNode;
  /**
   * Makes the header itself a programmatic focus target, for lists that move
   * focus back to their heading after removing a row. Additive and optional:
   * without it the rendered markup is byte-identical to before.
   */
  focusRef?: Ref<HTMLDivElement>;
};

/**
 * The single count-header used by every list surface (Library, Context, Agent,
 * Ask, Activity). One markup, one alignment rule: the count pill sits directly
 * to the right of the title, and an optional `action` sits at the far right.
 */
export function ListPanelHeader({
  title,
  count,
  unit,
  countText,
  total = null,
  approxMore = false,
  action,
  focusRef,
}: ListPanelHeaderProps) {
  return (
    <div
      className="panel-header"
      ref={focusRef}
      tabIndex={focusRef ? -1 : undefined}
    >
      <div className="panel-title-with-count">
        <span className="panel-title">{title}</span>
        <CountChip
          value={count}
          label={unit}
          formatted={countText}
          total={total}
          approxMore={approxMore}
          compact
        />
      </div>
      {action ?? null}
    </div>
  );
}
