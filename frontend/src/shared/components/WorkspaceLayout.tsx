import type { CSSProperties, ReactNode } from "react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { BottomSheet } from "./BottomSheet";
import { FAB } from "./FAB";
import { MobileDrawer } from "./MobileDrawer";
import { PanelVisibilityIcon } from "./PanelVisibilityIcon";
import { useMobileModalFocus } from "./useMobileModalFocus";

const DEFAULT_LEFT_WIDTHS = [280, 420, 560] as const;
const ASK_LEFT_WIDTHS = [300, 420, 560] as const;
// Review rail starts one step wider than other rails (no narrow 280px step) so
// the lifecycle tabs + audience facet + queue rows have room by default.
const REVIEW_LEFT_WIDTHS = [420, 560] as const;
const COLLAPSED_PANEL_WIDTH = 44;
const INSPECTOR_WIDTH = 300;
const SWIPE_DISMISS_THRESHOLD = 64;

type MobileAction = {
  ariaLabel: string;
  busy?: boolean;
  disabled?: boolean;
  icon?: ReactNode;
  label: string;
  onClick: () => Promise<void> | void;
  title?: string;
};

type WorkspaceLayoutProps = {
  defaultInspectorOpen?: boolean;
  defaultLeftOpen?: boolean;
  inspector?: ReactNode;
  inspectorAutoOpenKey?: string | null;
  inspectorHideLabel?: string;
  inspectorLabel?: string;
  inspectorRestoreLabel?: string;
  layoutKey: string;
  left: (controls: {
    expandWidthLabel: string;
    expandWidthAtMax: boolean;
    onClose: () => void;
    onExpandWidth?: () => void;
  }) => ReactNode;
  leftLabel: string;
  leftRestoreLabel?: string;
  main: ReactNode;
  mobileAction?: MobileAction;
  variant?: "default" | "ask" | "context" | "review";
};

export function WorkspaceLayout({
  defaultInspectorOpen = true,
  defaultLeftOpen = true,
  inspector,
  inspectorAutoOpenKey,
  inspectorHideLabel,
  inspectorLabel,
  inspectorRestoreLabel,
  layoutKey,
  left,
  leftLabel,
  leftRestoreLabel,
  main,
  mobileAction,
  variant = "default",
}: WorkspaceLayoutProps) {
  const { t } = useTranslation("shell");
  const leftPanelId = useId();
  const inspectorPanelId = useId();
  const mobileLayout = useMediaQuery("(max-width: 1024px)");
  const leftPanelRef = useRef<HTMLDivElement | null>(null);
  const inspectorPanelRef = useRef<HTMLElement | null>(null);
  const mobileDrawerToggleRef = useRef<HTMLButtonElement | null>(null);
  const mobileInspectorToggleRef = useRef<HTMLButtonElement | null>(null);
  const drawerSwipeStartXRef = useRef<number | null>(null);
  const drawerSwipeLatestXRef = useRef<number | null>(null);
  const sheetSwipeStartYRef = useRef<number | null>(null);
  const sheetSwipeLatestYRef = useRef<number | null>(null);
  const [leftOpen, setLeftOpen] = useState(() =>
    readStoredPanelState(`${layoutKey}:left`, defaultLeftOpen),
  );
  const [mobileLeftOpen, setMobileLeftOpen] = useState(false);
  const [mobileInspectorOpen, setMobileInspectorOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(() =>
    readStoredPanelState(`${layoutKey}:inspector`, defaultInspectorOpen),
  );
  const hasInspector = inspector !== undefined && inspector !== null;
  const resolvedInspectorLabel =
    inspectorLabel ?? t("panel.detailsFallback", { defaultValue: "Details" });
  const leftWidths =
    variant === "ask"
      ? ASK_LEFT_WIDTHS
      : variant === "review"
        ? REVIEW_LEFT_WIDTHS
        : DEFAULT_LEFT_WIDTHS;
  const [leftWidthStep, setLeftWidthStep] = useState(() =>
    readStoredNumber(`${layoutKey}:leftWidthStep`, 0, leftWidths.length - 1),
  );
  const normalizedLeftWidthStep = Math.min(leftWidthStep, leftWidths.length - 1);
  const leftWidth = leftWidths[normalizedLeftWidthStep];
  const effectiveLeftWidth = leftOpen ? leftWidth : COLLAPSED_PANEL_WIDTH;
  const effectiveRightWidth = hasInspector
    ? inspectorOpen
      ? INSPECTOR_WIDTH
      : COLLAPSED_PANEL_WIDTH
    : 0;
  const expandWidthAtMax = normalizedLeftWidthStep === leftWidths.length - 1;
  const expandWidthLabel = expandWidthAtMax
    ? t("panel.resetWidth", {
        defaultValue: `Reset ${leftLabel} width`,
        label: leftLabel,
      })
    : t("panel.widen", { defaultValue: `Widen ${leftLabel}`, label: leftLabel });
  const openSheetLabel = t("panel.openSheet", {
    defaultValue: `Open ${resolvedInspectorLabel} sheet`,
    label: resolvedInspectorLabel,
  });
  const hideInspectorLabel =
    inspectorHideLabel
    ?? t("panel.hide", {
      defaultValue: `Hide ${resolvedInspectorLabel}`,
      label: resolvedInspectorLabel,
    });
  const showInspectorLabel =
    inspectorRestoreLabel
    ?? t("panel.show", {
      defaultValue: `Show ${resolvedInspectorLabel}`,
      label: resolvedInspectorLabel,
    });
  const mobileControlsVisible =
    mobileLayout && !mobileLeftOpen && !mobileInspectorOpen;
  const closeMobileLeft = useCallback(() => setMobileLeftOpen(false), []);
  const closeMobileInspector = useCallback(() => setMobileInspectorOpen(false), []);

  function startDrawerSwipe(clientX: number) {
    if (!mobileLayout || !mobileLeftOpen) {
      return;
    }
    drawerSwipeStartXRef.current = clientX;
    drawerSwipeLatestXRef.current = clientX;
  }

  function updateDrawerSwipe(clientX: number) {
    if (drawerSwipeStartXRef.current === null) {
      return;
    }
    drawerSwipeLatestXRef.current = clientX;
  }

  function finishDrawerSwipe() {
    const startX = drawerSwipeStartXRef.current;
    const latestX = drawerSwipeLatestXRef.current;
    drawerSwipeStartXRef.current = null;
    drawerSwipeLatestXRef.current = null;
    if (startX === null || latestX === null) {
      return;
    }
    if (latestX - startX <= -SWIPE_DISMISS_THRESHOLD) {
      setMobileLeftOpen(false);
    }
  }

  function startSheetSwipe(clientY: number) {
    if (!mobileLayout || !mobileInspectorOpen) {
      return;
    }
    sheetSwipeStartYRef.current = clientY;
    sheetSwipeLatestYRef.current = clientY;
  }

  function updateSheetSwipe(clientY: number) {
    if (sheetSwipeStartYRef.current === null) {
      return;
    }
    sheetSwipeLatestYRef.current = clientY;
  }

  function finishSheetSwipe() {
    const startY = sheetSwipeStartYRef.current;
    const latestY = sheetSwipeLatestYRef.current;
    sheetSwipeStartYRef.current = null;
    sheetSwipeLatestYRef.current = null;
    if (startY === null || latestY === null) {
      return;
    }
    if (latestY - startY >= SWIPE_DISMISS_THRESHOLD) {
      setMobileInspectorOpen(false);
    }
  }

  useEffect(() => {
    writeStoredPanelState(`${layoutKey}:left`, leftOpen);
  }, [layoutKey, leftOpen]);

  useEffect(() => {
    if (!leftOpen) {
      setMobileLeftOpen(false);
    }
  }, [leftOpen]);

  useEffect(() => {
    if (hasInspector) {
      writeStoredPanelState(`${layoutKey}:inspector`, inspectorOpen);
    }
  }, [hasInspector, inspectorOpen, layoutKey]);

  useEffect(() => {
    if (!inspectorOpen) {
      setMobileInspectorOpen(false);
    }
  }, [inspectorOpen]);

  useEffect(() => {
    if (!hasInspector) {
      setMobileInspectorOpen(false);
    }
  }, [hasInspector]);

  useEffect(() => {
    if (!mobileLayout) {
      setMobileLeftOpen(false);
      setMobileInspectorOpen(false);
    }
  }, [mobileLayout]);

  useEffect(() => {
    writeStoredNumber(`${layoutKey}:leftWidthStep`, normalizedLeftWidthStep);
  }, [layoutKey, normalizedLeftWidthStep]);

  useEffect(() => {
    if (hasInspector && inspectorAutoOpenKey) {
      setInspectorOpen(true);
      setMobileLeftOpen(false);
      setMobileInspectorOpen(mobileLayout);
    }
  }, [hasInspector, inspectorAutoOpenKey, mobileLayout]);

  useMobileModalFocus({
    active: mobileLayout && mobileLeftOpen,
    containerRef: leftPanelRef,
    onClose: closeMobileLeft,
    restoreFocusRef: mobileDrawerToggleRef,
  });

  useMobileModalFocus({
    active: mobileLayout && mobileInspectorOpen,
    containerRef: inspectorPanelRef,
    onClose: closeMobileInspector,
    restoreFocusRef: mobileInspectorToggleRef,
  });

  return (
    <div
      className={`workspace-screen${variant === "default" ? "" : ` ${variant}-screen`}`}
      data-inspector-collapsed={hasInspector && inspectorOpen ? "false" : "true"}
      data-left-collapsed={leftOpen ? "false" : "true"}
      data-mobile-inspector-open={mobileInspectorOpen ? "true" : "false"}
      data-mobile-left-open={mobileLeftOpen ? "true" : "false"}
      data-mobile-action-reserve={hasInspector || mobileAction ? "true" : "false"}
      style={{
        "--workspace-left-w": `${effectiveLeftWidth}px`,
        "--workspace-right-w": `${effectiveRightWidth}px`,
      } as CSSProperties}
    >
      {mobileControlsVisible ? (
        <button
          aria-controls={leftPanelId}
          aria-expanded={mobileLeftOpen}
          aria-label={t("panel.openDrawer", {
            defaultValue: `Open ${leftLabel} drawer`,
            label: leftLabel,
          })}
          className="workspace-mobile-drawer-toggle icon-button text-button"
          ref={mobileDrawerToggleRef}
          onClick={() => {
            setLeftOpen(true);
            setMobileLeftOpen(true);
            setMobileInspectorOpen(false);
          }}
          type="button"
        >
          <PanelVisibilityIcon side="left" action="show" size={16} />
          <span>{leftLabel}</span>
        </button>
      ) : null}
      {mobileLayout && mobileLeftOpen ? (
        <button
          aria-label={t("panel.closeDrawer", {
            defaultValue: `Close ${leftLabel} drawer`,
            label: leftLabel,
          })}
          className="workspace-mobile-drawer-scrim"
          onClick={() => setMobileLeftOpen(false)}
          type="button"
        />
      ) : null}
      {mobileControlsVisible && (hasInspector || mobileAction) ? (
        <div
          className="workspace-mobile-actions"
          aria-label={t("panel.mobileActions", {
            defaultValue: "Mobile workspace actions",
          })}
        >
          {mobileAction ? (
            <FAB
              ariaLabel={mobileAction.ariaLabel}
              busy={mobileAction.busy}
              disabled={mobileAction.disabled}
              icon={mobileAction.icon}
              label={mobileAction.label}
              onClick={mobileAction.onClick}
              title={mobileAction.title}
            />
          ) : null}
          {hasInspector ? (
            <button
              aria-controls={inspectorPanelId}
              aria-expanded={mobileInspectorOpen}
              aria-label={openSheetLabel}
              className="workspace-mobile-inspector-toggle icon-button text-button"
              ref={mobileInspectorToggleRef}
              onClick={() => {
                setInspectorOpen(true);
                setMobileInspectorOpen(true);
                setMobileLeftOpen(false);
              }}
              title={openSheetLabel}
              type="button"
            >
              <PanelVisibilityIcon side="bottom" action="show" size={16} />
              <span>{resolvedInspectorLabel}</span>
            </button>
          ) : null}
        </div>
      ) : null}
      {hasInspector && mobileLayout && inspectorOpen && mobileInspectorOpen ? (
        <button
          aria-label={t("panel.closeSheet", {
            defaultValue: `Close ${resolvedInspectorLabel} sheet`,
            label: resolvedInspectorLabel,
          })}
          className="workspace-mobile-inspector-scrim"
          onClick={() => setMobileInspectorOpen(false)}
          type="button"
        />
      ) : null}
      {leftOpen ? (
        <MobileDrawer
          id={leftPanelId}
          label={leftLabel}
          mobileLayout={mobileLayout}
          mobileOpen={mobileLeftOpen}
          onPointerCancel={finishDrawerSwipe}
          onPointerDown={(event) => startDrawerSwipe(event.clientX)}
          onPointerMove={(event) => updateDrawerSwipe(event.clientX)}
          onPointerUp={finishDrawerSwipe}
          ref={leftPanelRef}
        >
          {left({
            expandWidthLabel,
            expandWidthAtMax,
            onClose: () => {
              setMobileLeftOpen(false);
              setLeftOpen(false);
            },
            onExpandWidth: mobileLayout
              ? undefined
              : () => {
                  setLeftWidthStep((current) => (current + 1) % leftWidths.length);
                },
          })}
        </MobileDrawer>
      ) : (
        <button
          aria-label={
            leftRestoreLabel
            ?? t("panel.show", { defaultValue: `Show ${leftLabel}`, label: leftLabel })
          }
          className="workspace-panel-restore workspace-panel-restore-left"
          onClick={() => setLeftOpen(true)}
          type="button"
        >
          <PanelVisibilityIcon side="left" action="show" size={16} />
          <span>{leftLabel}</span>
        </button>
      )}
      {main}
      {hasInspector && inspectorOpen ? (
        <BottomSheet
          id={inspectorPanelId}
          label={resolvedInspectorLabel}
          mobileLayout={mobileLayout}
          mobileOpen={mobileInspectorOpen}
          onPointerCancel={finishSheetSwipe}
          onPointerDown={(event) => startSheetSwipe(event.clientY)}
          onPointerMove={(event) => updateSheetSwipe(event.clientY)}
          onPointerUp={finishSheetSwipe}
          ref={inspectorPanelRef}
        >
          <div className="inspector-panel-toolbar">
            <span>{resolvedInspectorLabel}</span>
            <button
              aria-label={hideInspectorLabel}
              className="icon-button"
              onClick={() => {
                setMobileInspectorOpen(false);
                setInspectorOpen(false);
              }}
              title={hideInspectorLabel}
              type="button"
            >
              <PanelVisibilityIcon
                side={mobileLayout ? "bottom" : "right"}
                action="hide"
                size={15}
              />
            </button>
          </div>
          {/* Its own scroll region, sized around the toolbar above (not the
              panel's own overflow:hidden) -- consumers with content taller
              than the panel (e.g. a long version history) need this to
              actually scroll instead of being silently clipped. */}
          <div className="inspector-panel-body">{inspector}</div>
        </BottomSheet>
      ) : hasInspector ? (
        <button
          aria-label={showInspectorLabel}
          className="workspace-panel-restore workspace-panel-restore-right"
          onClick={() => {
            setInspectorOpen(true);
            setMobileInspectorOpen(mobileLayout);
          }}
          title={showInspectorLabel}
          type="button"
        >
          <PanelVisibilityIcon side="right" action="show" size={16} />
        </button>
      ) : null}
    </div>
  );
}

function readStoredPanelState(key: string, fallback: boolean) {
  if (typeof window === "undefined") {
    return fallback;
  }
  const value = window.localStorage.getItem(storageKey(key));
  if (value === null) {
    return fallback;
  }
  return value === "true";
}

function writeStoredPanelState(key: string, value: boolean) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(storageKey(key), String(value));
}

function readStoredNumber(key: string, fallback: number, max: number) {
  if (typeof window === "undefined") {
    return fallback;
  }
  const value = Number(window.localStorage.getItem(storageKey(key)));
  if (!Number.isInteger(value) || value < 0 || value > max) {
    return fallback;
  }
  return value;
}

function writeStoredNumber(key: string, value: number) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(storageKey(key), String(value));
}

function storageKey(key: string) {
  // Browse-screen LEFT rails now default open, so their state keys are versioned:
  // a stale "false" from the former collapsed default must not win before the new
  // default is read. Other preferences keep their original keys.
  if (
    key === "context:left" ||
    key === "library:left" ||
    key === "agent:left"
  ) {
    return `dtxwiki:workspace-layout:v2:${key}`;
  }
  return `dtxwiki:workspace-layout:${key}`;
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => getMediaQueryMatch(query));

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mediaQuery = window.matchMedia(query);
    setMatches(mediaQuery.matches);

    function handleChange(event: MediaQueryListEvent) {
      setMatches(event.matches);
    }

    if (typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", handleChange);
      return () => mediaQuery.removeEventListener("change", handleChange);
    }

    // Safari before 14 uses the legacy MediaQueryList listener API. This
    // decides whether the TreeRail drawer exists, so keep the mobile layout
    // responsive rather than falling back to the desktop rail there.
    mediaQuery.addListener(handleChange);
    return () => mediaQuery.removeListener(handleChange);
  }, [query]);

  return matches;
}

function getMediaQueryMatch(query: string) {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia(query).matches;
}
