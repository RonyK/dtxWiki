import {
  BookOpen,
  Download,
  FileArchive,
  FileText,
  Pencil,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import type { TFunction } from "i18next";
import {
  type ChangeEvent,
  type DragEvent,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError } from "../../shared/api/client";
import { translate } from "../../shared/i18n";
import {
  addLibraryItemVersion,
  deleteLibraryItem,
  fetchLibraryOriginal,
  getLibraryCapabilities,
  isProtectedLibraryOriginalUrl,
  libraryOriginalRequiresWarning,
  UNAVAILABLE_LIBRARY_CAPABILITIES,
  libraryUploadAccept,
  listLibraryItemReferences,
  listLibraryItems,
  LibraryUploadApiError,
  matchingLibraryFormat,
  readLibraryItem,
  readLibraryArtifact,
  readLibraryVersion,
  retryLibraryConversion,
  uploadLibraryFile,
} from "../../shared/api/library";
import type {
  LibraryCapabilities,
  LibraryItemDetail,
  LibraryItemReference,
  LibraryItemSummary,
  LibraryArtifactPayload,
  LibraryItemVersionPayload,
} from "../../shared/api/library";
import {
  createTreeFolder,
  deleteTreeNode,
  listTreeData,
  readTreeNode,
  moveTreeNode,
  reorderTreeNodeAt,
  updateTreeNode,
} from "../../shared/api/workspace";
import type {
  TreeNodePayload,
  TreeSectionPolicy,
} from "../../shared/api/workspace";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { WorkspaceFirstUseChecklist } from "../../shared/components/FirstUseChecklist";
import {
  NewItemDialog,
  type NewItemType,
} from "../../shared/components/NewItemDialog";
import {
  DeleteTreeNodeDialog,
  MoveTreeNodeDialog,
  RenameTreeNodeDialog,
} from "../../shared/components/TreeNodeActionDialogs";
import { HistoryCard } from "../../shared/components/HistoryCard";
import { HistoryGraphList } from "../../shared/components/HistoryGraphList";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import { ReaderTabs } from "../../shared/components/ReaderTabs";
import { RecentItemsList } from "../../shared/components/RecentItemsList";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { TreeRail } from "../../shared/components/TreeRail";
import { WorkspaceLayout } from "../../shared/components/WorkspaceLayout";
import { targetBreadcrumbs } from "../../shared/components/treeBreadcrumbs";
import {
  resolveExpandedNodeIds,
  TREE_EXPANDED_PARAM,
  writeExpandedNodeIdsToStorage,
} from "../../shared/components/treeRouteState";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { useDocumentTitle } from "../../shared/hooks/useDocumentTitle";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { domainLabel } from "../../shared/lib/domainLabel";
import { buildHistoryGraph } from "../../shared/lib/historyGraph";
import { isAbortError } from "../../shared/lib/abortError";
import { createUuidV4 } from "../../shared/lib/uuidV4";
import { libraryHistoryNodes } from "./libraryHistory";
import { contextPageHref, useRouteWorkspaceId, useWorkspaceRoute, workspacePath, workspaceTargetHref, withWorkspaceQuery } from "../../shared/lib/workspaceRoutes";
import {
  UploadQueue,
  type ResilientUploadQueueStatus,
} from "../../shared/media/uploadQueue";
import { isEditorRole } from "../../shared/workspace/permissions";
import { useOptionalAuth } from "../../shared/auth/AuthContext";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { ArtifactChatsSection } from "../mcp-chats/ArtifactChatsSection";
import { LibrarySemanticMarkdownEditor } from "./LibrarySemanticMarkdownEditor";
import { NotFoundScreen } from "../errors/NotFoundScreen";
import {
  currentVersionStatusKey,
  previewVersionId,
  PreviewPane,
} from "./preview/PreviewPane";

type LibraryState = {
  workspaceId: string;
  capabilities: LibraryCapabilities;
  items: LibraryItemSummary[];
  itemsCursor: string | null;
  itemsTotal: number | null;
  itemsHasMore: boolean;
  tree: TreeNodePayload[];
  treePolicies: Record<string, TreeSectionPolicy>;
  detail: LibraryItemDetail | null;
  selectedArtifactId: string | null;
  selectedArtifact: LibraryArtifactPayload | null;
  selectedArtifactVersion: LibraryItemVersionPayload | null;
};

type UploadStatus = ResilientUploadQueueStatus;

type UploadEntry = {
  id: string;
  file: File;
  operationId: string;
  parentNodeId: string | null;
  status: UploadStatus;
  message?: string;
  retryable?: boolean;
  retryAfterSeconds?: number | null;
  retryAvailableAt?: number | null;
};

const LIBRARY_NEW_ITEM_TYPE_ORDER: readonly NewItemType[] = [
  "upload",
  "folder",
];

class LibraryRouteTargetNotFoundError extends Error {}

export function LibraryScreen() {
  const { t } = useTranslation("library");
  const routeWorkspaceId = useRouteWorkspaceId();
  const route = useWorkspaceRoute();
  const location = useLocation();
  const navigate = useNavigate();
  // Codex #25: hide write affordances from viewers (the backend re-enforces).
  const auth = useOptionalAuth();
  const workspaceContext = useOptionalWorkspaceContext();
  const canEdit = isEditorRole(workspaceContext?.selectedWorkspace?.role);
  const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadEntries, setUploadEntries] = useState<UploadEntry[]>([]);
  const [renameNode, setRenameNode] = useState<TreeNodePayload | null>(null);
  const [moveNode, setMoveNode] = useState<TreeNodePayload | null>(null);
  const [deleteNode, setDeleteNode] = useState<TreeNodePayload | null>(null);
  const [deleteItem, setDeleteItem] = useState<LibraryItemDetail | null>(null);
  const [errorActionLink, setErrorActionLink] = useState<string | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  // Fixed load-more page size — the page-size selector was removed workspace-wide.
  const pageSize = 10;
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadQueueRef = useRef<UploadQueue<UploadEntry, true> | null>(null);
  const uploadRunGenerationRef = useRef(0);
  const uploadRouteIdentityRef = useRef(location.pathname);
  const uploadWorkspaceIdRef = useRef<string | null>(routeWorkspaceId);
  const uploadParentNodeIdRef = useRef<string | null>(null);
  const uploadSequenceRef = useRef(0);
  // Uploads select their item locally before the router commits the matching
  // target param; while this ref is set the URL is known-stale, so the
  // reconcile effect below must not clear errors or refetch from it.
  const localTargetIdRef = useRef<string | null>(null);
  // A detail 404 removes a stale row and clears the route. Keep the failed id
  // fenced during the render before that route update commits, otherwise the
  // bare-id resolver immediately issues the same doomed request again and
  // clears the explanatory notice.
  const unavailableTargetIdRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    uploadRouteIdentityRef.current = location.pathname;
  }, [location.pathname]);
  useLayoutEffect(() => {
    uploadWorkspaceIdRef.current = routeWorkspaceId;
    return () => {
      uploadWorkspaceIdRef.current = null;
      uploadRunGenerationRef.current += 1;
    };
  }, [routeWorkspaceId]);
  useEffect(() => {
    uploadRunGenerationRef.current += 1;
    uploadQueueRef.current = null;
    setIsUploading(false);
    setUploadEntries([]);
    setErrorActionLink(null);
  }, [routeWorkspaceId]);
  const [routeNotFoundKey, setRouteNotFoundKey] = useState<string | null>(null);
  useEffect(() => {
    setRouteNotFoundKey(null);
  }, [routeWorkspaceId, route?.suffix, route?.targetId]);
  // A route target expands its ancestors once. Subsequent tree/detail refreshes
  // must preserve an intentional user collapse instead of reopening the path.
  const revealedRouteTargetRef = useRef<string | null>(null);
  const expandedWorkspaceRef = useRef<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTargetId = route?.targetId ?? null;
  const {
    data: state,
    error,
    reload,
    setData: setState,
    setError,
  } = useAsyncLoad<LibraryState>(
    async () => {
      const workspaceId = routeWorkspaceId;
      const [itemsPage, treeData, capabilities] = await Promise.all([
        listLibraryItems(workspaceId, { limit: pageSize }),
        listTreeData(workspaceId, "library"),
        getLibraryCapabilities(workspaceId).catch(() => UNAVAILABLE_LIBRARY_CAPABILITIES),
      ]);
      const items = itemsPage.data;
      const tree = treeData.nodes;
      if (route?.targetKind === "folder" && route.targetId) {
        let folder = tree.find((node) => node.id === route.targetId);
        if (!folder) {
          try {
            folder = await readTreeNode(workspaceId, route.targetId);
          } catch (exc) {
            if (exc instanceof ApiError && exc.status === 404) {
              throw new LibraryRouteTargetNotFoundError();
            }
            throw exc;
          }
        }
        if (folder.section !== "library" || folder.node_kind !== "folder") {
          throw new LibraryRouteTargetNotFoundError();
        }
      }
      let artifact: LibraryArtifactPayload | null = null;
      let referencedVersion: LibraryItemVersionPayload | null = null;
      let selectedItemId: string | null;
      if (route?.targetKind === "artifact" && route.targetId) {
        try {
          artifact = await readLibraryArtifact(workspaceId, route.targetId);
          referencedVersion = await readLibraryVersion(workspaceId, artifact.library_item_version_id);
        } catch (exc) {
          if (exc instanceof ApiError && exc.status === 404) throw new LibraryRouteTargetNotFoundError();
          throw exc;
        }
        selectedItemId = referencedVersion.library_item_id;
      } else if (route?.targetKind === "item" && route.targetId) {
        selectedItemId = route.targetId;
      } else {
        selectedItemId = resolveRequestedLibraryItemId(items, tree, requestedTargetId);
      }
      const detail = selectedItemId
        ? await readLibraryItem(workspaceId, selectedItemId).catch((exc) => {
            if ((route?.targetKind === "item" || route?.targetKind === "artifact") && exc instanceof ApiError && exc.status === 404) {
              throw new LibraryRouteTargetNotFoundError();
            }
            throw exc;
          })
        : null;
      return {
        workspaceId,
        capabilities,
        items,
        itemsCursor: itemsPage.next_cursor,
        itemsTotal: itemsPage.total,
        itemsHasMore: itemsPage.has_more,
        tree,
        treePolicies: treeData.policies,
        detail,
        selectedArtifactId: artifact?.id ?? null,
        selectedArtifact: artifact,
        selectedArtifactVersion: referencedVersion,
      };
    },
    [routeWorkspaceId, route?.targetKind === "artifact" ? route.targetId : null, pageSize, t],
    {
      errorMessage: t("errors.load"),
      formatError: (exc, fallback) => {
        if (exc instanceof LibraryRouteTargetNotFoundError) {
          setRouteNotFoundKey(`${routeWorkspaceId}:${route?.suffix ?? "library"}`);
          return "Library target not found";
        }
        return libraryErrorMessage(exc, fallback);
      },
      fallbackData: {
        workspaceId: "",
        capabilities: UNAVAILABLE_LIBRARY_CAPABILITIES,
        items: [],
        itemsCursor: null,
        itemsTotal: null,
        itemsHasMore: false,
        tree: [],
        treePolicies: {},
        detail: null,
        selectedArtifactId: null,
        selectedArtifact: null,
        selectedArtifactVersion: null,
      },
      onSuccess: (nextState) => {
        const libraryTree = libraryExplorerTreeNodes(nextState.tree);
        const selectedNode = requestedTargetId
          ? (libraryTree.find(
              (node) =>
                node.id === requestedTargetId ||
                node.target_id === requestedTargetId,
            ) ?? null)
          : null;
        const selectedNodeId = selectedNode?.id ?? null;
        const revealKey = selectedNodeId
          ? `${nextState.workspaceId}:${selectedNodeId}`
          : null;
        const workspaceChanged =
          expandedWorkspaceRef.current !== nextState.workspaceId;
        const validNodeIds = new Set(libraryTree.map((node) => node.id));
        setExpandedNodeIds((current) => {
          if (workspaceChanged) {
            return resolveExpandedNodeIds(
              libraryTree,
              searchParams.get(TREE_EXPANDED_PARAM),
              selectedNodeId,
              { defaultMode: "none", storageKey: "library", workspaceId: routeWorkspaceId },
            );
          }
          if (revealKey && revealedRouteTargetRef.current !== revealKey) {
            return resolveExpandedNodeIds(
              libraryTree,
              current.join(","),
              selectedNodeId,
              { defaultMode: "none" },
            );
          }
          return current.filter((nodeId) => validNodeIds.has(nodeId));
        });
        expandedWorkspaceRef.current = nextState.workspaceId;
        revealedRouteTargetRef.current = revealKey;
      },
    },
  );

  const libraryWorkspaceId = state?.workspaceId ?? "";
  // The loader fetches page 1 (state.items); these accumulate the appended
  // pages on top of it. Keyed on the base page so a fresh first page (page-size
  // change / upload / delete) drops the appended tail. Derived synchronously so
  // Load more always reads the live cursor rather than a one-render-stale one.
  const overviewBaseKey = `${libraryWorkspaceId}:${pageSize}:${
    state?.items.length ?? 0
  }:${state?.items[0]?.id ?? ""}`;
  const [appended, setAppended] = useState<{
    key: string;
    items: LibraryItemSummary[];
    cursor: string | null;
  }>({ key: overviewBaseKey, items: [], cursor: state?.itemsCursor ?? null });
  const [loadingMore, setLoadingMore] = useState(false);
  useEffect(() => {
    setAppended({
      key: overviewBaseKey,
      items: [],
      cursor: state?.itemsCursor ?? null,
    });
  }, [overviewBaseKey, state?.itemsCursor]);
  const routedItemId = route?.targetKind === "item"
    ? route.targetId
    : route?.targetKind === "artifact"
      ? state?.selectedArtifactId === route.targetId
        ? state.selectedArtifactVersion?.library_item_id ?? null
        : null
      : null;
  const artifactRouteMatchesState = route?.targetKind !== "artifact" ||
    state?.selectedArtifactId === route.targetId;
  const selectedDetail = routedItemId && state?.detail?.id === routedItemId
    ? state.detail
    : null;
  const readerDetail = selectedDetail && route?.targetKind === "artifact" && state?.selectedArtifactVersion
    ? libraryDetailAtArtifactVersion(
        selectedDetail,
        state.selectedArtifactVersion,
        state.selectedArtifact,
      )
    : selectedDetail;
  useEffect(() => {
    if (!selectedDetail) {
      return;
    }
    setState((current) => {
      if (
        current?.workspaceId !== libraryWorkspaceId ||
        current.detail !== selectedDetail
      ) {
        return current;
      }
      const items = updateLibraryItemSummaries(current.items, selectedDetail);
      return items === current.items ? current : { ...current, items };
    });
    setAppended((current) => {
      if (current.key !== overviewBaseKey) {
        return current;
      }
      const items = updateLibraryItemSummaries(current.items, selectedDetail);
      return items === current.items ? current : { ...current, items };
    });
  }, [libraryWorkspaceId, overviewBaseKey, selectedDetail, setState]);

  // When the accumulator key matches the current base its cursor is
  // authoritative (it starts at the base cursor and advances/exhausts to null);
  // only fall back to the base cursor during the one render before the reset
  // effect re-keys the accumulator to a fresh first page.
  const appendedForBase = appended.key === overviewBaseKey ? appended : null;
  const overviewItems = [
    ...(state?.items ?? []),
    ...(appendedForBase?.items ?? []),
  ];
  const overviewCursor = appendedForBase
    ? appendedForBase.cursor
    : (state?.itemsCursor ?? null);
  const overviewHasMore = overviewCursor !== null;
  const overviewTotal = state?.itemsTotal ?? null;

  async function loadMoreOverview() {
    if (!overviewCursor || loadingMore) {
      return;
    }
    const requestCursor = overviewCursor;
    setLoadingMore(true);
    try {
      const next = await listLibraryItems(libraryWorkspaceId, {
        limit: pageSize,
        cursor: requestCursor,
      });
      setAppended((current) =>
        // Skip the append when a fresh first page replaced the base mid-flight.
        // The base key must still match AND the live cursor must still be the
        // one this request paged from: the key alone is a length+first-id
        // heuristic, so a reload that preserves it while advancing the cursor
        // would otherwise graft a stale page on. Matches useLoadMore's
        // next_cursor guard so all four list surfaces gate identically.
        current.key === overviewBaseKey && current.cursor === requestCursor
          ? {
              key: current.key,
              items: [...current.items, ...next.data],
              cursor: next.next_cursor,
            }
          : current,
      );
    } finally {
      setLoadingMore(false);
    }
  }

  function selectLibraryItem(libraryItemId: string) {
    if (!state) {
      return;
    }
    setErrorActionLink(null);
    setError(null);
    navigate(withWorkspaceQuery(workspaceTargetHref(routeWorkspaceId, "library", libraryItemId, "item"), searchParams));
  }

  useEffect(() => {
    if (!state?.workspaceId) {
      return;
    }
    if (route?.targetKind === "artifact") {
      return;
    }
    const targetNode = requestedTargetId
      ? (state.tree.find(
          (node) =>
            node.id === requestedTargetId ||
            node.target_id === requestedTargetId,
        ) ?? null)
      : null;
    if (targetNode) {
      const revealKey = `${state.workspaceId}:${targetNode.id}`;
      if (revealedRouteTargetRef.current !== revealKey) {
        revealedRouteTargetRef.current = revealKey;
        setExpandedNodeIds((current) => {
          const next = resolveExpandedNodeIds(
            state.tree,
            current.join(","),
            targetNode.id,
            { defaultMode: "none" },
          );
          if (
            next.length === current.length &&
            next.every((nodeId, index) => nodeId === current[index])
          ) {
            return current;
          }
          writeExpandedNodeIdsToStorage("library", next, routeWorkspaceId);
          return next;
        });
      }
    } else {
      revealedRouteTargetRef.current = null;
    }
    if (unavailableTargetIdRef.current) {
      if (requestedTargetId === unavailableTargetIdRef.current) {
        return;
      }
      unavailableTargetIdRef.current = null;
    }
    const selectedItemId = route?.targetKind === "item"
      ? route.targetId
      : resolveRequestedLibraryItemId(
      state.items,
      state.tree,
      requestedTargetId,
    );
    if (localTargetIdRef.current) {
      if (selectedItemId !== localTargetIdRef.current) {
        return;
      }
      localTargetIdRef.current = null;
    }
    if (!selectedItemId) {
      if (state.detail) {
        setState((current) =>
          current ? { ...current, detail: null } : current,
        );
      }
      return;
    }
    if (state.detail?.id === selectedItemId) {
      return;
    }

    let cancelled = false;
    setErrorActionLink(null);
    setError(null);
    void readLibraryItem(state.workspaceId, selectedItemId)
      .then((detail) => {
        if (cancelled) {
          return;
        }
        setState((current) => (current ? { ...current, detail } : current));
      })
      .catch((exc) => {
        if (cancelled) {
          return;
        }
        if (exc instanceof ApiError && exc.status === 404) {
          unavailableTargetIdRef.current = requestedTargetId ?? selectedItemId;
          setAppended((current) => ({
            ...current,
            items: current.items.filter(
              (item) => item.id !== selectedItemId,
            ),
          }));
          setState((current) =>
            current
              ? {
                  ...current,
                  items: current.items.filter(
                    (item) => item.id !== selectedItemId,
                  ),
                  itemsTotal:
                    current.itemsTotal === null
                      ? null
                      : Math.max(0, current.itemsTotal - 1),
                  tree: current.tree.filter(
                    (node) => node.target_id !== selectedItemId,
                  ),
                  detail: null,
                }
              : current,
          );
          navigate(withWorkspaceQuery(workspacePath(routeWorkspaceId, "library"), searchParams), { replace: true });
          setError(
            t("errors.itemUnavailable", {
              defaultValue:
                "That Library item is no longer available. It was removed from the list.",
            }),
          );
          return;
        }
        setError(libraryErrorMessage(exc, t("errors.loadDetail")));
      });
    return () => {
      cancelled = true;
    };
  }, [navigate, requestedTargetId, route?.targetKind, route?.targetId, routeWorkspaceId, searchParams, setError, setSearchParams, setState, state, t]);

  useEffect(() => {
    if (route?.targetKind !== "folder" || !route.targetId || !state?.workspaceId) return;
    const existing = state.tree.find((node) => node.id === route.targetId);
    if (existing) {
      if (existing.section !== "library" || existing.node_kind !== "folder") {
        setRouteNotFoundKey(`${routeWorkspaceId}:${route.suffix}`);
        setError("Library folder not found");
      }
      return;
    }
    let cancelled = false;
    void readTreeNode(routeWorkspaceId, route.targetId).then((node) => {
      if (!cancelled && (node.section !== "library" || node.node_kind !== "folder")) {
        setRouteNotFoundKey(`${routeWorkspaceId}:${route.suffix}`);
        setError("Library folder not found");
      }
    }).catch((exc) => {
      if (!cancelled && exc instanceof ApiError && exc.status === 404) {
        setRouteNotFoundKey(`${routeWorkspaceId}:${route.suffix}`);
        setError("Library folder not found");
      } else if (!cancelled) {
        setError(libraryErrorMessage(exc, t("errors.load")));
      }
    });
    return () => { cancelled = true; };
  }, [route?.suffix, route?.targetId, route?.targetKind, routeWorkspaceId, setError, state?.tree, state?.workspaceId, t]);

  const selectedProcessingStatus = selectedDetail
    ? libraryProcessingStatus(selectedDetail)
    : null;
  const shouldPollSelectedItem = isPollingStatus(selectedProcessingStatus);
  useEffect(() => {
    const workspaceId = state?.workspaceId;
    const itemId = selectedDetail?.id;
    const currentVersionId = selectedDetail?.current_version?.id ?? null;
    if (!workspaceId || !itemId || !shouldPollSelectedItem) {
      return;
    }
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    let delayMs = 1_000;

    const schedule = (delay: number) => {
      if (cancelled || document.visibilityState === "hidden") return;
      if (timeoutId) clearTimeout(timeoutId);
      timeoutId = setTimeout(() => void poll(), delay);
    };
    const poll = async () => {
      if (cancelled || document.visibilityState === "hidden") return;
      try {
        const detail = await readLibraryItem(workspaceId, itemId);
        if (cancelled) return;
        setState((current) =>
          current?.workspaceId === workspaceId &&
          current.detail?.id === itemId &&
          (current.detail.current_version?.id ?? null) === currentVersionId
            ? { ...current, detail }
            : current,
        );
        if (isPollingStatus(libraryProcessingStatus(detail))) {
          delayMs = Math.min(delayMs * 2, 10_000);
          schedule(delayMs);
        }
      } catch {
        delayMs = Math.min(delayMs * 2, 10_000);
        schedule(delayMs);
      }
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        if (timeoutId) clearTimeout(timeoutId);
        timeoutId = null;
      } else {
        schedule(0);
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    schedule(delayMs);
    return () => {
      cancelled = true;
      if (timeoutId) clearTimeout(timeoutId);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [
    setState,
    shouldPollSelectedItem,
    selectedDetail?.current_version?.id,
    selectedDetail?.id,
    state?.workspaceId,
  ]);

  async function createFolder(input: {
    parentNodeId: string | null;
    slug?: string;
    title: string;
  }) {
    if (!state) {
      return;
    }
    const originPath = location.pathname;
    if (input.parentNodeId && !state.tree.some((node) => node.id === input.parentNodeId && node.section === "library" && node.can_have_children)) {
      setError(t("errors.folderParentUnavailable", { defaultValue: "The selected folder is unavailable" }));
      return;
    }
    clearError();
    const folder = await createTreeFolder(state.workspaceId, {
      parent_id: input.parentNodeId,
      section: "library",
      slug: input.slug,
      title: input.title,
    });
    if (
      uploadRouteIdentityRef.current !== originPath ||
      routeWorkspaceId !== state.workspaceId
    ) return;
    const nextExpandedNodeIds = expandNodeIds(
      expandedNodeIds,
      folder.parent_id,
    );
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    navigate(withWorkspaceQuery(workspaceTargetHref(routeWorkspaceId, "library", folder.id, "folder"), searchParams), { replace: true });
    setState((current) =>
      current
        ? {
            ...current,
            tree: [...current.tree, folder],
            detail: null,
          }
        : current,
    );
  }

  async function moveTreeNodeToParent(
    node: TreeNodePayload,
    newParentId: string,
    reason: string,
  ) {
    if (!state) {
      return;
    }
    clearError();
    await moveTreeNode(state.workspaceId, {
      node_id: node.id,
      new_parent_id: newParentId,
      reason,
    });
    await reload();
  }

  async function moveTreeNodeByDrop(
    node: TreeNodePayload,
    newParentNode: TreeNodePayload,
  ) {
    try {
      await moveTreeNodeToParent(
        node,
        newParentNode.id,
        "Drag-and-drop move in Library explorer",
      );
    } catch (exc) {
      setError(libraryErrorMessage(exc, t("errors.moveTreeNode")));
    }
  }

  async function reorderTreeNodeToAnchor(
    node: TreeNodePayload,
    anchorNode: TreeNodePayload,
    placement: "before" | "after",
  ) {
    if (!state) {
      return;
    }
    clearError();
    try {
      await reorderTreeNodeAt(
        state.workspaceId,
        node.id,
        anchorNode.id,
        placement,
      );
      await reload();
    } catch (exc) {
      setError(libraryErrorMessage(exc, t("errors.reorderTreeNode")));
      // Rethrow so TreeRail's live region announces the failed move rather
      // than a landing.
      throw exc;
    }
  }

  async function renameTreeNodeById(
    node: TreeNodePayload,
    displayName: string,
  ) {
    if (!state) {
      return;
    }
    clearError();
    await updateTreeNode(state.workspaceId, node.id, {
      display_name: displayName,
    });
    await reload();
  }

  async function deleteTreeNodeById(node: TreeNodePayload) {
    if (!state) {
      return;
    }
    clearError();
    await deleteTreeNode(state.workspaceId, node.id);
    // A folder delete can archive items that exist only in the Load-more
    // tail. The reloaded first page may keep the same key and cursor, so reset
    // the tail explicitly instead of relying on the base-page effect to infer
    // that destructive mutation.
    setAppended({
      key: overviewBaseKey,
      items: [],
      cursor: state.itemsCursor,
    });
    await reload();
  }

  function copyTreeNodeLink(node: TreeNodePayload) {
    const path = workspaceTargetHref(routeWorkspaceId, "library", node.target_id ?? node.id, node.target_kind === "library_artifact" ? "artifact" : node.node_kind === "folder" ? "folder" : "item");
    const href =
      typeof window === "undefined" ? path : `${window.location.origin}${path}`;
    void navigator.clipboard?.writeText(href);
  }

  async function handleFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";
    const parentNodeId = uploadParentNodeIdRef.current;
    uploadParentNodeIdRef.current = null;
    await uploadFiles(files, parentNodeId);
  }

  async function uploadFiles(files: File[], parentNodeId?: string | null): Promise<boolean> {
    if (!state || files.length === 0) {
      return false;
    }
    let entries: UploadEntry[];
    try {
      entries = files.map((file) => ({
        id: `${Date.now()}-${uploadSequenceRef.current++}`,
        file,
        operationId: createUuidV4(),
        parentNodeId: parentNodeId ?? null,
        status: "queued" as const,
      }));
    } catch {
      setErrorActionLink(null);
      setError(t("errors.upload"));
      return false;
    }
    setUploadEntries(entries);
    return runUploadEntries(entries, location.pathname);
  }

  async function retryUpload(id: string, retryClock: number) {
    if (!state || isUploading) {
      return;
    }
    const entry = uploadEntries.find((candidate) => candidate.id === id);
    if (
      !entry ||
      !["failed", "cancelled", "outcome-unknown"].includes(entry.status) ||
      (entry.status === "failed" && entry.retryable !== true)
    ) {
      return;
    }
    if (
      entry.retryAvailableAt !== null &&
      entry.retryAvailableAt !== undefined &&
      retryClock < entry.retryAvailableAt
    ) {
      return;
    }
    const retryEntry: UploadEntry = {
      ...entry,
      status: "queued",
      message: undefined,
      retryable: undefined,
      retryAfterSeconds: null,
      retryAvailableAt: null,
    };
    setUploadEntries((current) =>
      current.map((candidate) => (candidate.id === id ? retryEntry : candidate)),
    );
    await runUploadEntries([retryEntry], location.pathname);
  }

  async function runUploadEntries(entries: UploadEntry[], originRouteIdentity: string): Promise<boolean> {
    if (!state || entries.length === 0) {
      return false;
    }
    const originWorkspaceId = state.workspaceId;
    const runGeneration = ++uploadRunGenerationRef.current;
    const isCurrentRun = () =>
      uploadRunGenerationRef.current === runGeneration &&
      uploadWorkspaceIdRef.current === originWorkspaceId &&
      routeWorkspaceId === originWorkspaceId;
    const isOriginRoute = () =>
      isCurrentRun() && uploadRouteIdentityRef.current === originRouteIdentity;
    setIsUploading(true);
    clearError();
    const failures: { file: File; error: unknown }[] = [];
    const uploadedById = new Map<string, LibraryItemDetail>();
    const errorsById = new Map<string, unknown>();
    try {
      const batchLimit = state.capabilities.max_files_per_batch;
      const queue = new UploadQueue<UploadEntry, true>({
        async upload(entry, signal) {
          const index = entries.findIndex((candidate) => candidate.id === entry.id);
          const validationError = validateLibraryUploadFile(entry.file, state.capabilities, t);
          if (validationError) {
            const error = new LibraryUploadValidationError(validationError);
            errorsById.set(entry.id, error);
            throw error;
          }
          if (index >= batchLimit) {
            const error = new LibraryUploadValidationError(t("upload.errors.batchLimit", {
              count: batchLimit,
              defaultValue: "Select no more than {{count}} files at once.",
            }));
            errorsById.set(entry.id, error);
            throw error;
          }
          try {
            uploadedById.set(entry.id, await uploadLibraryFile(
              state.workspaceId,
              entry.file,
              {
                operationId: entry.operationId,
                parentNodeId: entry.parentNodeId,
                signal,
              },
            ));
          } catch (error) {
            errorsById.set(entry.id, error);
            throw error;
          }
        },
        errorMessage: (error) =>
          libraryUploadError(error, t("errors.upload"), routeWorkspaceId).message,
        onChange: (entry) => {
          if (!isCurrentRun()) return;
          const failure = entry.status === "failed"
            ? libraryUploadRetryMetadata(errorsById.get(entry.id))
            : null;
          updateUploadEntry(
            entry.id,
            entry.status,
            entry.message,
            failure?.retryable,
            failure?.retryAfterSeconds,
            failure?.retryAvailableAt,
          );
        },
      }, { activeCancelOutcomeUnknown: true });
      uploadQueueRef.current = queue;
      await Promise.all(entries.map((entry) => queue.enqueue(entry.id, entry)));
      for (const entry of queue.snapshot()) {
        if (entry.status === "failed") {
          failures.push({
            file: entry.value.file,
            error: errorsById.get(entry.id) ?? new Error(entry.message ?? "Upload failed."),
          });
        }
      }
      if (!isCurrentRun()) return false;
      const lastUploaded = Array.from(uploadedById.values()).at(-1) ?? null;
      let uploadedHref: string | null = null;
      if (lastUploaded) {
        const uploaded = lastUploaded;
        uploadedHref = workspaceTargetHref(routeWorkspaceId, "library", uploaded.id, "item");
        try {
          const [itemsPage, treeData] = await Promise.all([
            listLibraryItems(state.workspaceId, { limit: pageSize }),
            listTreeData(state.workspaceId, "library"),
          ]);
          if (!isCurrentRun()) return false;
          const tree = treeData.nodes;
          const libraryTree = libraryExplorerTreeNodes(tree);
          const selectUploaded = isOriginRoute();
          if (selectUploaded) localTargetIdRef.current = uploaded.id;
          setState((current) =>
            current
              ? {
                  ...current,
                  items: itemsPage.data,
                  itemsCursor: itemsPage.next_cursor,
                  itemsTotal: itemsPage.total,
                  itemsHasMore: itemsPage.has_more,
                  tree,
                  treePolicies: treeData.policies,
                  ...(selectUploaded ? { detail: uploaded } : {}),
                }
              : current,
          );
          if (selectUploaded) {
            const uploadedNode = libraryTree.find(
              (node) => node.target_id === uploaded.id,
            );
            setStoredExpandedNodeIds(
              expandNodeIds(expandedNodeIds, uploadedNode?.parent_id ?? null),
            );
          }
        } catch {
          if (!isCurrentRun()) return false;
          if (isOriginRoute()) {
            localTargetIdRef.current = uploaded.id;
            setState((current) =>
              current ? { ...current, detail: uploaded } : current,
            );
          }
          setError(
            t("upload.refreshFailed", {
              defaultValue:
                "File upload succeeded, but Library could not be refreshed. Reload to see the latest item.",
            }),
          );
        }
      }
      if (!isCurrentRun()) return false;
      if (failures.length > 0) {
        reportUploadFailures(entries.length, failures);
      }
      if (uploadedHref && isOriginRoute()) {
        navigate(uploadedHref, { replace: true });
      }
      return Boolean(lastUploaded);
    } catch (exc) {
      if (!isCurrentRun()) return false;
      const uploadError = libraryUploadError(exc, t("errors.upload"), routeWorkspaceId);
      setErrorActionLink(uploadError.settingsLink);
      setError(uploadError.message);
      return false;
    } finally {
      if (uploadRunGenerationRef.current === runGeneration) {
        uploadQueueRef.current = null;
        setIsUploading(false);
      }
    }
  }

  function updateUploadEntry(
    id: string,
    status: UploadStatus,
    message?: string,
    retryable?: boolean,
    retryAfterSeconds?: number | null,
    retryAvailableAt?: number | null,
  ) {
    setUploadEntries((current) =>
      current.map((entry) =>
        entry.id === id
          ? {
              ...entry,
              status,
              message,
              retryable,
              retryAfterSeconds,
              retryAvailableAt,
            }
          : entry,
      ),
    );
  }

  function cancelUpload(id: string) {
    uploadQueueRef.current?.cancel(id);
  }

  function reportUploadFailures(
    totalCount: number,
    failures: { file: File; error: unknown }[],
  ) {
    const uploadErrors = failures.map(({ error }) =>
      libraryUploadError(error, t("errors.upload"), routeWorkspaceId),
    );
    setErrorActionLink(
      uploadErrors.find((entry) => entry.settingsLink)?.settingsLink ?? null,
    );
    if (totalCount === 1) {
      setError(uploadErrors[0].message);
      return;
    }
    setError(
      t("upload.partialFailure", {
        defaultValue:
          "Failed to upload {{failedCount}} of {{totalCount}} files. {{details}}",
        failedCount: failures.length,
        totalCount,
        details: failures
          .map(
            ({ file }, index) => `${file.name}: ${uploadErrors[index].message}`,
          )
          .join("; "),
      }),
    );
  }

  function handleDragOver(event: DragEvent<HTMLElement>) {
    if (!canUpload || !event.dataTransfer.types.includes("Files")) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setIsDragOver(true);
  }

  function handleDragLeave(event: DragEvent<HTMLElement>) {
    const nextTarget = event.relatedTarget;
    if (
      nextTarget instanceof Node &&
      event.currentTarget.contains(nextTarget)
    ) {
      return;
    }
    setIsDragOver(false);
  }

  function handleDrop(event: DragEvent<HTMLElement>) {
    if (!canUpload) {
      return;
    }
    event.preventDefault();
    setIsDragOver(false);
    const files = Array.from(event.dataTransfer.files ?? []);
    void uploadFiles(files);
  }

  function clearError() {
    setErrorActionLink(null);
    setError(null);
  }

  function setStoredExpandedNodeIds(nodeIds: string[]) {
    setExpandedNodeIds(nodeIds);
    writeExpandedNodeIdsToStorage("library", nodeIds, routeWorkspaceId);
  }

  useDocumentTitle(
    selectedDetail ? libraryItemDisplayTitle(selectedDetail) : null,
    " - ",
  );

  if (routeNotFoundKey === `${routeWorkspaceId}:${route?.suffix ?? "library"}`) {
    return <NotFoundScreen />;
  }
  if (!state) {
    return <RouteLoading label={t("loading")} />;
  }
  if (route?.targetKind === "artifact" && !artifactRouteMatchesState) {
    if (error) {
      return (
        <section className="feature-main">
          <StatusBanner
            tone="warning"
            action={<button className="icon-button" onClick={reload} type="button">{t("workspaceResolution.retry", { ns: "shell", defaultValue: "Retry" })}</button>}
          >
            {error}
          </StatusBanner>
        </section>
      );
    }
    return <RouteLoading label={t("loading")} />;
  }
  const libraryTree = libraryExplorerTreeNodes(state.tree);
  const libraryPolicy = state.treePolicies.library;
  const uploadAccept = libraryUploadAccept(state.capabilities);
  const capabilitiesUnavailable =
    state.capabilities.capability_revision === "unavailable";
  const canUpload = canEdit && route?.targetKind !== "artifact" && state.capabilities.formats.some((format) => format.upload);
  // Named once: the rail's "+" uses it to decide whether the new-item dialog
  // has a choice worth showing, and the dialog itself gates the folder branch
  // on the same policy.
  const canCreateLibraryFolder =
    canEdit && (libraryPolicy?.allow_user_folder_create ?? false);
  const folderCreateRoute = Boolean(route?.create && route.suffix === "library/folders/new");
  const uploadCreateRoute = Boolean(route?.create && route.suffix === "library/items/new");
  const routeFolderParentId = folderCreateRoute ? searchParams.get("parent") : null;
  const routeFolderParent = routeFolderParentId
    ? libraryTree.find((node) => node.id === routeFolderParentId && node.section === "library" && node.can_have_children)
    : null;
  const routeActionParentId = (folderCreateRoute || uploadCreateRoute) ? searchParams.get("parent") : null;
  const routeActionParentValid = !routeActionParentId || libraryTree.some((node) => node.id === routeActionParentId && node.section === "library" && node.can_have_children);
  const activeCreateDialog = (
    uploadCreateRoute && routeActionParentValid ? { initialItemType: "upload" as const, parentNodeId: routeActionParentId }
      : folderCreateRoute && (!routeFolderParentId || routeFolderParent)
        ? { initialItemType: "folder" as const, parentNodeId: routeFolderParentId }
        : null
  );
  const detailFormatCapability = selectedDetail
    ? findLibraryFormatCapability(selectedDetail, state.capabilities)
    : null;
  const createDialogParent = activeCreateDialog?.parentNodeId
    ? (libraryTree.find((node) => node.id === activeCreateDialog.parentNodeId) ??
      null)
    : null;
  // F-5: folder destinations the new-folder dialog's location picker can target.
  const libraryFolderNodes = state.tree.filter(
    (node) => node.can_have_children,
  );
  const selectedNode = requestedTargetId
    ? (libraryTree.find(
        (node) =>
          node.id === requestedTargetId || node.target_id === requestedTargetId,
      ) ?? null)
    : null;

  function pickFiles(parentNodeId: string | null = null) {
    uploadParentNodeIdRef.current = parentNodeId;
    fileInputRef.current?.click();
  }

  return (
    <>
      <WorkspaceLayout
        defaultInspectorOpen={false}
        inspectorLabel={t("layout.details")}
        layoutKey="library"
        leftLabel={t("layout.explorer")}
        left={({
          expandWidthLabel,
          expandWidthAtMax,
          onClose,
          onExpandWidth,
        }) => (
          <TreeRail
            favoriteScope={auth?.user && state.workspaceId ? { actorId: auth.user.user_id, workspaceId: state.workspaceId } : undefined}
            title={t("libraryName")}
            accent="library"
            nodes={libraryTree}
            activeTargetId={selectedDetail?.id}
            activeNodeId={selectedNode?.id ?? null}
            expandedNodeIds={expandedNodeIds}
            forceActivePathExpanded={false}
            emptyLabel={t("treeEmpty")}
            hideSystemRootNodes
            expandWidthLabel={expandWidthLabel}
            expandWidthAtMax={expandWidthAtMax}
            canCreateFolder={canCreateLibraryFolder}
            canCreatePage={false}
            canUpload={canUpload}
            onCreateItem={
              canEdit
                ? ({ itemType, parentNodeId }) => {
                    if (!canCreateLibraryFolder) {
                      pickFiles(parentNodeId);
                      return;
                    }
                    // The dialog earns its step only when it has a choice to
                    // offer. With folder creation disabled by policy, upload is
                    // the only type left and NewItemDialog would render no
                    // segmented control -- so go straight to the file picker
                    // and keep uploading a one-click action, as it was before
                    // the rail's "+" learned to offer folders.
                    const destination = itemType === "folder" && canCreateLibraryFolder
                      ? "library/folders/new"
                      : "library/items/new";
                    const query = parentNodeId ? `?parent=${encodeURIComponent(parentNodeId)}` : "";
                    navigate(`${workspacePath(routeWorkspaceId, destination)}${query}`);
                  }
                : undefined
            }
            onCopyNodeLink={copyTreeNodeLink}
            onDeleteNode={canEdit ? setDeleteNode : undefined}
            onDropNode={canEdit ? moveTreeNodeByDrop : undefined}
            onMoveNode={canEdit ? setMoveNode : undefined}
            onReorderNodeAt={canEdit ? reorderTreeNodeToAnchor : undefined}
            onRenameNode={canEdit ? setRenameNode : undefined}
            onClose={onClose}
            onExpandWidth={onExpandWidth}
            onExpandedNodeIdsChange={(nodeIds) => {
              setStoredExpandedNodeIds(nodeIds);
            }}
            onRefresh={reload}
            onSelectTarget={(node) => {
              if (node.target_kind === "library_item" && node.target_id) {
                selectLibraryItem(node.target_id);
                return;
              }
              navigate(withWorkspaceQuery(workspaceTargetHref(routeWorkspaceId, "library", node.target_id ?? node.id, node.target_kind === "library_artifact" ? "artifact" : "folder"), searchParams));
            }}
          />
        )}
        main={
          <section
            className={`feature-main feature-main--reader library-main ${isDragOver ? "feature-main--drag-over" : ""}`}
            onDragLeave={handleDragLeave}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
          >
            {canUpload ? (
              <input
                ref={fileInputRef}
                aria-label={t("upload.title")}
                hidden
                tabIndex={-1}
                type="file"
                accept={uploadAccept}
                multiple
                onChange={handleFileInputChange}
              />
            ) : null}
            {isDragOver ? (
              <div className="upload-drop-overlay" aria-hidden="true">
                <Upload size={32} />
                <span>{t("upload.drop")}</span>
              </div>
            ) : null}
            <Breadcrumbs
              items={targetBreadcrumbs({
                workspaceId: routeWorkspaceId,
                basePath: workspacePath(routeWorkspaceId, "library"),
                rootLabel: t("libraryName"),
                nodes: libraryTree,
                targetId: selectedDetail?.id,
                targetNodeId: selectedNode?.id,
                fallbackLabel: selectedDetail
                  ? libraryItemDisplayTitle(selectedDetail)
                  : undefined,
              })}
            />
            {folderCreateRoute && routeFolderParentId && !routeFolderParent ? (
              <StatusBanner tone="warning">{t("errors.folderParentUnavailable", { defaultValue: "The selected folder is unavailable" })}</StatusBanner>
            ) : null}
            {uploadCreateRoute && routeActionParentId && !routeActionParentValid ? (
              <StatusBanner tone="warning">{t("errors.folderParentUnavailable", { defaultValue: "The selected folder is unavailable" })}</StatusBanner>
            ) : null}
            {!selectedDetail ? (
              <div className="feature-header">
                <div>
                  <p className="eyebrow">
                    {t("overview.eyebrow", { defaultValue: "Reference store" })}
                  </p>
                  <h1>{t("overview.title", { defaultValue: "Library" })}</h1>
                </div>
                <div className="feature-header-actions">
                  {canUpload && overviewItems.length > 0 ? (
                      <button
                        className="icon-button primary"
                        disabled={isUploading}
                        type="button"
                        onClick={() => pickFiles()}
                      >
                        <Upload size={16} aria-hidden="true" />
                        <span>
                          {isUploading
                            ? t("upload.uploading")
                            : t("upload.action")}
                        </span>
                      </button>
                  ) : null}
                </div>
              </div>
            ) : null}
            {capabilitiesUnavailable && canEdit ? (
              <StatusBanner
                tone="warning"
                action={
                  <button
                    className="icon-button text-button"
                    onClick={reload}
                    type="button"
                  >
                    {t("upload.reloadCapabilities", {
                      defaultValue: "Reload policy",
                    })}
                  </button>
                }
              >
                {t("upload.errors.capabilitiesUnavailable", {
                  defaultValue:
                    "Upload policy could not be verified. Upload is disabled until you retry.",
                })}
              </StatusBanner>
            ) : null}
            {error ? (
              <StatusBanner
                tone="warning"
                action={
                  errorActionLink ? (
                    <Link
                      className="icon-button text-button"
                      to={errorActionLink}
                    >
                      {t("upload.openPolicySettings")}
                    </Link>
                  ) : undefined
                }
              >
                {error}
              </StatusBanner>
            ) : null}
            <UploadStatusRegion
              entries={uploadEntries}
              retryDisabled={isUploading}
              onCancel={cancelUpload}
              onRetry={retryUpload}
            />
            {!selectedDetail ? (
              overviewItems.length === 0 ? (
                <LibraryList
                  activeId={null}
                  onSelect={selectLibraryItem}
                  onUpload={
                    canUpload ? () => pickFiles() : undefined
                  }
                  items={[]}
                />
              ) : (
                <RecentItemsList
                  kind="library"
                  items={overviewItems.map((item) => ({
                    ...item,
                    // RecentItem.title is non-null; Library items can lack a
                    // title, so fall back to the uri (also shown as subtitle).
                    title: libraryItemDisplayTitle(item),
                    lifecycle_status:
                      libraryProcessingStatus(item) ?? item.lifecycle_status,
                  }))}
                  total={overviewTotal}
                  hasMore={overviewHasMore}
                  loading={loadingMore}
                  onLoadMore={loadMoreOverview}
                  onSelect={selectLibraryItem}
                  activeId={null}
                  headingLabel={t("panel.recentHeading", {
                    defaultValue: "Recent updates",
                  })}
                  emptyHeading={t("overview.empty", {
                    defaultValue: "No Library items yet",
                  })}
                  loadMoreLabel={t("overview.loadMore", {
                    defaultValue: "Load more",
                  })}
                  loadingMoreLabel={t("overview.loadingMore", {
                    defaultValue: "Loading…",
                  })}
                  noun={t("panel.items", {
                    count: overviewTotal ?? overviewItems.length,
                    defaultValue_one: "item",
                    defaultValue_other: "items",
                  })}
                  renderRow={(item, isActive) => (
                    <button
                      aria-pressed={isActive}
                      className={`entity-row selectable-row ${
                        isActive ? "active" : ""
                      }`}
                      key={item.id}
                      type="button"
                      onClick={() => selectLibraryItem(item.id)}
                    >
                      <BookOpen size={16} aria-hidden="true" />
                      <div>
                        <strong>{libraryItemDisplayTitle(item)}</strong>
                        {sourceSubtitle(item) ? <p>{sourceSubtitle(item)}</p> : null}
                      </div>
                      <span>{domainLabel("library", "connectorType", item.connector_type)}</span>
                      <StatusPill
                        status={libraryProcessingStatus(item) ?? item.lifecycle_status}
                        tone={libraryStatusTone(
                          libraryProcessingStatus(item) ?? item.lifecycle_status,
                        )}
                        label={domainLabel(
                          "library",
                          "processingStatus",
                          libraryProcessingStatus(item) ?? item.lifecycle_status,
                        )}
                        compact
                      />
                    </button>
                  )}
                />
              )
            ) : null}
            <LibraryReader
              canEdit={canEdit && route?.targetKind !== "artifact"}
              canUpload={canUpload}
              capabilities={state.capabilities}
              detail={readerDetail}
              key={`${state.workspaceId}:${readerDetail?.id ?? "none"}:${readerDetail?.current_version?.id ?? "none"}`}
              workspaceId={state.workspaceId}
              previewKind={detailFormatCapability?.preview_kind ?? null}
              onConversionRetried={(retriedDetail) => {
                const retryWorkspaceId = state.workspaceId;
                setState((current) =>
                  current?.workspaceId === retryWorkspaceId &&
                  current.detail?.id === retriedDetail.id &&
                  current.detail.current_version?.id ===
                    retriedDetail.current_version?.id
                    ? {
                        ...current,
                        detail: retriedDetail,
                      }
                    : current,
                );
              }}
              onDelete={() => {
                if (selectedDetail) {
                  setDeleteItem(selectedDetail);
                }
              }}
              onVersionAdded={(updatedDetail) => {
                const uploadWorkspaceId = state.workspaceId;
                setState((current) =>
                  current?.workspaceId === uploadWorkspaceId &&
                  current.detail?.id === updatedDetail.id
                    ? { ...current, detail: updatedDetail }
                    : current,
                );
              }}
            />
          </section>
        }
        inspector={
          readerDetail ? <LibraryInspector detail={readerDetail} /> : undefined
        }
      />
      {activeCreateDialog ? (
        <NewItemDialog
          canCreateFolder={libraryPolicy?.allow_user_folder_create ?? false}
          canCreatePage={false}
          canUpload={canUpload}
          typeOrder={LIBRARY_NEW_ITEM_TYPE_ORDER}
          uploadAccept={uploadAccept}
          initialItemType={
            canUpload && activeCreateDialog.initialItemType !== "folder"
              ? "upload"
              : activeCreateDialog.initialItemType
          }
          initialParentId={activeCreateDialog.parentNodeId ?? ""}
          parentLabel={createDialogParent?.title ?? t("libraryName")}
          parentNodes={libraryFolderNodes}
          rootLabel={t("libraryName")}
          onClose={(reason) => {
            if (reason === "cancel") {
              navigate(workspacePath(routeWorkspaceId, "library"), { replace: true });
            }
          }}
          onPickFiles={(files, parentNodeId) =>
            uploadFiles(files, parentNodeId)
          }
          onSubmit={(input) =>
            createFolder({
              parentNodeId: input.parentNodeId,
              slug: input.slug,
              title: input.title,
            })
          }
        />
      ) : null}
      {renameNode ? (
        <RenameTreeNodeDialog
          node={renameNode}
          onClose={() => setRenameNode(null)}
          onSubmit={(displayName) =>
            renameTreeNodeById(renameNode, displayName).then(() =>
              setRenameNode(null),
            )
          }
        />
      ) : null}
      {moveNode ? (
        <MoveTreeNodeDialog
          node={moveNode}
          targets={selectableMoveTargets(libraryTree, moveNode)}
          onClose={() => setMoveNode(null)}
          onSubmit={(parentNodeId, reason) =>
            moveTreeNodeToParent(moveNode, parentNodeId, reason).then(() =>
              setMoveNode(null),
            )
          }
        />
      ) : null}
      {deleteNode ? (
        <DeleteTreeNodeDialog
          node={deleteNode}
          onClose={() => setDeleteNode(null)}
          onSubmit={() =>
            deleteTreeNodeById(deleteNode).then(() => setDeleteNode(null))
          }
        />
      ) : null}
      {deleteItem ? (
        <LibraryDeleteDialog
          workspaceId={state.workspaceId}
          item={deleteItem}
          onClose={() => setDeleteItem(null)}
          onDeleted={() => {
            const deletedId = deleteItem.id;
            setDeleteItem(null);
            unavailableTargetIdRef.current = requestedTargetId ?? deletedId;
            navigate(withWorkspaceQuery(workspacePath(routeWorkspaceId, "library"), searchParams), { replace: true });
            // Drop every cached occurrence before the route update commits.
            // The overview has a separate Load-more accumulator, so filtering
            // only the first page leaves a dead selectable row behind when the
            // deleted item came from an appended page.
            setAppended((current) => ({
              ...current,
              items: current.items.filter((item) => item.id !== deletedId),
            }));
            setState((current) =>
              current
                ? {
                    ...current,
                    detail: null,
                    items: current.items.filter(
                      (item) => item.id !== deletedId,
                    ),
                    itemsTotal:
                      current.itemsTotal === null
                        ? null
                        : Math.max(0, current.itemsTotal - 1),
                    tree: current.tree.filter(
                      (node) => node.target_id !== deletedId,
                    ),
                  }
                : current,
            );
          }}
        />
      ) : null}
    </>
  );
}

// An upload:// handle is internal plumbing — a workspace UUID plus the file
// name already shown as the title. A web source keeps its URL, which tells the
// reader where the material came from. Uploads fall back to the format so the
// row keeps its second line (a one-line row broke the list's rhythm) without
// repeating the "Manual upload" connector already shown on the right.
function UploadStatusRegion({
  entries,
  retryDisabled,
  onCancel,
  onRetry,
}: {
  entries: UploadEntry[];
  retryDisabled: boolean;
  onCancel: (id: string) => void;
  onRetry: (id: string, retryClock: number) => void;
}) {
  const { t } = useTranslation("library");
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const nextAvailableAt = entries
      .map((entry) => entry.retryAvailableAt)
      .filter((value): value is number => typeof value === "number" && value > now)
      .sort((left, right) => left - right)[0];
    if (nextAvailableAt === undefined) return;
    const timer = window.setTimeout(
      () => setNow(Date.now()),
      Math.min(1_000, Math.max(1, nextAvailableAt - Date.now())),
    );
    return () => window.clearTimeout(timer);
  }, [entries, now]);
  if (entries.length === 0) return null;
  return (
    <section
      aria-label={t("upload.queue", { defaultValue: "Upload queue" })}
      aria-live="polite"
      aria-relevant="additions text"
      className="feature-section library-upload-queue"
    >
      <p className="library-upload-queue-title">
        {t("upload.queue", { defaultValue: "Upload queue" })}
      </p>
      <ul>
        {entries.map((entry) => (
          <li className="library-upload-row" key={entry.id}>
            <span className="library-upload-name">{entry.file.name}</span>{" "}
            <StatusPill
              compact
              status={entry.status}
              tone={libraryStatusTone(entry.status)}
              label={t(`upload.status.${entry.status}`, {
                defaultValue: entry.status,
              })}
            />
            <span className="library-upload-row-actions">
            {entry.status === "queued" || entry.status === "uploading" ? (
              <button
                className="text-button compact-button"
                type="button"
                onClick={() => onCancel(entry.id)}
              >
                {t("upload.cancel", { defaultValue: "Cancel" })}
              </button>
            ) : null}
            {((entry.status === "failed" && entry.retryable === true) ||
            entry.status === "cancelled" ||
            entry.status === "outcome-unknown") ? (
              <button
                aria-label={t("upload.retryAriaLabel", {
                  defaultValue: "Retry upload for {{name}}",
                  name: entry.file.name,
                })}
                aria-describedby={libraryUploadRetryDelaySeconds(entry, now) > 0
                  ? `library-upload-retry-${entry.id}`
                  : undefined}
                className="text-button compact-button"
                type="button"
                disabled={
                  retryDisabled || libraryUploadRetryDelaySeconds(entry, now) > 0
                }
                onClick={() => onRetry(entry.id, now)}
              >
                {t("upload.retry", { defaultValue: "Retry" })}
              </button>
            ) : null}
            {entry.status === "failed" &&
            entry.retryable === true &&
            libraryUploadRetryDelaySeconds(entry, now) > 0 ? (
              <span
                className="library-upload-retry-after"
                id={`library-upload-retry-${entry.id}`}
              >
                {t("upload.retryAfter", {
                  seconds: libraryUploadRetryDelaySeconds(entry, now),
                  defaultValue: "Try again in {{seconds}} seconds.",
                })}
              </span>
            ) : null}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function sourceSubtitle(item: {
  title?: string | null;
  uri: string;
  format_hint: string;
}) {
  if (item.uri.startsWith("upload://")) {
    return domainLabel("library", "format", item.format_hint);
  }
  return item.uri;
}

function libraryItemDisplayTitle(item: {
  title?: string | null;
  uri: string;
  format_hint: string;
}) {
  if (item.title) return item.title;
  if (!item.uri.startsWith("upload://")) return item.uri;
  return domainLabel("library", "format", item.format_hint);
}

function LibraryDeleteDialog({
  workspaceId,
  item,
  onClose,
  onDeleted,
}: {
  workspaceId: string;
  item: LibraryItemDetail;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [references, setReferences] = useState<LibraryItemReference[] | null>(
    null,
  );
  const { t } = useTranslation("library");
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listLibraryItemReferences(workspaceId, item.id)
      .then((refs) => {
        if (!cancelled) {
          setReferences(refs);
        }
      })
      .catch((exc) => {
        if (!cancelled) {
          setError(libraryErrorMessage(
            exc,
            t("deleteDialog.errors.loadReferences"),
          ));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [item.id, t, workspaceId]);

  const referenced = (references?.length ?? 0) > 0;
  const title = libraryItemDisplayTitle(item);

  function requestClose() {
    if (!deleting) {
      onClose();
    }
  }

  async function confirmDelete() {
    setDeleting(true);
    setError(null);
    try {
      await deleteLibraryItem(workspaceId, item.id);
      onDeleted();
    } catch (exc) {
      setError(libraryErrorMessage(exc, t("deleteDialog.errors.delete")));
      setDeleting(false);
    }
  }

  return (
    <Dialog
      dismissible={!deleting}
      labelledBy="delete-item-title"
      onClose={requestClose}
    >
      <div className="dialog-form">
        <header className="dialog-header">
          <div>
            <p className="eyebrow">
              {t("deleteDialog.eyebrow", { defaultValue: "Delete item" })}
            </p>
            <h2 id="delete-item-title">
              {t("deleteDialog.title", {
                defaultValue: "Delete {{title}}",
                title,
              })}
            </h2>
          </div>
          <DialogCloseButton
            disabled={deleting}
            label={t("deleteDialog.closeDialog", {
              defaultValue: "Close delete dialog",
            })}
            onClick={requestClose}
          />
        </header>
        {references === null && !error ? (
          <p className="feature-subtitle">
            {t("deleteDialog.checking", {
              defaultValue: "Checking references…",
            })}
          </p>
        ) : referenced ? (
          <>
            <StatusBanner tone="warning">
              {references!.length === 1
                ? t("deleteDialog.referenced_one", {
                    defaultValue:
                      "1 page cites this item. Deleting it leaves a deleted-item marker on that page and notifies reviewers.",
                  })
                : t("deleteDialog.referenced_other", {
                    count: references!.length,
                    defaultValue:
                      "{{count}} pages cite this item. Deleting it leaves deleted-item markers on those pages and notifies reviewers.",
                  })}
            </StatusBanner>
            <ul className="library-reference-list">
              {references!.map((reference) => (
                <li key={reference.id}>
                  <Link to={contextPageHref(workspaceId, reference.id)}>
                    {reference.title ?? reference.slug}
                  </Link>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="feature-subtitle">
            {t("deleteDialog.unreferenced", {
              defaultValue:
                "This permanently deletes the item and all its versions. This cannot be undone.",
            })}
          </p>
        )}
        {error ? <p className="form-error">{error}</p> : null}
        <div className="dialog-actions">
          <button
            className="icon-button text-button"
            disabled={deleting}
            type="button"
            onClick={requestClose}
          >
            {t("deleteDialog.cancel", { defaultValue: "Cancel" })}
          </button>
          {references !== null ? (
            <button
              className="icon-button danger"
              disabled={deleting}
              type="button"
              onClick={confirmDelete}
            >
              {deleting
                ? t("deleteDialog.deleting", { defaultValue: "Deleting" })
                : t("deleteDialog.deletePermanently", {
                    defaultValue: "Delete permanently",
                  })}
            </button>
          ) : null}
        </div>
      </div>
    </Dialog>
  );
}

function libraryErrorMessage(exc: unknown, fallback: string) {
  return exc instanceof ApiError || exc instanceof LibraryUploadValidationError
    ? exc.message
    : fallback;
}

class LibraryUploadValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "LibraryUploadValidationError";
  }
}

function libraryUploadError(exc: unknown, fallback: string, workspaceId: string) {
  if (
    exc instanceof ApiError &&
    (exc.body.error_code === "library_upload_too_large" ||
      exc.body.error_code === "too_large")
  ) {
    return {
      message: exc.message,
      settingsLink: exc.body.settings_link ?? `${workspacePath(workspaceId, "settings")}?section=library`,
    };
  }
  return {
    message: libraryErrorMessage(exc, fallback),
    settingsLink: null,
  };
}

function LibraryList({
  items,
  activeId,
  onSelect,
  onUpload,
}: {
  items: LibraryItemSummary[];
  activeId: string | null;
  onSelect: (libraryItemId: string) => void;
  onUpload?: () => void;
}) {
  const { t } = useTranslation("library");
  const routeWorkspaceId = useRouteWorkspaceId();
  if (items.length === 0) {
    return (
      <div className="empty-state empty-state-rich">
        <BookOpen size={22} aria-hidden="true" />
        <h2>{t("overview.empty", { defaultValue: "No Library items yet" })}</h2>
        <p>
          {t("overview.emptyDescription", {
            defaultValue:
              "Library stores trusted source material that Context pages and Ask answers should cite.",
          })}
        </p>
        <WorkspaceFirstUseChecklist nextStep="library" />
        <div className="empty-state-actions">
          {onUpload ? (
            <button
              className="icon-button primary"
              type="button"
              onClick={onUpload}
            >
              {t("overview.addFirst", {
                defaultValue: "Add first Library item",
              })}
            </button>
          ) : null}
          <Link className="icon-button text-button" to={workspacePath(routeWorkspaceId, "context")}>
            {t("overview.openContext", { defaultValue: "Open Context" })}
          </Link>
        </div>
      </div>
    );
  }
  const countLabel = t("panel.items", {
    count: items.length,
    defaultValue_one: "item",
    defaultValue_other: "items",
  });
  return (
    <section className="feature-section">
      <ListPanelHeader
        title={t("panel.itemsHeading", { defaultValue: "Items" })}
        count={items.length}
        unit={countLabel}
      />
      <div className="entity-list">
        {items.map((item) => (
          <button
            aria-pressed={item.id === activeId}
            className={`entity-row selectable-row ${
              item.id === activeId ? "active" : ""
            }`}
            key={item.id}
            type="button"
            onClick={() => onSelect(item.id)}
          >
            <BookOpen size={16} aria-hidden="true" />
            <div>
              <strong>{libraryItemDisplayTitle(item)}</strong>
              {sourceSubtitle(item) ? <p>{sourceSubtitle(item)}</p> : null}
            </div>
            <span>{domainLabel("library", "connectorType", item.connector_type)}</span>
            <StatusPill
              status={libraryProcessingStatus(item) ?? item.lifecycle_status}
              tone={libraryStatusTone(
                libraryProcessingStatus(item) ?? item.lifecycle_status,
              )}
              label={domainLabel(
                "library",
                "processingStatus",
                libraryProcessingStatus(item) ?? item.lifecycle_status,
              )}
              compact
            />
          </button>
        ))}
      </div>
    </section>
  );
}

function LibraryReader({
  canEdit,
  canUpload,
  capabilities,
  detail,
  onConversionRetried,
  onDelete,
  onVersionAdded,
  previewKind,
  workspaceId,
}: {
  canEdit: boolean;
  canUpload: boolean;
  capabilities: LibraryCapabilities;
  detail: LibraryItemDetail | null;
  onConversionRetried: (detail: LibraryItemDetail) => void;
  onDelete: () => void;
  onVersionAdded: (detail: LibraryItemDetail) => void;
  previewKind: string | null;
  workspaceId: string;
}) {
  const { t } = useTranslation("library");
  const [tab, setTab] = useState<"view" | "history" | "activity">("view");
  const [editingMarkdown, setEditingMarkdown] = useState(false);
  const [previewRefresh, setPreviewRefresh] = useState(0);
  const [conversionRetryStatus, setConversionRetryStatus] = useState<
    "idle" | "retrying" | "failed"
  >("idle");
  const [conversionRetryError, setConversionRetryError] = useState<string | null>(null);
  const editableVersionId = editableLibraryMarkdownVersionId(detail);
  // D-9/§3-H: the editable version is only ever the item's actual latest
  // version now (doc §3-D removed History's past-version preview, so there
  // is no other version screen to diverge from).
  const currentVersionId = detail
    ? (detail.current_version?.id ?? detail.current_version_id ?? null)
    : null;
  const canEditCurrentVersion =
    editableVersionId !== null && editableVersionId === currentVersionId;
  const originalUrl = detail ? libraryOriginalUrl(detail) : null;
  useEffect(() => {
    setTab("view");
    setEditingMarkdown(false);
    setPreviewRefresh(0);
  }, [detail?.id]);
  useEffect(() => {
    setConversionRetryStatus("idle");
    setConversionRetryError(null);
  }, [detail?.current_version?.id, detail?.id]);
  useEffect(() => {
    setEditingMarkdown(false);
  }, [editableVersionId]);
  if (!detail) {
    return null;
  }
  const retryableConversionVersion =
    canEdit && detail.current_version?.conversion?.retryable
      ? detail.current_version
      : null;
  // §3-D: Library always displays the latest version, so this is simply the
  // current version's own conversion-lifecycle status -- no more "only when
  // the displayed version happens to be current" gate.
  const displayedConversionStatusKey = currentVersionStatusKey(detail);
  async function retryConversion() {
    if (!retryableConversionVersion || conversionRetryStatus === "retrying") {
      return;
    }
    setConversionRetryStatus("retrying");
    setConversionRetryError(null);
    try {
      const retriedDetail = await retryLibraryConversion(
        workspaceId,
        retryableConversionVersion.id,
      );
      onConversionRetried(retriedDetail);
      setConversionRetryStatus("idle");
    } catch (caught) {
      setConversionRetryStatus("failed");
      setConversionRetryError(
        caught instanceof ApiError
          ? caught.displayMessage
          : t("reader.retryConversionFailed", {
              defaultValue: "Document conversion could not be retried.",
            }),
      );
    }
  }
  const hasTrailingActions =
    Boolean(originalUrl) || canEdit || Boolean(canEdit && editableVersionId);
  // Retry lives in the conversion banner inside the view panel (merge of
  // fix/Epic-1/Lint-Library), not here: the banner already states the
  // conversion status it belongs to, and a second copy in the heading row
  // rendered the same button twice whenever a conversion was retryable.
  const readerActions = hasTrailingActions ? (
    <div className="library-reader-actions">
      {canEdit && canEditCurrentVersion && !editingMarkdown ? (
        <button
          className="icon-button text-button library-reader-edit-action"
          type="button"
          onClick={() => {
            setTab("view");
            setEditingMarkdown(true);
          }}
        >
          <Pencil size={16} aria-hidden="true" />
          <span>{t("reader.edit", { defaultValue: "Edit Markdown" })}</span>
        </button>
      ) : null}
      {canUpload ? (
        <LibraryAddVersionAction
          capabilities={capabilities}
          detail={detail}
          workspaceId={workspaceId}
          onVersionAdded={onVersionAdded}
        />
      ) : null}
      {originalUrl ? (
        <LibraryOriginalDownloadAction
          detail={detail}
          originalUrl={originalUrl}
        />
      ) : null}
      {canEdit ? (
        <button
          aria-label={t("reader.delete")}
          className="icon-button danger library-delete-action library-reader-delete-action"
          type="button"
          onClick={onDelete}
        >
          <Trash2 size={16} aria-hidden="true" />
          <span>{t("reader.delete")}</span>
        </button>
      ) : null}
    </div>
  ) : null;
  return (
    <section className="feature-section detail-section library-reader-detail">
      <div className="detail-heading">
        <div>
          <h2>{libraryItemDisplayTitle(detail)}</h2>
        </div>
        {readerActions ? (
          <div className="detail-heading-actions">{readerActions}</div>
        ) : null}
      </div>
      <div className="reader-meta-row">
        <span className="reader-chip">
          {domainLabel("library", "connectorType", detail.connector_type)}
        </span>
        <span className="reader-chip">
          {domainLabel("library", "format", detail.format_hint)}
        </span>
        <span>v{detail.current_version?.version_no ?? "none"}</span>
        <StatusPill
          status={libraryProcessingStatus(detail) ?? detail.lifecycle_status}
          tone={libraryStatusTone(
            libraryProcessingStatus(detail) ?? detail.lifecycle_status,
          )}
          label={domainLabel(
            "library",
            "processingStatus",
            libraryProcessingStatus(detail) ?? detail.lifecycle_status,
          )}
          compact
        />
        <time
          className="reader-meta-date"
          dateTime={detail.updated_at}
          title={formatVersionTimestamp(detail.updated_at, "long")}
        >
          {t("reader.updated", {
            value: formatVersionTimestamp(detail.updated_at),
          })}
        </time>
      </div>
      <ReaderTabs
        accent="library"
        active={tab}
        ariaLabel={t("reader.ariaLabel")}
        idBase="library-reader"
        onChange={setTab}
        tabs={[
          { key: "view", label: t("reader.tabs.view") },
          {
            key: "history",
            label: t("reader.tabs.history"),
            count: detail.versions.length,
          },
          { key: "activity", label: t("reader.tabs.activity") },
        ]}
      />
      {tab === "view" ? (
        <div
          id="library-reader-panel-view"
          role="tabpanel"
          aria-labelledby="library-reader-tab-view"
          tabIndex={0}
        >
          {editingMarkdown && editableVersionId ? (
            <LibrarySemanticMarkdownEditor
              onCancel={() => setEditingMarkdown(false)}
              onSaved={() => setPreviewRefresh((current) => current + 1)}
              versionId={editableVersionId}
              workspaceId={workspaceId}
            />
          ) : (
            <>
              {displayedConversionStatusKey ? (
                <div
                  className="library-preview-notice library-preview-conversion-banner"
                  role="status"
                >
                  <p>{t(displayedConversionStatusKey)}</p>
                  {retryableConversionVersion ? (
                    <div className="library-preview-state-actions">
                      <button
                        aria-busy={conversionRetryStatus === "retrying"}
                        className="icon-button text-button"
                        disabled={conversionRetryStatus === "retrying"}
                        type="button"
                        onClick={() => void retryConversion()}
                      >
                        <RefreshCw size={16} aria-hidden="true" />
                        <span>
                          {conversionRetryStatus === "retrying"
                            ? t("reader.retryingConversion", {
                                defaultValue: "Retrying conversion…",
                              })
                            : t("reader.retryConversion", {
                                defaultValue: "Retry conversion",
                              })}
                        </span>
                      </button>
                      {conversionRetryError ? (
                        <span
                          className="library-reader-conversion-retry-error"
                          role="alert"
                        >
                          {conversionRetryError}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : null}
              <PreviewPane
                key={`${detail.id}:${previewRefresh}`}
                detail={detail}
                legacyPreviewKind={previewKind}
                workspaceId={workspaceId}
              />
            </>
          )}
        </div>
      ) : null}
      {tab === "history" ? (
        <div
          id="library-reader-panel-history"
          role="tabpanel"
          aria-labelledby="library-reader-tab-history"
          tabIndex={0}
        >
          <LibraryHistory detail={detail} />
        </div>
      ) : null}
      {/* No in-app reader for this one yet. The tab stays reachable and says
          where the data lives instead of rendering dimmed -- a native
          `disabled` button leaves the tab ring, so its `title` was unreachable
          by keyboard and unrendered by browsers. */}
      {tab === "activity" ? (
        <div
          className="wiki-empty"
          id="library-reader-panel-activity"
          role="tabpanel"
          aria-labelledby="library-reader-tab-activity"
          tabIndex={0}
        >
          {t("reader.noActivity", {
            defaultValue:
              "No per-source activity stream yet. Uploads and revisions are under History, and the workspace feed lives on the Activity page.",
          })}
        </div>
      ) : null}
    </section>
  );
}

function isBrowserReadableSourceUrl(value: string) {
  const trimmed = value.trim();
  return (
    !trimmed.startsWith("upload://") &&
    (/^https?:\/\//i.test(trimmed) ||
      trimmed.startsWith("/") ||
      trimmed.startsWith("blob:"))
  );
}

/**
 * P10.2: uploads a new version onto the CURRENT item via the explicit
 * add-a-version command (never the create-new-item upload) -- see
 * `addLibraryItemVersion` in shared/api/library.ts. The server always makes
 * the accepted version the item's new current_version, so the caller only
 * needs to swap in the returned detail.
 */
function LibraryAddVersionAction({
  capabilities,
  detail,
  workspaceId,
  onVersionAdded,
}: {
  capabilities: LibraryCapabilities;
  detail: LibraryItemDetail;
  workspaceId: string;
  onVersionAdded: (detail: LibraryItemDetail) => void;
}) {
  const { t } = useTranslation("library");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState<"idle" | "uploading" | "failed">("idle");
  const [error, setError] = useState<string | null>(null);
  const [errorActionLink, setErrorActionLink] = useState<string | null>(null);
  const uploadAccept = libraryUploadAccept(capabilities);

  useEffect(() => {
    setStatus("idle");
    setError(null);
    setErrorActionLink(null);
  }, [detail.id]);

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0] ?? null;
    event.currentTarget.value = "";
    if (!file) {
      return;
    }
    const validationError = validateLibraryUploadFile(file, capabilities, t);
    if (validationError) {
      setStatus("failed");
      setError(validationError);
      setErrorActionLink(null);
      return;
    }
    setStatus("uploading");
    setError(null);
    setErrorActionLink(null);
    try {
      const updated = await addLibraryItemVersion(workspaceId, detail.id, file, {
        operationId: createUuidV4(),
      });
      setStatus("idle");
      onVersionAdded(updated);
    } catch (exc) {
      const uploadError = libraryUploadError(exc, t("errors.upload"), workspaceId);
      setStatus("failed");
      setError(uploadError.message);
      setErrorActionLink(uploadError.settingsLink);
    }
  }

  const label = status === "uploading"
    ? t("reader.addingVersion", { defaultValue: "Uploading new version…" })
    : t("reader.addVersion", { defaultValue: "Upload new version" });

  return (
    <div className="library-reader-add-version-action">
      <input
        ref={fileInputRef}
        aria-label={label}
        hidden
        tabIndex={-1}
        type="file"
        accept={uploadAccept}
        onChange={(event) => void handleFileChange(event)}
      />
      <button
        aria-busy={status === "uploading"}
        className="icon-button text-button"
        disabled={status === "uploading"}
        type="button"
        onClick={() => fileInputRef.current?.click()}
      >
        <Upload size={16} aria-hidden="true" />
        <span>{label}</span>
      </button>
      {error ? (
        <span className="library-reader-add-version-error" role="alert">
          {error}{" "}
          {errorActionLink ? (
            <Link to={errorActionLink}>
              {t("upload.openPolicySettings")}
            </Link>
          ) : null}
        </span>
      ) : null}
    </div>
  );
}

function LibraryOriginalDownloadAction({
  compactLabel,
  detail,
  downloadingLabel,
  label,
  originalUrl,
}: {
  /** History rows: the short visible label, shown like every other History
   * action (icon alone on narrow screens). The accessible name stays `label`. */
  compactLabel?: string;
  detail: LibraryItemDetail;
  /** Overrides the default "Download original file" / "Downloading…" pair --
   * e.g. History's per-row action names the specific version instead. */
  downloadingLabel?: string;
  label?: string;
  originalUrl: string;
}) {
  const { t } = useTranslation("library");
  const downloadControllerRef = useRef<AbortController | null>(null);
  const [status, setStatus] = useState<"idle" | "downloading" | "failed">("idle");
  const [error, setError] = useState<string | null>(null);
  const protectedOriginalUrl = isProtectedLibraryOriginalUrl(originalUrl)
    ? originalUrl
    : null;
  const requiresOriginalWarning = libraryOriginalRequiresWarning(detail.format_hint);

  function confirmOriginalDownload() {
    return !requiresOriginalWarning || window.confirm(
      t("reader.potentiallyActiveOriginalDownloadWarning", {
        defaultValue:
          "dtxWiki does not execute this file during preview, but the original may contain active content that another application can run after download. Download only if you trust the file.",
      }),
    );
  }

  useEffect(() => {
    setStatus("idle");
    setError(null);
    return () => downloadControllerRef.current?.abort();
  }, [detail.id, originalUrl]);

  async function downloadOriginal() {
    if (!protectedOriginalUrl || status === "downloading") return;
    if (!confirmOriginalDownload()) return;
    downloadControllerRef.current?.abort();
    const controller = new AbortController();
    downloadControllerRef.current = controller;
    setStatus("downloading");
    setError(null);
    try {
      const download = await fetchLibraryOriginal(
        protectedOriginalUrl,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const objectUrl = URL.createObjectURL(download.blob);
      const anchor = document.createElement("a");
      try {
        anchor.href = objectUrl;
        anchor.download = download.filename ?? "library-original";
        anchor.hidden = true;
        document.body.append(anchor);
        anchor.click();
      } finally {
        anchor.remove();
        URL.revokeObjectURL(objectUrl);
      }
      setStatus("idle");
    } catch (caught) {
      if (controller.signal.aborted || isAbortError(caught)) return;
      setStatus("failed");
      setError(
        caught instanceof ApiError
          ? caught.displayMessage
          : t("reader.downloadFailed", {
              defaultValue: "The original file could not be downloaded. Please try again.",
            }),
      );
    } finally {
      if (downloadControllerRef.current === controller) {
        downloadControllerRef.current = null;
      }
    }
  }

  const resolvedLabel = status === "downloading"
    ? downloadingLabel ?? t("reader.downloadingOriginal", { defaultValue: "Downloading original file…" })
    : label ?? t("reader.downloadOriginal", { defaultValue: "Download original file" });
  // The History-row form takes exactly HistoryActionButton's classes, so it
  // sizes and collapses like every other History action rather than like the
  // reader toolbar's 44px download button.
  const triggerClassName = compactLabel
    ? "text-button compact-button history-action"
    : "icon-button text-button";
  const triggerContent = (
    <>
      <Download
        aria-hidden="true"
        className={compactLabel ? "history-action-icon" : undefined}
        size={16}
      />
      <span className={compactLabel ? "history-action-label" : undefined}>
        {compactLabel && status !== "downloading" ? compactLabel : resolvedLabel}
      </span>
    </>
  );

  return (
    <div className="library-reader-download-action">
      {protectedOriginalUrl ? (
        <button
          aria-busy={status === "downloading"}
          aria-label={resolvedLabel}
          className={triggerClassName}
          disabled={status === "downloading"}
          onClick={() => void downloadOriginal()}
          title={compactLabel ? resolvedLabel : undefined}
          type="button"
        >
          {triggerContent}
        </button>
      ) : (
        <a
          aria-label={resolvedLabel}
          className={triggerClassName}
          download
          href={originalUrl}
          onClick={(event) => {
            if (!confirmOriginalDownload()) event.preventDefault();
          }}
          rel="noreferrer"
          title={compactLabel ? resolvedLabel : undefined}
        >
          {triggerContent}
        </a>
      )}
      {error ? (
        <span className="library-reader-download-error" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}

function libraryUploadRetryDelaySeconds(entry: UploadEntry, now: number) {
  if (!entry.retryAvailableAt) return 0;
  const remaining = Math.max(
    0,
    Math.ceil((entry.retryAvailableAt - now) / 1_000),
  );
  return entry.retryAfterSeconds === null || entry.retryAfterSeconds === undefined
    ? remaining
    : Math.min(entry.retryAfterSeconds, remaining);
}

function libraryUploadRetryMetadata(exc: unknown) {
  if (exc instanceof LibraryUploadApiError) {
    return {
      retryable: exc.retryable,
      retryAfterSeconds: exc.retryAfterSeconds,
      retryAvailableAt: exc.retryAfterSeconds === null
        ? null
        : Date.now() + exc.retryAfterSeconds * 1_000,
    };
  }
  return {
    retryable: exc instanceof TypeError,
    retryAfterSeconds: null,
    retryAvailableAt: null,
  };
}

// Doc §3-D: History rows show metadata only -- no whole-row "preview this
// version" click, and no History-driven version selection anywhere in the
// reader. A row is a plain container (never itself interactive) so it can
// hold its own independent download button without nesting one interactive
// control inside another. Library has no version diff, so unlike Context and
// Agent a row offers no Changes action; the lane graph and card are the same.
function LibraryHistory({ detail }: { detail: LibraryItemDetail }) {
  const { t } = useTranslation("library");
  const historyGraph = useMemo(
    () =>
      buildHistoryGraph(libraryHistoryNodes(detail.versions), {
        headCandidateIds: [detail.current_version?.id],
        complete: true,
      }),
    [detail.current_version?.id, detail.versions],
  );
  if (detail.versions.length === 0) {
    return (
      <div className="wiki-empty">
        {translate("context:inspector.noVersionHistory", "No history yet.")}
      </div>
    );
  }
  return (
    <HistoryGraphList
      accent="library"
      graph={historyGraph}
      renderCard={(row) => {
        const version = row.entry;
        return (
          <HistoryCard
            actions={
              version.original_download_available && version.original_url ? (
                <LibraryOriginalDownloadAction
                  compactLabel={t("reader.history.downloadShort", {
                    defaultValue: "Download",
                  })}
                  detail={detail}
                  downloadingLabel={t("reader.history.downloadingVersion", {
                    defaultValue: "Downloading this version…",
                  })}
                  label={t("reader.history.downloadVersion", {
                    defaultValue: "Download this version",
                  })}
                  originalUrl={version.original_url}
                />
              ) : (
                <span className="history-card-unavailable">
                  {t("reader.history.sourceUnavailable", {
                    defaultValue: "The original file is no longer retained.",
                  })}
                </span>
              )
            }
            author={
              <span className="history-card-meta">
                <span title={version.content_hash}>
                  {version.content_hash.slice(0, 12)}
                </span>
                <time
                  dateTime={version.created_at}
                  title={formatVersionTimestamp(version.created_at, "long")}
                >
                  {formatVersionTimestamp(version.created_at)}
                </time>
              </span>
            }
            description={libraryVersionHistorySummary(version, t)}
            row={row}
            status={
              <StatusPill
                status={version.processing_status}
                tone={libraryStatusTone(version.processing_status)}
                label={domainLabel(
                  "library",
                  "processingStatus",
                  version.processing_status,
                )}
                compact
              />
            }
          />
        );
      }}
      rowView={(row) => ({
        ariaCurrent:
          detail.current_version?.id === row.node.id ? "page" : undefined,
        tone: row.entry.processing_status === "ready" ? "published" : "draft",
      })}
    />
  );
}

function libraryVersionHistorySummary(
  version: LibraryItemDetail["versions"][number],
  t: TFunction,
) {
  const reason = version.conversion?.reason;
  if (reason === "document_conversion_failed" || version.processing_status === "failed") {
    return t("reader.history.conversionFailed");
  }
  if (reason === "document_conversion_timed_out" || version.processing_status === "timed_out") {
    return t("reader.history.conversionTimedOut");
  }
  if (
    reason === "document_conversion_unavailable" ||
    version.processing_status === "stored_unconverted"
  ) {
    return t("reader.history.originalStored");
  }
  if (["pending", "queued", "converting", "projecting"].includes(
    version.processing_status,
  )) {
    return t("reader.history.conversionInProgress");
  }
  if (version.processing_status === "ready_partial") {
    return t("reader.history.conversionReadyPartial");
  }
  if (
    version.processing_status === "ready" ||
    version.processing_status === "completed"
  ) {
    return t("reader.history.conversionReady");
  }
  return t("reader.history.sourceVersion");
}

function LibraryInspector({ detail }: { detail: LibraryItemDetail | null }) {
  const { t } = useTranslation("library");
  if (!detail) {
    return (
      <div className="inspector-empty">
        <h2>{t("inspector.emptyTitle")}</h2>
        <p>{t("inspector.emptyBody")}</p>
      </div>
    );
  }
  const artifacts = readyLibraryArtifacts(detail);

  return (
    <div className="inspector-stack">
      <section className="inspector-section">
        <header>
          <span>{t("inspector.trust")}</span>
        </header>
        <div className="trust-anchor">
          <span className={`trust-badge trust-${detail.trust_level}`}>
            {domainLabel("library", "trustLevel", detail.trust_level)}
          </span>
          <div>
            <strong>{libraryItemDisplayTitle(detail)}</strong>
            <p>{domainLabel("library", "lifecycleStatus", detail.lifecycle_status)}</p>
          </div>
        </div>
      </section>
      <section className="inspector-section">
        <header>
          <span>{t("inspector.versions")}</span>
          <span>{detail.versions.length}</span>
        </header>
        <dl>
          <div className="version-row">
            <dt>{t("inspector.current")}</dt>
            <dd>
              {detail.current_version
                ? `v${detail.current_version.version_no}`
                : t("inspector.none")}
            </dd>
          </div>
          <div className="version-row">
            <dt>{t("inspector.status")}</dt>
            <dd>
              {domainLabel(
                "library",
                "processingStatus",
                detail.current_version?.processing_status ??
                  detail.lifecycle_status,
              )}
            </dd>
          </div>
          <div className="version-row">
            <dt>{t("inspector.updated")}</dt>
            <dd>{formatVersionTimestamp(detail.updated_at)}</dd>
          </div>
        </dl>
      </section>
      <section className="inspector-section">
        <header>
          <span>{t("inspector.artifacts")}</span>
          <span>{artifacts.length}</span>
        </header>
        <div className="artifact-list">
          {artifacts.length === 0 ? (
            <div className="wiki-empty">{t("inspector.noArtifacts")}</div>
          ) : (
            artifacts.map((artifact) => (
              <div className="artifact-row" key={artifact.id}>
                <FileArchive size={16} aria-hidden="true" />
                <div>
                  <strong>{domainLabel("library", "artifactType", artifact.artifact_type)}</strong>
                  <p>
                    {artifact.extraction_method
                      ? domainLabel(
                          "library",
                          "extractionMethod",
                          artifact.extraction_method,
                        )
                      : formatVersionTimestamp(artifact.created_at)}
                  </p>
                </div>
                <FileText size={15} aria-hidden="true" />
              </div>
            ))
          )}
        </div>
      </section>
      <ArtifactChatsSection
        resourceId={detail.id}
        resourceKind="library_item"
      />
    </div>
  );
}

function readyLibraryArtifacts(detail: LibraryItemDetail) {
  const readyVersion = readyLibraryVersion(detail);
  if (!readyVersion) return [];
  return detail.artifacts.filter(
    (artifact) => artifact.library_item_version_id === readyVersion.id,
  );
}

function libraryDetailAtArtifactVersion(
  detail: LibraryItemDetail,
  version: LibraryItemVersionPayload,
  artifact: LibraryArtifactPayload | null,
): LibraryItemDetail {
  return {
    ...detail,
    current_version: version,
    current_version_id: version.id,
    current_ready_version: null,
    current_ready_version_id: null,
    current_preview_version_id: version.id,
    versions: [...detail.versions.filter((item) => item.id !== version.id), version],
    artifacts: artifact
      ? [...detail.artifacts.filter((item) => item.id !== artifact.id), artifact]
      : detail.artifacts,
  };
}

function editableLibraryMarkdownVersionId(detail: LibraryItemDetail | null) {
  if (!detail) return null;
  const version = readyLibraryVersion(detail);
  if (!version?.selected_conversion_attempt_id) return null;
  const hasCanonicalMarkdown = readyLibraryArtifacts(detail).some(
    (artifact) =>
      artifact.role === "markdown" &&
      artifact.media_type === "text/markdown" &&
      artifact.is_complete !== false &&
      artifact.index_text !== null &&
      artifact.index_text !== undefined,
  );
  return hasCanonicalMarkdown ? version.id : null;
}

function readyLibraryVersion(detail: LibraryItemDetail) {
  if (detail.current_ready_version) return detail.current_ready_version;
  if (detail.current_ready_version_id !== undefined) {
    return detail.current_ready_version_id
      ? detail.versions.find(
          (version) => version.id === detail.current_ready_version_id,
        ) ?? null
      : null;
  }
  return detail.current_version;
}

/**
 * P4.4: the download action must target the exact version the Preview pane
 * is displaying -- never a separately-guessed candidate (a stale
 * `readyLibraryVersion`, the item-level `original_url`, or the raw
 * connector `uri`) that could silently point at a different version than
 * what's on screen. An artifact route supplies detail pinned to the
 * server-confirmed artifact version. History's per-version download is
 * separately gated on server-authoritative `original_download_available`.
 */
function libraryOriginalUrl(detail: LibraryItemDetail) {
  const versionId = previewVersionId(detail);
  if (!versionId) {
    return null;
  }
  const version =
    detail.current_version?.id === versionId
      ? detail.current_version
      : (detail.versions.find((candidate) => candidate.id === versionId) ??
        null);
  const originalUrl = version?.original_url;
  return originalUrl && isBrowserReadableSourceUrl(originalUrl)
    ? originalUrl
    : null;
}

function updateLibraryItemSummaries(
  items: LibraryItemSummary[],
  detail: LibraryItemDetail,
) {
  let changed = false;
  const updated = items.map((item) => {
    if (item.id !== detail.id) {
      return item;
    }
    if (
      item.current_version_id === detail.current_version_id &&
      item.processing_status === detail.processing_status &&
      item.updated_at === detail.updated_at
    ) {
      return item;
    }
    changed = true;
    return {
      ...item,
      current_version_id: detail.current_version_id,
      processing_status: detail.processing_status,
      updated_at: detail.updated_at,
    };
  });
  return changed ? updated : items;
}

function libraryProcessingStatus(
  item: LibraryItemSummary | LibraryItemDetail,
) {
  if ("current_version" in item) {
    const detailStatus = item.current_version?.processing_status;
    if (detailStatus) return detailStatus;
  }
  if (item.processing_status) return item.processing_status;
  if (typeof item.processing === "string") return item.processing;
  if (item.processing && typeof item.processing.status === "string") {
    return item.processing.status;
  }
  return null;
}

function isPollingStatus(status: string | null) {
  return status === "queued" || status === "converting" || status === "projecting";
}

function libraryStatusTone(status: string) {
  if (status === "ready" || status === "succeeded") return "ok";
  if (
    status === "queued" ||
    status === "uploading" ||
    status === "converting" ||
    status === "projecting" ||
    status === "ready_partial"
  ) {
    return "warn";
  }
  if (status === "failed" || status === "timed_out") return "danger";
  if (status === "cancelled") return "muted";
  return "info";
}

function findLibraryFormatCapability(
  item: Pick<LibraryItemDetail, "format_hint" | "uri">,
  capabilities: LibraryCapabilities,
) {
  const byId = capabilities.formats.find(
    (format) => format.format_id.toLowerCase() === item.format_hint.toLowerCase(),
  );
  if (byId) return byId;
  return matchingLibraryFormat(
    { name: item.uri, type: item.format_hint },
    capabilities,
  );
}

function validateLibraryUploadFile(
  file: File,
  capabilities: LibraryCapabilities,
  t: TFunction,
) {
  const format = matchingLibraryFormat(file, capabilities);
  if (!format) {
    return t("upload.errors.unsupportedFormat", {
      defaultValue: "This file format is not enabled for this workspace.",
    });
  }
  if (Number.isFinite(format.max_bytes) && file.size > format.max_bytes) {
    return t("upload.errors.tooLarge", {
      maxSize: formatBytes(format.max_bytes),
      defaultValue: "This file exceeds the {{maxSize}} limit for its format.",
    });
  }
  return null;
}

function formatBytes(bytes: number) {
  if (bytes < 1_000_000) return `${Math.max(1, Math.round(bytes / 1_000))} KB`;
  return `${Math.max(1, Math.round(bytes / 1_000_000))} MB`;
}

function resolveRequestedLibraryItemId(
  _items: LibraryItemSummary[],
  nodes: TreeNodePayload[],
  requestedTargetId: string | null,
) {
  if (!requestedTargetId) {
    // No target -> show the overview, don't auto-open items[0]. The overview
    // (RecentItemsList) is the intended Library landing, matching Context.
    return null;
  }
  // Trust the tree node's target_id (or a bare item-id deep link); the flat
  // list is just a cache, so an item past the list cap still resolves and the
  // detail is fetched by id rather than being a silent no-op.
  const node = nodes.find((candidate) => candidate.id === requestedTargetId);
  if (node) {
    // A folder (or other non-item) node has no library detail to show.
    return node.target_kind === "library_item" && node.target_id
      ? node.target_id
      : null;
  }
  // No matching node: treat the target as a bare library-item id deep link.
  return requestedTargetId;
}

function selectableMoveTargets(
  nodes: TreeNodePayload[],
  movingNode: TreeNodePayload,
) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const isDescendant = (candidateId: string) => {
    let parentId = byId.get(candidateId)?.parent_id ?? null;
    while (parentId) {
      if (parentId === movingNode.id) {
        return true;
      }
      parentId = byId.get(parentId)?.parent_id ?? null;
    }
    return false;
  };
  return nodes.filter(
    (node) =>
      node.can_have_children &&
      node.id !== movingNode.id &&
      node.section === movingNode.section &&
      !isDescendant(node.id),
  );
}

function libraryExplorerTreeNodes(nodes: TreeNodePayload[]) {
  const visibleNodes = nodes.filter(
    (node) => node.target_kind !== "library_artifact",
  );
  const visibleNodeIds = new Set(visibleNodes.map((node) => node.id));
  const visibleChildrenByParentId = new Map<string, number>();
  for (const node of visibleNodes) {
    if (!node.parent_id || !visibleNodeIds.has(node.parent_id)) {
      continue;
    }
    visibleChildrenByParentId.set(
      node.parent_id,
      (visibleChildrenByParentId.get(node.parent_id) ?? 0) + 1,
    );
  }
  return visibleNodes.map((node) => {
    const childrenCount = visibleChildrenByParentId.get(node.id) ?? 0;
    return {
      ...node,
      can_have_children:
        node.node_kind === "folder"
          ? node.can_have_children
          : childrenCount > 0,
      children_count: childrenCount,
      // Recompute has_children against the FILTERED tree: the backend value
      // counts hidden library_artifact children, so a leaf whose only children
      // are artifacts kept a stale expand caret that opened to nothing (item 21).
      has_children: childrenCount > 0,
    };
  });
}

function expandNodeIds(current: string[], nodeId: string | null) {
  if (!nodeId || current.includes(nodeId)) {
    return current;
  }
  return [...current, nodeId];
}
