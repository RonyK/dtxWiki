import type { CSSProperties, ReactNode } from "react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { BottomSheet } from "./BottomSheet";
import { MobileDrawer } from "./MobileDrawer";
import { PanelVisibilityIcon } from "./PanelVisibilityIcon";
import { useMobileModalFocus } from "./useMobileModalFocus";
import { COLLAPSED_RAIL_WIDTH, DEFAULT_RAIL_WIDTHS, readWorkspacePanelState, useWorkspaceRailState, writeWorkspacePanelState } from "./useWorkspaceRailState";

const ASK_LEFT_WIDTHS = [300, 420, 560] as const;
// Review rail starts one step wider than other rails (no narrow 280px step) so
// the lifecycle tabs + audience facet + queue rows have room by default.
const REVIEW_LEFT_WIDTHS = [420, 560] as const;
const INSPECTOR_WIDTH = 300;
const SWIPE_DISMISS_THRESHOLD = 64;
/** Below this width the inspector is a bottom sheet and the left rail a drawer. */
export const WORKSPACE_SHEET_LAYOUT_QUERY = "(max-width: 1024px)";

type WorkspaceLayoutProps = {
  defaultInspectorOpen?: boolean;
  defaultLeftOpen?: boolean;
  inspector?: ReactNode;
  /** Marks the collapsed inspector's restore control as holding new content. */
  inspectorAttention?: string | null;
  inspectorAutoOpenKey?: string | null;
  inspectorHideLabel?: string;
  inspectorLabel?: string;
  inspectorRestoreLabel?: string;
  layoutKey: string;
  left?: (controls: {
    expandWidthLabel: string;
    expandWidthAtMax: boolean;
    onClose: () => void;
    onExpandWidth?: () => void;
  }) => ReactNode;
  leftLabel?: string;
  leftRestoreLabel?: string;
  main: ReactNode;
  mobileAction?: ReactNode;
  variant?: "default" | "ask" | "context" | "review";
};

export function WorkspaceLayout({
  defaultInspectorOpen = true,
  defaultLeftOpen = true,
  inspector,
  inspectorAttention = null,
  inspectorAutoOpenKey,
  inspectorHideLabel,
  inspectorLabel,
  inspectorRestoreLabel,
  layoutKey,
  left,
  leftLabel = "",
  leftRestoreLabel,
  main,
  mobileAction,
  variant = "default",
}: WorkspaceLayoutProps) {
  const { t } = useTranslation("shell");
  const leftPanelId = useId();
  const inspectorPanelId = useId();
  const mobileLayout = useMediaQuery(WORKSPACE_SHEET_LAYOUT_QUERY);
  const leftPanelRef = useRef<HTMLDivElement | null>(null);
  const inspectorPanelRef = useRef<HTMLElement | null>(null);
  const mobileDrawerToggleRef = useRef<HTMLButtonElement | null>(null);
  const mobileInspectorToggleRef = useRef<HTMLButtonElement | null>(null);
  const drawerSwipeStartXRef = useRef<number | null>(null);
  const drawerSwipeLatestXRef = useRef<number | null>(null);
  const sheetSwipeStartYRef = useRef<number | null>(null);
  const sheetSwipeLatestYRef = useRef<number | null>(null);
  const [mobileLeftOpen, setMobileLeftOpen] = useState(false);
  const [mobileInspectorOpen, setMobileInspectorOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(() =>
    readWorkspacePanelState(`${layoutKey}:inspector`, defaultInspectorOpen),
  );
  const hasLeft = left !== undefined;
  const hasInspector = inspector !== undefined && inspector !== null;
  const resolvedInspectorLabel =
    inspectorLabel ?? t("panel.detailsFallback", { defaultValue: "Details" });
  const leftWidths =
    variant === "ask"
      ? ASK_LEFT_WIDTHS
      : variant === "review"
        ? REVIEW_LEFT_WIDTHS
        : DEFAULT_RAIL_WIDTHS;
  const {
    open: leftOpen,
    setOpen: setLeftOpen,
    width: leftWidth,
    expandWidth: expandLeftWidth,
    expandWidthAtMax,
  } = useWorkspaceRailState(layoutKey, defaultLeftOpen, leftWidths);
  const effectiveLeftWidth = hasLeft ? leftWidth : 0;
  const effectiveRightWidth = hasInspector
    ? inspectorOpen ? INSPECTOR_WIDTH : COLLAPSED_RAIL_WIDTH
    : 0;
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
  const restoreInspectorLabel = inspectorAttention ? `${showInspectorLabel} · ${inspectorAttention}` : showInspectorLabel;
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
    if (!leftOpen) {
      setMobileLeftOpen(false);
    }
  }, [leftOpen]);

  useEffect(() => {
    if (hasInspector) {
      writeWorkspacePanelState(`${layoutKey}:inspector`, inspectorOpen);
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
      data-has-left={hasLeft ? "true" : "false"}
      data-inspector-collapsed={hasInspector && inspectorOpen ? "false" : "true"}
      data-left-collapsed={hasLeft && leftOpen ? "false" : "true"}
      data-mobile-inspector-open={mobileInspectorOpen ? "true" : "false"}
      data-mobile-left-open={mobileLeftOpen ? "true" : "false"}
      data-mobile-action-reserve={hasInspector || mobileAction ? "true" : "false"}
      style={{
        "--workspace-left-w": `${effectiveLeftWidth}px`,
        "--workspace-right-w": `${effectiveRightWidth}px`,
      } as CSSProperties}
    >
      {hasLeft && mobileControlsVisible ? (
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
      {hasLeft && mobileLayout && mobileLeftOpen ? (
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
          {mobileAction}
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
      {left && leftOpen ? (
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
              : expandLeftWidth,
          })}
        </MobileDrawer>
      ) : hasLeft ? (
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
      ) : null}
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
          aria-label={restoreInspectorLabel}
          className="workspace-panel-restore workspace-panel-restore-right"
          data-attention={inspectorAttention ? "true" : undefined}
          onClick={() => {
            setInspectorOpen(true);
            setMobileInspectorOpen(mobileLayout);
          }}
          title={restoreInspectorLabel}
          type="button"
        >
          <PanelVisibilityIcon side="right" action="show" size={16} />
        </button>
      ) : null}
    </div>
  );
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
