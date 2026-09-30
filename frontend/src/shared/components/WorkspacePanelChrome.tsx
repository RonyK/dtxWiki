import { MoreHorizontal, RefreshCcw } from "lucide-react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { PanelVisibilityIcon } from "./PanelVisibilityIcon";
import { RailResizeIcon } from "./RailResizeIcon";

export type WorkspacePanelRefreshControl = {
  /** Invoked when the refresh button is pressed. */
  onRefresh: () => Promise<void> | void;
  /** When true, the button is disabled and marked busy for assistive tech. */
  refreshing?: boolean;
  /** Accessible label for the idle state. */
  label: string;
  /** Accessible label while refreshing (falls back to `label`). */
  busyLabel?: string;
};

export type WorkspacePanelExpandWidthControl = {
  /** Invoked when the widen/reset toggle is pressed. */
  onExpandWidth: () => void;
  /** When true, the panel is at its widest step; the icon flips to "collapse"
   * and aria-pressed becomes true so users know the next press resets. */
  atMax?: boolean;
  /** Accessible label resolved by the caller for the current state. */
  label: string;
};

export type WorkspacePanelCloseControl = {
  /** Invoked when the hide/close button is pressed. */
  onClose: () => void;
  /** Accessible label for the hide action. */
  label: string;
};

export type WorkspacePanelOverflowItem = {
  /** Stable identity for the menu row. */
  key: string;
  /** Leading icon for the menu row. */
  icon: ReactNode;
  /** Visible + accessible label for the action. */
  label: string;
  /** Invoked when the row is chosen; the menu closes afterwards. */
  onSelect: () => Promise<void> | void;
  /** Disables the row (e.g. while a refresh is in flight). */
  disabled?: boolean;
  /** Marks the row busy for assistive tech. */
  busy?: boolean;
  /** Renders the row in a pressed/active state (e.g. expand-width at max).
   * Sets aria-pressed and an `is-active` class so the menu can convey toggle
   * state that the inline accent CSS cannot (the row is not inline). */
  active?: boolean;
};

type WorkspacePanelChromeProps = {
  refresh?: WorkspacePanelRefreshControl;
  expandWidth?: WorkspacePanelExpandWidthControl;
  close?: WorkspacePanelCloseControl;
  /** Secondary actions collapsed behind a trailing "⋯" overflow menu. The
   * trigger only renders when at least one item is present. */
  overflow?: WorkspacePanelOverflowItem[];
  /** Accessible label for the "⋯" overflow trigger (required if overflow set). */
  overflowLabel?: string;
  /** Icon edge length in px. Defaults to 14 (matches the tree rail). */
  iconSize?: number;
};

/**
 * The standard right-aligned rail header chrome shared across workspace panels
 * (Context/Library/Agent tree rails, the Ask session rail, etc.): optional
 * Refresh, widen/reset-width, and hide/close buttons render directly inline in
 * that order. The optional "⋯" overflow menu is only for auxiliary actions.
 *
 * It is purely presentational: the open/collapse/width state lives in
 * WorkspaceLayout, which passes `onExpandWidth`/`onClose` down through each
 * rail. Each rail keeps its own surface-specific affordances (counts, create,
 * upload, new-session) inline and renders this for the shared trio. Labels are
 * supplied by the caller so there is no cross-namespace i18n coupling.
 *
 * Rails keep the shared refresh/widen/hide trio visible by passing those props
 * directly. Less common surface-specific commands can be passed via `overflow`.
 */
export function WorkspacePanelChrome({
  refresh,
  expandWidth,
  close,
  overflow,
  overflowLabel,
  iconSize = 14,
}: WorkspacePanelChromeProps) {
  return (
    <>
      {refresh ? (
        <button
          aria-busy={refresh.refreshing ?? false}
          aria-label={
            refresh.refreshing ? refresh.busyLabel ?? refresh.label : refresh.label
          }
          className="icon-button"
          disabled={refresh.refreshing ?? false}
          onClick={() => void refresh.onRefresh()}
          title={
            refresh.refreshing ? refresh.busyLabel ?? refresh.label : refresh.label
          }
          type="button"
        >
          <RefreshCcw size={iconSize} aria-hidden="true" />
        </button>
      ) : null}
      {expandWidth ? (
        <button
          aria-label={expandWidth.label}
          aria-pressed={expandWidth.atMax ?? false}
          className="icon-button"
          data-expand-width-at-max={expandWidth.atMax ? "true" : "false"}
          onClick={expandWidth.onExpandWidth}
          title={expandWidth.label}
          type="button"
        >
          <RailResizeIcon
            mode={expandWidth.atMax ? "reset" : "widen"}
            size={iconSize}
          />
        </button>
      ) : null}
      {close ? (
        <button
          aria-label={close.label}
          className="icon-button"
          onClick={close.onClose}
          title={close.label}
          type="button"
        >
          <PanelVisibilityIcon side="left" action="hide" size={iconSize} />
        </button>
      ) : null}
      {overflow && overflow.length > 0 ? (
        <WorkspacePanelOverflowMenu
          items={overflow}
          label={overflowLabel ?? "More actions"}
          iconSize={iconSize}
        />
      ) : null}
    </>
  );
}

function getFocusableMenuItems(container: HTMLElement | null): HTMLElement[] {
  if (!container) {
    return [];
  }
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      '[role="menuitem"]:not([disabled]),[role="menuitemcheckbox"]:not([disabled])',
    ),
  );
}

function WorkspacePanelOverflowMenu({
  items,
  label,
  iconSize,
}: {
  items: WorkspacePanelOverflowItem[];
  label: string;
  iconSize: number;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  const closeMenu = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) {
      triggerRef.current?.focus();
    }
  }, []);

  // Escape / outside-pointerdown dismissal mirrors TreeNodeContextMenu so the
  // overflow popover behaves like the rest of the app's menus.
  useEffect(() => {
    if (!open) {
      return undefined;
    }
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (
        !menuRef.current?.contains(target) &&
        !triggerRef.current?.contains(target)
      ) {
        closeMenu(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenu(true);
      }
    }
    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [open, closeMenu]);

  // Land focus on the first actionable row when the menu opens.
  useEffect(() => {
    if (!open) {
      return;
    }
    const [first] = getFocusableMenuItems(menuRef.current);
    first?.focus();
  }, [open]);

  const handleMenuKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const menuItems = getFocusableMenuItems(menuRef.current);
      if (menuItems.length === 0) {
        return;
      }
      const activeElement = document.activeElement as HTMLElement | null;
      const currentIndex = activeElement ? menuItems.indexOf(activeElement) : -1;
      switch (event.key) {
        case "ArrowDown": {
          event.preventDefault();
          menuItems[(currentIndex + 1 + menuItems.length) % menuItems.length]?.focus();
          return;
        }
        case "ArrowUp": {
          event.preventDefault();
          menuItems[
            (currentIndex - 1 + menuItems.length) % menuItems.length
          ]?.focus();
          return;
        }
        case "Home": {
          event.preventDefault();
          menuItems[0]?.focus();
          return;
        }
        case "End": {
          event.preventDefault();
          menuItems[menuItems.length - 1]?.focus();
          return;
        }
        case "Tab": {
          // ARIA menu-button pattern: Tab/Shift+Tab dismisses the menu. The menu
          // items are about to unmount, so we must NOT let the browser advance
          // focus from a removed element (that drops focus to <body>). Instead,
          // preventDefault and explicitly return focus to the trigger — a stable
          // control the user can Tab onward from.
          event.preventDefault();
          closeMenu(true);
          return;
        }
        default:
          return;
      }
    },
    [closeMenu],
  );

  return (
    <div className="tree-rail-menu">
      <button
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={label}
        className="icon-button"
        onClick={() => setOpen((value) => !value)}
        ref={triggerRef}
        title={label}
        type="button"
      >
        <MoreHorizontal size={iconSize} aria-hidden="true" />
      </button>
      {open ? (
        <div
          aria-label={label}
          className="tree-rail-menu-panel"
          id={menuId}
          onKeyDown={handleMenuKeyDown}
          ref={menuRef}
          role="menu"
          tabIndex={-1}
        >
          {items.map((item) => {
            // A toggle item (e.g. expand-width at max) is a checkbox-style menu
            // item: menuitem does not support a checked/pressed state, so AT
            // would not announce it. Use menuitemcheckbox + aria-checked for
            // toggles; plain action rows stay role=menuitem.
            const isToggle = item.active !== undefined;
            return (
              <button
                aria-busy={item.busy ?? undefined}
                aria-checked={isToggle ? Boolean(item.active) : undefined}
                className={`tree-rail-menu-item${item.active ? " is-active" : ""}`}
                disabled={item.disabled ?? false}
                key={item.key}
                onClick={() => {
                  void item.onSelect();
                  closeMenu(true);
                }}
                role={isToggle ? "menuitemcheckbox" : "menuitem"}
                type="button"
              >
                {item.icon}
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
