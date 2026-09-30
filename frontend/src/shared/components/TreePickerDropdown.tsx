import {
  autoUpdate,
  computePosition,
  flip,
  offset,
  shift,
} from "@floating-ui/dom";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  Search,
} from "lucide-react";
import type { CSSProperties, KeyboardEvent, ReactNode } from "react";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

import type { TreeNodePayload } from "../api/workspace";
import { focusableElements } from "../lib/trapTabFocus";
import { useDialogOverlayScope } from "./Dialog";

export type TreePickerDropdownProps = {
  ariaLabel: string;
  disabled?: boolean;
  emptyLabel?: string;
  /**
   * Label for the section root (e.g. "Context"). When set, the root becomes a
   * selectable option at the top of the tree with folders nested under it, and
   * the trigger shows this label instead of a generic "no selection" string.
   */
  rootLabel?: string;
  nodes: TreeNodePayload[];
  onChange: (nodeId: string) => void;
  /**
   * Receives the portaled menu while it is mounted, then `null` when it
   * unmounts. A parent with its own document-level dismissal handler can use
   * this to recognize the picker menu as an owned interaction surface.
   */
  onMenuElementChange?: (element: HTMLDivElement | null) => void;
  selectedId: string;
};

const ROOT_ENTRY_ID = "__tree_picker_root__";
const VIEWPORT_MARGIN = 12;
const MIN_ANCHORED_RESULTS_HEIGHT = 240;
const MAX_ANCHORED_MENU_HEIGHT = 520;
const PREFERRED_MENU_WIDTH = 460;

type MenuGeometry = {
  left: number;
  maxHeight: number;
  top: number;
  viewportSurface: boolean;
  width: number;
};

export function TreePickerDropdown({
  ariaLabel,
  disabled = false,
  emptyLabel = "No folders available",
  rootLabel,
  nodes,
  onChange,
  onMenuElementChange,
  selectedId,
}: TreePickerDropdownProps) {
  const optionIdPrefix = useId();
  const menuId = `${optionIdPrefix}-tree-picker-menu`;
  const selectedPathDescriptionId = `${optionIdPrefix}-selected-path`;
  const { t } = useTranslation("tree");
  const dialogOverlay = useDialogOverlayScope();
  const [open, setOpen] = useState(false);
  const [activeNodeId, setActiveNodeId] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [menuGeometry, setMenuGeometry] = useState<MenuGeometry | null>(null);
  const menuReady = open && menuGeometry !== null;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const resultsRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const setMenuElement = useCallback(
    (element: HTMLDivElement | null) => {
      menuRef.current = element;
      onMenuElementChange?.(element);
    },
    [onMenuElementChange],
  );
  const nodeById = useMemo(
    () => new Map(nodes.map((node) => [node.id, node])),
    [nodes],
  );
  const selectedNode = selectedId ? nodeById.get(selectedId) ?? null : null;
  const selectedPath = selectedNode ? treePathLabel(selectedNode) : null;
  const selectedAncestorIds = useMemo(
    () => expandedAncestorIds(selectedNode, nodeById),
    [nodeById, selectedNode],
  );
  const selectedExpansionKey = `${selectedId}:${selectedAncestorIds.join("/")}`;
  const lastSelectedExpansionKeyRef = useRef(selectedExpansionKey);
  const [expandedNodeIds, setExpandedNodeIds] = useState<Set<string>>(
    () => new Set(selectedAncestorIds),
  );
  const rootNodes = useMemo(
    () =>
      nodes.filter((node) => !node.parent_id || !nodeById.has(node.parent_id)),
    [nodeById, nodes],
  );
  const childrenByParentId = useMemo(() => {
    const byParentId = new Map<string, TreeNodePayload[]>();
    for (const node of nodes) {
      if (!node.parent_id || !nodeById.has(node.parent_id)) {
        continue;
      }
      const siblings = byParentId.get(node.parent_id) ?? [];
      siblings.push(node);
      byParentId.set(node.parent_id, siblings);
    }
    return byParentId;
  }, [nodeById, nodes]);
  const visibleNodes = useMemo(
    () => visibleTreeNodes(rootNodes, childrenByParentId, expandedNodeIds),
    [childrenByParentId, expandedNodeIds, rootNodes],
  );
  const normalizedSearch = searchQuery.trim().toLocaleLowerCase();
  const matchingNodes = useMemo(
    () =>
      normalizedSearch
        ? nodes.filter((node) =>
            treePathLabel(node).toLocaleLowerCase().includes(normalizedSearch),
          )
        : visibleNodes,
    [nodes, normalizedSearch, visibleNodes],
  );
  const rootEntryVisible = Boolean(
    rootLabel !== undefined
      && (!normalizedSearch
        || rootLabel.toLocaleLowerCase().includes(normalizedSearch)),
  );
  const navigableIds = useMemo(
    () => [
      ...(rootEntryVisible ? [ROOT_ENTRY_ID] : []),
      ...matchingNodes.map((node) => node.id),
    ],
    [matchingNodes, rootEntryVisible],
  );

  useEffect(() => {
    if (!open) {
      return;
    }
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (
        !containerRef.current?.contains(target)
        && !menuRef.current?.contains(target)
      ) {
        setOpen(false);
        setSearchQuery("");
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  useEffect(() => {
    if (lastSelectedExpansionKeyRef.current === selectedExpansionKey) {
      return;
    }
    lastSelectedExpansionKeyRef.current = selectedExpansionKey;
    setExpandedNodeIds(new Set(selectedAncestorIds));
  }, [selectedAncestorIds, selectedExpansionKey]);

  useEffect(() => {
    if (!open) {
      return;
    }
    setActiveNodeId((current) => {
      if (navigableIds.includes(current)) {
        return current;
      }
      if (selectedNode && navigableIds.includes(selectedNode.id)) {
        return selectedNode.id;
      }
      if (!selectedId && rootEntryVisible) {
        return ROOT_ENTRY_ID;
      }
      return navigableIds[0] ?? "";
    });
  }, [navigableIds, open, rootEntryVisible, selectedId, selectedNode]);

  useLayoutEffect(() => {
    if (!menuReady) {
      return;
    }
    // The floating menu starts hidden until placement completes. Focus once
    // it is visible, not on every subsequent scroll/resize reposition.
    searchRef.current?.focus({ preventScroll: true });
  }, [menuReady]);

  useEffect(() => {
    if (!open || !activeNodeId) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      document
        .getElementById(treePickerOptionId(optionIdPrefix, activeNodeId))
        ?.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeNodeId, open, optionIdPrefix]);

  useEffect(() => {
    if (!open || !dialogOverlay?.scrollRegion) {
      return;
    }
    const scrollRegion = dialogOverlay.scrollRegion;
    const priorOverflow = scrollRegion.style.overflow;
    const priorOverscrollBehavior = scrollRegion.style.overscrollBehavior;
    const priorScrollTop = scrollRegion.scrollTop;
    scrollRegion.style.overflow = "hidden";
    scrollRegion.style.overscrollBehavior = "none";
    return () => {
      scrollRegion.style.overflow = priorOverflow;
      scrollRegion.style.overscrollBehavior = priorOverscrollBehavior;
      scrollRegion.scrollTop = priorScrollTop;
    };
  }, [dialogOverlay?.scrollRegion, open]);

  useLayoutEffect(() => {
    if (!open) {
      setMenuGeometry(null);
      return;
    }
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) {
      return;
    }
    let disposed = false;
    const update = () => {
      const viewport = visualViewportBounds();
      const triggerRect = trigger.getBoundingClientRect();
      const availableBelow = Math.max(
        0,
        viewport.top + viewport.height - triggerRect.bottom - VIEWPORT_MARGIN,
      );
      const availableAbove = Math.max(
        0,
        triggerRect.top - viewport.top - VIEWPORT_MARGIN,
      );
      const largestSide = Math.max(availableBelow, availableAbove);
      const viewportSurface = largestSide < MIN_ANCHORED_RESULTS_HEIGHT;
      const width = Math.max(
        0,
        Math.min(
          Math.max(triggerRect.width, PREFERRED_MENU_WIDTH),
          viewport.width - VIEWPORT_MARGIN * 2,
        ),
      );
      const maxHeight = viewportSurface
        ? Math.max(0, viewport.height - VIEWPORT_MARGIN * 2)
        : Math.min(MAX_ANCHORED_MENU_HEIGHT, largestSide);

      if (viewportSurface) {
        setMenuGeometry({
          left: viewport.left + VIEWPORT_MARGIN,
          maxHeight,
          top: viewport.top + VIEWPORT_MARGIN,
          viewportSurface: true,
          width: Math.max(0, viewport.width - VIEWPORT_MARGIN * 2),
        });
        return;
      }

      menu.style.width = `${width}px`;
      menu.style.maxHeight = `${maxHeight}px`;
      void computePosition(trigger, menu, {
        middleware: [
          offset(8),
          flip({ padding: VIEWPORT_MARGIN }),
          shift({ padding: VIEWPORT_MARGIN }),
        ],
        placement: availableBelow >= availableAbove ? "bottom-start" : "top-start",
        strategy: "fixed",
      }).then(({ x, y }) => {
        if (disposed) {
          return;
        }
        setMenuGeometry({
          left: clamp(
            x,
            viewport.left + VIEWPORT_MARGIN,
            viewport.left + viewport.width - width - VIEWPORT_MARGIN,
          ),
          maxHeight,
          top: clamp(
            y,
            viewport.top + VIEWPORT_MARGIN,
            viewport.top + viewport.height - maxHeight - VIEWPORT_MARGIN,
          ),
          viewportSurface: false,
          width,
        });
      });
    };
    const cleanupAutoUpdate = autoUpdate(trigger, menu, update);
    const visualViewport = window.visualViewport;
    visualViewport?.addEventListener("resize", update);
    visualViewport?.addEventListener("scroll", update);
    update();
    return () => {
      disposed = true;
      cleanupAutoUpdate();
      visualViewport?.removeEventListener("resize", update);
      visualViewport?.removeEventListener("scroll", update);
    };
  }, [open]);

  function openMenu() {
    setOpen(true);
    setSearchQuery("");
    setActiveNodeId(
      selectedNode?.id
        ?? (!selectedId && rootLabel !== undefined ? ROOT_ENTRY_ID : visibleNodes[0]?.id)
        ?? "",
    );
  }

  function closeMenu({ restoreFocus = true } = {}) {
    setOpen(false);
    setSearchQuery("");
    if (restoreFocus) {
      triggerRef.current?.focus();
    }
  }

  function toggleExpanded(nodeId: string) {
    setExpandedNodeIds((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
      return next;
    });
  }

  function pickNode(nodeId: string) {
    onChange(nodeId === ROOT_ENTRY_ID ? "" : nodeId);
    closeMenu();
  }

  function focusAdjacentDialogControl(backward: boolean) {
    const trigger = triggerRef.current;
    if (!trigger) {
      return;
    }
    const scope = dialogOverlay?.panel ?? document.body;
    const popup = menuRef.current;
    const controls = focusableElements(scope).filter(
      (element) => !popup?.contains(element),
    );
    const triggerIndex = controls.indexOf(trigger);
    const next = controls[triggerIndex + (backward ? -1 : 1)] ?? trigger;
    closeMenu({ restoreFocus: false });
    window.requestAnimationFrame(() => next.focus());
  }

  function handleResultsKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeMenu();
      return;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      focusAdjacentDialogControl(event.shiftKey);
      return;
    }
    if (navigableIds.length === 0) {
      return;
    }
    const currentIndex = Math.max(0, navigableIds.indexOf(activeNodeId));
    const activeId = navigableIds[currentIndex] ?? navigableIds[0];
    const activeNode = activeId === ROOT_ENTRY_ID ? null : nodeById.get(activeId) ?? null;

    if (["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) {
      // A mouse click can focus a native chevron. Tree navigation returns
      // focus to the aria-activedescendant owner before moving the active row.
      resultsRef.current?.focus();
    }

    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveNodeId(
        navigableIds[Math.min(currentIndex + 1, navigableIds.length - 1)],
      );
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveNodeId(navigableIds[Math.max(currentIndex - 1, 0)]);
    } else if (event.key === "Home") {
      event.preventDefault();
      setActiveNodeId(navigableIds[0]);
    } else if (event.key === "End") {
      event.preventDefault();
      setActiveNodeId(navigableIds[navigableIds.length - 1]);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pickNode(activeId);
    } else if (
      !normalizedSearch
      && activeNode
      && event.key === "ArrowRight"
    ) {
      const children = childrenByParentId.get(activeNode.id) ?? [];
      if (children.length > 0) {
        event.preventDefault();
        if (!expandedNodeIds.has(activeNode.id)) {
          toggleExpanded(activeNode.id);
        } else {
          setActiveNodeId(children[0].id);
        }
      }
    } else if (!normalizedSearch && activeNode && event.key === "ArrowLeft") {
      const children = childrenByParentId.get(activeNode.id) ?? [];
      if (children.length > 0 && expandedNodeIds.has(activeNode.id)) {
        event.preventDefault();
        toggleExpanded(activeNode.id);
      } else if (activeNode.parent_id && nodeById.has(activeNode.parent_id)) {
        event.preventDefault();
        setActiveNodeId(activeNode.parent_id);
      }
    }
  }

  function renderNode(node: TreeNodePayload, depth: number): ReactNode {
    const children = childrenByParentId.get(node.id) ?? [];
    const expanded = expandedNodeIds.has(node.id);
    const selected = node.id === selectedId;
    const active = node.id === activeNodeId;
    const Icon = expanded ? FolderOpen : Folder;
    return (
      <div className="tree-picker-node" key={node.id} role="none">
        {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events -- The composite tree owns keyboard navigation and selection via aria-activedescendant. */}
        <div
          aria-expanded={children.length > 0 ? expanded : undefined}
          aria-level={depth + 1}
          aria-selected={selected}
          className={`tree-picker-row ${selected ? "tree-picker-row-selected" : ""} ${
            active ? "tree-picker-row-active" : ""
          }`}
          id={treePickerOptionId(optionIdPrefix, node.id)}
          role="treeitem"
          style={{ "--tree-picker-level": depth } as CSSProperties}
          tabIndex={-1}
          onClick={() => pickNode(node.id)}
          onMouseEnter={() => setActiveNodeId(node.id)}
        >
          {children.length > 0 && !normalizedSearch ? (
            <button
              aria-expanded={expanded}
              aria-label={t(expanded ? "collapse" : "expand", {
                title: node.title,
              })}
              className="tree-picker-chevron"
              data-tree-toggle={node.id}
              onClick={(event) => {
                event.stopPropagation();
                toggleExpanded(node.id);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.stopPropagation();
                }
              }}
              tabIndex={-1}
              type="button"
            >
              {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            </button>
          ) : (
            <span className="tree-picker-spacer" aria-hidden="true" />
          )}
          <Icon size={16} aria-hidden="true" />
          <span className="tree-picker-copy">
            <span className="tree-picker-label">{node.title}</span>
            {normalizedSearch ? (
              <span className="tree-picker-row-path">{treePathLabel(node)}</span>
            ) : null}
          </span>
        </div>
        {!normalizedSearch && expanded && children.length > 0 ? (
          <div role="group">
            {children.map((child) => renderNode(child, depth + 1))}
          </div>
        ) : null}
      </div>
    );
  }

  const menu = open ? (
    <div
      className={`tree-picker-menu ${
        menuGeometry?.viewportSurface ? "tree-picker-menu-viewport" : ""
      }`}
      ref={setMenuElement}
      style={
        menuGeometry
          ? {
              left: menuGeometry.left,
              maxHeight: menuGeometry.maxHeight,
              top: menuGeometry.top,
              visibility: "visible",
              width: menuGeometry.width,
            }
          : { visibility: "hidden" }
      }
    >
      <div className="tree-picker-menu-header">
        <label className="tree-picker-search">
          <Search aria-hidden="true" size={16} />
          <input
            aria-label={t("picker.search")}
            onChange={(event) => setSearchQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                closeMenu();
              } else if (event.key === "ArrowDown") {
                event.preventDefault();
                resultsRef.current?.focus();
              } else if (event.key === "Tab" && event.shiftKey) {
                event.preventDefault();
                closeMenu();
              }
            }}
            placeholder={t("picker.searchPlaceholder")}
            ref={searchRef}
            type="search"
            value={searchQuery}
          />
        </label>
        <div className="tree-picker-selected-path" role="status">
          <span>{t("picker.selectedPath")}</span>
          <strong>{selectedPath ?? rootLabel ?? emptyLabel}</strong>
        </div>
      </div>
      <div
        aria-activedescendant={
          activeNodeId
            ? treePickerOptionId(optionIdPrefix, activeNodeId)
            : undefined
        }
        aria-label={t("picker.options", { label: ariaLabel })}
        className="tree-picker-results"
        id={menuId}
        onKeyDown={handleResultsKeyDown}
        ref={resultsRef}
        role="tree"
        tabIndex={0}
      >
        {rootEntryVisible ? (
          // eslint-disable-next-line jsx-a11y/click-events-have-key-events -- The composite tree owns keyboard navigation and selection via aria-activedescendant.
          <div
            aria-level={1}
            aria-selected={selectedId === ""}
            className={`tree-picker-row ${
              selectedId === "" ? "tree-picker-row-selected" : ""
            } ${activeNodeId === ROOT_ENTRY_ID ? "tree-picker-row-active" : ""}`}
            id={treePickerOptionId(optionIdPrefix, ROOT_ENTRY_ID)}
            onClick={() => pickNode(ROOT_ENTRY_ID)}
            onMouseEnter={() => setActiveNodeId(ROOT_ENTRY_ID)}
            role="treeitem"
            style={{ "--tree-picker-level": 0 } as CSSProperties}
            tabIndex={-1}
          >
            <span className="tree-picker-spacer" aria-hidden="true" />
            <Folder size={16} aria-hidden="true" />
            <span className="tree-picker-label">{rootLabel}</span>
          </div>
        ) : null}
        {normalizedSearch ? (
          matchingNodes.length > 0 ? (
            matchingNodes.map((node) => renderNode(node, 0))
          ) : (
            <div className="tree-picker-empty">{t("picker.noMatches")}</div>
          )
        ) : rootNodes.length > 0 ? (
          rootNodes.map((node) => renderNode(node, rootLabel === undefined ? 0 : 1))
        ) : rootLabel === undefined ? (
          <div className="tree-picker-empty">{emptyLabel}</div>
        ) : null}
      </div>
    </div>
  ) : null;

  return (
    <div className="tree-picker" ref={containerRef}>
      <button
        aria-controls={open ? menuId : undefined}
        aria-describedby={selectedPath ? selectedPathDescriptionId : undefined}
        aria-expanded={open}
        aria-haspopup="tree"
        aria-label={ariaLabel}
        className="tree-picker-trigger"
        disabled={disabled}
        ref={triggerRef}
        title={selectedPath ?? undefined}
        type="button"
        onClick={() => (open ? closeMenu() : openMenu())}
      >
        <Folder size={16} aria-hidden="true" />
        <span className="tree-picker-trigger-label">
          {selectedNode ? compactTreePathLabel(selectedNode) : rootLabel ?? emptyLabel}
        </span>
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      {selectedPath ? (
        <span className="visually-hidden" id={selectedPathDescriptionId}>
          {selectedPath}
        </span>
      ) : null}
      {menu && typeof document !== "undefined"
        ? createPortal(menu, dialogOverlay?.overlayRoot ?? document.body)
        : menu}
    </div>
  );
}

function visualViewportBounds() {
  const viewport = window.visualViewport;
  return {
    height: viewport?.height ?? window.innerHeight,
    left: viewport?.offsetLeft ?? 0,
    top: viewport?.offsetTop ?? 0,
    width: viewport?.width ?? window.innerWidth,
  };
}

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(value, Math.max(min, max)));
}

function treePathLabel(node: TreeNodePayload) {
  return treePathParts(node).join(" / ");
}

function compactTreePathLabel(node: TreeNodePayload) {
  const path = treePathParts(node);
  return path.length > 2 ? `… / ${path.slice(-2).join(" / ")}` : path.join(" / ");
}

function treePathParts(node: TreeNodePayload) {
  const ancestors = node.breadcrumb
    .filter((item) => item.id !== node.id)
    .map((item) => item.title);
  return [...ancestors, node.title];
}

function treePickerOptionId(prefix: string, nodeId: string) {
  return `${prefix}-tree-picker-option-${nodeId}`;
}

function visibleTreeNodes(
  rootNodes: TreeNodePayload[],
  childrenByParentId: Map<string, TreeNodePayload[]>,
  expandedNodeIds: Set<string>,
) {
  const visible: TreeNodePayload[] = [];
  const appendVisible = (node: TreeNodePayload) => {
    visible.push(node);
    if (!expandedNodeIds.has(node.id)) {
      return;
    }
    for (const child of childrenByParentId.get(node.id) ?? []) {
      appendVisible(child);
    }
  };
  for (const node of rootNodes) {
    appendVisible(node);
  }
  return visible;
}

function expandedAncestorIds(
  selectedNode: TreeNodePayload | null,
  nodeById: Map<string, TreeNodePayload>,
) {
  if (!selectedNode) {
    return [];
  }
  const expanded: string[] = [];
  const seen = new Set<string>();
  let current = selectedNode;
  while (current.parent_id && !seen.has(current.parent_id)) {
    seen.add(current.parent_id);
    expanded.push(current.parent_id);
    const parent = nodeById.get(current.parent_id);
    if (!parent) {
      break;
    }
    current = parent;
  }
  return expanded;
}
