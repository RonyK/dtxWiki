import type { KeyboardEvent, ReactNode } from "react";
import { useRef } from "react";

export type ReaderAccent =
  | "context"
  | "agent"
  | "ask"
  | "review"
  | "library";

export type ReaderTabItem<Key extends string> = {
  key: Key;
  label: string;
  badge?: {
    label: string;
    title?: string;
  };
  count?: number;
  countLabel?: string;
  // Renders a native `disabled` button, which browsers drop from the tab ring
  // and skip for pointer events -- and keyboard nav below filters it out too.
  // So `title` below is NOT reachable on a disabled tab: the user sees a dimmed
  // control with no way to learn why. If a view is unavailable, prefer leaving
  // the tab enabled and explaining inside its panel (see the reader screens'
  // `wiki-empty` panels) or omitting the tab outright.
  disabled?: boolean;
  showZero?: boolean;
  // Hover `title` hint for ENABLED tabs. See the note on `disabled` above.
  title?: string;
};

type ReaderTabsProps<Key extends string> = {
  accent?: ReaderAccent;
  active: Key;
  ariaLabel: string;
  idBase: string;
  onChange: (key: Key) => void;
  tabs: ReaderTabItem<Key>[];
  trailing?: ReactNode;
};

export function ReaderTabs<Key extends string>({
  accent,
  active,
  ariaLabel,
  idBase,
  onChange,
  tabs,
  trailing,
}: ReaderTabsProps<Key>) {
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  function activateTab(index: number) {
    const enabledTabs = tabs
      .map((tab, tabIndex) => ({ tab, tabIndex }))
      .filter(({ tab }) => !tab.disabled);
    const target = enabledTabs[index];
    if (!target) {
      return;
    }
    onChange(target.tab.key);
    requestAnimationFrame(() => tabRefs.current[target.tabIndex]?.focus());
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const enabledTabs = tabs
      .map((tab, tabIndex) => ({ tab, tabIndex }))
      .filter(({ tab }) => !tab.disabled);
    if (enabledTabs.length === 0) {
      return;
    }
    const currentEnabledIndex = enabledTabs.findIndex(
      ({ tab }) => tab.key === active,
    );
    if (event.key === "ArrowRight") {
      event.preventDefault();
      activateTab((currentEnabledIndex + 1) % enabledTabs.length);
    }
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      activateTab(
        (currentEnabledIndex - 1 + enabledTabs.length) % enabledTabs.length,
      );
    }
    if (event.key === "Home") {
      event.preventDefault();
      activateTab(0);
    }
    if (event.key === "End") {
      event.preventDefault();
      activateTab(enabledTabs.length - 1);
    }
  }

  return (
    <div className="reader-tabs-row" data-accent={accent}>
      <div
        aria-label={ariaLabel}
        className="reader-tabs"
        onKeyDown={handleKeyDown}
        role="tablist"
        tabIndex={-1}
      >
        {tabs.map((tab, index) => {
          const selected = tab.key === active;
          const showCount =
            tab.count !== undefined && (tab.count > 0 || tab.showZero === true);
          const accessibleLabel = tab.badge
            ? [
                tab.label,
                showCount ? (tab.countLabel ?? String(tab.count)) : null,
                tab.badge.label,
              ]
                .filter((part): part is string => Boolean(part))
                .join(", ")
            : showCount && tab.countLabel
              ? `${tab.label}, ${tab.countLabel}`
              : undefined;
          return (
            <button
              // Consumers conditionally render only the active tabpanel, so
              // referencing an inactive tab's (absent) panel id would dangle.
              // Point aria-controls at the panel only for the selected tab (C15).
              aria-label={accessibleLabel}
              aria-controls={selected ? `${idBase}-panel-${tab.key}` : undefined}
              aria-selected={selected}
              className={`reader-tab ${selected ? "active" : ""}`}
              disabled={tab.disabled}
              id={`${idBase}-tab-${tab.key}`}
              key={tab.key}
              onClick={() => onChange(tab.key)}
              ref={(element) => {
                tabRefs.current[index] = element;
              }}
              role="tab"
              tabIndex={selected ? 0 : -1}
              title={tab.title ?? tab.badge?.title}
              type="button"
            >
              <span>{tab.label}</span>
              {tab.badge ? (
                <span
                  aria-hidden="true"
                  className="nav-preview-badge"
                  title={tab.badge.title}
                >
                  {tab.badge.label}
                </span>
              ) : null}
              {showCount ? (
                <>
                  <span className="reader-tab-count" aria-hidden="true">
                    {tab.count}
                  </span>
                  {tab.countLabel ? (
                    <span className="visually-hidden">{tab.countLabel}</span>
                  ) : null}
                </>
              ) : null}
            </button>
          );
        })}
      </div>
      {trailing ? <div className="reader-tabs-trailing">{trailing}</div> : null}
    </div>
  );
}
