import type { KeyboardEvent, ReactNode } from "react";
import { useId, useRef, useState } from "react";

export type TabbedInspectorTab = {
  badge?: ReactNode;
  content: ReactNode;
  key: string;
  label: string;
};

type TabbedInspectorProps = {
  activeKey?: string;
  ariaLabel: string;
  className?: string;
  defaultActiveKey?: string;
  onActiveKeyChange?: (key: string) => void;
  tabs: TabbedInspectorTab[];
};

export function TabbedInspector({
  activeKey,
  ariaLabel,
  className = "",
  defaultActiveKey,
  onActiveKeyChange,
  tabs,
}: TabbedInspectorProps) {
  const generatedId = useId();
  const [internalActiveKey, setInternalActiveKey] = useState(
    defaultActiveKey ?? tabs[0]?.key ?? "",
  );
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const selectedKey = activeKey ?? internalActiveKey;
  const activeTab = tabs.find((tab) => tab.key === selectedKey) ?? tabs[0] ?? null;
  const rootClassName = ["tabbed-inspector", className].filter(Boolean).join(" ");

  function activateTab(nextKey: string) {
    if (activeKey === undefined) {
      setInternalActiveKey(nextKey);
    }
    onActiveKeyChange?.(nextKey);
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!activeTab || tabs.length === 0) {
      return;
    }
    const currentIndex = tabs.findIndex((tab) => tab.key === activeTab.key);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % tabs.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    const nextTab = tabs[nextIndex];
    activateTab(nextTab.key);
    tabRefs.current[nextTab.key]?.focus();
  }

  if (!activeTab) {
    return null;
  }

  return (
    <section className={rootClassName} aria-label={ariaLabel}>
      <div
        className="tabbed-inspector-tabs"
        role="tablist"
        aria-label={`${ariaLabel} tabs`}
        onKeyDown={handleTabKeyDown}
        tabIndex={-1}
      >
        {tabs.map((tab) => {
          const selected = tab.key === activeTab.key;
          const tabId = `${generatedId}-${tab.key}-tab`;
          const panelId = `${generatedId}-${tab.key}-panel`;
          return (
            <button
              aria-controls={panelId}
              aria-selected={selected}
              className="tabbed-inspector-tab"
              id={tabId}
              key={tab.key}
              onClick={() => activateTab(tab.key)}
              ref={(node) => {
                tabRefs.current[tab.key] = node;
              }}
              role="tab"
              tabIndex={selected ? 0 : -1}
              type="button"
            >
              <span>{tab.label}</span>
              {tab.badge ? <span className="tabbed-inspector-badge">{tab.badge}</span> : null}
            </button>
          );
        })}
      </div>
      <div
        aria-labelledby={`${generatedId}-${activeTab.key}-tab`}
        className="tabbed-inspector-panel"
        id={`${generatedId}-${activeTab.key}-panel`}
        role="tabpanel"
      >
        {activeTab.content}
      </div>
    </section>
  );
}
