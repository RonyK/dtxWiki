import {
  ChevronDown,
  ChevronUp,
  Copy,
  Edit2,
  FilePlus,
  FolderPlus,
  FolderTree,
  Trash2,
  Star,
} from "lucide-react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

import type { TreeNodePayload } from "../api/workspace";
import type { NewItemType } from "./NewItemDialog";

export type TreeNodeContextMenuAction =
  | { type: "new-item"; itemType: NewItemType; node: TreeNodePayload }
  | { type: "rename"; node: TreeNodePayload }
  | { type: "move"; node: TreeNodePayload }
  | { type: "move-up"; node: TreeNodePayload }
  | { type: "move-down"; node: TreeNodePayload }
  | { type: "delete"; node: TreeNodePayload }
  | { type: "favorite"; node: TreeNodePayload }
  | { type: "copy-link"; node: TreeNodePayload };

type TreeNodeContextMenuProps = {
  canCreateFolder: boolean;
  canCreatePage: boolean;
  canDelete: boolean;
  canMove: boolean;
  canMoveUp?: boolean;
  canMoveDown?: boolean;
  canRename: boolean;
  canCopyLink: boolean;
  canFavorite?: boolean;
  favoritePending?: boolean;
  statusSummary?: string;
  node: TreeNodePayload;
  position: { left: number; top: number };
  onAction: (action: TreeNodeContextMenuAction) => void;
  onClose: () => void;
};

function getFocusableMenuItems(container: HTMLElement | null): HTMLElement[] {
  if (!container) {
    return [];
  }
  return Array.from(
    container.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])'),
  );
}

export function TreeNodeContextMenu({
  canCreateFolder,
  canCreatePage,
  canDelete,
  canMove,
  canMoveUp = false,
  canMoveDown = false,
  canRename,
  canCopyLink,
  canFavorite = false,
  favoritePending = false,
  statusSummary,
  node,
  position,
  onAction,
  onClose,
}: TreeNodeContextMenuProps) {
  const { t } = useTranslation("tree");
  const summaryId = useId();
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [fittedPosition, setFittedPosition] = useState<{
    left: number;
    top: number;
    maxHeight?: number;
  }>({ ...position });
  const canCreateChild = node.can_have_children && (canCreateFolder || canCreatePage);

  useLayoutEffect(() => {
    const fitToViewport = () => {
      const menu = menuRef.current;
      if (!menu) {
        return;
      }
      const margin = 8;
      const rect = menu.getBoundingClientRect();
      const visualViewport = window.visualViewport;
      const viewportLeft = visualViewport?.offsetLeft ?? 0;
      const viewportTop = visualViewport?.offsetTop ?? 0;
      const viewportWidth =
        visualViewport?.width ||
        document.documentElement.clientWidth ||
        window.innerWidth;
      const viewportHeight =
        visualViewport?.height ||
        document.documentElement.clientHeight ||
        window.innerHeight;
      const viewportRight = viewportLeft + viewportWidth;
      const viewportBottom = viewportTop + viewportHeight;
      const maxHeight = Math.max(44, viewportHeight - margin * 2);
      const fittedHeight = Math.min(rect.height, maxHeight);
      const next = {
        left: Math.max(
          viewportLeft + margin,
          Math.min(
            position.left,
            Math.max(viewportLeft + margin, viewportRight - rect.width - margin),
          ),
        ),
        top: Math.max(
          viewportTop + margin,
          Math.min(
            position.top,
            Math.max(viewportTop + margin, viewportBottom - fittedHeight - margin),
          ),
        ),
        maxHeight,
      };
      setFittedPosition((current) =>
        current.left === next.left &&
        current.top === next.top &&
        current.maxHeight === next.maxHeight
          ? current
          : next,
      );
    };

    fitToViewport();
    window.addEventListener("resize", fitToViewport);
    const visualViewport = window.visualViewport;
    visualViewport?.addEventListener("resize", fitToViewport);
    visualViewport?.addEventListener("scroll", fitToViewport);
    return () => {
      window.removeEventListener("resize", fitToViewport);
      visualViewport?.removeEventListener("resize", fitToViewport);
      visualViewport?.removeEventListener("scroll", fitToViewport);
    };
  }, [position]);

  useEffect(() => {
    function handlePointerDown(event: PointerEvent) {
      if (!menuRef.current?.contains(event.target as Node)) {
        onClose();
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    }
    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [onClose]);

  // Focus the first menuitem on mount so screen readers / keyboard users land
  // on an actionable element immediately.
  useEffect(() => {
    const [first] = getFocusableMenuItems(menuRef.current);
    (first ?? menuRef.current)?.focus();
  }, []);

  const handleMenuKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const items = getFocusableMenuItems(menuRef.current);
      if (items.length === 0) {
        return;
      }
      const activeElement = document.activeElement as HTMLElement | null;
      const currentIndex = activeElement ? items.indexOf(activeElement) : -1;

      switch (event.key) {
        case "ArrowDown": {
          event.preventDefault();
          const next = items[(currentIndex + 1 + items.length) % items.length];
          next?.focus();
          return;
        }
        case "ArrowUp": {
          event.preventDefault();
          const prev =
            items[(currentIndex - 1 + items.length) % items.length] ?? items[items.length - 1];
          prev?.focus();
          return;
        }
        case "Home": {
          event.preventDefault();
          items[0]?.focus();
          return;
        }
        case "End": {
          event.preventDefault();
          items[items.length - 1]?.focus();
          return;
        }
        case "Tab": {
          // Tab is a logical "close and let focus move on" — Escape stays for
          // explicit dismissal. We prevent default to stop focus escape from
          // jumping to random page chrome while the menu is open.
          event.preventDefault();
          onClose();
          return;
        }
        default:
          return;
      }
    },
    [onClose],
  );

  const menu = (
    <div
      ref={menuRef}
      className="tree-node-menu"
      role="menu"
      aria-label={t("openActions", { title: node.title })}
      aria-describedby={statusSummary ? summaryId : undefined}
      onKeyDown={handleMenuKeyDown}
      tabIndex={-1}
      style={{
        left: fittedPosition.left,
        maxHeight: fittedPosition.maxHeight,
        top: fittedPosition.top,
      }}
    >
      {statusSummary ? <div className="tree-node-menu-summary" id={summaryId}>{node.title}<br />{statusSummary}</div> : null}
      {canFavorite ? <MenuButton
        icon={<Star size={14} aria-hidden="true" />}
        label={t(node.is_favorite ? "favorite.remove" : "favorite.add")}
        disabled={favoritePending}
        onClick={() => onAction({ type: "favorite", node })}
      /> : null}
      {canCreateChild && canCreatePage ? (
        <MenuButton
          icon={<FilePlus size={14} aria-hidden="true" />}
          label={t("newPage")}
          onClick={() => onAction({ type: "new-item", itemType: "page", node })}
        />
      ) : null}
      {canCreateChild && canCreateFolder ? (
        <MenuButton
          icon={<FolderPlus size={14} aria-hidden="true" />}
          label={t("newFolder")}
          onClick={() => onAction({ type: "new-item", itemType: "folder", node })}
        />
      ) : null}
      {canCreateChild ? <div className="tree-node-menu-separator" role="separator" /> : null}
      {canMoveUp ? (
        <MenuButton
          icon={<ChevronUp size={14} aria-hidden="true" />}
          label={t("moveUp")}
          onClick={() => onAction({ type: "move-up", node })}
        />
      ) : null}
      {canMoveDown ? (
        <MenuButton
          icon={<ChevronDown size={14} aria-hidden="true" />}
          label={t("moveDown")}
          onClick={() => onAction({ type: "move-down", node })}
        />
      ) : null}
      {canRename ? (
        <MenuButton
          icon={<Edit2 size={14} aria-hidden="true" />}
          label={t("rename")}
          onClick={() => onAction({ type: "rename", node })}
        />
      ) : null}
      {canMove ? (
        <MenuButton
          icon={<FolderTree size={14} aria-hidden="true" />}
          label={t("move")}
          onClick={() => onAction({ type: "move", node })}
        />
      ) : null}
      {canCopyLink ? (
        <MenuButton
          icon={<Copy size={14} aria-hidden="true" />}
          label={t("copyLink")}
          onClick={() => onAction({ type: "copy-link", node })}
        />
      ) : null}
      {canDelete ? <div className="tree-node-menu-separator" role="separator" /> : null}
      {canDelete ? (
        <MenuButton
          danger
          icon={<Trash2 size={14} aria-hidden="true" />}
          label={t("delete")}
          onClick={() => onAction({ type: "delete", node })}
        />
      ) : null}
    </div>
  );

  // All of the geometry above is in viewport coordinates, but the callers put
  // this menu inside the rail: at mobile widths the rail sits in an off-canvas
  // drawer that is transformed (`.workspace-left-panel`), which makes that
  // drawer — not the viewport — the containing block for a fixed-position
  // descendant, and its `overflow: hidden` then slices the menu off at the
  // drawer's trailing edge. Rendering at the document root keeps the fitted
  // position honest no matter which surface opened the menu.
  return typeof document === "undefined"
    ? menu
    : createPortal(menu, document.body);
}

function MenuButton({
  danger,
  disabled,
  icon,
  label,
  onClick,
}: {
  danger?: boolean;
  disabled?: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className={`tree-node-menu-item ${danger ? "danger" : ""}`}
      role="menuitem"
      disabled={disabled}
      type="button"
      onClick={onClick}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}
