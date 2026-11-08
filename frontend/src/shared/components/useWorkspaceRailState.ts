import { useCallback, useEffect, useState } from "react";

export const DEFAULT_RAIL_WIDTHS = [280, 420, 560] as const;
export const COLLAPSED_RAIL_WIDTH = 44;

/** The reader and editor share the same rail preferences and width controls. */
export function useWorkspaceRailState(
  layoutKey: string,
  defaultOpen = true,
  widths: readonly number[] = DEFAULT_RAIL_WIDTHS,
) {
  const [open, setOpen] = useState(() =>
    readWorkspacePanelState(`${layoutKey}:left`, defaultOpen),
  );
  const [widthStep, setWidthStep] = useState(() => {
    if (typeof window === "undefined") return 0;
    const value = Number(window.localStorage.getItem(storageKey(`${layoutKey}:leftWidthStep`)));
    return Number.isInteger(value) && value >= 0 && value < widths.length ? value : 0;
  });
  const normalizedStep = Math.min(widthStep, widths.length - 1);

  useEffect(() => {
    writeWorkspacePanelState(`${layoutKey}:left`, open);
  }, [layoutKey, open]);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(storageKey(`${layoutKey}:leftWidthStep`), String(normalizedStep));
    }
  }, [layoutKey, normalizedStep]);

  const expandWidth = useCallback(() => {
    setWidthStep((current) => (current + 1) % widths.length);
  }, [widths.length]);

  return {
    open,
    setOpen,
    width: open ? widths[normalizedStep] : COLLAPSED_RAIL_WIDTH,
    expandWidth,
    expandWidthAtMax: normalizedStep === widths.length - 1,
  };
}

export function readWorkspacePanelState(key: string, fallback: boolean) {
  if (typeof window === "undefined") return fallback;
  const value = window.localStorage.getItem(storageKey(key));
  return value === null ? fallback : value === "true";
}

export function writeWorkspacePanelState(key: string, value: boolean) {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(storageKey(key), String(value));
  }
}

function storageKey(key: string) {
  // Preserve the browse layouts' versioned default-open preference.
  const versioned = key === "context:left" || key === "library:left" || key === "agent:left";
  return `dtxwiki:workspace-layout:${versioned ? "v2:" : ""}${key}`;
}
