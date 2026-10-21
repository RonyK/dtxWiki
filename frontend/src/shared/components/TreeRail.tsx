import {
  BadgeCheck,
  ChevronDown,
  ChevronRight,
  FileText,
  Folder,
  FolderPlus,
  MoreHorizontal,
  Plus,
  Search,
  Upload,
  Star,
} from "lucide-react";
import type {
  CSSProperties,
  DragEvent as ReactDragEvent,
  KeyboardEvent,
  PointerEvent,
  ReactNode,
} from "react";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";

import type { TreeNodePayload } from "../api/workspace";
import {
  favoriteTargetKind,
  getTreeFavoriteSnapshot,
  subscribeTreeFavorites,
  toggleTreeFavorite,
  treeFavoriteKey,
  type TreeFavoriteScope,
} from "../api/treeFavorites";
import { CountChip } from "./CountChip";
import type { NewItemType } from "./NewItemDialog";
import { TreeNodeContextMenu } from "./TreeNodeContextMenu";
import type { TreeNodeContextMenuAction } from "./TreeNodeContextMenu";
import { collectHiddenSystemRootNodeIds } from "./treeSectionRoot";
import { WorkspacePanelChrome } from "./WorkspacePanelChrome";
import { useNowTick } from "./RelativeTime";
import { contentChangedAt, TREE_RECENT_CHANGE_MS, treeNodeStatus, treeNodeStatusDescription } from "./treeNodeStatus";
import { useTreeRowTooltip, type TreeRowTooltip } from "./useTreeRowTooltip";

type TreeRowStyle = CSSProperties & {
  "--tree-level": number;
};

type TreeIndentGuideStyle = CSSProperties & {
  "--tree-guide-level": number;
};

type TreeDropPlacement = "before" | "inside" | "after";

const LONG_PRESS_CONTEXT_MENU_MS = 550;
const LONG_PRESS_MOVE_CANCEL_PX = 12;
const TREE_NODE_DRAG_DATA = "application/x-dtxwiki-tree-node";

type TreeRailProps = {
  title: string;
  accent?: "library" | "context" | "agent" | "ask";
  nodes: TreeNodePayload[];
  favoriteScope?: TreeFavoriteScope;
  activeTargetId?: string | null;
  activeNodeId?: string | null;
  expandedNodeIds?: string[];
  emptyLabel?: string;
  /**
   * Compatibility prop retained for callers that control expansion themselves.
   * Filtering always keeps the active node and its ancestors visible; actual
   * expansion remains controlled exclusively by `expandedNodeIds`.
   */
  forceActivePathExpanded?: boolean;
  hideSystemRootNodes?: boolean;
  canCreateFolder?: boolean;
  canCreatePage?: boolean;
  canUpload?: boolean;
  onExpandedNodeIdsChange?: (nodeIds: string[]) => void;
  onCreateFolder?: (parentNodeId: string | null) => void;
  onCreateItem?: (input: {
    itemType: NewItemType;
    parentNodeId: string | null;
  }) => void;
  onCopyNodeLink?: (node: TreeNodePayload) => void;
  onDeleteNode?: (node: TreeNodePayload) => void;
  onDropNode?: (
    node: TreeNodePayload,
    newParentNode: TreeNodePayload,
  ) => Promise<void> | void;
  onExpandWidth?: () => void;
  expandWidthLabel?: string;
  /** When true, the next click on the expand-width button resets the panel
   * back to its narrowest size. The icon flips from "expand" to "collapse"
   * so users can tell they have reached the widest step. */
  expandWidthAtMax?: boolean;
  onClose?: () => void;
  onMoveNode?: (node: TreeNodePayload) => void;
  onReorderNodeAt?: (
    node: TreeNodePayload,
    anchorNode: TreeNodePayload,
    placement: "before" | "after",
  ) => Promise<void> | void;
  onRenameNode?: (node: TreeNodePayload) => void;
  onRefresh?: () => Promise<void> | void;
  /** When true, marks the Refresh button as busy for assistive tech. */
  refreshing?: boolean;
  onSelectTarget?: (node: TreeNodePayload) => void;
  uploadAction?: {
    ariaLabel: string;
    busy?: boolean;
    disabled?: boolean;
    label?: string;
    onClick: () => void;
    title?: string;
  };
  headerSlot?: ReactNode;
};

export function TreeRail({
  title,
  accent,
  nodes,
  favoriteScope,
  activeTargetId,
  activeNodeId,
  expandedNodeIds,
  emptyLabel = "No tree nodes",
  hideSystemRootNodes = false,
  canCreateFolder,
  canCreatePage,
  canUpload,
  onExpandedNodeIdsChange,
  onCreateFolder,
  onCreateItem,
  onCopyNodeLink,
  onDeleteNode,
  onDropNode,
  onExpandWidth,
  expandWidthLabel,
  expandWidthAtMax = false,
  onClose,
  onMoveNode,
  onReorderNodeAt,
  onRenameNode,
  onRefresh,
  refreshing = false,
  onSelectTarget,
  uploadAction,
  headerSlot,
}: TreeRailProps) {
  const { t, i18n } = useTranslation("tree");
  const relativeNow = useNowTick();
  const [expiryNow, setExpiryNow] = useState(() => Date.now());
  const now = Math.max(relativeNow, expiryNow);
  const favoriteSnapshot = useSyncExternalStore(subscribeTreeFavorites, getTreeFavoriteSnapshot);
  const favoriteScopeKey = favoriteScope ? JSON.stringify(favoriteScope) : "";
  const currentScopeRef = useRef(favoriteScopeKey);
  useLayoutEffect(() => { currentScopeRef.current = favoriteScopeKey; }, [favoriteScopeKey]);
  const resolvedNodes = useMemo(() => nodes.map((node) => {
    const value = favoriteScope ? favoriteSnapshot.get(treeFavoriteKey(favoriteScope, node)) : undefined;
    return value ? { ...node, is_favorite: value.isFavorite } : node;
  }), [favoriteScope, favoriteSnapshot, nodes]);
  useEffect(() => {
    const currentTime = Date.now();
    const expiries = nodes
      .filter((node) => treeNodeStatus(node, now) === "recent")
      .map((node) => contentChangedAt(node) + TREE_RECENT_CHANGE_MS)
      .filter((expires) => expires > now);
    if (!expiries.length) return;
    // Schedule the actual 72h boundary; the shared relative-time tick alone can
    // run up to 30 seconds later. Expiration never depends on reading a page.
    const timer = window.setTimeout(() => setExpiryNow(Date.now()),
      Math.max(0, Math.min(2_147_483_647, Math.min(...expiries) - currentTime)));
    return () => window.clearTimeout(timer);
  }, [nodes, now]);
  const [favoriteFailure, setFavoriteFailure] = useState<{ scope: string; message: string } | null>(null);
  const [filterQuery, setFilterQuery] = useState("");
  const [contextMenuState, setContextMenu] = useState<{
    node: TreeNodePayload;
    position: { left: number; top: number };
    scopeKey: string;
  } | null>(null);
  const contextMenu = contextMenuState?.scopeKey === favoriteScopeKey ? contextMenuState : null;
  const contextMenuInvokerRef = useRef<HTMLElement | null>(null);
  const [dragState, setDragState] = useState<{
    draggedNodeId: string;
    dropTargetNodeId: string | null;
    placement: TreeDropPlacement | null;
  } | null>(null);
  const [refreshAnnouncement, setRefreshAnnouncement] = useState("");
  const [dragAnnouncement, setDragAnnouncement] = useState("");
  const [focusedNodeId, setFocusedNodeId] = useState("");
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const revealRevisionRef = useRef(0);
  const previousRefreshingRef = useRef(refreshing);
  useEffect(() => {
    const wasRefreshing = previousRefreshingRef.current;
    previousRefreshingRef.current = refreshing;
    if (wasRefreshing && !refreshing) {
      const timer = window.setTimeout(() => {
        setRefreshAnnouncement(t("refreshed", { title }));
      }, 0);
      const clearTimer = window.setTimeout(() => {
        setRefreshAnnouncement("");
      }, 2000);
      return () => {
        window.clearTimeout(timer);
        window.clearTimeout(clearTimer);
      };
    }
    return undefined;
  }, [refreshing, t, title]);
  const orderedNodes = useMemo(() => orderTreeNodes(resolvedNodes), [resolvedNodes]);
  const nodeById = useMemo(
    () => new Map(orderedNodes.map((node) => [node.id, node])),
    [orderedNodes],
  );
  const tooltip = useTreeRowTooltip({
    nodes: nodeById,
    now,
    disabled: Boolean(contextMenu || dragState),
    scopeKey: favoriteScopeKey,
  });
  const hiddenRootNodeIds = useMemo(
    () =>
      hideSystemRootNodes
        ? collectHiddenSystemRootNodeIds(orderedNodes, title)
        : new Set<string>(),
    [hideSystemRootNodes, orderedNodes, title],
  );
  const displayNodes = useMemo(
    () => orderedNodes.filter((node) => !hiddenRootNodeIds.has(node.id)),
    [hiddenRootNodeIds, orderedNodes],
  );
  const defaultExpandedNodeIds = useMemo(
    () =>
      displayNodes
        .filter((node) => nodeHasChildren(node))
        .map((node) => node.id),
    [displayNodes],
  );
  const expandedNodeIdSet = useMemo(
    () => new Set(expandedNodeIds ?? defaultExpandedNodeIds),
    [defaultExpandedNodeIds, expandedNodeIds],
  );
  const activePath = useMemo(
    () => collectActivePath(orderedNodes, activeTargetId, activeNodeId),
    [activeNodeId, activeTargetId, orderedNodes],
  );
  const visibleNodes = useMemo(
    () =>
      filterVisibleNodes(
        orderedNodes,
        expandedNodeIdSet,
        filterQuery,
        activePath.nodeIds,
        hiddenRootNodeIds,
      ),
    [
      activePath.nodeIds,
      expandedNodeIdSet,
      filterQuery,
      hiddenRootNodeIds,
      orderedNodes,
    ],
  );
  // The header badge counts content nodes only; folders are containers and
  // would contradict the main pane (e.g. "2" beside "No Context pages yet").
  const contentNodeCount = useMemo(
    () => countContentNodes(displayNodes),
    [displayNodes],
  );
  const visibleContentNodeCount = useMemo(
    () => countContentNodes(visibleNodes),
    [visibleNodes],
  );
  const filterPlaceholder = t("filter", { title });
  const treeLabel = t("treeLabel", { title });
  const createFolderEnabled = Boolean(
    onCreateFolder && canCreateFolder === true,
  );
  const createItemEnabled = Boolean(
    onCreateItem &&
    (canCreatePage === true || canCreateFolder === true || canUpload === true),
  );
  const anchoredReorderEnabled = Boolean(onReorderNodeAt);
  const reorderableNodeIds = useMemo(() => {
    const available = new Set<string>();
    if (!anchoredReorderEnabled || filterQuery.trim()) return available;
    const previousByParent = new Map<string | null, TreeNodePayload>();
    for (const node of orderedNodes) {
      const previous = previousByParent.get(node.parent_id);
      if (previous && !previous.is_system_node && !node.is_system_node) {
        available.add(previous.id);
        available.add(node.id);
      }
      previousByParent.set(node.parent_id, node);
    }
    return available;
  }, [anchoredReorderEnabled, filterQuery, orderedNodes]);

  function canOpenNodeContextMenu(node: TreeNodePayload) {
    return Boolean(
      onCopyNodeLink || onRenameNode ||
      (!node.is_system_node && (onDeleteNode || onMoveNode)) ||
      reorderableNodeIds.has(node.id) ||
      (node.can_have_children && (
        ((onCreateItem || onCreateFolder) && canCreateFolder === true) ||
        (onCreateItem && canCreatePage === true)
      )) ||
      (favoriteScope && favoriteTargetKind(node)),
    );
  }
  const resolvedExpandWidthLabel =
    expandWidthLabel ??
    (expandWidthAtMax ? t("resetWidth", { title }) : t("widen", { title }));
  // Rail controls are exposed as direct icon buttons (no "⋯" overflow menu):
  // WorkspacePanelChrome renders Refresh -> Widen/Narrow -> Hide inline in that
  // order, consistent across every rail.
  const focusedFallbackNodeId =
    visibleNodes.find(
      (node) =>
        (activeNodeId != null && node.id === activeNodeId) ||
        (activeTargetId != null && node.target_id === activeTargetId),
    )?.id ??
    visibleNodes[0]?.id ??
    "";
  const effectiveFocusedNodeId = visibleNodes.some(
    (node) => node.id === focusedNodeId,
  )
    ? focusedNodeId
    : focusedFallbackNodeId;

  const activeVisibleNodeId = visibleNodes.find(
    (node) =>
      (activeNodeId != null && node.id === activeNodeId) ||
      (activeTargetId != null && node.target_id === activeTargetId),
  )?.id;

  useEffect(() => {
    if (!activeVisibleNodeId) {
      return undefined;
    }
    const revision = ++revealRevisionRef.current;
    const frame = window.requestAnimationFrame(() => {
      if (revision !== revealRevisionRef.current) {
        return;
      }
      const row = rowRefs.current.get(activeVisibleNodeId);
      if (row?.isConnected) {
        row.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
    });
    return () => {
      revealRevisionRef.current += 1;
      window.cancelAnimationFrame(frame);
    };
  }, [activeVisibleNodeId]);

  function toggleExpanded(nodeId: string) {
    if (!onExpandedNodeIdsChange) {
      return;
    }
    const next = new Set(expandedNodeIdSet);
    if (next.has(nodeId)) {
      next.delete(nodeId);
    } else {
      next.add(nodeId);
    }
    onExpandedNodeIdsChange(Array.from(next));
  }

  function draggableNode(node: TreeNodePayload) {
    return Boolean(
      (onDropNode || anchoredReorderEnabled) && !node.is_system_node,
    );
  }

  function canReparentNodeOnTarget(
    draggedNodeId: string,
    targetNode: TreeNodePayload,
  ) {
    const draggedNode = nodeById.get(draggedNodeId);
    if (!draggedNode) {
      return false;
    }
    return (
      targetNode.can_have_children &&
      targetNode.id !== draggedNode.id &&
      targetNode.id !== draggedNode.parent_id &&
      targetNode.section === draggedNode.section &&
      !isTreeDescendant(orderedNodes, targetNode.id, draggedNode.id)
    );
  }

  function canInsertNodeAtTarget(
    draggedNodeId: string,
    targetNode: TreeNodePayload,
  ) {
    const draggedNode = nodeById.get(draggedNodeId);
    return Boolean(
      anchoredReorderEnabled &&
      !filterQuery.trim() &&
      draggedNode &&
      targetNode.id !== draggedNode.id &&
      !targetNode.is_system_node &&
      targetNode.visibility_status === "visible" &&
      targetNode.section === draggedNode.section &&
      targetNode.parent_id === draggedNode.parent_id,
    );
  }

  function dropPlacementForRow(
    draggedNodeId: string,
    targetNode: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ): TreeDropPlacement | null {
    const bounds = event.currentTarget.getBoundingClientRect();
    const relativeY =
      bounds.height > 0 ? event.clientY - bounds.top : bounds.height / 2;
    const ratio = bounds.height > 0 ? relativeY / bounds.height : 0.5;
    const canInsert = canInsertNodeAtTarget(draggedNodeId, targetNode);

    if (targetNode.can_have_children) {
      if (ratio < 1 / 3) {
        return canInsert ? "before" : null;
      }
      if (ratio > 2 / 3) {
        return canInsert ? "after" : null;
      }
      return canReparentNodeOnTarget(draggedNodeId, targetNode)
        ? "inside"
        : null;
    }
    if (!canInsert) {
      return null;
    }
    return ratio < 0.5 ? "before" : "after";
  }

  function draggedNodeIdFromEvent(event: ReactDragEvent<HTMLDivElement>) {
    return (
      event.dataTransfer.getData(TREE_NODE_DRAG_DATA) ||
      event.dataTransfer.getData("text/plain") ||
      dragState?.draggedNodeId ||
      ""
    );
  }

  function handleRowDragStart(
    node: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ) {
    if (!draggableNode(node)) {
      event.preventDefault();
      return;
    }
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(TREE_NODE_DRAG_DATA, node.id);
    event.dataTransfer.setData("text/plain", node.id);
    setDragState({
      draggedNodeId: node.id,
      dropTargetNodeId: null,
      placement: null,
    });
    setDragAnnouncement(t("moving", { title: node.title }));
  }

  function handleRowDragOver(
    node: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ) {
    const draggedNodeId = draggedNodeIdFromEvent(event);
    const placement = draggedNodeId
      ? dropPlacementForRow(draggedNodeId, node, event)
      : null;
    if (!draggedNodeId || !placement) {
      event.dataTransfer.dropEffect = "none";
      setDragState((current) =>
        current?.dropTargetNodeId === node.id
          ? { ...current, dropTargetNodeId: null, placement: null }
          : current,
      );
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setDragState((current) =>
      current?.draggedNodeId === draggedNodeId &&
      current.dropTargetNodeId === node.id &&
      current.placement === placement
        ? current
        : { draggedNodeId, dropTargetNodeId: node.id, placement },
    );
  }

  function handleRowDragLeave(
    node: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ) {
    if (
      event.currentTarget.contains(event.relatedTarget as Node | null) ||
      dragState?.dropTargetNodeId !== node.id
    ) {
      return;
    }
    setDragState((current) =>
      current?.dropTargetNodeId === node.id
        ? { ...current, dropTargetNodeId: null, placement: null }
        : current,
    );
  }

  async function performAnchoredReorder(
    node: TreeNodePayload,
    anchorNode: TreeNodePayload,
    placement: "before" | "after",
  ) {
    if (!onReorderNodeAt) {
      return;
    }
    setDragAnnouncement(t("moving", { title: node.title }));
    try {
      await onReorderNodeAt(node, anchorNode, placement);
      setDragAnnouncement(
        t(placement === "before" ? "landedBefore" : "landedAfter", {
          anchor: anchorNode.title,
          title: node.title,
        }),
      );
    } catch {
      setDragAnnouncement(t("moveFailed", { title: node.title }));
    }
  }

  async function performReparent(
    node: TreeNodePayload,
    parentNode: TreeNodePayload,
  ) {
    setDragAnnouncement(t("moving", { title: node.title }));
    try {
      await onDropNode?.(node, parentNode);
      setDragAnnouncement(
        t("landedInside", { parent: parentNode.title, title: node.title }),
      );
    } catch {
      setDragAnnouncement(t("moveFailed", { title: node.title }));
    }
  }

  function handleRowDrop(
    node: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ) {
    const draggedNodeId = draggedNodeIdFromEvent(event);
    const placement = draggedNodeId
      ? dropPlacementForRow(draggedNodeId, node, event)
      : null;
    if (!draggedNodeId || !placement) {
      setDragState(null);
      setDragAnnouncement("");
      return;
    }
    event.preventDefault();
    const draggedNode = nodeById.get(draggedNodeId);
    setDragState(null);
    if (!draggedNode) {
      return;
    }
    if (placement === "inside") {
      void performReparent(draggedNode, node);
      return;
    }
    void performAnchoredReorder(draggedNode, node, placement);
  }

  function handleRowDragEnd() {
    setDragState(null);
    setDragAnnouncement("");
  }

  function openContextMenu(
    node: TreeNodePayload,
    position: { left: number; top: number },
    invoker: HTMLElement | null,
  ) {
    if (!canOpenNodeContextMenu(node)) {
      return;
    }
    contextMenuInvokerRef.current = invoker;
    setContextMenu({
      node,
      position: clampContextMenuPosition(position),
      scopeKey: favoriteScopeKey,
    });
  }

  function handleContextMenuAction(action: TreeNodeContextMenuAction) {
    const invoker = contextMenuInvokerRef.current;
    contextMenuInvokerRef.current = null;
    setContextMenu(null);
    const restoreInvokerFocus = () => {
      window.requestAnimationFrame(() => {
        if (invoker?.isConnected) {
          invoker.focus();
          return;
        }
        rowRefs.current.get(action.node.id)?.focus();
      });
    };
    if (action.type === "new-item") {
      if (onCreateItem) {
        onCreateItem({
          itemType: action.itemType,
          parentNodeId: action.node.id,
        });
      } else if (action.itemType === "folder") {
        onCreateFolder?.(action.node.id);
      }
      return;
    }
    if (action.type === "rename") {
      onRenameNode?.(action.node);
      return;
    }
    if (action.type === "move") {
      onMoveNode?.(action.node);
      return;
    }
    if (action.type === "move-up" || action.type === "move-down") {
      const direction = action.type === "move-up" ? "up" : "down";
      const anchor = adjacentReorderAnchor(
        action.node,
        orderedNodes,
        direction,
      );
      if (anchor) {
        void performAnchoredReorder(
          action.node,
          anchor,
          direction === "up" ? "before" : "after",
        );
      }
      restoreInvokerFocus();
      return;
    }
    if (action.type === "delete") {
      onDeleteNode?.(action.node);
      return;
    }
    if (action.type === "favorite") {
      if (favoriteScope) {
        setFavoriteFailure(null);
        void toggleTreeFavorite(favoriteScope, action.node).catch(() => {
          if (currentScopeRef.current === favoriteScopeKey) {
            setFavoriteFailure({ scope: favoriteScopeKey, message: t("favorite.failed", { title: action.node.title }) });
          }
        });
      }
      restoreInvokerFocus();
      return;
    }
    onCopyNodeLink?.(action.node);
    restoreInvokerFocus();
  }

  function registerRowRef(nodeId: string) {
    return (element: HTMLDivElement | null) => {
      if (element) {
        rowRefs.current.set(nodeId, element);
        return;
      }
      rowRefs.current.delete(nodeId);
    };
  }

  function focusTreeNode(nodeId: string) {
    setFocusedNodeId(nodeId);
    rowRefs.current.get(nodeId)?.focus();
  }

  function invokePrimaryAction(node: TreeNodePayload) {
    if (onSelectTarget) {
      onSelectTarget(node);
      return;
    }
    if (nodeHasChildren(node) && onExpandedNodeIdsChange) {
      toggleExpanded(node.id);
    }
  }

  function handleTreeRowKeyDown(
    node: TreeNodePayload,
    event: KeyboardEvent<HTMLDivElement>,
  ) {
    if (event.target !== event.currentTarget) {
      return;
    }
    const currentIndex = Math.max(
      0,
      visibleNodes.findIndex((candidate) => candidate.id === node.id),
    );
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusTreeNode(
        visibleNodes[Math.min(currentIndex + 1, visibleNodes.length - 1)].id,
      );
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      focusTreeNode(visibleNodes[Math.max(currentIndex - 1, 0)].id);
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      focusTreeNode(visibleNodes[0].id);
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      focusTreeNode(visibleNodes[visibleNodes.length - 1].id);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      invokePrimaryAction(node);
      return;
    }
    if (event.shiftKey && event.key === "F10" && canOpenNodeContextMenu(node)) {
      event.preventDefault();
      const target = event.currentTarget.getBoundingClientRect();
      openContextMenu(
        node,
        {
          left: target.left + 24,
          top: target.top + 24,
        },
        event.currentTarget,
      );
      return;
    }
    if (
      nodeHasChildren(node) &&
      onExpandedNodeIdsChange &&
      event.key === "ArrowRight"
    ) {
      event.preventDefault();
      if (!expandedNodeIdSet.has(node.id)) {
        toggleExpanded(node.id);
        return;
      }
      const firstChild = visibleNodes.find(
        (candidate) => candidate.parent_id === node.id,
      );
      if (firstChild) {
        focusTreeNode(firstChild.id);
      }
      return;
    }
    if (event.key === "ArrowLeft") {
      if (
        nodeHasChildren(node) &&
        onExpandedNodeIdsChange &&
        expandedNodeIdSet.has(node.id)
      ) {
        event.preventDefault();
        toggleExpanded(node.id);
        return;
      }
      if (node.parent_id && rowRefs.current.has(node.parent_id)) {
        event.preventDefault();
        focusTreeNode(node.parent_id);
      }
    }
  }

  return (
    <aside className="tree-rail" data-accent={accent} aria-label={treeLabel}>
      <span
        className="visually-hidden"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {(favoriteFailure?.scope === favoriteScopeKey ? favoriteFailure.message : "") || refreshAnnouncement || dragAnnouncement}
      </span>
      <div className="tree-rail-header">
        <div className="panel-title-with-count">
          <span>{title}</span>
          <CountChip
            compact
            outOf={filterQuery ? contentNodeCount : null}
            value={filterQuery ? visibleContentNodeCount : contentNodeCount}
          />
        </div>
        <div className="tree-rail-header-actions">
          {createItemEnabled ? (
            <button
              aria-label={t("createItem", { title })}
              className="icon-button"
              onClick={() =>
                onCreateItem?.({
                  itemType:
                    canCreatePage === true
                      ? "page"
                      : canUpload === true
                        ? "upload"
                        : "folder",
                  parentNodeId: null,
                })
              }
              title={t("createItem", { title })}
              type="button"
            >
              <Plus size={14} aria-hidden="true" />
            </button>
          ) : createFolderEnabled ? (
            <button
              aria-label={t("createFolder", { title })}
              className="icon-button"
              onClick={() => onCreateFolder?.(null)}
              title={t("createFolder", { title })}
              type="button"
            >
              <FolderPlus size={14} aria-hidden="true" />
            </button>
          ) : null}
          {uploadAction ? (
            <button
              aria-busy={uploadAction.busy ?? false}
              aria-label={uploadAction.ariaLabel}
              className="icon-button compact-icon-button"
              disabled={uploadAction.disabled ?? uploadAction.busy}
              onClick={uploadAction.onClick}
              title={uploadAction.title ?? uploadAction.ariaLabel}
              type="button"
            >
              <Upload size={14} aria-hidden="true" />
              {uploadAction.label ? (
                <span className="visually-hidden">{uploadAction.label}</span>
              ) : null}
            </button>
          ) : null}
          {headerSlot}
          <WorkspacePanelChrome
            refresh={
              onRefresh
                ? {
                    label: refreshing
                      ? t("refreshing", { title })
                      : t("refresh", { title }),
                    onRefresh: () => onRefresh(),
                    refreshing,
                  }
                : undefined
            }
            expandWidth={
              onExpandWidth
                ? {
                    label: resolvedExpandWidthLabel,
                    onExpandWidth,
                    atMax: expandWidthAtMax,
                  }
                : undefined
            }
            close={
              onClose
                ? { onClose, label: t("hideExplorer", { title }) }
                : undefined
            }
          />
        </div>
      </div>
      <label className="tree-filter" data-tree-filter-scope={title}>
        <Search size={14} aria-hidden="true" />
        <input
          type="search"
          aria-label={filterPlaceholder}
          placeholder={filterPlaceholder}
          value={filterQuery}
          onChange={(event) => setFilterQuery(event.target.value)}
        />
      </label>
      <div className="tree-rail-body">
        {displayNodes.length === 0 ? (
          <div className="tree-empty">{emptyLabel}</div>
        ) : visibleNodes.length === 0 ? (
          <div className="tree-empty">{t("noMatches")}</div>
        ) : (
          <div
            className="tree-list"
            role="tree"
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) {
                setFocusedNodeId("");
              }
            }}
          >
            {visibleNodes.map((node) => (
              <TreeRow
                key={node.id}
                node={node}
                now={now}
                active={
                  (activeNodeId != null && node.id === activeNodeId) ||
                  (activeTargetId != null && node.target_id === activeTargetId)
                }
                activePath={activePath.nodeIds.has(node.id)}
                actionsOpen={contextMenu?.node.id === node.id}
                canCreateFolder={createFolderEnabled}
                canCreateItem={createItemEnabled}
                canCreatePage={canCreatePage === true}
                canOpenContextMenu={canOpenNodeContextMenu(node)}
                canToggleExpanded={Boolean(onExpandedNodeIdsChange)}
                draggable={draggableNode(node)}
                dragState={dragState}
                expanded={expandedNodeIdSet.has(node.id)}
                onCreateFolder={onCreateFolder}
                onCreateItem={onCreateItem}
                onDragEnd={handleRowDragEnd}
                onDragLeave={handleRowDragLeave}
                onDragOver={handleRowDragOver}
                onDragStart={handleRowDragStart}
                onDrop={handleRowDrop}
                onFocusNode={setFocusedNodeId}
                onKeyDown={handleTreeRowKeyDown}
                onOpenContextMenu={openContextMenu}
                refCallback={registerRowRef(node.id)}
                tabIndex={node.id === effectiveFocusedNodeId ? 0 : -1}
                onToggleExpanded={toggleExpanded}
                onSelectTarget={onSelectTarget}
                visualLevel={visualTreeLevel(node, nodeById, hiddenRootNodeIds)}
                tooltip={tooltip}
              />
            ))}
          </div>
        )}
      </div>
      {tooltip.popup}
      {contextMenu ? (
        <TreeNodeContextMenu
          canCreateFolder={Boolean(
            (onCreateItem || onCreateFolder) && canCreateFolder === true,
          )}
          canCreatePage={Boolean(onCreateItem && canCreatePage === true)}
          canCopyLink={Boolean(onCopyNodeLink)}
          canDelete={Boolean(onDeleteNode && !contextMenu.node.is_system_node)}
          canMove={Boolean(onMoveNode && !contextMenu.node.is_system_node)}
          canMoveUp={
            anchoredReorderEnabled &&
            !filterQuery.trim() &&
            reorderAvailability(contextMenu.node, orderedNodes).up
          }
          canMoveDown={
            anchoredReorderEnabled &&
            !filterQuery.trim() &&
            reorderAvailability(contextMenu.node, orderedNodes).down
          }
          canRename={Boolean(onRenameNode)}
          canFavorite={Boolean(favoriteScope && favoriteTargetKind(contextMenu.node))}
          favoritePending={Boolean(favoriteScope && favoriteSnapshot.get(treeFavoriteKey(favoriteScope, contextMenu.node))?.pending)}
          statusSummary={treeNodeStatusDescription(nodeById.get(contextMenu.node.id) ?? contextMenu.node, now, i18n.resolvedLanguage ?? i18n.language, t)}
          node={nodeById.get(contextMenu.node.id) ?? contextMenu.node}
          position={contextMenu.position}
          onAction={handleContextMenuAction}
          onClose={() => {
            const nodeId = contextMenu.node.id;
            const invoker = contextMenuInvokerRef.current;
            contextMenuInvokerRef.current = null;
            setContextMenu(null);
            window.requestAnimationFrame(() => {
              if (invoker?.isConnected) {
                invoker.focus();
                return;
              }
              rowRefs.current.get(nodeId)?.focus();
            });
          }}
        />
      ) : null}
    </aside>
  );
}

function TreeRow({
  node,
  now,
  active,
  activePath,
  actionsOpen,
  canCreateFolder,
  canCreateItem,
  canCreatePage,
  canOpenContextMenu,
  canToggleExpanded,
  draggable,
  dragState,
  expanded,
  onToggleExpanded,
  onCreateFolder,
  onCreateItem,
  onDragEnd,
  onDragLeave,
  onDragOver,
  onDragStart,
  onDrop,
  onFocusNode,
  onKeyDown,
  onOpenContextMenu,
  refCallback,
  tabIndex,
  onSelectTarget,
  visualLevel,
  tooltip,
}: {
  node: TreeNodePayload;
  now: number;
  active: boolean;
  activePath: boolean;
  actionsOpen: boolean;
  canCreateFolder: boolean;
  canCreateItem: boolean;
  canCreatePage: boolean;
  canOpenContextMenu: boolean;
  canToggleExpanded: boolean;
  draggable: boolean;
  dragState: {
    draggedNodeId: string;
    dropTargetNodeId: string | null;
    placement: TreeDropPlacement | null;
  } | null;
  expanded: boolean;
  onToggleExpanded: (nodeId: string) => void;
  onCreateFolder?: (parentNodeId: string | null) => void;
  onCreateItem?: (input: {
    itemType: NewItemType;
    parentNodeId: string | null;
  }) => void;
  onDragEnd: () => void;
  onDragLeave: (
    node: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ) => void;
  onDragOver: (
    node: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ) => void;
  onDragStart: (
    node: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ) => void;
  onDrop: (
    node: TreeNodePayload,
    event: ReactDragEvent<HTMLDivElement>,
  ) => void;
  onFocusNode: (nodeId: string) => void;
  onKeyDown: (
    node: TreeNodePayload,
    event: KeyboardEvent<HTMLDivElement>,
  ) => void;
  onOpenContextMenu: (
    node: TreeNodePayload,
    position: { left: number; top: number },
    invoker: HTMLElement | null,
  ) => void;
  refCallback: (element: HTMLDivElement | null) => void;
  tabIndex: number;
  onSelectTarget?: (node: TreeNodePayload) => void;
  visualLevel: number;
  tooltip: TreeRowTooltip;
}) {
  const { t, i18n } = useTranslation("tree");
  const longPressTimerRef = useRef<number | null>(null);
  const longPressStartRef = useRef<{ x: number; y: number } | null>(null);
  const longPressOpenedRef = useRef(false);
  const [isLongPressing, setIsLongPressing] = useState(false);
  const titleId = useId();
  const statusId = useId();
  const status = treeNodeStatus(node, now);
  const statusDescription = treeNodeStatusDescription(node, now, i18n.resolvedLanguage ?? i18n.language, t);
  const level = visualLevel;
  const Icon = node.node_kind === "folder" ? Folder : FileText;
  const hasChildren = nodeHasChildren(node);
  const hasCountBadge = node.children_count > 0;
  const canSelect = Boolean(onSelectTarget);
  const targetGroup = targetKindGroup(node.target_kind);
  const isDragging = dragState?.draggedNodeId === node.id;
  const isDropTarget = dragState?.dropTargetNodeId === node.id;
  const dropPlacement = isDropTarget ? dragState?.placement : null;
  const className = [
    "tree-row",
    active ? "active" : "",
    activePath ? "active-path" : "",
    hasChildren ? "has-children" : "",
    canSelect ? "selectable" : "",
    draggable ? "draggable" : "",
    node.is_system_node ? "system-node" : "",
    isLongPressing ? "tree-row-pressing" : "",
    isDragging ? "tree-row-dragging" : "",
    dropPlacement === "inside" ? "tree-row-drop-target" : "",
    dropPlacement === "before" ? "tree-row-insert-before" : "",
    dropPlacement === "after" ? "tree-row-insert-after" : "",
  ]
    .filter(Boolean)
    .join(" ");
  const rowProps = {
    className,
    "aria-expanded": hasChildren ? expanded : undefined,
    "aria-level": level,
    "aria-labelledby": titleId,
    "aria-describedby": statusDescription ? statusId : undefined,
    "aria-grabbed": draggable ? isDragging : undefined,
    style: { "--tree-level": level } as TreeRowStyle,
    "data-active-path": activePath ? "true" : "false",
    "data-actions-open": actionsOpen ? "true" : undefined,
    "data-draggable": draggable ? "true" : "false",
    "data-drop-target": isDropTarget ? "true" : "false",
    "data-drop-placement": dropPlacement ?? undefined,
    "data-node-id": node.id,
    "data-node-kind": node.node_kind,
    "data-primary-placement": node.primary_placement ? "true" : "false",
    "data-system-node": node.is_system_node ? "true" : "false",
    "data-target-id": node.target_id ?? undefined,
    "data-target-kind": node.target_kind ?? undefined,
    "data-document-state": status ?? undefined,
    "data-visibility-status": node.visibility_status,
  };

  function clearLongPressTimer() {
    if (longPressTimerRef.current !== null) {
      window.clearTimeout(longPressTimerRef.current);
      longPressTimerRef.current = null;
    }
    longPressStartRef.current = null;
    setIsLongPressing(false);
  }

  function startLongPressContextMenu(event: PointerEvent<HTMLDivElement>) {
    if (
      !canOpenContextMenu ||
      event.pointerType === "mouse" ||
      (typeof event.button === "number" && event.button !== 0) ||
      isInteractiveTreeRowTarget(event.target)
    ) {
      return;
    }
    const position = {
      left: event.clientX,
      top: event.clientY,
    };
    const invoker = event.currentTarget;
    longPressStartRef.current = { x: event.clientX, y: event.clientY };
    longPressOpenedRef.current = false;
    setIsLongPressing(true);
    longPressTimerRef.current = window.setTimeout(() => {
      longPressTimerRef.current = null;
      longPressOpenedRef.current = true;
      setIsLongPressing(false);
      if (
        typeof navigator !== "undefined" &&
        typeof navigator.vibrate === "function"
      ) {
        try {
          navigator.vibrate(10);
        } catch {
          // ignore vibration errors (unsupported platforms)
        }
      }
      onOpenContextMenu(node, position, invoker);
    }, LONG_PRESS_CONTEXT_MENU_MS);
  }

  function updateLongPressContextMenu(event: PointerEvent<HTMLDivElement>) {
    const start = longPressStartRef.current;
    if (!start) {
      return;
    }
    const moved =
      Math.abs(event.clientX - start.x) > LONG_PRESS_MOVE_CANCEL_PX ||
      Math.abs(event.clientY - start.y) > LONG_PRESS_MOVE_CANCEL_PX;
    if (moved) {
      clearLongPressTimer();
    }
  }

  useEffect(() => clearLongPressTimer, []);

  const rowContent = (
    <>
      {level > 1 ? (
        <span className="tree-row-indent-guides" aria-hidden="true">
          {Array.from({ length: level - 1 }, (_, index) => (
            <span
              className="tree-row-indent-guide"
              key={index}
              style={{ "--tree-guide-level": index + 1 } as TreeIndentGuideStyle}
            />
          ))}
        </span>
      ) : null}
      {hasChildren ? (
        <button
          className="tree-row-chevron"
          type="button"
          aria-label={t(expanded ? "collapse" : "expand", {
            title: node.title,
          })}
          aria-expanded={expanded}
          data-tree-chevron="true"
          disabled={!canToggleExpanded}
          onClick={(event) => {
            event.stopPropagation();
            onToggleExpanded(node.id);
          }}
        >
          {expanded ? (
            <ChevronDown size={14} aria-hidden="true" />
          ) : (
            <ChevronRight size={14} aria-hidden="true" />
          )}
        </button>
      ) : (
        <span className="tree-row-spacer" aria-hidden="true" />
      )}
      <span className="tree-row-document-icon" aria-hidden="true">
        <Icon className={node.node_kind === "folder" ? "tree-row-icon-folder" : ""} size={15} />
        {favoriteTargetKind(node) && node.is_favorite ? <Star className="tree-row-favorite" size={11} /> : null}
      </span>
      <span className="tree-row-label">
        {targetGroup ? (
          <span
            className={`tree-row-state-dot ${status ? `tree-row-state-${status}` : `lc-dot-${targetGroup}`}`}
            data-target-group={targetGroup}
            data-document-state={status ?? undefined}
            aria-hidden="true"
          >
            {status === "review" ? <svg className="tree-row-review-clock" viewBox="0 0 12 12" width="7" height="7">
              <circle cx="6" cy="6" r="6" fill="currentColor" />
              <path d="M6 2.5V6L8.3 7.3" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
            </svg> : null}
          </span>
        ) : null}
        <span
          id={titleId}
          className="tree-row-title"
          aria-label={node.title}
        >
          {node.title}
        </span>
        {node.is_system_node ? (
          <span
            className="tree-row-system-badge"
            title={t("systemFolder")}
            aria-label={t("systemFolder")}
          >
            <BadgeCheck size={12} aria-hidden="true" />
          </span>
        ) : null}
        {hasCountBadge ? (
          <span
            className="tree-row-child-count"
            aria-label={t("childPages", { count: node.children_count })}
          >
            {node.children_count}
          </span>
        ) : null}
      </span>
      {statusDescription ? <span className="visually-hidden tree-row-description" id={statusId}>{statusDescription}</span> : null}
      {(canCreateItem && onCreateItem && node.can_have_children) ||
      (canCreateFolder && onCreateFolder && node.can_have_children) ||
      canOpenContextMenu ? (
        <span className="tree-row-actions">
          {canCreateItem && onCreateItem && node.can_have_children ? (
            <button
              aria-label={t("createUnder", { title: node.title })}
              className="tree-row-add"
              onClick={(event) => {
                event.stopPropagation();
                onCreateItem({
                  itemType: canCreatePage ? "page" : "folder",
                  parentNodeId: node.id,
                });
              }}
              title={t("createUnder", { title: node.title })}
              type="button"
            >
              <Plus size={13} aria-hidden="true" />
            </button>
          ) : canCreateFolder && onCreateFolder && node.can_have_children ? (
            <button
              aria-label={t("createFolderUnder", { title: node.title })}
              className="tree-row-add"
              onClick={(event) => {
                event.stopPropagation();
                onCreateFolder(node.id);
              }}
              title={t("createFolderUnder", { title: node.title })}
              type="button"
            >
              <Plus size={13} aria-hidden="true" />
            </button>
          ) : null}
          {canOpenContextMenu ? (
            <button
              aria-label={t("openActions", { title: node.title })}
              aria-expanded={actionsOpen}
              aria-haspopup="menu"
              className="tree-row-menu-button"
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                const target = event.currentTarget.getBoundingClientRect();
                onOpenContextMenu(
                  node,
                  {
                    left: target.right - 6,
                    top: target.bottom + 4,
                  },
                  event.currentTarget,
                );
              }}
            >
              <MoreHorizontal size={13} aria-hidden="true" />
            </button>
          ) : null}
        </span>
      ) : null}
    </>
  );
  return (
    <div
      {...rowProps}
      aria-selected={active}
      draggable={draggable}
      ref={refCallback}
      role="treeitem"
      tabIndex={tabIndex}
      onClick={(event) => {
        if (longPressOpenedRef.current) {
          event.preventDefault();
          longPressOpenedRef.current = false;
          return;
        }
        if (onSelectTarget) {
          onSelectTarget(node);
          return;
        }
        if (hasChildren && canToggleExpanded) {
          onToggleExpanded(node.id);
        }
      }}
      onContextMenu={(event) => {
        if (!canOpenContextMenu) {
          return;
        }
        event.preventDefault();
        onOpenContextMenu(
          node,
          {
            left: event.clientX,
            top: event.clientY,
          },
          event.currentTarget,
        );
      }}
      onFocus={(event) => {
        tooltip.focus(node.id, event.currentTarget);
        onFocusNode(node.id);
      }}
      onBlur={(event) => {
        tooltip.blur(event.relatedTarget);
      }}
      onMouseEnter={(event) => {
        tooltip.enter(node.id, event.currentTarget, event);
      }}
      onMouseLeave={tooltip.leave}
      onDragEnd={onDragEnd}
      onDragLeave={(event) => onDragLeave(node, event)}
      onDragOver={(event) => onDragOver(node, event)}
      onDragStart={(event) => onDragStart(node, event)}
      onDrop={(event) => onDrop(node, event)}
      onKeyDown={(event) => {
        onKeyDown(node, event);
      }}
      onPointerCancel={clearLongPressTimer}
      onPointerDown={startLongPressContextMenu}
      onPointerLeave={clearLongPressTimer}
      onPointerMove={updateLongPressContextMenu}
      onPointerUp={clearLongPressTimer}
    >
      {rowContent}
    </div>
  );
}

function isInteractiveTreeRowTarget(target: EventTarget) {
  return (
    target instanceof Element && Boolean(target.closest("button, input, a"))
  );
}

function clampContextMenuPosition(position: { left: number; top: number }) {
  if (typeof window === "undefined") {
    return position;
  }
  const margin = 8;
  return {
    left: Math.max(
      margin,
      Math.min(position.left, Math.max(window.innerWidth - margin, margin)),
    ),
    top: Math.max(
      margin,
      Math.min(position.top, Math.max(window.innerHeight - margin, margin)),
    ),
  };
}

function orderTreeNodes(nodes: TreeNodePayload[]) {
  const byParent = new Map<string, TreeNodePayload[]>();
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const roots: TreeNodePayload[] = [];
  for (const node of nodes) {
    if (node.parent_id && byId.has(node.parent_id)) {
      const siblings = byParent.get(node.parent_id) ?? [];
      siblings.push(node);
      byParent.set(node.parent_id, siblings);
    } else {
      roots.push(node);
    }
  }

  // Codepoint comparison, not locale collation: the backend orders siblings
  // with Python tuple comparison, and reorder swaps act on that order. Locale
  // tiebreaks ("alpha" < "Beta") would make Move up/down target the wrong row.
  const compareValues = (left: string, right: string) =>
    left < right ? -1 : left > right ? 1 : 0;
  const compareNodes = (left: TreeNodePayload, right: TreeNodePayload) =>
    compareValues(left.sort_key, right.sort_key) ||
    compareValues(left.title, right.title) ||
    compareValues(left.id, right.id);
  roots.sort(compareNodes);
  for (const siblings of byParent.values()) {
    siblings.sort(compareNodes);
  }

  const ordered: TreeNodePayload[] = [];
  const visit = (node: TreeNodePayload) => {
    ordered.push(node);
    for (const child of byParent.get(node.id) ?? []) {
      visit(child);
    }
  };
  for (const root of roots) {
    visit(root);
  }
  return ordered;
}

function filterVisibleNodes(
  nodes: TreeNodePayload[],
  expandedNodeIds: Set<string>,
  filterQuery: string,
  activePathNodeIds: Set<string>,
  hiddenRootNodeIds: Set<string>,
) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const normalizedQuery = normalizeFilterValue(filterQuery);
  if (normalizedQuery) {
    const includedNodeIds = new Set(activePathNodeIds);
    for (const node of nodes) {
      if (matchesNodeFilter(node, normalizedQuery)) {
        addNodeAndAncestors(node, byId, includedNodeIds);
      }
    }
    return nodes.filter(
      (node) =>
        !hiddenRootNodeIds.has(node.id) &&
        includedNodeIds.has(node.id) &&
        areAncestorsIncluded(node, byId, includedNodeIds, hiddenRootNodeIds),
    );
  }
  return nodes.filter((node) => {
    if (hiddenRootNodeIds.has(node.id)) {
      return false;
    }
    let parentId = node.parent_id;
    while (parentId) {
      if (!hiddenRootNodeIds.has(parentId) && !expandedNodeIds.has(parentId)) {
        return false;
      }
      parentId = byId.get(parentId)?.parent_id ?? null;
    }
    return true;
  });
}

function collectActivePath(
  nodes: TreeNodePayload[],
  activeTargetId?: string | null,
  activeNodeId?: string | null,
) {
  const nodeIds = new Set<string>();
  if (!activeTargetId && !activeNodeId) {
    return { nodeIds };
  }

  const byId = new Map(nodes.map((node) => [node.id, node]));
  const activeNode =
    (activeNodeId ? byId.get(activeNodeId) : null) ??
    nodes.find((node) => node.target_id === activeTargetId);
  if (!activeNode) {
    return { nodeIds };
  }

  nodeIds.add(activeNode.id);
  let parentId = activeNode.parent_id;
  while (parentId) {
    nodeIds.add(parentId);
    parentId = byId.get(parentId)?.parent_id ?? null;
  }
  return { nodeIds };
}

function addNodeAndAncestors(
  node: TreeNodePayload,
  byId: Map<string, TreeNodePayload>,
  includedNodeIds: Set<string>,
) {
  includedNodeIds.add(node.id);
  let parentId = node.parent_id;
  while (parentId) {
    includedNodeIds.add(parentId);
    parentId = byId.get(parentId)?.parent_id ?? null;
  }
}

function areAncestorsIncluded(
  node: TreeNodePayload,
  byId: Map<string, TreeNodePayload>,
  includedNodeIds: Set<string>,
  hiddenRootNodeIds: Set<string>,
) {
  let parentId = node.parent_id;
  while (parentId) {
    if (!hiddenRootNodeIds.has(parentId) && !includedNodeIds.has(parentId)) {
      return false;
    }
    parentId = byId.get(parentId)?.parent_id ?? null;
  }
  return true;
}

function visualTreeLevel(
  node: TreeNodePayload,
  byId: Map<string, TreeNodePayload>,
  hiddenRootNodeIds: Set<string>,
) {
  let level = 1;
  let parentId = node.parent_id;
  while (parentId) {
    if (!hiddenRootNodeIds.has(parentId)) {
      level += 1;
    }
    parentId = byId.get(parentId)?.parent_id ?? null;
  }
  return level;
}

function isTreeDescendant(
  nodes: TreeNodePayload[],
  candidateNodeId: string,
  ancestorNodeId: string,
) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  let parentId = byId.get(candidateNodeId)?.parent_id ?? null;
  while (parentId) {
    if (parentId === ancestorNodeId) {
      return true;
    }
    parentId = byId.get(parentId)?.parent_id ?? null;
  }
  return false;
}

// A node can move up/down only among non-system siblings: it must not be a
// system node itself, and the neighbour it would swap with must not be one
// either, so system folders stay pinned at the top. orderedNodes is already in
// sort order, so filtering by parent preserves sibling order.
function reorderAvailability(
  node: TreeNodePayload,
  orderedNodes: TreeNodePayload[],
): { up: boolean; down: boolean } {
  if (node.is_system_node) {
    return { up: false, down: false };
  }
  const siblings = orderedNodes.filter(
    (candidate) => candidate.parent_id === node.parent_id,
  );
  const index = siblings.findIndex((candidate) => candidate.id === node.id);
  if (index < 0) {
    return { up: false, down: false };
  }
  const previous = index > 0 ? siblings[index - 1] : null;
  const next = siblings[index + 1] ?? null;
  return {
    up: Boolean(previous && !previous.is_system_node),
    down: Boolean(next && !next.is_system_node),
  };
}

function adjacentReorderAnchor(
  node: TreeNodePayload,
  orderedNodes: TreeNodePayload[],
  direction: "up" | "down",
) {
  const siblings = orderedNodes.filter(
    (candidate) => candidate.parent_id === node.parent_id,
  );
  const index = siblings.findIndex((candidate) => candidate.id === node.id);
  const anchor = direction === "up" ? siblings[index - 1] : siblings[index + 1];
  return anchor && !anchor.is_system_node ? anchor : null;
}

function matchesNodeFilter(node: TreeNodePayload, normalizedQuery: string) {
  return [
    node.title,
    node.slug,
    node.target_kind,
    node.visibility_status,
    ...node.breadcrumb.map((item) => item.title),
  ]
    .filter(Boolean)
    .some((value) => normalizeFilterValue(value).includes(normalizedQuery));
}

function normalizeFilterValue(value: string | null | undefined) {
  return value?.trim().toLowerCase() ?? "";
}

function nodeHasChildren(node: TreeNodePayload) {
  return node.has_children ?? node.children_count > 0;
}

function countContentNodes(nodes: TreeNodePayload[]) {
  return nodes.reduce(
    (count, node) => (node.node_kind === "folder" ? count : count + 1),
    0,
  );
}

function targetKindGroup(targetKind: string | null) {
  if (!targetKind) {
    return null;
  }
  if (targetKind.startsWith("library")) {
    return "library";
  }
  if (targetKind.startsWith("context")) {
    return "context";
  }
  if (targetKind.startsWith("agent")) {
    return "agent";
  }
  return null;
}
