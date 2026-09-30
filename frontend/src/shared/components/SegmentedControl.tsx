import type { KeyboardEvent, ReactNode } from "react";

export type SegmentedControlOption<T extends string> = {
  key: T;
  label: ReactNode;
  disabled?: boolean;
  title?: string;
};

type SegmentedControlProps<T extends string> = {
  className?: string;
  compact?: boolean;
  label: string;
  options: Array<SegmentedControlOption<T>>;
  value: T;
  onChange: (value: T) => void;
};

export function SegmentedControl<T extends string>({
  className = "",
  compact = false,
  label,
  options,
  value,
  onChange,
}: SegmentedControlProps<T>) {
  const classes = ["segmented-control", compact ? "compact" : "", className]
    .filter(Boolean)
    .join(" ");
  const enabledOptions = options.filter((option) => !option.disabled);
  const selectedEnabled = enabledOptions.some((option) => option.key === value);
  const focusKey = selectedEnabled ? value : enabledOptions[0]?.key;

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const keys = [
      "ArrowRight",
      "ArrowDown",
      "ArrowLeft",
      "ArrowUp",
      "Home",
      "End",
    ];
    if (!keys.includes(event.key) || enabledOptions.length === 0) {
      return;
    }
    const group = event.currentTarget;
    event.preventDefault();
    const activeKey = (event.target as HTMLButtonElement | null)?.dataset.value as
      | T
      | undefined;
    const activeIndex = Math.max(
      0,
      enabledOptions.findIndex((option) => option.key === (activeKey ?? focusKey)),
    );
    let nextIndex = activeIndex;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (activeIndex + 1) % enabledOptions.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (activeIndex - 1 + enabledOptions.length) % enabledOptions.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = enabledOptions.length - 1;
    }
    const next = enabledOptions[nextIndex];
    onChange(next.key);
    const scheduleFocus =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame
        : (callback: FrameRequestCallback) =>
            window.setTimeout(() => callback(performance.now()), 0);
    scheduleFocus(() => {
      group.querySelector<HTMLButtonElement>(`[data-value="${next.key}"]`)
        ?.focus();
    });
  }

  return (
    <div
      className={classes}
      aria-label={label}
      role="radiogroup"
      onKeyDown={handleKeyDown}
      tabIndex={-1}
    >
      {options.map((option) => (
        <button
          aria-checked={option.key === value}
          className={option.key === value ? "active" : ""}
          data-value={option.key}
          disabled={option.disabled}
          key={option.key}
          role="radio"
          tabIndex={option.key === focusKey ? 0 : -1}
          title={option.title}
          type="button"
          onClick={() => onChange(option.key)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
