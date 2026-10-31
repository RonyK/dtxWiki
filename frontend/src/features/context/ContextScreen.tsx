import {
  Copy,
  ArrowRight,
  Edit,
  FileText,
  Folder,
  GitMerge,
  MoreHorizontal,
  Plus,
  RotateCcw,
  Share2,
  Trash2,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ComponentProps,
} from "react";
import { useTranslation } from "react-i18next";
import {
  Link,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";

import { NotFoundScreen } from "../errors/NotFoundScreen";
import { GraphView } from "../graph/GraphView";
import { useWorkspaceGraphCapability } from "../graph/useWorkspaceGraphCapability";
import { MarkdownView } from "../pages/MarkdownView";
import { PagePublicShareControl } from "../pages/PagePublicShareControl";
import { ContextReconciliationWorkbench } from "./ContextReconciliationWorkbench";
import { citationKeyForSource } from "../pages/editorCompletions";
import {
  contextGraphQuery,
  queryGraph,
  type GraphDepth,
  type GraphHistoryMode,
  type GraphNode,
  type GraphQueryPayload,
} from "../../shared/api/graph";
import {
  contextMediaPageVersionContentUrl,
  type ContextMediaAssetUrlResolver,
} from "../../shared/api/contextMedia";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
  listLibraryTombstones,
  type LibraryItemTombstone,
} from "../../shared/api/library";
import {
  discardAlternativeDraft,
  diffPageVersions,
  restorePageVersion,
} from "../../shared/api/pages";
import {
  listContextChanges,
  listContextPages,
  readContextPage,
} from "../../shared/api/context";
import {
  favoriteTargetKind,
  getTreeFavoriteSnapshot,
  subscribeTreeFavorites,
  toggleTreeFavorite,
  treeFavoriteKey,
  type TreeFavoriteScope,
} from "../../shared/api/treeFavorites";
import {
  decideReview,
  getContextPageNeighbors,
  getReview,
  listContextPageActivity,
} from "../../shared/api/operations";
import type {
  ActivityItem,
  GraphNeighborEntry,
  GraphNeighborsPayload,
} from "../../shared/api/operations";
import type {
  ContextChangeHistory,
  ContextPageDetail,
  ContextPageSummary,
  VersionEntry,
} from "../../shared/api/context";
import {
  createTreeFolder,
  deleteTreeNode,
  fetchTreeNodeIndexPreview,
  listTreeData,
  moveTreeNode,
  readTreeNode,
  reorderTreeNodeAt,
  updateTreeNode,
} from "../../shared/api/workspace";
import {
  FolderSummaryCard,
  nodeIdForPage,
} from "../../shared/components/FolderSummaryCard";
import type {
  TreeNodePayload,
  TreeSectionPolicy,
} from "../../shared/api/workspace";
import { AvatarWithTimestamp } from "../../shared/components/Avatar";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import {
  ContentBacklinksPanel,
  ContentPropertiesPanel,
  ContentSummaryPanel,
} from "../../shared/components/ContentInspectorPanels";
import { CountChip } from "../../shared/components/CountChip";
import {
  HistoryActionButton,
  HistoryCard,
  HistoryChangesButton,
  HistoryChangesUnavailable,
  HistoryLineDelta,
} from "../../shared/components/HistoryCard";
import { HistoryDiffDialog } from "../../shared/components/HistoryDiffDialog";
import { HistoryGraphList } from "../../shared/components/HistoryGraphList";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { WorkspaceFirstUseChecklist } from "../../shared/components/FirstUseChecklist";
import { NewItemDialog } from "../../shared/components/NewItemDialog";
import type { NewItemType } from "../../shared/components/NewItemDialog";
import { ReaderTabs } from "../../shared/components/ReaderTabs";
import { RecentItemsList } from "../../shared/components/RecentItemsList";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import { TabbedInspector } from "../../shared/components/TabbedInspector";
import { TreeNodeContextMenu } from "../../shared/components/TreeNodeContextMenu";
import type { TreeNodeContextMenuAction } from "../../shared/components/TreeNodeContextMenu";
import {
  DeleteTreeNodeDialog,
  MoveTreeNodeDialog,
  RenameTreeNodeDialog,
} from "../../shared/components/TreeNodeActionDialogs";
import { TreeRail } from "../../shared/components/TreeRail";
import { WorkspaceLayout } from "../../shared/components/WorkspaceLayout";
import { targetBreadcrumbs } from "../../shared/components/treeBreadcrumbs";
import { isSemanticSectionRoot } from "../../shared/components/treeSectionRoot";
import { displayConceptPath } from "../../shared/lib/conceptPath";
import { readContextPublishNotice } from "../../shared/lib/contextPublishNavigation";
import {
  contextNewHref,
  contextEditHref,
  contextPageHref,
  useRouteWorkspaceId,
  useWorkspaceRoute,
  workspacePath,
  workspaceTargetHref,
} from "../../shared/lib/workspaceRoutes";
import {
  resolveExpandedNodeIds,
  TREE_EXPANDED_PARAM,
  updateTreeSearchParams,
  writeExpandedNodeIdsToStorage,
} from "../../shared/components/treeRouteState";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { useScreenEntryRefresh } from "../../shared/hooks/useScreenEntryRefresh";
import { useDocumentTitle } from "../../shared/hooks/useDocumentTitle";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import {
  copyTextWithReason,
  type CopyTextDetailedResult,
} from "../../shared/lib/copyText";
import { ActivityEventCard } from "../../shared/components/ActivityEventCard";
import {
  contextHistoryNodes,
  contextHistoryPillTone,
  contextHistoryPresentation,
  priorRelationForVersion,
} from "../../shared/lib/contextHistory";
import {
  buildHistoryGraph,
  type HistoryGraphRow,
} from "../../shared/lib/historyGraph";
import { normalizeVersionPair } from "../../shared/lib/historyVersionPair";
import { useVersionPairDiff } from "../../shared/hooks/useVersionPairDiff";
import { domainLabel, humanizeEnum } from "../../shared/lib/domainLabel";
import { searchResultHref } from "../../shared/lib/searchResultHref";
import { isEditorRole } from "../../shared/workspace/permissions";
import { useOptionalAuth } from "../../shared/auth/AuthContext";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";

const CONTEXT_CHILD_PAGE_SIZE = 5;
const CONTEXT_FOLDER_CHILD_PAGE_PARAM = "childrenPage";
const CONTEXT_CHILD_TAB_PAGE_PARAM = "page";

type PublishedVersionInfo = {
  version_id: string;
  version_no: number;
  published_at: string;
};

type ContextDetailWithPublishedVersion = ContextPageDetail & {
  published_version_info?: PublishedVersionInfo | null;
};

type ContextState = {
  workspaceId: string;
  routeTargetId: string | null;
  pages: ContextPageSummary[];
  pagesCursor: string | null;
  pagesTotal: number | null;
  pagesHasMore: boolean;
  tree: TreeNodePayload[];
  treePolicies: Record<string, TreeSectionPolicy>;
  selectedNodeId: string | null;
  detail: ContextPageDetail | null;
  history: ContextChangeHistory | null;
  tombstones: LibraryItemTombstone[];
};

export function ContextScreen() {
  const actorId = useOptionalAuth()?.user?.user_id ?? null;
  const workspaceContext = useOptionalWorkspaceContext();
  const selectedId = useRouteWorkspaceId();
  const selectedWorkspace = workspaceContext?.selectedWorkspace?.id === selectedId
    ? workspaceContext.selectedWorkspace
    : workspaceContext?.workspaces?.find((workspace) => workspace.id === selectedId);
  const observedRole = selectedWorkspace?.role ?? null;
  // This is the owner of the whole Context payload and Reader, not just its
  // canvas. Replace it in this render so previous-authority content and pins
  // disappear before fresh reads; unmount cleanup also fences late requests.
  const authorityKey = JSON.stringify([actorId, selectedId, observedRole]);
  return <ScopedContextScreen key={authorityKey} />;
}

function ScopedContextScreen() {
  const { t } = useTranslation("context");
  // Codex #25: hide write affordances from viewers (the backend re-enforces).
  const auth = useOptionalAuth();
  const workspaceContext = useOptionalWorkspaceContext();
  const workspaceRoute = useWorkspaceRoute();
  const routeWorkspaceId = useRouteWorkspaceId();
  const routeWorkspace = workspaceContext?.selectedWorkspace?.id === routeWorkspaceId
    ? workspaceContext.selectedWorkspace
    : (workspaceContext?.workspaces ?? []).find(
        (workspace) => workspace.id === routeWorkspaceId,
      ) ?? null;
  const canEdit = isEditorRole(routeWorkspace?.role);
  const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([]);
  const [renameNode, setRenameNode] = useState<TreeNodePayload | null>(null);
  const [moveNode, setMoveNode] = useState<TreeNodePayload | null>(null);
  const [deleteNode, setDeleteNode] = useState<TreeNodePayload | null>(null);
  const [treeRefreshing, setTreeRefreshing] = useState(false);
  const [detailUnavailableNotice, setDetailUnavailableNotice] = useState<string | null>(null);
  const [treeCopyFeedback, setTreeCopyFeedback] =
    useState<CopyTextDetailedResult | null>(null);
  useEffect(() => {
    if (!treeCopyFeedback) {
      return undefined;
    }
    const timer = window.setTimeout(
      () => setTreeCopyFeedback(null),
      treeCopyFeedback.status === "copied" ? 1800 : 5000,
    );
    return () => window.clearTimeout(timer);
  }, [treeCopyFeedback]);
  // Fixed load-more page size — the page-size selector was removed workspace-wide.
  const pageSize = 10;
  const [draftFilter, setDraftFilter] = useState<"all" | "with" | "without">(
    "all",
  );
  const hasDraft = draftFilter === "all" ? undefined : draftFilter === "with";
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const routedPageId = workspaceRoute?.targetKind === "page"
    ? workspaceRoute.targetId
    : null;
  const [contextPublishNotice] = useState(() =>
    readContextPublishNotice(location.state),
  );
  const [publishArrivalPath] = useState(
    () => `${location.pathname}${location.search}`,
  );
  const requestedTargetId = workspaceRoute?.targetId ?? null;
  const folderCreateRoute = Boolean(
    workspaceRoute?.create && workspaceRoute.section === "context" &&
    workspaceRoute.targetKind === "folder",
  );
  const routeFolderParentId = searchParams.get("parent");
  const routeIdentity = `${routeWorkspaceId}:${location.pathname}`;
  const routeIdentityRef = useRef(routeIdentity);
  const [routeNotFoundKey, setRouteNotFoundKey] = useState<string | null>(null);
  useLayoutEffect(() => {
    routeIdentityRef.current = routeIdentity;
    return () => {
      if (routeIdentityRef.current === routeIdentity) {
        routeIdentityRef.current = "";
      }
    };
  }, [routeIdentity]);
  useEffect(() => {
    setRouteNotFoundKey(null);
  }, [routeIdentity]);
  const expansionWorkspaceRef = useRef<string | null>(null);
  const routeRevealKeyRef = useRef<string | null>(null);
  // Removing a selected page updates the in-memory tree before React Router
  // necessarily commits the matching query-param removal. Remember that
  // coordinated transition so the intermediate render does not mistake the
  // just-deleted target for an invalid deep link.
  const unavailableTargetIdRef = useRef<string | null>(null);
  useEffect(() => {
    if (!contextPublishNotice) {
      return;
    }
    void navigate(publishArrivalPath, { replace: true, state: null });
  }, [contextPublishNotice, navigate, publishArrivalPath]);
  const {
    data: state,
    revision: overviewRevision,
    error,
    reload,
    setData: setState,
    setError,
  } = useAsyncLoad<ContextState>(
    async () => {
      const workspaceId = routeWorkspaceId;
      if (!workspaceId) {
        throw new Error(
          t("errors.workspaceNotFound", {
            defaultValue: "Workspace not found",
          }),
        );
      }
      const [pageData, treeData, tombstones] = await Promise.all([
        listContextPages(workspaceId, {
          has_draft: hasDraft,
          limit: pageSize,
        }),
        listTreeData(workspaceId, "context"),
        listLibraryTombstones(workspaceId).catch(() => []),
      ]);
      const pages = pageData.data;
      const tree = treeData.nodes;
      if (workspaceRoute?.targetKind === "folder" && workspaceRoute.targetId) {
        const folder = tree.find((node) => node.id === workspaceRoute.targetId) ??
          await readTreeNode(workspaceId, workspaceRoute.targetId);
        if (folder.section !== "context" || folder.node_kind !== "folder") {
          throw new ApiError(404, {
            error_code: "not_found",
            message: "Folder not found.",
            request_id: "",
            warnings: [],
          });
        }
      }
      const selectedNodeId = resolveRequestedNodeId(
        tree,
        pages,
        requestedTargetId,
      );
      return {
        workspaceId,
        routeTargetId: requestedTargetId,
        pages,
        pagesCursor: pageData.next_cursor,
        pagesTotal: pageData.total,
        pagesHasMore: pageData.has_more,
        tree,
        treePolicies: treeData.policies,
        selectedNodeId,
        detail: null,
        history: null,
        tombstones,
      };
    },
    [pageSize, draftFilter, routeWorkspaceId, routedPageId, requestedTargetId, workspaceRoute?.targetKind],
    {
      errorMessage: t("errors.load", {
        defaultValue: "Failed to load Context",
      }),
      formatError: (caught, fallback) => {
        if (
          caught instanceof ApiError &&
          caught.status === 404 &&
          workspaceRoute?.targetKind === "folder"
        ) {
          setRouteNotFoundKey(routeIdentity);
          return t("errors.itemUnavailable", {
            defaultValue: "That Context page is no longer available.",
          });
        }
        const workspaceNotFound = t("errors.workspaceNotFound", {
          defaultValue: "Workspace not found",
        });
        return caught instanceof Error && caught.message === workspaceNotFound
          ? workspaceNotFound
          : apiErrorMessage(caught, fallback);
      },
      fallbackData: {
        workspaceId: routeWorkspaceId,
        routeTargetId: requestedTargetId,
        pages: [],
        pagesCursor: null,
        pagesTotal: null,
        pagesHasMore: false,
        tree: [],
        treePolicies: {},
        selectedNodeId: null,
        detail: null,
        history: null,
        tombstones: [],
      },
      onSuccess: (nextState) => {
        setExpandedNodeIds((current) => {
          const workspaceChanged =
            expansionWorkspaceRef.current !== nextState.workspaceId;
          expansionWorkspaceRef.current = nextState.workspaceId;
          if (workspaceChanged) {
            routeRevealKeyRef.current = null;
          }
          const next = resolveExpandedNodeIds(
            nextState.tree,
            workspaceChanged
              ? searchParams.get(TREE_EXPANDED_PARAM)
              : current.join(","),
            workspaceChanged
              ? (nextState.selectedNodeId ?? nextState.detail?.id ?? null)
              : null,
            { defaultMode: "none", storageKey: "context", workspaceId: nextState.workspaceId },
          );
          writeExpandedNodeIdsToStorage("context", next, nextState.workspaceId);
          return next;
        });
      },
    },
  );

  useScreenEntryRefresh(workspacePath(routeWorkspaceId, "context"), reload, {
    isHome: requestedTargetId === null && !workspaceRoute?.create,
    queryDeps: [pageSize, draftFilter, routeWorkspaceId, routedPageId, requestedTargetId, workspaceRoute?.targetKind],
    readArrivalNotice: readContextPublishNotice,
  });

  const workspaceId = state?.workspaceId === routeWorkspaceId
    ? state.workspaceId
    : routeWorkspaceId;
  const replaceContextRouteTarget = useCallback(
    (targetId: string | null, nodes = state?.tree ?? []) => {
      const pageId = selectedPageIdForNode(nodes, state?.pages ?? [], targetId);
      const nextParams = updateTreeSearchParams(searchParams, { versionId: null });
      nextParams.delete(CONTEXT_FOLDER_CHILD_PAGE_PARAM);
      nextParams.delete(CONTEXT_CHILD_TAB_PAGE_PARAM);
      const query = nextParams.toString();
      void navigate({
        pathname: pageId
          ? contextPageHref(workspaceId, pageId)
          : targetId
            ? workspaceTargetHref(workspaceId, "context", targetId, "folder")
            : workspacePath(workspaceId, "context"),
        search: query ? `?${query}` : "",
      }, { replace: true });
    },
    [navigate, searchParams, state?.pages, state?.tree, workspaceId],
  );
  // The loader fetches page 1 (state.pages); these accumulate the appended
  // pages on top of it. Keyed on the base page so a fresh first page (page-size
  // change / reload) drops the appended tail. Derived synchronously so Load
  // more always reads the live cursor rather than a one-render-stale one.
  const overviewBaseKey = `${workspaceId}:${pageSize}:${draftFilter}:${overviewRevision}:${
    state?.pages.length ?? 0
  }:${state?.pages[0]?.id ?? ""}`;
  const [appended, setAppended] = useState<{
    key: string;
    items: ContextPageSummary[];
    cursor: string | null;
  }>({ key: overviewBaseKey, items: [], cursor: state?.pagesCursor ?? null });
  const [loadingMore, setLoadingMore] = useState(false);
  const overviewGenerationRef = useRef(0);
  useEffect(() => {
    overviewGenerationRef.current += 1;
    setLoadingMore(false);
    setAppended({
      key: overviewBaseKey,
      items: [],
      cursor: state?.pagesCursor ?? null,
    });
  }, [overviewBaseKey, state?.pages, state?.pagesCursor]);

  // When the accumulator key matches the current base its cursor is
  // authoritative (it starts at the base cursor and advances/exhausts to null);
  // only fall back to the base cursor during the one render before the reset
  // effect re-keys the accumulator to a fresh first page.
  const appendedForBase = appended.key === overviewBaseKey ? appended : null;
  const overviewItems = [
    ...(state?.pages ?? []),
    ...(appendedForBase?.items ?? []),
  ];
  const overviewCursor = appendedForBase
    ? appendedForBase.cursor
    : (state?.pagesCursor ?? null);
  const overviewHasMore = overviewCursor !== null;
  const overviewTotal = state?.pagesTotal ?? null;
  const draftStateLabel = t("filters.draftState", {
    defaultValue: "Draft state",
  });
  const draftFilterControl = canEdit ? (
    <div className="context-list-filters">
      <span className="eyebrow">{draftStateLabel}</span>
      <label className="select-field compact">
        <select
          aria-label={draftStateLabel}
          value={draftFilter}
          onChange={(event) =>
            setDraftFilter(event.target.value as "all" | "with" | "without")
          }
        >
          <option value="all">
            {t("filters.draftOptions.all", {
              defaultValue: "All pages",
            })}
          </option>
          <option value="with">
            {t("filters.draftOptions.with", {
              defaultValue: "Has unpublished draft",
            })}
          </option>
          <option value="without">
            {t("filters.draftOptions.without", {
              defaultValue: "No unpublished draft",
            })}
          </option>
        </select>
      </label>
    </div>
  ) : null;

  async function loadMoreOverview() {
    if (!overviewCursor || loadingMore) {
      return;
    }
    const requestCursor = overviewCursor;
    const requestGeneration = overviewGenerationRef.current;
    setLoadingMore(true);
    try {
      const next = await listContextPages(workspaceId, {
        has_draft: hasDraft,
        limit: pageSize,
        cursor: requestCursor,
      });
      setAppended((current) =>
        // The successful-load revision distinguishes refreshes even when the
        // first item and cursor are unchanged. Older requests belong to that
        // prior first page and must not append to the refreshed overview.
        overviewGenerationRef.current === requestGeneration &&
        current.key === overviewBaseKey && current.cursor === requestCursor
          ? {
              key: current.key,
              items: [...current.items, ...next.data],
              cursor: next.next_cursor,
            }
          : current,
      );
    } catch (exc) {
      if (requestGeneration === overviewGenerationRef.current) {
        setError(apiErrorMessage(exc, t("errors.load")));
      }
    } finally {
      if (requestGeneration === overviewGenerationRef.current) {
        setLoadingMore(false);
      }
    }
  }

  function selectPage(pageId: string) {
    if (!state) {
      return;
    }
    setError(null);
    setDetailUnavailableNotice(null);
    const nextParams = updateTreeSearchParams(searchParams, { versionId: null });
    nextParams.delete("tab");
    nextParams.delete("compareFrom");
    nextParams.delete("compareTo");
    nextParams.delete("reconcile");
    nextParams.delete(CONTEXT_FOLDER_CHILD_PAGE_PARAM);
    nextParams.delete(CONTEXT_CHILD_TAB_PAGE_PARAM);
    const query = nextParams.toString();
    void navigate({
      pathname: contextPageHref(workspaceId, pageId),
      search: query ? `?${query}` : "",
    }, { replace: true });
  }

  function selectNode(node: TreeNodePayload) {
    setDetailUnavailableNotice(null);
    if (node.target_kind === "context_page" && node.target_id) {
      selectPage(node.target_id);
      return;
    }
    const nextParams = updateTreeSearchParams(searchParams, { versionId: null });
    nextParams.delete("tab");
    nextParams.delete("compareFrom");
    nextParams.delete("compareTo");
    nextParams.delete("reconcile");
    nextParams.delete(CONTEXT_FOLDER_CHILD_PAGE_PARAM);
    nextParams.delete(CONTEXT_CHILD_TAB_PAGE_PARAM);
    const query = nextParams.toString();
    void navigate({
      pathname: workspaceTargetHref(workspaceId, "context", node.id, "folder"),
      search: query ? `?${query}` : "",
    }, { replace: true });
  }

  async function createFolder(input: {
    parentNodeId: string | null;
    slug?: string;
    title: string;
  }) {
    if (!state) {
      return;
    }
    const originRouteIdentity = routeIdentity;
    setError(null);
    const folder = await createTreeFolder(state.workspaceId, {
      parent_id: input.parentNodeId,
      section: "context",
      slug: input.slug,
      title: input.title,
    });
    if (routeIdentityRef.current !== originRouteIdentity) return;
    const nextExpandedNodeIds = expandNodeIds(
      expandedNodeIds,
      folder.parent_id,
    );
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    const nextParams = updateTreeSearchParams(searchParams, { versionId: null });
    nextParams.delete("tab");
    nextParams.delete("compareFrom");
    nextParams.delete("compareTo");
    nextParams.delete(CONTEXT_FOLDER_CHILD_PAGE_PARAM);
    nextParams.delete(CONTEXT_CHILD_TAB_PAGE_PARAM);
    const query = nextParams.toString();
    void navigate({
      pathname: workspaceTargetHref(state.workspaceId, "context", folder.id, "folder"),
      search: query ? `?${query}` : "",
    }, { replace: true });
    setState((current) =>
      current
        ? {
            ...current,
            tree: [...current.tree, folder],
            selectedNodeId: folder.id,
            detail: null,
            history: null,
          }
        : current,
    );
  }

  function createPage(input: { parentNodeId: string | null; title: string }) {
    const href = contextNewHref(workspaceId, input.parentNodeId);
    const titleQuery = input.title.trim()
      ? `${href.includes("?") ? "&" : "?"}title=${encodeURIComponent(input.title.trim())}`
      : "";
    navigate(`${href}${titleQuery}`);
  }

  async function refreshTree(
    options: {
      clearSelection?: boolean;
      expandedNodeIds?: string[];
      refreshOverview?: boolean;
      targetNodeId?: string | null;
    } = {},
  ) {
    if (!state) {
      return;
    }
    const originRouteIdentity = routeIdentity;
    const [treeData, pageData] = await Promise.all([
      listTreeData(state.workspaceId, "context"),
      options.refreshOverview
        ? listContextPages(state.workspaceId, {
            has_draft: hasDraft,
            limit: pageSize,
          })
        : Promise.resolve(null),
    ]);
    if (routeIdentityRef.current !== originRouteIdentity) return;
    if (pageData) {
      const pages = pageData.data;
      setAppended({
        key: `${state.workspaceId}:${pageSize}:${draftFilter}:${overviewRevision}:${pages.length}:${pages[0]?.id ?? ""}`,
        items: [],
        cursor: pageData.next_cursor,
      });
    }
    const selectedNodeId = options.clearSelection
      ? null
      : (options.targetNodeId ?? state.selectedNodeId);
    const nextExpandedNodeIds =
      options.expandedNodeIds ??
      resolveExpandedNodeIds(treeData.nodes, expandedNodeIds.join(","), null, {
        defaultMode: "none",
      });
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    setState((current) =>
      current
        ? {
            ...current,
            pages: pageData ? pageData.data : current.pages,
            pagesCursor: pageData
              ? pageData.next_cursor
              : current.pagesCursor,
            pagesTotal: pageData ? pageData.total : current.pagesTotal,
            pagesHasMore: pageData ? pageData.has_more : current.pagesHasMore,
            tree: treeData.nodes,
            treePolicies: treeData.policies,
            selectedNodeId,
            detail: options.clearSelection ? null : current.detail,
            history: options.clearSelection ? null : current.history,
          }
        : current,
    );
    // A plain rail refresh must preserve the URL's existing target form. A
    // page id and its tree-node id resolve to the same selection, but rewriting
    // between them changes the route-reveal key and would reopen a folder that
    // the actor deliberately collapsed. Selection-changing operations pass an
    // explicit target (or clearSelection) and still update the route.
    if (options.clearSelection === true || options.targetNodeId !== undefined) {
      replaceContextRouteTarget(selectedNodeId);
    }
  }

  async function refreshTreeFromRail() {
    if (treeRefreshing) {
      return;
    }
    setTreeRefreshing(true);
    setError(null);
    try {
      await refreshTree({ refreshOverview: true });
    } catch (exc) {
      setError(
        apiErrorMessage(
          exc,
          t("errors.refreshTree", {
            defaultValue: "Could not refresh the Context tree. Try again.",
          }),
        ),
      );
    } finally {
      setTreeRefreshing(false);
    }
  }

  async function saveNavSummary(
    node: TreeNodePayload,
    navSummary: string | null,
  ) {
    if (!state) {
      return;
    }
    setError(null);
    await updateTreeNode(state.workspaceId, node.id, {
      display_name: node.title,
      nav_summary: navSummary,
    });
    await refreshTree({ targetNodeId: node.id });
  }

  const folderChildMeta = (child: TreeNodePayload) => {
    const listedPage = child.target_id
      ? state?.pages.find((candidate) => candidate.id === child.target_id)
      : null;
    const pageType = child.target_meta?.page_type ?? listedPage?.page_type;
    const lifecycleStatus =
      child.target_meta?.lifecycle_status ?? listedPage?.lifecycle_status;
    return {
      subtitle: pageType
        ? `${domainLabel("context", "pageType", pageType)} · ${lifecycleStatus ?? ""}`
        : "",
      pillStatus: child.target_kind ?? child.node_kind,
      pillLabel: domainLabel(
        "context",
        "nodeKind",
        child.target_kind ?? child.node_kind,
      ),
    };
  };

  async function renameTreeNode(node: TreeNodePayload, displayName: string) {
    if (!state) {
      return;
    }
    setError(null);
    await updateTreeNode(state.workspaceId, node.id, {
      display_name: displayName,
    });
    await refreshTree({ targetNodeId: node.id });
  }

  async function moveTreeNodeToParent(
    node: TreeNodePayload,
    newParentId: string,
    reason: string,
  ) {
    if (!state) {
      return;
    }
    setError(null);
    await moveTreeNode(state.workspaceId, {
      new_parent_id: newParentId,
      node_id: node.id,
      ...(reason.trim() ? { reason: reason.trim() } : {}),
    });
    await refreshTree({
      expandedNodeIds: expandNodeIds(expandedNodeIds, newParentId),
      targetNodeId: node.id,
    });
  }

  async function moveTreeNodeByDrop(
    node: TreeNodePayload,
    newParentNode: TreeNodePayload,
  ) {
    try {
      await moveTreeNodeToParent(
        node,
        newParentNode.id,
        "Drag-and-drop move in Context explorer",
      );
    } catch (exc) {
      setError(
        apiErrorMessage(
          exc,
          t("errors.moveTreeNode", {
            defaultValue: "Could not move the Context item. Try again.",
          }),
        ),
      );
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
    setError(null);
    try {
      await reorderTreeNodeAt(
        state.workspaceId,
        node.id,
        anchorNode.id,
        placement,
      );
      await refreshTree({ targetNodeId: node.id });
    } catch (exc) {
      setError(
        apiErrorMessage(
          exc,
          t("errors.reorderTreeNode", {
            defaultValue: "Could not reorder the Context item. Try again.",
          }),
        ),
      );
      // Rethrow so TreeRail's live region announces the failed move rather
      // than a landing.
      throw exc;
    }
  }

  async function deleteTreeNodeById(node: TreeNodePayload) {
    if (!state) {
      return;
    }
    setError(null);
    const deletedNodeIds = collectSubtreeNodeIds(state.tree, node.id);
    const clearsSelection = state.selectedNodeId
      ? deletedNodeIds.has(state.selectedNodeId)
      : false;
    await deleteTreeNode(state.workspaceId, node.id);
    if (clearsSelection && requestedTargetId) {
      unavailableTargetIdRef.current = requestedTargetId;
    }
    await refreshTree({
      clearSelection: clearsSelection,
      refreshOverview: true,
    });
  }

  async function reloadContextAfterRestore(pageId: string) {
    if (!state || requestedTargetId !== pageId) {
      return;
    }
    const originRouteIdentity = routeIdentity;
    setError(null);
    const [pageData, treeData, detail, history] =
      await Promise.all([
        listContextPages(state.workspaceId, {
          has_draft: hasDraft,
          limit: pageSize,
        }),
        listTreeData(state.workspaceId, "context"),
        readContextPage(state.workspaceId, pageId),
        listContextChanges(state.workspaceId, pageId),
      ]);
    if (routeIdentityRef.current !== originRouteIdentity) return;
    if (requestedTargetId !== pageId) return;
    const pages = pageData.data;
    const targetNodeId = nodeIdForPage(treeData.nodes, pageId) ?? pageId;
    const nextExpandedNodeIds = resolveExpandedNodeIds(
      treeData.nodes,
      expandedNodeIds.join(","),
      null,
      { defaultMode: "none", storageKey: "context" },
    );
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    setState((current) =>
      current
        ? {
            ...current,
            pages,
            pagesCursor: pageData.next_cursor,
            pagesTotal: pageData.total,
            pagesHasMore: pageData.has_more,
            tree: treeData.nodes,
            treePolicies: treeData.policies,
            selectedNodeId: targetNodeId,
            detail,
            history,
          }
        : current,
    );
    // One mutation, computed from the params as they are NOW. Reconciliation
    // used to close by deleting `reconcile` and then call this reload, which
    // rebuilt the URL from a `searchParams` snapshot captured before that
    // delete -- putting `reconcile` straight back and re-opening the workbench
    // on a branch the user had just consumed. Clearing it here means the close
    // and the reload cannot disagree about which URL is current.
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete(CONTEXT_FOLDER_CHILD_PAGE_PARAM);
    nextParams.delete(CONTEXT_CHILD_TAB_PAGE_PARAM);
    nextParams.delete("reconcile");
    const query = nextParams.toString();
    void navigate({
      pathname: contextPageHref(workspaceId, pageId),
      search: query ? `?${query}` : "",
    }, { replace: true });
  }

  async function copyTreeNodeLink(node: TreeNodePayload) {
    const path =
      node.target_kind === "context_page" && node.target_id
        ? contextPageHref(workspaceId, node.target_id)
        : workspaceTargetHref(workspaceId, "context", node.id, "folder");
    const href =
      typeof window === "undefined" ? path : `${window.location.origin}${path}`;
    return copyTextWithReason(href);
  }

  function setStoredExpandedNodeIds(nodeIds: string[]) {
    setExpandedNodeIds(nodeIds);
    writeExpandedNodeIdsToStorage("context", nodeIds, workspaceId);
  }

  useEffect(() => {
    if (
      unavailableTargetIdRef.current &&
      requestedTargetId !== unavailableTargetIdRef.current
    ) {
      unavailableTargetIdRef.current = null;
    }
  }, [requestedTargetId]);

  useEffect(() => {
    if (
      !state?.workspaceId ||
      state.workspaceId !== routeWorkspaceId ||
      state.routeTargetId !== requestedTargetId
    ) {
      return;
    }
    if (unavailableTargetIdRef.current === requestedTargetId) {
      return;
    }
    const targetNodeId = resolveRequestedNodeId(
      state.tree,
      state.pages,
      requestedTargetId,
    );
    if (!targetNodeId && !routedPageId) {
      routeRevealKeyRef.current = `${state.workspaceId}:${requestedTargetId ?? ""}:${searchParams.get(TREE_EXPANDED_PARAM) ?? ""}`;
      if (
        (state.selectedNodeId ||
          state.detail ||
          state.history)
      ) {
        setState((current) =>
          current
            ? {
                ...current,
                selectedNodeId: null,
                detail: null,
                history: null,
              }
            : current,
        );
      }
      return;
    }

    const routeRevealKey = `${state.workspaceId}:${requestedTargetId ?? ""}:${searchParams.get(TREE_EXPANDED_PARAM) ?? ""}`;
    if (routeRevealKeyRef.current !== routeRevealKey) {
      routeRevealKeyRef.current = routeRevealKey;
      setExpandedNodeIds((current) => {
        const next = resolveExpandedNodeIds(
          state.tree,
          searchParams.get(TREE_EXPANDED_PARAM) ?? current.join(","),
          targetNodeId,
          { defaultMode: "none" },
        );
        if (
          next.length === current.length &&
          next.every((nodeId, index) => nodeId === current[index])
        ) {
          return current;
        }
        writeExpandedNodeIdsToStorage("context", next, workspaceId);
        return next;
      });
    }

    const selectedPageId = routedPageId ?? selectedPageIdForNode(
      state.tree,
      state.pages,
      targetNodeId,
    );
    if (!selectedPageId) {
      if (
        state.selectedNodeId !== targetNodeId ||
        state.detail ||
        state.history
      ) {
        setState((current) =>
          current
            ? {
                ...current,
                selectedNodeId: targetNodeId,
                detail: null,
                history: null,
              }
            : current,
        );
      }
      return;
    }
    if (
      state.selectedNodeId === targetNodeId &&
      state.detail?.id === selectedPageId
    ) {
      return;
    }

    let cancelled = false;
    setError(null);
    void (async () => {
      const detail = await readContextPage(state.workspaceId, selectedPageId);
      if (detail.workspace_id !== state.workspaceId || detail.id !== selectedPageId) {
        throw new ApiError(404, {
          error_code: "not_found",
          message: "Context page not found.",
          request_id: "",
          warnings: [],
        });
      }
      const history = await listContextChanges(state.workspaceId, selectedPageId);
      return [detail, history] as const;
    })()
      .then(([detail, history]) => {
        if (cancelled) {
          return;
        }
        setState((current) =>
          current
            ? {
                ...current,
                selectedNodeId: targetNodeId,
                detail,
                history,
              }
            : current,
        );
      })
      .catch((exc) => {
        if (cancelled) {
          return;
        }
        if (
          routedPageId &&
          exc instanceof ApiError &&
          [403, 404, 422].includes(exc.status)
        ) {
          setRouteNotFoundKey(routeIdentity);
          return;
        }
        if (exc instanceof ApiError && exc.status === 404) {
          unavailableTargetIdRef.current = requestedTargetId ?? selectedPageId;
          setAppended((current) => ({
            ...current,
            items: current.items.filter(
              (page) => page.id !== selectedPageId,
            ),
          }));
          setState((current) =>
            current
              ? {
                  ...current,
                  pages: current.pages.filter(
                    (page) => page.id !== selectedPageId,
                  ),
                  pagesTotal:
                    current.pagesTotal !== null
                      ? Math.max(0, current.pagesTotal - 1)
                      : current.pagesTotal,
                  tree: current.tree.filter(
                    (node) => node.target_id !== selectedPageId,
                  ),
                  selectedNodeId: null,
                  detail: null,
                  history: null,
                }
              : current,
          );
          replaceContextRouteTarget(null);
          const unavailableMessage = t("errors.itemUnavailable", {
            defaultValue:
              "That Context page is no longer available. It was removed from the list.",
          });
          setDetailUnavailableNotice(unavailableMessage);
          setError(unavailableMessage);
          return;
        }
        setError(
          apiErrorMessage(
            exc,
            t("errors.loadDetail", {
              defaultValue: "Could not load the Context page. Try again.",
            }),
          ),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [
    requestedTargetId,
    routedPageId,
    replaceContextRouteTarget,
    routeIdentity,
    routeWorkspaceId,
    searchParams,
    setError,
    setState,
    state,
    t,
    workspaceId,
  ]);

  useDocumentTitle(state?.detail?.title ?? null, " - ");

  if (routeNotFoundKey === routeIdentity) {
    return <NotFoundScreen />;
  }
  if (!state || state.workspaceId !== routeWorkspaceId) {
    return (
      <RouteLoading label={t("loading", { defaultValue: "Loading Context" })} />
    );
  }
  if (routedPageId && state.detail?.id !== routedPageId && !error) {
    return <RouteLoading label={t("loading", { defaultValue: "Loading Context" })} />;
  }
  if (
    requestedTargetId &&
    requestedTargetId !== unavailableTargetIdRef.current &&
    state.workspaceId &&
    !error &&
    !resolveRequestedNodeId(state.tree, state.pages, requestedTargetId)
  ) {
    return <NotFoundScreen />;
  }
  const selectedNode = state.selectedNodeId
    ? (state.tree.find((node) => node.id === state.selectedNodeId) ?? null)
    : null;
  const favoriteScope =
    auth?.user && state.workspaceId
      ? { actorId: auth.user.user_id, workspaceId: state.workspaceId }
      : undefined;
  const contextRoot =
    state.tree.find((node) =>
      isSemanticSectionRoot(node, t("rail.title", { defaultValue: "Context" })),
    ) ?? null;
  const contextPolicy = state.treePolicies.context;
  const routeFolderParent = routeFolderParentId
    ? state.tree.find((node) => node.id === routeFolderParentId) ?? null
    : null;
  const routeFolderParentValid = !routeFolderParentId || Boolean(
    routeFolderParent?.section === "context" && routeFolderParent.can_have_children,
  );
  const activeNewItemDialog = folderCreateRoute && canEdit && routeFolderParentValid;
  // F-5: folder destinations the new-item dialog's location picker can target.
  const contextFolderNodes = state.tree.filter(
    (node) => node.can_have_children,
  );
  const newItemParentNodeId = selectedNode?.can_have_children
    ? selectedNode.id
    : (selectedNode?.parent_id ?? null);
  function openNewItem(itemType: NewItemType, parentNodeId: string | null) {
    if (itemType === "page") {
      navigate(contextNewHref(workspaceId, parentNodeId));
      return;
    }
    const parentQuery = parentNodeId
      ? `?parent=${encodeURIComponent(parentNodeId)}`
      : "";
    navigate(`${workspacePath(workspaceId, "context/folders/new")}${parentQuery}`);
  }
  const moveTargetNodes = moveNode
    ? selectableMoveTargets(state.tree, moveNode)
    : [];
  if (folderCreateRoute && (!canEdit || !routeFolderParentValid)) {
    return <NotFoundScreen />;
  }
  return (
    <>
      <WorkspaceLayout
        defaultInspectorOpen={false}
        inspectorLabel={t("layout.details", { defaultValue: "Details" })}
        layoutKey="context"
        leftLabel={t("layout.explorer", { defaultValue: "Explorer" })}
        mobileAction={
          canEdit
            ? {
                ariaLabel: t("actions.createItem", {
                  defaultValue: "Create Context item",
                }),
                label: t("actions.new", { defaultValue: "New" }),
                onClick: () => openNewItem("page", newItemParentNodeId),
                title: t("actions.createItem", {
                  defaultValue: "Create Context item",
                }),
              }
            : undefined
        }
        variant="context"
        left={({
          expandWidthLabel,
          expandWidthAtMax,
          onClose,
          onExpandWidth,
        }) => (
          <TreeRail
            favoriteScope={favoriteScope}
            title={t("rail.title", { defaultValue: "Context" })}
            accent="context"
            nodes={state.tree}
            activeTargetId={state.detail?.id}
            activeNodeId={state.selectedNodeId}
            expandedNodeIds={expandedNodeIds}
            forceActivePathExpanded={false}
            emptyLabel={t("rail.empty", {
              defaultValue: "No Context tree nodes",
            })}
            hideSystemRootNodes
            expandWidthLabel={expandWidthLabel}
            expandWidthAtMax={expandWidthAtMax}
            canCreateFolder={
              canEdit && contextPolicy?.allow_user_folder_create === true
            }
            canCreatePage={canEdit}
            onCreateItem={
              canEdit
                ? ({ itemType, parentNodeId }) => {
                    openNewItem(itemType, parentNodeId);
                  }
                : undefined
            }
            onCopyNodeLink={(node) => {
              void copyTreeNodeLink(node).then(setTreeCopyFeedback);
            }}
            onDeleteNode={canEdit ? setDeleteNode : undefined}
            onDropNode={canEdit ? moveTreeNodeByDrop : undefined}
            onClose={onClose}
            onExpandWidth={onExpandWidth}
            onExpandedNodeIdsChange={(nodeIds) => {
              setStoredExpandedNodeIds(nodeIds);
            }}
            onMoveNode={canEdit ? setMoveNode : undefined}
            onReorderNodeAt={canEdit ? reorderTreeNodeToAnchor : undefined}
            onRenameNode={canEdit ? setRenameNode : undefined}
            onRefresh={refreshTreeFromRail}
            refreshing={treeRefreshing}
            onSelectTarget={(node) => {
              selectNode(node);
            }}
          />
        )}
        main={
          <section className="feature-main feature-main--reader context-main">
            <Breadcrumbs
              items={targetBreadcrumbs({
                basePath: workspacePath(workspaceId, "context"),
                workspaceId: state.workspaceId,
                rootLabel: t("breadcrumb.root", { defaultValue: "Context" }),
                nodes: state.tree,
                targetId: state.detail?.id,
                targetNodeId: state.selectedNodeId,
                fallbackLabel: state.detail?.title,
              })}
            />
            {!state.detail ? (
              <div className="feature-header">
                <div>
                  <p className="eyebrow">
                    {t("header.eyebrow", { defaultValue: "Knowledge base" })}
                  </p>
                  <h1>{t("header.title", { defaultValue: "Context" })}</h1>
                  <p className="feature-subtitle">
                    {t("header.subtitle", {
                      defaultValue:
                        "Durable pages the team and the AI both read. Drafts stay out of answers until they are published.",
                    })}
                  </p>
                </div>
                {/* Library and Agent both put their primary create action in this
                slot; without it the only way to start a page from the list is
                the tree rail's small +. Editors only — viewers cannot create. */}
                {canEdit ? (
                  <div className="feature-header-actions">
                    <Link className="icon-button primary" to={contextNewHref(workspaceId, newItemParentNodeId)}>
                      <Plus size={16} aria-hidden="true" />
                      <span>
                        {t("header.newPage", {
                          defaultValue: "New Context page",
                        })}
                      </span>
                    </Link>
                  </div>
                ) : null}
              </div>
            ) : null}
            {contextPublishNotice ? (
              <StatusBanner
                tone={
                  contextPublishNotice.kind === "review_failed" ? "warning" : "info"
                }
              >
                {t(`publishArrival.${contextPublishNotice.kind}`, {
                  defaultValue: "Published.",
                  error:
                    contextPublishNotice.kind === "review_failed"
                      ? contextPublishNotice.error
                      : undefined,
                })}
              </StatusBanner>
            ) : null}
            {detailUnavailableNotice || error ? (
              <StatusBanner tone="warning">
                {detailUnavailableNotice ?? error}
              </StatusBanner>
            ) : null}
            {treeCopyFeedback ? (
              <StatusBanner
                tone={
                  treeCopyFeedback.status === "unavailable" ? "warning" : "info"
                }
              >
                {treeCopyFeedback.status === "copied"
                  ? t("actions.linkCopied")
                  : treeCopyFeedback.status === "selected"
                    ? t("actions.linkSelected")
                    : t(
                        `actions.linkCopyReasons.${treeCopyFeedback.reason ?? "selection"}`,
                      )}
              </StatusBanner>
            ) : null}
            {state.detail ? (
              <ContextReader
                key={`${state.workspaceId}:${state.detail.id}:${state.detail.version_info?.version_id ?? ""}`}
                canEdit={canEdit}
                detail={state.detail}
                favoriteScope={favoriteScope}
                history={state.history}
                node={selectedNode}
                nodes={state.tree}
                tombstones={state.tombstones}
                workspaceId={state.workspaceId || null}
                onCopyNodeLink={copyTreeNodeLink}
                onDeleteNode={setDeleteNode}
                onMoveNode={setMoveNode}
                onRenameNode={setRenameNode}
                onSelectNode={selectNode}
                onReviewDecided={() =>
                  reloadContextAfterRestore(state.detail!.id)
                }
                onVersionRestored={() =>
                  reloadContextAfterRestore(state.detail!.id)
                }
              />
            ) : selectedNode ? (
              <ContextFolderSummaryCard
                canEdit={canEdit}
                childMeta={folderChildMeta}
                eyebrow={domainLabel(
                  "context",
                  "nodeKind",
                  selectedNode.node_kind,
                )}
                fallbackSummary={selectedNode.nav_summary_auto ?? null}
                loadIndexPreview={
                  selectedNode.node_kind === "folder"
                    ? () =>
                        fetchTreeNodeIndexPreview(
                          state.workspaceId,
                          selectedNode.id,
                        )
                    : undefined
                }
                node={selectedNode}
                nodes={state.tree}
                onSaveNavSummary={(navSummary) =>
                  saveNavSummary(selectedNode, navSummary)
                }
                onSelectNode={selectNode}
                workspaceId={state.workspaceId}
              />
            ) : contextRoot ? (
              <>
                <ContextFolderSummaryCard
                  canEdit={false}
                  childMeta={folderChildMeta}
                  eyebrow={t("home.eyebrow", { defaultValue: "Context home" })}
                  fallbackSummary={contextRoot.nav_summary_auto ?? null}
                  loadIndexPreview={() =>
                    fetchTreeNodeIndexPreview(state.workspaceId, contextRoot.id)
                  }
                  node={contextRoot}
                  nodes={state.tree}
                  onSelectNode={selectNode}
                  workspaceId={state.workspaceId}
                />
                <RecentItemsList
                  kind="context"
                  items={overviewItems}
                  total={overviewTotal}
                  hasMore={overviewHasMore}
                  loading={loadingMore}
                  onLoadMore={loadMoreOverview}
                  onSelect={selectPage}
                  activeId={null}
                  headingLabel={t("panel.recentHeading", {
                    defaultValue: "Recent updates",
                  })}
                  emptyHeading={t("empty.title", {
                    defaultValue: "No Context pages yet",
                  })}
                  loadMoreLabel={t("list.loadMore", {
                    defaultValue: "Load more",
                  })}
                  loadingMoreLabel={t("list.loadingMore", {
                    defaultValue: "Loading…",
                  })}
                  noun={t(
                    (overviewTotal ?? overviewItems.length) === 1
                      ? "units.item_one"
                      : "units.item_other",
                    {
                      defaultValue:
                        (overviewTotal ?? overviewItems.length) === 1
                          ? "item"
                          : "items",
                    },
                  )}
                  headerAction={draftFilterControl}
                  renderRow={(page) => (
                    <ContextHomeRecentRow page={page} onSelect={selectPage} />
                  )}
                />
              </>
            ) : overviewItems.length === 0 ? (
              <>
                {draftFilterControl}
                <ContextPageList
                  activeId={null}
                  onSelect={selectPage}
                  pages={[]}
                  workspaceId={routeWorkspaceId}
                />
              </>
            ) : (
              <>
                <RecentItemsList
                  kind="context"
                  items={overviewItems}
                  total={overviewTotal}
                  hasMore={overviewHasMore}
                  loading={loadingMore}
                  onLoadMore={loadMoreOverview}
                  onSelect={selectPage}
                  activeId={null}
                  headingLabel={t("panel.recentHeading", {
                    defaultValue: "Recent updates",
                  })}
                  emptyHeading={t("empty.title", {
                    defaultValue: "No Context pages yet",
                  })}
                  loadMoreLabel={t("list.loadMore", {
                    defaultValue: "Load more",
                  })}
                  loadingMoreLabel={t("list.loadingMore", {
                    defaultValue: "Loading…",
                  })}
                  noun={t(
                    (overviewTotal ?? overviewItems.length) === 1
                      ? "units.item_one"
                      : "units.item_other",
                    {
                      defaultValue:
                        (overviewTotal ?? overviewItems.length) === 1
                          ? "item"
                          : "items",
                    },
                  )}
                  headerAction={draftFilterControl}
                  renderRow={(page, isActive) => {
                    // U-7: a never-published page is itself `lifecycle_status
                    // === "draft"`, so pairing it with "Unpublished draft" reads
                    // as two labels for one fact. Show the second badge only
                    // when the page's live state is something other than draft
                    // (i.e. it has been published at least once and now carries
                    // newer staged edits).
                    const showDraftBadge =
                      Boolean(page.latest_draft_version_id) &&
                      page.lifecycle_status !== "draft";
                    return (
                      <button
                        aria-pressed={isActive}
                        className={`entity-row selectable-row ${
                          isActive ? "active" : ""
                        }`}
                        key={page.id}
                        type="button"
                        onClick={() => selectPage(page.id)}
                      >
                        <FileText size={16} aria-hidden="true" />
                        <div>
                          <strong>{page.title}</strong>
                          <p>{page.slug}</p>
                        </div>
                        <span>
                          {domainLabel("context", "pageType", page.page_type)}
                        </span>
                        {/* U-1: keep the grid's child count fixed at 4 regardless
                        of how many badges apply (see .row-badges, shared
                        with SearchScreen's .operations-row rows). */}
                        <span className="row-badges">
                          <StatusPill status={page.lifecycle_status} compact />
                          {showDraftBadge ? (
                            <StatusPill
                              status="draft"
                              compact
                              label={t("list.draftIndicator", {
                                defaultValue: "Unpublished draft",
                              })}
                            />
                          ) : null}
                        </span>
                      </button>
                    );
                  }}
                />
              </>
            )}
          </section>
        }
        inspector={
          state.detail ? (
            <ContextInspector
              key={`${state.workspaceId}:${state.detail.id}:${state.detail.version_info?.version_id ?? ""}`}
              detail={state.detail}
              workspaceId={state.workspaceId}
            />
          ) : undefined
        }
      />
      {activeNewItemDialog ? (
        <NewItemDialog
          key={`${location.pathname}?${searchParams.toString()}`}
          canCreateFolder={contextPolicy?.allow_user_folder_create === true}
          canCreatePage
          initialItemType="folder"
          initialParentId={routeFolderParentId ?? ""}
          parentLabel={
            routeFolderParent?.title ?? t("rail.title", { defaultValue: "Context" })
          }
          parentNodes={contextFolderNodes}
          rootLabel={t("rail.title", { defaultValue: "Context" })}
          onClose={(reason) => {
            if (reason === "cancel" && folderCreateRoute) {
              void navigate(workspacePath(workspaceId, "context"), { replace: true });
            }
          }}
          onSubmit={(input) => {
            if (input.itemType === "page") {
              createPage({
                parentNodeId: input.parentNodeId,
                title: input.title,
              });
              return;
            }
            return createFolder({
              parentNodeId: input.parentNodeId,
              slug: input.slug,
              title: input.title,
            });
          }}
        />
      ) : null}
      {renameNode ? (
        <RenameTreeNodeDialog
          node={renameNode}
          onClose={() => setRenameNode(null)}
          onSubmit={(displayName) =>
            renameTreeNode(renameNode, displayName).then(() =>
              setRenameNode(null),
            )
          }
        />
      ) : null}
      {moveNode ? (
        <MoveTreeNodeDialog
          node={moveNode}
          targets={moveTargetNodes}
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
    </>
  );
}

function ContextPageList({
  pages,
  activeId,
  onSelect,
  workspaceId,
}: {
  pages: ContextPageSummary[];
  activeId: string | null;
  onSelect: (pageId: string) => void;
  workspaceId: string;
}) {
  const { t } = useTranslation("context");
  if (pages.length === 0) {
    return (
      <div className="empty-state empty-state-rich">
        <FileText size={22} aria-hidden="true" />
        <h2>{t("empty.title", { defaultValue: "No Context pages yet" })}</h2>
        <p>
          {t("empty.description", {
            defaultValue:
              "Context pages are durable knowledge that Ask and Agent workflows can cite.",
          })}
        </p>
        <WorkspaceFirstUseChecklist nextStep="context" />
        <div className="empty-state-actions">
          <Link className="icon-button primary" to={contextNewHref(workspaceId)}>
            {t("empty.writeFirst", {
              defaultValue: "Write first Context page",
            })}
          </Link>
          <Link className="icon-button text-button" to={workspacePath(workspaceId, "library")}>
            {t("empty.openLibrary", { defaultValue: "Open Library" })}
          </Link>
        </div>
      </div>
    );
  }
  const countLabel = t(
    pages.length === 1 ? "units.page_one" : "units.page_other",
    { defaultValue: pages.length === 1 ? "page" : "pages" },
  );
  return (
    <section className="feature-section">
      <div className="panel-header">
        <div className="panel-title-with-count">
          <span>{t("header.title", { defaultValue: "Context" })}</span>
          <CountChip compact label={countLabel} value={pages.length} />
        </div>
      </div>
      <div className="entity-list">
        {pages.map((page) => (
          <button
            aria-pressed={page.id === activeId}
            className={`entity-row selectable-row ${
              page.id === activeId ? "active" : ""
            }`}
            key={page.id}
            type="button"
            onClick={() => onSelect(page.id)}
          >
            <FileText size={16} aria-hidden="true" />
            <div>
              <strong>{page.title}</strong>
              <p>{page.slug}</p>
            </div>
            <span>{domainLabel("context", "pageType", page.page_type)}</span>
            <StatusPill status={page.lifecycle_status} compact />
            {page.latest_draft_version_id ? (
              <StatusPill
                status="draft"
                compact
                label={t("list.draftIndicator", {
                  defaultValue: "Unpublished draft",
                })}
              />
            ) : null}
          </button>
        ))}
      </div>
    </section>
  );
}

function PendingDraftReview({
  review,
  draftWarnings,
  reconciliationPendingCount,
  canReview,
  workspaceId,
  onViewChanges,
  viewChangesDisabledReason,
  onDecided,
}: {
  review: NonNullable<ContextPageDetail["pending_review"]>;
  draftWarnings: string[];
  reconciliationPendingCount: number;
  canReview: boolean;
  workspaceId: string;
  onViewChanges: (() => void) | null;
  viewChangesDisabledReason: string | null;
  onDecided: (
    result: Awaited<ReturnType<typeof decideReview>>,
  ) => Promise<void> | void;
}) {
  const { t } = useTranslation("context");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState("");
  const impactKey = `${workspaceId ?? ""}:${review.review_item_id}`;
  const [impact, setImpact] = useState<{
    key: string;
    earlierReviewCount: number;
  } | null>(null);
  const [impactErrorKey, setImpactErrorKey] = useState<string | null>(null);
  const earlierReviewCount =
    impact?.key === impactKey ? impact.earlierReviewCount : null;
  const impactLoadFailed = impactErrorKey === impactKey;
  const pagePendingReviewCount =
    review.status !== "pending" || earlierReviewCount == null
      ? null
      : earlierReviewCount + 1;

  useEffect(() => {
    let active = true;
    setImpact(null);
    setImpactErrorKey(null);
    if (!workspaceId || review.status !== "pending") {
      return () => {
        active = false;
      };
    }
    void getReview(workspaceId, review.review_item_id)
      .then((detail) => {
        if (active) {
          setImpact({
            key: impactKey,
            earlierReviewCount: Math.max(
              0,
              detail.superseded_review_count ?? 0,
            ),
          });
        }
      })
      .catch(() => {
        if (active) {
          setImpactErrorKey(impactKey);
        }
      });
    return () => {
      active = false;
    };
  }, [impactKey, review.review_item_id, review.status, workspaceId]);

  async function decide(type: "approve" | "request_changes") {
    if (
      !workspaceId ||
      busy ||
      (type === "approve" &&
        review.status === "pending" &&
        earlierReviewCount == null)
    ) {
      return;
    }
    if (
      type === "approve" &&
      reconciliationPendingCount > 0 &&
      !window.confirm(
        t("pendingReview.reconciliationPublishWarning", {
          count: reconciliationPendingCount,
          defaultValue:
            "{{count}} branch is still waiting for reconciliation. Publish this version anyway?",
        }),
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await decideReview(
        workspaceId,
        review.review_item_id,
        type,
        type === "request_changes" ? reason : undefined,
      );
      await onDecided(result);
    } catch {
      setError(
        type === "approve"
          ? t("pendingReview.approveError", {
              defaultValue:
                "Could not approve and publish this version. Earlier pending reviews were not settled.",
            })
          : t("pendingReview.error", {
              defaultValue: "Could not record the decision.",
            }),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <StatusBanner tone="info">
      <div className="pending-review">
        <span className="pending-review-label">
          {review.status === "changes_requested"
            ? t("pendingReview.changesRequested", {
                defaultValue: "Draft has changes requested",
              })
            : pagePendingReviewCount != null
              ? t("pendingReview.pagePendingCount", {
                  count: pagePendingReviewCount,
                  defaultValue: "{{count}} pending reviews for this page",
                })
              : impactLoadFailed
                ? t("pendingReview.countUnavailable", {
                    defaultValue: "Pending review count unavailable",
                  })
                : t("pendingReview.countLoading", {
                    defaultValue: "Checking pending reviews",
                  })}
          {" · "}
          <StatusPill status={review.risk_level} compact />
          {draftWarnings.length > 0
            ? ` · ${t("pendingReview.warnings", {
                defaultValue: "{{count}} lint warnings",
                count: draftWarnings.length,
              })}`
            : ""}
        </span>
        {review.status === "pending" &&
        earlierReviewCount != null &&
        earlierReviewCount > 0 ? (
          <p className="pending-review-impact">
            {t("pendingReview.approveImpact", {
              count: earlierReviewCount,
              defaultValue:
                "Approving will publish this version. {{count}} earlier pending reviews will be settled with it.",
            })}
          </p>
        ) : null}
        <div className="pending-review-actions">
          <button
            type="button"
            className="icon-button"
            disabled={!onViewChanges}
            onClick={onViewChanges ?? undefined}
          >
            {t("pendingReview.viewChanges", { defaultValue: "View changes" })}
          </button>
          {pagePendingReviewCount != null && pagePendingReviewCount > 1 ? (
            <Link
              className="icon-button text-button"
              to={workspacePath(
                workspaceId,
                `review/${encodeURIComponent(review.review_item_id)}`,
              )}
            >
              {t("pendingReview.viewPageReviews", {
                count: pagePendingReviewCount,
                defaultValue: "View all {{count}} pending reviews in the queue",
              })}
            </Link>
          ) : impactLoadFailed || review.status === "changes_requested" ? (
            <Link className="icon-button text-button" to={workspacePath(workspaceId, "review")}>
              {t("pendingReview.openQueue", {
                defaultValue: "Open Review queue",
              })}
            </Link>
          ) : null}
          {canReview ? (
            <>
              <button
                type="button"
                className="icon-button primary"
                disabled={
                  busy ||
                  (review.status === "pending" && earlierReviewCount == null)
                }
                onClick={() => decide("approve")}
              >
                {t("pendingReview.approve", { defaultValue: "Approve" })}
              </button>
              <button
                type="button"
                className="icon-button"
                disabled={busy}
                onClick={() => setReasonOpen((open) => !open)}
              >
                {t("pendingReview.requestChanges", {
                  defaultValue: "Request changes",
                })}
              </button>
            </>
          ) : null}
        </div>
        {viewChangesDisabledReason ? (
          <p className="pending-review-error">{viewChangesDisabledReason}</p>
        ) : null}
        {canReview && reasonOpen ? (
          <div className="pending-review-reason">
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={t("pendingReview.reasonPlaceholder", {
                defaultValue: "What should change before this is approved?",
              })}
            />
            <button
              type="button"
              className="icon-button primary"
              disabled={busy || !reason.trim()}
              onClick={() => decide("request_changes")}
            >
              {t("pendingReview.submitChanges", {
                defaultValue: "Send request",
              })}
            </button>
          </div>
        ) : null}
        {review.status === "pending" && impactLoadFailed ? (
          <p className="pending-review-error">
            {t("pendingReview.impactLoadError", {
              defaultValue:
                "Could not load what approval will settle. Open the Review queue to decide safely.",
            })}
          </p>
        ) : null}
        {error ? <p className="pending-review-error">{error}</p> : null}
      </div>
    </StatusBanner>
  );
}

type ContextReaderTab = "view" | "history" | "graph" | "activity" | "children";
type CompareOrigin = "banner" | "history" | "deep-link";
type ComparePair = {
  from: string | null;
  to: string;
  origin: CompareOrigin;
};

// URL stand-in for `from: null` (the v0 empty baseline). Dropping the param
// instead would make the deep link unrestorable: the restore effect cannot
// tell "compare v1 against nothing" from a half-built URL.
const EMPTY_BASELINE_COMPARE_PARAM = "empty";

type CompareCandidate = ComparePair;

function contextReaderTabFromParam(value: string | null): ContextReaderTab {
  return value === "history" || value === "graph" || value === "activity" || value === "children"
    ? value
    : "view";
}

function mergeByKey<T>(current: T[], next: T[], keyFor: (entry: T) => string) {
  const seen = new Set(current.map(keyFor));
  return [
    ...current,
    ...next.filter((entry) => {
      const key = keyFor(entry);
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    }),
  ];
}

function mergeContextChangeHistory(
  current: ContextChangeHistory,
  next: ContextChangeHistory,
): ContextChangeHistory {
  return {
    versions: mergeByKey(
      current.versions,
      next.versions,
      (entry) => entry.version_id,
    ),
    changesets: mergeByKey(
      current.changesets,
      next.changesets,
      (entry) => entry.change_set_id,
    ),
    review_decisions: mergeByKey(
      current.review_decisions,
      next.review_decisions,
      (entry) => entry.decision_id,
    ),
    diffs: current.diffs ?? next.diffs,
    next_cursor: next.next_cursor,
  };
}

function normalizeComparePair(
  versions: VersionEntry[],
  candidate: CompareCandidate,
): ComparePair | null {
  return normalizeVersionPair(contextHistoryNodes(versions), candidate);
}

function defaultHistoryCompareCandidate(
  _versions: VersionEntry[],
  selected: VersionEntry,
): CompareCandidate {
  return {
    from: selected.prior_version_id ?? null,
    to: selected.version_id,
    origin: "history",
  };
}

function comparePairCrossesLineage(
  versions: VersionEntry[],
  pair: ComparePair | null,
): boolean {
  if (!pair?.from) {
    return false;
  }
  const byId = new Map(versions.map((entry) => [entry.version_id, entry]));
  let cursor = byId.get(pair.to);
  const visited = new Set<string>();
  while (cursor && !visited.has(cursor.version_id)) {
    visited.add(cursor.version_id);
    const priorId = cursor.prior_version_id;
    if (priorId === pair.from) {
      return false;
    }
    if (priorId === null) {
      return true;
    }
    if (typeof priorId !== "string") {
      return false;
    }
    cursor = byId.get(priorId);
    if (!cursor) {
      // The visible history may omit an ancestor. Without its metadata the
      // frontend cannot honestly classify the pair as cross-lineage.
      return false;
    }
  }
  return false;
}

function pendingReviewCompareCandidate(
  detail: ContextPageDetail,
  versions: VersionEntry[],
): CompareCandidate | null {
  const targetId = detail.pending_draft_version_id;
  const target = versions.find((entry) => entry.version_id === targetId);
  if (
    !target ||
    !(
      target.history_status === "current_draft" ||
      versionStringField(target, "version_status") === "draft"
    ) ||
    !detail.pending_review ||
    !["pending", "changes_requested"].includes(detail.pending_review.status)
  ) {
    return null;
  }
  if (!detail.current_published_version_id) {
    return defaultHistoryCompareCandidate(versions, target);
  }
  const published = versions.find(
    (entry) => entry.version_id === detail.current_published_version_id,
  );
  if (!published || published.version_no >= target.version_no) {
    return null;
  }
  return {
    from: published.version_id,
    to: target.version_id,
    origin: "banner",
  };
}

function ContextReader({
  canEdit,
  detail,
  favoriteScope,
  history,
  node,
  nodes,
  tombstones,
  workspaceId,
  onCopyNodeLink,
  onDeleteNode,
  onMoveNode,
  onRenameNode,
  onSelectNode,
  onReviewDecided,
  onVersionRestored,
}: {
  canEdit: boolean;
  detail: ContextPageDetail;
  favoriteScope?: TreeFavoriteScope;
  history: ContextChangeHistory | null;
  node: TreeNodePayload | null;
  nodes: TreeNodePayload[];
  tombstones: LibraryItemTombstone[];
  workspaceId: string | null;
  onCopyNodeLink: (node: TreeNodePayload) => Promise<CopyTextDetailedResult>;
  onDeleteNode: (node: TreeNodePayload) => void;
  onMoveNode: (node: TreeNodePayload) => void;
  onRenameNode: (node: TreeNodePayload) => void;
  onSelectNode: (node: TreeNodePayload) => void;
  onReviewDecided: () => Promise<void> | void;
  onVersionRestored: () => Promise<void> | void;
}) {
  const { t } = useTranslation("context");
  const { t: tShell } = useTranslation("shell");
  const workspaceGraph = useWorkspaceGraphCapability(workspaceId);
  const workspaceContext = useOptionalWorkspaceContext();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const compareFromParam = searchParams.get("compareFrom");
  const compareToParam = searchParams.get("compareTo");
  const reconcileVersionParam = searchParams.get("reconcile");
  const routeTab = contextReaderTabFromParam(tabParam);
  const directChildren = useMemo(
    () => (node ? orderedContextChildren(nodes, node.id) : []),
    [node, nodes],
  );
  const hasChildren = directChildren.length > 0;
  const [historyPageState, setHistoryPageState] = useState<{
    detailId: string;
    history: ContextChangeHistory | null;
  }>(() => ({ detailId: detail.id, history }));
  const loadedHistory =
    historyPageState.detailId === detail.id
      ? historyPageState.history
      : history;
  const versions = useMemo(
    () => loadedHistory?.versions ?? [],
    [loadedHistory],
  );
  const reconcilableBranches = useMemo(
    () =>
      versions.filter(
        (entry) =>
          entry.history_status === "alternative_draft"
          && entry.reconciliation_available === true,
      ),
    [versions],
  );
  const reconciliationEntry = reconcileVersionParam
    ? reconcilableBranches.find(
        (entry) => entry.version_id === reconcileVersionParam,
      ) ?? null
    : null;
  const reconciliationVersionId =
    reconciliationEntry?.version_id
    ?? (detail.reconciliation_available === true
      && detail.next_reconciliation_version_id === reconcileVersionParam
      ? reconcileVersionParam
      : null);
  const nextReconciliationVersionId =
    detail.reconciliation_available === true
    && detail.reconciliation_pending_count > 0
      ? detail.next_reconciliation_version_id
      : null;
  const [historyLoadingOlder, setHistoryLoadingOlder] = useState(false);
  const [historyLoadError, setHistoryLoadError] = useState<string | null>(null);
  const historyLoadingRef = useRef(false);
  const historyLoadGenerationRef = useRef(0);
  // The URL is the tab source of truth. Keeping a second local tab state made
  // BrowserRouter popstate navigation restore the search string while the UI
  // remained on the previously clicked tab until another render happened.
  const tab: ContextReaderTab =
    routeTab === "children" && !hasChildren ? "view" : routeTab;
  const [graphMountedPageId, setGraphMountedPageId] = useState<string | null>(
    routeTab === "graph" ? detail.id : null,
  );
  // Reader identity is scoped to workspace, page, and visible head above. A
  // scene is loaded only on first activation and stays mounted across tabs;
  // neither its latency nor failure participates in body/Inspector loading.
  const [graph, setGraph] = useState<GraphQueryPayload | null>(null);
  const [graphFailed, setGraphFailed] = useState(false);
  const currentGraphDepthRef = useRef<GraphDepth>(2);
  useEffect(() => {
    currentGraphDepthRef.current = 2;
  }, [detail.id, workspaceId]);
  useEffect(() => {
    if (graph?.history.mode === "current") {
      currentGraphDepthRef.current = graph.query.depth === 1 ? 1 : 2;
    }
  }, [graph]);
  const graphRequestRef = useRef<{
    controller: AbortController | null;
    generation: number;
    pageId: string | null;
  }>({
    controller: null,
    generation: 0,
    pageId: detail.id,
  });
  const [comparePair, setComparePair] = useState<ComparePair | null>(null);
  const contextMediaVersionId = detail.version_info?.version_id ?? null;
  const resolveContextMediaAsset = useMemo<ContextMediaAssetUrlResolver>(
    () => (assetId: string) =>
      contextMediaVersionId && workspaceId
        ? {
            status: "ready",
            url: contextMediaPageVersionContentUrl(
              workspaceId,
              detail.id,
              contextMediaVersionId,
              assetId,
            ),
          }
        : { status: "unavailable", reason: "version_unavailable" },
    [contextMediaVersionId, detail.id, workspaceId],
  );
  const [compareStateError, setCompareStateError] = useState<string | null>(
    null,
  );
  const appliedCompareRouteRef = useRef<string | null>(null);
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);
  const [activityItems, setActivityItems] = useState<ActivityItem[] | null>(
    null,
  );
  const [activityNextCursor, setActivityNextCursor] = useState<string | null>(
    null,
  );
  const [activityLoadingMore, setActivityLoadingMore] = useState(false);
  const activityLoadGenerationRef = useRef(0);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const [historyFocusVersionId, setHistoryFocusVersionId] = useState<
    string | null
  >(null);
  const pendingHistoryFocusRef = useRef<{
    detailId: string;
    versionId: string;
  } | null>(null);
  const historyFocusRevisionRef = useRef(0);
  const [isPublic, setIsPublic] = useState(Boolean(detail.is_public));
  useEffect(() => {
    historyLoadGenerationRef.current += 1;
    historyLoadingRef.current = false;
    setHistoryPageState({ detailId: detail.id, history });
    setHistoryLoadingOlder(false);
    setHistoryLoadError(null);
  }, [detail.id, history]);
  useEffect(() => {
    setComparePair(null);
    setCompareStateError(null);
    appliedCompareRouteRef.current = null;
    setReviewNotice(null);
    setShareOpen(false);
    setHistoryFocusVersionId(null);
    pendingHistoryFocusRef.current = null;
    setIsPublic(Boolean(detail.is_public));
  }, [detail.id, detail.is_public]);
  useEffect(() => {
    if (tab !== routeTab) {
      const next = new URLSearchParams(searchParams);
      next.delete("tab");
      next.delete(CONTEXT_CHILD_TAB_PAGE_PARAM);
      setSearchParams(next, { replace: true });
    }
    if (routeTab !== "history") {
      setHistoryFocusVersionId(null);
      pendingHistoryFocusRef.current = null;
      setComparePair(null);
      setCompareStateError(null);
      appliedCompareRouteRef.current = null;
    }
  }, [detail.id, routeTab, searchParams, setSearchParams, tab]);
  useEffect(() => {
    if (tab === "graph") {
      setGraphMountedPageId(detail.id);
    }
  }, [detail.id, tab]);
  useEffect(() => {
    // Abort both the deferred initial request and subsequent graph actions when
    // this workspace/page/head-scoped Reader is replaced or unmounted.
    const previous = graphRequestRef.current;
    previous.controller?.abort();
    graphRequestRef.current = {
      controller: null,
      generation: previous.generation + 1,
      pageId: detail.id,
    };
    return () => {
      const current = graphRequestRef.current;
      if (current.pageId !== detail.id) {
        return;
      }
      current.controller?.abort();
      graphRequestRef.current = {
        controller: null,
        generation: current.generation + 1,
        pageId: current.pageId,
      };
    };
  }, [detail.id, workspaceId]);

  const loadOlderHistory = useCallback(
    async (targetVersionId?: string): Promise<ContextChangeHistory | null> => {
      if (
        !workspaceId ||
        historyLoadingRef.current ||
        !loadedHistory?.next_cursor ||
        (targetVersionId &&
          loadedHistory.versions.some(
            (entry) => entry.version_id === targetVersionId,
          ))
      ) {
        return loadedHistory ?? null;
      }
      const generation = historyLoadGenerationRef.current;
      let merged = loadedHistory;
      let cursor: string | null = loadedHistory.next_cursor;
      historyLoadingRef.current = true;
      setHistoryLoadingOlder(true);
      setHistoryLoadError(null);
      try {
        do {
          const next = await listContextChanges(workspaceId, detail.id, {
            cursor,
          });
          if (generation !== historyLoadGenerationRef.current) {
            return null;
          }
          merged = mergeContextChangeHistory(merged, next);
          cursor = merged.next_cursor;
          setHistoryPageState({ detailId: detail.id, history: merged });
        } while (
          targetVersionId &&
          cursor &&
          !merged.versions.some((entry) => entry.version_id === targetVersionId)
        );
        return merged;
      } catch {
        if (generation === historyLoadGenerationRef.current) {
          setHistoryLoadError(
            t("history.loadOlderFailed", {
              defaultValue: "Could not load older versions.",
            }),
          );
        }
        return null;
      } finally {
        if (generation === historyLoadGenerationRef.current) {
          historyLoadingRef.current = false;
          setHistoryLoadingOlder(false);
        }
      }
    },
    [detail.id, loadedHistory, t, workspaceId],
  );

  useEffect(() => {
    if (
      !reconcileVersionParam
      || reconciliationEntry
      || !loadedHistory?.next_cursor
    ) {
      return;
    }
    void loadOlderHistory(reconcileVersionParam);
  }, [
    loadOlderHistory,
    loadedHistory?.next_cursor,
    reconcileVersionParam,
    reconciliationEntry,
  ]);

  const openReconciliation = useCallback(
    (versionId: string) => {
      const next = new URLSearchParams(searchParams);
      next.set("tab", "history");
      next.set("reconcile", versionId);
      setSearchParams(next);
    },
    [searchParams, setSearchParams],
  );

  const closeReconciliation = useCallback(() => {
    const next = new URLSearchParams(searchParams);
    next.delete("reconcile");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const openComparison = useCallback(
    (
      candidate: CompareCandidate,
      replace = false,
      availableVersions: VersionEntry[] = versions,
    ) => {
      const normalized = normalizeComparePair(availableVersions, candidate);
      if (!normalized) {
        setComparePair(null);
        setCompareStateError(
          tShell("diff.invalidComparison", {
            defaultValue:
              "These versions cannot be compared from this history.",
          }),
        );
        const nextParams = new URLSearchParams(searchParams);
        nextParams.set("tab", "history");
        nextParams.delete("compareFrom");
        nextParams.delete("compareTo");
        if (nextParams.toString() !== searchParams.toString()) {
          setSearchParams(nextParams, { replace });
        }
        return;
      }

      setComparePair(normalized);
      setCompareStateError(null);
      const nextParams = new URLSearchParams(searchParams);
      nextParams.set("tab", "history");
      nextParams.set(
        "compareFrom",
        normalized.from ?? EMPTY_BASELINE_COMPARE_PARAM,
      );
      nextParams.set("compareTo", normalized.to);
      appliedCompareRouteRef.current = `${detail.id}:${normalized.from ?? ""}:${normalized.to}`;
      if (nextParams.toString() !== searchParams.toString()) {
        setSearchParams(nextParams, { replace });
      }
    },
    [detail.id, searchParams, setSearchParams, tShell, versions],
  );

  const openHistoryComparison = useCallback(
    async (entry: VersionEntry) => {
      const relation = priorRelationForVersion(entry);
      const parentId = entry.prior_version_id;
      if (relation !== "visible" || !parentId) {
        setComparePair(null);
        setCompareStateError(
          relation === "root"
            ? t("history.firstVersionNoComparison", {
                defaultValue:
                  "The first version has no previous version to compare.",
              })
            : t("history.previousVersionUnavailable", {
                defaultValue: "The previous version is not available.",
              }),
        );
        return;
      }

      let availableVersions = versions;
      if (
        !availableVersions.some(
          (candidate) => candidate.version_id === parentId,
        )
      ) {
        const merged = await loadOlderHistory(parentId);
        availableVersions = merged?.versions ?? availableVersions;
      }
      if (
        !availableVersions.some(
          (candidate) => candidate.version_id === parentId,
        )
      ) {
        setComparePair(null);
        setCompareStateError(
          t("history.previousVersionUnavailable", {
            defaultValue: "The previous version is not available.",
          }),
        );
        return;
      }
      openComparison(
        { from: parentId, to: entry.version_id, origin: "history" },
        false,
        availableVersions,
      );
    },
    [loadOlderHistory, openComparison, t, versions],
  );

  useEffect(() => {
    if (!compareFromParam || !compareToParam) {
      setComparePair(null);
      setCompareStateError(null);
      appliedCompareRouteRef.current = null;
      return;
    }
    const requestedFrom =
      compareFromParam === EMPTY_BASELINE_COMPARE_PARAM
        ? null
        : compareFromParam;
    const routeKey = `${detail.id}:${requestedFrom ?? ""}:${compareToParam}`;
    if (appliedCompareRouteRef.current === routeKey) {
      return;
    }
    openComparison(
      {
        from: requestedFrom,
        to: compareToParam,
        origin: "deep-link",
      },
      true,
    );
  }, [compareFromParam, compareToParam, detail.id, openComparison]);

  const closeComparison = useCallback(() => {
    setComparePair(null);
    setCompareStateError(null);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("compareFrom");
    nextParams.delete("compareTo");
    appliedCompareRouteRef.current = null;
    if (nextParams.toString() !== searchParams.toString()) {
      setSearchParams(nextParams, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  function changeTab(nextTab: ContextReaderTab) {
    const nextParams = new URLSearchParams(searchParams);
    if (nextTab === "view") {
      nextParams.delete("tab");
    } else {
      nextParams.set("tab", nextTab);
    }
    if (nextTab !== "history") {
      nextParams.delete("compareFrom");
      nextParams.delete("compareTo");
      setComparePair(null);
      setCompareStateError(null);
      appliedCompareRouteRef.current = null;
      setHistoryFocusVersionId(null);
      pendingHistoryFocusRef.current = null;
    }
    if (nextTab !== "children") {
      nextParams.delete(CONTEXT_CHILD_TAB_PAGE_PARAM);
    }
    if (nextParams.toString() !== searchParams.toString()) {
      setSearchParams(nextParams);
    }
  }

  const replaceGraphPayload = useCallback(
    async (query: GraphQueryPayload["query"]) => {
      if (!workspaceId) {
        return;
      }
      const pageId = detail.id;
      const previous = graphRequestRef.current;
      previous.controller?.abort();
      const controller = new AbortController();
      const generation = previous.generation + 1;
      graphRequestRef.current = { controller, generation, pageId };
      try {
        const incoming = await queryGraph(workspaceId, query, {
          signal: controller.signal,
        });
        const current = graphRequestRef.current;
        if (
          controller.signal.aborted ||
          current.controller !== controller ||
          current.generation !== generation ||
          current.pageId !== pageId
        ) {
          return;
        }
        // Each query replaces the full actor-authorized scene. Relationships
        // and visibility may have changed; never merge with an older payload.
        setGraph(incoming);
        setGraphFailed(false);
      } catch (error) {
        const current = graphRequestRef.current;
        // Superseded requests are normal during rapid graph actions. Their
        // transport may still settle after abort, so fence signal + generation.
        if (
          controller.signal.aborted ||
          current.controller !== controller ||
          current.generation !== generation ||
          current.pageId !== pageId
        ) {
          return;
        }
        throw error;
      } finally {
        const current = graphRequestRef.current;
        if (
          current.controller === controller &&
          current.generation === generation &&
          current.pageId === pageId
        ) {
          current.controller = null;
        }
      }
    },
    [detail.id, workspaceId],
  );

  useEffect(() => {
    if (graphMountedPageId !== detail.id || !workspaceId) return;
    let cancelled = false;
    setGraph(null);
    setGraphFailed(false);
    void replaceGraphPayload(contextGraphQuery(detail.id)).catch(() => {
      if (!cancelled) setGraphFailed(true);
    });
    // Leaving Graph does not cancel this initial load: its result is cached
    // for the selected page. Changing Reader identity/unmount aborts it via
    // graphRequestRef and fences transports that still resolve after abort.
    // A mutation reload supplies a fresh history snapshot even if publication
    // kept the same visible version; revalidate that previously activated scene.
    return () => {
      cancelled = true;
    };
  }, [detail.id, graphMountedPageId, history, replaceGraphPayload, workspaceId]);

  async function expandGraphNode(graphNode: GraphNode) {
    if (
      !workspaceId ||
      !graph ||
      graph.capabilities?.expand === false ||
      graphNode.entity_kind !== "context_page"
    ) {
      return;
    }
    const key = `${graphNode.entity_kind}:${graphNode.entity_id}`;
    if (
      graph.query.seeds.length >= 8 ||
      graph.query.seeds.some((seed) => `${seed.kind}:${seed.id}` === key)
    ) {
      return;
    }
    await replaceGraphPayload({
      ...graph.query,
      cursor: null,
      seeds: [
        ...graph.query.seeds,
        { kind: graphNode.entity_kind, id: graphNode.entity_id },
      ],
    });
  }

  async function collapseGraphNode(graphNode: GraphNode) {
    if (
      !workspaceId ||
      !graph ||
      graph.capabilities?.expand === false ||
      graphNode.entity_kind !== "context_page"
    ) {
      return;
    }
    const key = `${graphNode.entity_kind}:${graphNode.entity_id}`;
    const primarySeed = graph.query.seeds[0];
    if (primarySeed && `${primarySeed.kind}:${primarySeed.id}` === key) {
      return;
    }
    const seeds = graph.query.seeds.filter(
      (seed) => `${seed.kind}:${seed.id}` !== key,
    );
    if (seeds.length === graph.query.seeds.length) {
      return;
    }
    await replaceGraphPayload({
      ...graph.query,
      cursor: null,
      seeds,
    });
  }

  async function changeGraphHistoryMode(mode: GraphHistoryMode) {
    if (!workspaceId || !graph) {
      return;
    }
    await replaceGraphPayload({
      ...contextGraphQuery(detail.id, mode, currentGraphDepthRef.current),
      seeds: graph.query.seeds,
    });
  }

  async function changeGraphDepth(depth: GraphDepth) {
    if (!workspaceId || !graph || graph.history.mode !== "current") return;
    await replaceGraphPayload({
      ...graph.query,
      cursor: null,
      depth,
    });
  }

  function openPublishedHistory() {
    const versionId = detail.current_published_version_id;
    if (!versionId) {
      return;
    }
    pendingHistoryFocusRef.current = {
      detailId: detail.id,
      versionId,
    };
    setHistoryFocusVersionId(versionId);
    changeTab("history");
    if (!versions.some((entry) => entry.version_id === versionId)) {
      void loadOlderHistory(versionId);
    }
  }
  useEffect(() => {
    const pending = pendingHistoryFocusRef.current;
    if (
      tab !== "history" ||
      !historyFocusVersionId ||
      !pending ||
      pending.detailId !== detail.id ||
      pending.versionId !== historyFocusVersionId
    ) {
      return undefined;
    }
    const revision = ++historyFocusRevisionRef.current;
    const frame = window.requestAnimationFrame(() => {
      if (revision !== historyFocusRevisionRef.current) {
        return;
      }
      const row = document.querySelector<HTMLElement>(
        `[data-context-version-id="${historyFocusVersionId}"]`,
      );
      if (row?.isConnected) {
        pendingHistoryFocusRef.current = null;
        row.scrollIntoView({ block: "nearest" });
        row.focus();
      } else if (!loadedHistory?.next_cursor || historyLoadError) {
        pendingHistoryFocusRef.current = null;
        document.getElementById("context-reader-panel-history")?.focus();
      }
    });
    return () => {
      historyFocusRevisionRef.current += 1;
      window.cancelAnimationFrame(frame);
    };
  }, [
    detail.id,
    historyFocusVersionId,
    historyLoadError,
    historyLoadingOlder,
    loadedHistory,
    tab,
  ]);
  // Activity is read the first time its tab opens for a page, not on every
  // page view; the tab shows its count once that load has finished.
  const activityPageKey = `${workspaceId ?? ""}:${detail.id}`;
  const [activityRequestedKey, setActivityRequestedKey] = useState<
    string | null
  >(null);
  useEffect(() => {
    if (tab === "activity") {
      setActivityRequestedKey(activityPageKey);
    }
  }, [activityPageKey, tab]);
  const activityRequested = activityRequestedKey === activityPageKey;
  useEffect(() => {
    let active = true;
    const generation = ++activityLoadGenerationRef.current;
    setActivityItems(null);
    setActivityNextCursor(null);
    setActivityLoadingMore(false);
    setActivityError(null);
    if (!activityRequested) {
      return () => {
        active = false;
      };
    }
    if (!workspaceId) {
      setActivityItems([]);
      return () => {
        active = false;
      };
    }
    void listContextPageActivity(workspaceId, detail.id, {
      period: "all",
      limit: 50,
    })
      .then((data) => {
        if (active && generation === activityLoadGenerationRef.current) {
          setActivityItems(data.items);
          setActivityNextCursor(data.next_cursor);
        }
      })
      .catch(() => {
        if (active && generation === activityLoadGenerationRef.current) {
          setActivityItems([]);
          setActivityError(
            t("reader.activityLoadError", {
              defaultValue: "Could not load this page's activity.",
            }),
          );
        }
      });
    return () => {
      active = false;
    };
  }, [activityRequested, detail.id, workspaceId, t]);

  const loadMoreActivity = useCallback(async () => {
    if (!workspaceId || !activityNextCursor || activityLoadingMore) {
      return;
    }
    const cursor = activityNextCursor;
    const generation = activityLoadGenerationRef.current;
    setActivityLoadingMore(true);
    setActivityError(null);
    try {
      const data = await listContextPageActivity(workspaceId, detail.id, {
        period: "all",
        limit: 50,
        cursor,
      });
      if (generation !== activityLoadGenerationRef.current) {
        return;
      }
      setActivityItems((current) =>
        mergeByKey(current ?? [], data.items, (item) => item.id),
      );
      setActivityNextCursor(data.next_cursor);
    } catch {
      if (generation !== activityLoadGenerationRef.current) {
        return;
      }
      setActivityError(
        t("reader.activityLoadMoreError", {
          defaultValue: "Could not load older page activity.",
        }),
      );
    } finally {
      if (generation === activityLoadGenerationRef.current) {
        setActivityLoadingMore(false);
      }
    }
  }, [activityLoadingMore, activityNextCursor, detail.id, t, workspaceId]);
  const deletedSources = useMemo(
    () => buildDeletedSourceMarks(tombstones, t),
    [tombstones, t],
  );
  const readerVersionNo = readerVersionNumber(detail, loadedHistory);
  const publishedVersionInfo = (detail as ContextDetailWithPublishedVersion)
    .published_version_info;
  const publishedTipId = `context-published-version-tip-${detail.id}`;
  const displayedVersionIsDraft =
    detail.version_info?.history_status === "current_draft";
  const pendingCompareCandidate = pendingReviewCompareCandidate(
    detail,
    versions,
  );
  const pendingCompareDisabledReason =
    detail.pending_review && !pendingCompareCandidate
      ? tShell("diff.reviewComparisonUnavailable", {
          defaultValue:
            "View changes is unavailable because the pending review target is not a valid draft in this history.",
        })
      : null;
  return (
    <section className="feature-section detail-section context-reader">
      <div className="detail-heading">
        <div>
          <h2>{detail.title}</h2>
        </div>
        {/* Page-level actions live in the heading row, the same slot the Agent
            reader uses; the tab strip carries view-scoped actions only. */}
        <div className="detail-heading-actions">
          {canEdit ? (
            <Link
              className="context-reader-header-action context-reader-edit-action icon-button text-button"
              to={contextEditHref(workspaceId ?? detail.workspace_id, detail.id)}
            >
              <Edit size={16} aria-hidden="true" />
              <span>{t("actions.edit", { defaultValue: "Edit" })}</span>
            </Link>
          ) : null}
          <button
            className="context-reader-header-action context-reader-share-action icon-button text-button"
            onClick={() => setShareOpen(true)}
            type="button"
          >
            <Share2 size={16} aria-hidden="true" />
            <span>{t("share.action")}</span>
          </button>
          {node ? (
            <ContextReaderNodeMenu
              canEdit={canEdit}
              favoriteScope={favoriteScope}
              node={node}
              onCopyNodeLink={onCopyNodeLink}
              onDeleteNode={onDeleteNode}
              onMoveNode={onMoveNode}
              onRenameNode={onRenameNode}
            />
          ) : null}
        </div>
      </div>
      <div className="reader-meta-row">
        <span className="reader-chip">
          {domainLabel("context", "pageType", detail.page_type)}
        </span>
        {displayedVersionIsDraft ? (
          <span className="reader-chip">
            {t("reader.viewingDraft", { defaultValue: "Editing draft" })}
          </span>
        ) : null}
        {!detail.current_published_version_id ? (
          <span className="reader-chip">
            {t("reader.readersSeeNone", {
              defaultValue: "Not published yet",
            })}
          </span>
        ) : detail.current_published_version_id && readerVersionNo != null ? (
          <span className="context-version-tag-tip-wrap">
            <button
              aria-describedby={publishedTipId}
              aria-label={t("reader.readersSeeVersionAction", {
                defaultValue:
                  "Readers see v{{version}}; open this version in History",
                version: readerVersionNo,
              })}
              className="reader-chip reader-chip-action context-version-tag-tip"
              onClick={openPublishedHistory}
              type="button"
            >
              <span>
                {t("reader.readersSeeVersion", {
                  defaultValue: "Readers see v{{version}}",
                  version: readerVersionNo,
                })}
              </span>
              <span
                aria-hidden="true"
                className="context-version-tag-tip-label"
              >
                <ArrowRight size={13} />
              </span>
            </button>
            <span
              className="context-version-tag-tip-popup"
              id={publishedTipId}
              role="tooltip"
            >
              {t("reader.readersSeeVersionTip", {
                defaultValue:
                  "This is the published snapshot readers receive. Open it in History.",
              })}
              {publishedVersionInfo?.published_at ? (
                <time
                  dateTime={publishedVersionInfo.published_at}
                  title={formatVersionTimestamp(
                    publishedVersionInfo.published_at,
                    "long",
                  )}
                >
                  {` · ${formatVersionTimestamp(publishedVersionInfo.published_at)}`}
                </time>
              ) : null}
            </span>
          </span>
        ) : null}
        {/* A pending draft is a lifecycle state, not a property of the page, so
            it reads as a status pill here — the neutral chips beside it carry
            page attributes. */}
        {detail.latest_draft_version_id ? (
          <StatusPill
            compact
            status="draft"
            label={t("reader.draftPending", { defaultValue: "Draft pending" })}
          />
        ) : null}
        <time
          className="reader-meta-date"
          dateTime={detail.updated_at}
          title={formatVersionTimestamp(detail.updated_at, "long")}
        >
          {formatVersionTimestamp(detail.updated_at)}
        </time>
      </div>
      {reviewNotice ? (
        <StatusBanner tone="info">{reviewNotice}</StatusBanner>
      ) : null}
      {canEdit && nextReconciliationVersionId ? (
        <StatusBanner
          tone="warning"
          action={
            <button
              className="icon-button"
              onClick={() =>
                openReconciliation(nextReconciliationVersionId)
              }
              type="button"
            >
              {t("reconciliation.open", { defaultValue: "Reconcile" })}
            </button>
          }
        >
          {detail.reconciliation_pending_count_truncated
            ? t("reconciliation.pendingCountAtLeast", {
                count: detail.reconciliation_pending_count,
                defaultValue:
                  "At least {{count}} branches require reconciliation before they can rejoin HEAD.",
              })
            : t("reconciliation.pendingCount", {
                count: detail.reconciliation_pending_count,
                defaultValue:
                  "{{count}} branch requires reconciliation before it can rejoin HEAD.",
              })}
        </StatusBanner>
      ) : null}
      {detail.pending_review ? (
        <PendingDraftReview
          review={detail.pending_review}
          draftWarnings={detail.pending_draft_warnings}
          reconciliationPendingCount={detail.reconciliation_pending_count}
          canReview={canEdit}
          workspaceId={workspaceId ?? detail.workspace_id}
          onViewChanges={
            pendingCompareCandidate
              ? () => openComparison(pendingCompareCandidate)
              : null
          }
          viewChangesDisabledReason={pendingCompareDisabledReason}
          onDecided={async (result) => {
            if (result.published_version_id) {
              const settled = result.superseded_review_count ?? 0;
              setReviewNotice(
                settled > 0
                  ? t("pendingReview.approveSuccessSettled", {
                      defaultValue:
                        "Version published. {{count}} earlier pending reviews were settled with it.",
                      count: settled,
                    })
                  : t("pendingReview.approveSuccess", {
                      defaultValue: "Version published.",
                    }),
              );
            }
            await onReviewDecided();
          }}
        />
      ) : null}
      <ReaderTabs
        accent="context"
        active={tab}
        ariaLabel={t("reader.ariaLabel", {
          defaultValue: "Context reader views",
        })}
        idBase="context-reader"
        onChange={(nextTab) => changeTab(nextTab as ContextReaderTab)}
        tabs={[
          {
            key: "view",
            label: t("reader.tabs.view", { defaultValue: "View" }),
          },
          {
            key: "history",
            label: t("reader.tabs.history", { defaultValue: "History" }),
            count: loadedHistory?.versions.length ?? 0,
          },
          {
            key: "graph",
            label: t("reader.tabs.graph", { defaultValue: "Graph" }),
            badge: {
              label: tShell("preview.badge", { defaultValue: "Preview" }),
              title: tShell("preview.description", {
                defaultValue: "Preview: this area is still being refined.",
              }),
            },
          },
          {
            key: "activity",
            label: t("reader.tabs.activity", { defaultValue: "Activity" }),
            count: activityItems?.length ?? 0,
          },
          ...(hasChildren
            ? [{
                key: "children",
                label: t("reader.tabs.children", { defaultValue: "Children" }),
                count: directChildren.length,
              }]
            : []),
        ]}
      />
      {tab === "view" ? (
        <div
          id="context-reader-panel-view"
          role="tabpanel"
          aria-labelledby="context-reader-tab-view"
          tabIndex={0}
        >
          {detail.body_markdown ? (
            <MarkdownView
              body={detail.body_markdown}
              resolveContextMediaAsset={resolveContextMediaAsset}
              stripLeadingHeading={detail.title}
              deletedSources={deletedSources}
              wikilinkTargets={detail.wikilink_targets}
              linkTargets={detail.link_targets}
              workspaceId={workspaceId}
            />
          ) : (
            <div className="empty-state">
              {t("reader.noBody", { defaultValue: "No body" })}
            </div>
          )}
        </div>
      ) : null}
      {tab === "children" && node ? (
        <div
          id="context-reader-panel-children"
          role="tabpanel"
          aria-labelledby="context-reader-tab-children"
          tabIndex={0}
        >
          <ContextChildrenIndex node={node} nodes={nodes} onSelectNode={onSelectNode} />
        </div>
      ) : null}
      {tab === "history" ? (
        <div
          id="context-reader-panel-history"
          role="tabpanel"
          aria-labelledby="context-reader-tab-history"
          tabIndex={0}
        >
          <ContextHistory
            canEdit={canEdit}
            comparePair={comparePair}
            compareStateError={compareStateError}
            detail={detail}
            history={loadedHistory}
            historyLoadError={historyLoadError}
            historyLoadingOlder={historyLoadingOlder}
            highlightedVersionId={historyFocusVersionId}
            onCloseCompare={closeComparison}
            onCompare={(entry) => void openHistoryComparison(entry)}
            onComparePair={(candidate) => openComparison(candidate)}
            onLoadOlder={() => void loadOlderHistory()}
            onReconcile={openReconciliation}
            onVersionRestored={onVersionRestored}
            workspaceId={workspaceId}
          />
        </div>
      ) : null}
      {graphMountedPageId === detail.id ? (
        <div
          hidden={tab !== "graph"}
          id="context-reader-panel-graph"
          role="tabpanel"
          aria-labelledby="context-reader-tab-graph"
          tabIndex={0}
        >
          {graphFailed ? (
            <StatusBanner tone="warning">
              {t("neighbors.graphLoadError", {
                defaultValue: "Graph could not be loaded.",
              })}
            </StatusBanner>
          ) : graph ? (
            <GraphView
              key={detail.id}
              active={tab === "graph"}
              onCollapse={collapseGraphNode}
              onDepthChange={changeGraphDepth}
              onExpand={expandGraphNode}
              onHistoryModeChange={changeGraphHistoryMode}
              payload={graph}
              workspaceId={workspaceId}
              workspaceGraphHref={
                workspaceGraph.enabled
                  ? (graphNode) => {
                      // The Workspace Knowledge Map is published-head only.
                      // For the current Context page we have authoritative
                      // publication state, so do not create a link that its
                      // destination must fail to represent.
                      if (
                        graphNode.entity_kind === "context_page" &&
                        graphNode.entity_id === detail.id &&
                        !detail.current_published_version_id
                      ) {
                        return null;
                      }
                      const focusId = workspaceGraphFocusId(graphNode);
                      return focusId
                        ? `${workspacePath(workspaceId ?? detail.workspace_id, "graph")}?focus=${encodeURIComponent(focusId)}`
                        : null;
                    }
                  : undefined
              }
            />
          ) : (
            <p className="inspector-empty-copy">
              {t("neighbors.graphLoading", { defaultValue: "Loading graph..." })}
            </p>
          )}
        </div>
      ) : null}
      {tab === "activity" ? (
        <div
          id="context-reader-panel-activity"
          role="tabpanel"
          aria-labelledby="context-reader-tab-activity"
          tabIndex={0}
        >
          <ContextActivity
            workspaceId={workspaceId}
            error={activityError}
            items={activityItems}
            loadingMore={activityLoadingMore}
            nextCursor={activityNextCursor}
            onLoadMore={() => void loadMoreActivity()}
          />
        </div>
      ) : null}
      {shareOpen ? (
        <ContextShareDialog
          canPublish={
            canEdit &&
            (workspaceContext?.selectedWorkspace?.visibility === "public" ||
              workspaceContext?.selectedWorkspace?.role === "owner")
          }
          canUnpublish={canEdit}
          detail={detail}
          isPublic={isPublic}
          onClose={() => setShareOpen(false)}
          onPublicChange={setIsPublic}
          workspaceId={workspaceId}
          workspaceSlug={workspaceContext?.workspaceSlug ?? null}
          workspaceVisibility={
            workspaceContext?.selectedWorkspace?.visibility ?? null
          }
        />
      ) : null}
      {canEdit && reconciliationVersionId && workspaceId ? (
        <ContextReconciliationWorkbench
          branchVersionId={reconciliationVersionId}
          pageId={detail.id}
          workspaceId={workspaceId}
          onClose={closeReconciliation}
          // No separate close step: reloadContextAfterRestore clears
          // `reconcile` as part of its single URL mutation, so there is no
          // window in which one of the two writes overwrites the other.
          onCompleted={onVersionRestored}
        />
      ) : null}
    </section>
  );
}

function ContextShareDialog({
  canPublish,
  canUnpublish,
  detail,
  isPublic,
  onClose,
  onPublicChange,
  workspaceId,
  workspaceSlug,
  workspaceVisibility,
}: {
  canPublish: boolean;
  canUnpublish: boolean;
  detail: ContextPageDetail;
  isPublic: boolean;
  onClose: () => void;
  onPublicChange: (isPublic: boolean) => void;
  workspaceId: string | null;
  workspaceSlug: string | null;
  workspaceVisibility: string | null;
}) {
  const { t } = useTranslation("context");
  const [copyFeedback, setCopyFeedback] = useState<CopyTextDetailedResult | null>(null);
  const [savingPublicAccess, setSavingPublicAccess] = useState(false);
  const workspaceLinkRef = useRef<HTMLElement | null>(null);
  const workspacePath = `/workspaces/${workspaceId}/context/pages/${detail.id}`;
  const workspaceUrl =
    typeof window === "undefined"
      ? workspacePath
      : `${window.location.origin}${workspacePath}`;
  const workspaceIsPublic = workspaceVisibility === "public";
  const hasPublishedVersion = Boolean(detail.current_published_version_id);
  const publishedVersionInfo = (detail as ContextDetailWithPublishedVersion)
    .published_version_info;
  const publishDisabledReason = !workspaceSlug
    ? t("share.workspaceUnavailable")
    : !hasPublishedVersion
      ? t("share.publishVersionFirst")
      : null;

  useEffect(() => {
    if (!copyFeedback) {
      return undefined;
    }
    const timer = window.setTimeout(
      () => setCopyFeedback(null),
      copyFeedback.status === "copied" ? 1800 : 5000,
    );
    return () => window.clearTimeout(timer);
  }, [copyFeedback]);

  async function copyWorkspaceLink() {
    setCopyFeedback(
      await copyTextWithReason(workspaceUrl, workspaceLinkRef.current),
    );
  }

  function requestClose() {
    if (!savingPublicAccess) {
      onClose();
    }
  }

  return (
    <Dialog
      className="context-share-dialog"
      dismissible={!savingPublicAccess}
      labelledBy="context-share-dialog-title"
      onClose={requestClose}
    >
      <div className="context-share-dialog-heading dialog-header">
        <div>
          <p className="eyebrow">{t("share.eyebrow")}</p>
          <h2 id="context-share-dialog-title">
            {t("share.title", { title: detail.title })}
          </h2>
        </div>
        <DialogCloseButton
          disabled={savingPublicAccess}
          label={t("share.closeDialog")}
          onClick={requestClose}
        />
      </div>

      <section className="context-share-section">
        <h3>{t("share.workspaceLink.title")}</h3>
        <p>{t("share.workspaceLink.description")}</p>
        <div className="context-share-link-row">
          <code ref={workspaceLinkRef} title={workspaceUrl}>
            {workspacePath}
          </code>
          <button
            className="icon-button text-button"
            onClick={() => void copyWorkspaceLink()}
            type="button"
          >
            <Copy size={14} aria-hidden="true" />
            <span>{t("share.workspaceLink.copy")}</span>
          </button>
        </div>
        {copyFeedback ? (
          <span className="reader-action-status" role="status">
            {copyFeedback.status === "copied"
              ? t("share.copy.copied")
              : copyFeedback.status === "selected"
                ? t("share.copy.selected")
                : t(`share.copy.reasons.${copyFeedback.reason ?? "selection"}`)}
          </span>
        ) : null}
      </section>

      <section className="context-share-section context-share-section--web">
        <h3>{t("share.web.title")}</h3>
        <p>{t("share.web.description")}</p>
        <div className="context-share-prerequisites">
          <div>
            <span>{t("share.web.exposureScope")}</span>
            <strong data-ready="true">
              {workspaceIsPublic
                ? t("share.web.indexedScope")
                : t("share.web.unlistedScope")}
            </strong>
          </div>
          <div>
            <span>{t("share.web.publishedVersion")}</span>
            <strong data-ready={hasPublishedVersion}>
              {hasPublishedVersion ? t("share.ready") : t("share.required")}
            </strong>
          </div>
        </div>
        {detail.latest_draft_version_id && hasPublishedVersion ? (
          <p className="context-share-draft-note">{t("share.web.draftNote")}</p>
        ) : null}
        {!canPublish ? (
          <p className="context-share-editor-note">
            {workspaceIsPublic
              ? t("share.web.editorOnly")
              : t("share.web.ownerOnlyUnlisted")}
          </p>
        ) : null}
        <PagePublicShareControl
          canPublish={canPublish}
          canUnpublish={canUnpublish}
          isPublic={isPublic}
          onPublicChange={onPublicChange}
          onSavingChange={setSavingPublicAccess}
          pageId={detail.id}
          publishDisabledReason={canPublish ? publishDisabledReason : null}
          publishedVersionId={detail.current_published_version_id}
          publishedVersionNo={publishedVersionInfo?.version_no ?? null}
          slug={detail.slug}
          workspaceId={workspaceId}
          workspaceSlug={workspaceSlug}
          workspaceVisibility={workspaceVisibility}
        />
      </section>

    </Dialog>
  );
}

function ContextReaderNodeMenu({
  canEdit,
  favoriteScope,
  node,
  onCopyNodeLink,
  onDeleteNode,
  onMoveNode,
  onRenameNode,
}: {
  canEdit: boolean;
  favoriteScope?: TreeFavoriteScope;
  node: TreeNodePayload;
  onCopyNodeLink: (node: TreeNodePayload) => Promise<CopyTextDetailedResult>;
  onDeleteNode: (node: TreeNodePayload) => void;
  onMoveNode: (node: TreeNodePayload) => void;
  onRenameNode: (node: TreeNodePayload) => void;
}) {
  const { t } = useTranslation("context");
  const { t: tTree } = useTranslation("tree");
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [menuPosition, setMenuPosition] = useState<{
    left: number;
    top: number;
  } | null>(null);
  const [actionStatus, setActionStatus] = useState<{
    message: string;
    timeoutMs: number;
  } | null>(null);
  // The rail and this menu share one favorite store, so a toggle here also
  // updates the rail star and vice versa.
  const favoriteSnapshot = useSyncExternalStore(
    subscribeTreeFavorites,
    getTreeFavoriteSnapshot,
  );
  const canFavorite = Boolean(favoriteScope && favoriteTargetKind(node));
  const favorite =
    favoriteScope && canFavorite
      ? favoriteSnapshot.get(treeFavoriteKey(favoriteScope, node))
      : undefined;
  const menuNode = favorite ? { ...node, is_favorite: favorite.isFavorite } : node;

  useEffect(() => {
    if (!actionStatus) {
      return undefined;
    }
    const timeout = window.setTimeout(
      () => setActionStatus(null),
      actionStatus.timeoutMs,
    );
    return () => window.clearTimeout(timeout);
  }, [actionStatus]);

  function openMenu() {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) {
      return;
    }
    const menuWidth = 188;
    const left = Math.max(
      8,
      Math.min(window.innerWidth - menuWidth - 8, rect.right - menuWidth),
    );
    setMenuPosition({ left, top: rect.bottom + 6 });
  }

  function handleAction(action: TreeNodeContextMenuAction) {
    setMenuPosition(null);
    if (action.type === "rename") {
      onRenameNode(action.node);
      return;
    }
    if (action.type === "move") {
      onMoveNode(action.node);
      return;
    }
    if (action.type === "delete") {
      onDeleteNode(action.node);
      return;
    }
    if (action.type === "favorite") {
      if (favoriteScope) {
        setActionStatus(null);
        void toggleTreeFavorite(favoriteScope, action.node).catch(() =>
          setActionStatus({
            message: tTree("favorite.failed", { title: action.node.title }),
            timeoutMs: 5000,
          }),
        );
      }
      return;
    }
    if (action.type === "copy-link") {
      void onCopyNodeLink(action.node).then((result) =>
        setActionStatus({
          message:
            result.status === "copied"
              ? t("actions.linkCopied")
              : result.status === "selected"
                ? t("actions.linkSelected")
                : t(`actions.linkCopyReasons.${result.reason ?? "selection"}`),
          timeoutMs: result.status === "copied" ? 1800 : 5000,
        }),
      );
    }
  }

  return (
    <>
      <button
        aria-expanded={Boolean(menuPosition)}
        aria-haspopup="menu"
        aria-label={t("actions.morePageActions", {
          defaultValue: "More page actions",
        })}
        className="context-reader-header-action context-reader-more-action icon-button"
        ref={triggerRef}
        type="button"
        onClick={() => (menuPosition ? setMenuPosition(null) : openMenu())}
      >
        <MoreHorizontal size={16} aria-hidden="true" />
      </button>
      {actionStatus ? (
        <span className="reader-action-status" role="status">
          {actionStatus.message}
        </span>
      ) : null}
      {menuPosition ? (
        <TreeNodeContextMenu
          canCopyLink
          canCreateFolder={false}
          canCreatePage={false}
          canDelete={canEdit && !node.is_system_node}
          canFavorite={canFavorite}
          canMove={canEdit && !node.is_system_node}
          canRename={canEdit}
          favoritePending={Boolean(favorite?.pending)}
          node={menuNode}
          position={menuPosition}
          onAction={handleAction}
          onClose={() => setMenuPosition(null)}
        />
      ) : null}
    </>
  );
}

function readerVersionNumber(
  detail: ContextPageDetail,
  history: ContextChangeHistory | null,
): number | null {
  const publishedInfo = (detail as ContextDetailWithPublishedVersion)
    .published_version_info;
  if (publishedInfo && typeof publishedInfo.version_no === "number") {
    return publishedInfo.version_no;
  }
  const publishedId = detail.current_published_version_id;
  if (!publishedId) {
    return null;
  }
  if (
    detail.version_info?.version_id === publishedId &&
    typeof detail.version_info.version_no === "number"
  ) {
    return detail.version_info.version_no;
  }
  const match = history?.versions.find(
    (entry) => entry.version_id === publishedId,
  );
  return match?.version_no ?? null;
}

function ContextActivity({
  workspaceId,
  error,
  items,
  loadingMore,
  nextCursor,
  onLoadMore,
}: {
  workspaceId: string | null;
  error: string | null;
  items: ActivityItem[] | null;
  loadingMore: boolean;
  nextCursor: string | null;
  onLoadMore: () => void;
}) {
  const { t, i18n } = useTranslation("context");
  if (items === null) {
    return (
      <div className="wiki-empty">
        {t("reader.activityLoading", { defaultValue: "Loading activity" })}
      </div>
    );
  }
  const sortedItems = [...items].sort((left, right) =>
    right.created_at.localeCompare(left.created_at),
  );
  const dayFormatter = new Intl.DateTimeFormat(i18n.language, {
    dateStyle: "long",
  });
  const groupedItems = new Map<string, ActivityItem[]>();
  sortedItems.forEach((item) => {
    const date = new Date(item.created_at);
    const key = Number.isNaN(date.getTime())
      ? t("reader.activityUnknownDate", { defaultValue: "Date unavailable" })
      : dayFormatter.format(date);
    groupedItems.set(key, [...(groupedItems.get(key) ?? []), item]);
  });
  return (
    <div className="context-page-activity">
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      {sortedItems.length === 0 && !error ? (
        <div className="wiki-empty">
          {t("reader.activityEmpty", {
            defaultValue: "Nothing has happened to this page yet.",
          })}
        </div>
      ) : sortedItems.length > 0 ? (
        <div
          aria-label={t("reader.activityListLabel", {
            defaultValue: "Page activity",
          })}
          className="context-activity-days"
          role="region"
        >
          {[...groupedItems.entries()].map(([day, dayItems]) => (
            <section
              aria-labelledby={`context-activity-day-${dayItems[0].id}`}
              key={day}
            >
              <h3 id={`context-activity-day-${dayItems[0].id}`}>{day}</h3>
              <ul
                aria-label={t("reader.activityListLabel", {
                  defaultValue: "Page activity",
                })}
                className="context-activity-card-list"
              >
                {dayItems.map((item) => (
                  <li key={item.id}>
                    <ActivityEventCard item={item} workspaceId={workspaceId} />
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      ) : null}
      {nextCursor ? (
        <div className="context-activity-load-more">
          <button
            aria-busy={loadingMore}
            className="text-button"
            disabled={loadingMore}
            onClick={onLoadMore}
            type="button"
          >
            {loadingMore
              ? t("reader.activityLoadingOlder", {
                  defaultValue: "Loading older activity",
                })
              : t("reader.activityLoadOlder", {
                  defaultValue: "Load older activity",
                })}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ContextHomeRecentRow({
  page,
  onSelect,
}: {
  page: ContextPageSummary;
  onSelect: (pageId: string) => void;
}) {
  const { t } = useTranslation("context");
  const showDraftBadge =
    Boolean(page.latest_draft_version_id) && page.lifecycle_status !== "draft";

  return (
    <button
      aria-pressed={false}
      className="entity-row selectable-row"
      onClick={() => onSelect(page.id)}
      type="button"
    >
      <FileText size={16} aria-hidden="true" />
      <div>
        <strong>{page.title}</strong>
        <p>{page.slug}</p>
      </div>
      <span>{domainLabel("context", "pageType", page.page_type)}</span>
      <span className="row-badges">
        <StatusPill status={page.lifecycle_status} compact />
        {showDraftBadge ? (
          <StatusPill
            status="draft"
            compact
            label={t("list.draftIndicator", {
              defaultValue: "Unpublished draft",
            })}
          />
        ) : null}
      </span>
    </button>
  );
}

function orderedContextChildren(
  nodes: TreeNodePayload[],
  parentId: string,
): TreeNodePayload[] {
  return nodes
    .filter((candidate) => candidate.parent_id === parentId)
    .sort((left, right) => {
      const section = left.section.localeCompare(right.section);
      if (section !== 0) return section;
      const sortKey = String(left.sort_key ?? "").localeCompare(
        String(right.sort_key ?? ""),
        undefined,
        { numeric: true },
      );
      if (sortKey !== 0) return sortKey;
      const title = left.title.localeCompare(right.title, undefined, {
        sensitivity: "base",
      });
      return title !== 0 ? title : left.id.localeCompare(right.id);
    });
}

function useContextChildPage(totalItems: number, pageParam: string) {
  const [searchParams, setSearchParams] = useSearchParams();
  const totalPages = Math.max(
    1,
    Math.ceil(totalItems / CONTEXT_CHILD_PAGE_SIZE),
  );
  const rawPageParam = searchParams.get(pageParam);
  const validRawPage = rawPageParam
    ? /^[1-9]\d*$/.test(rawPageParam)
    : true;
  const requestedPage = validRawPage
    ? Number.parseInt(rawPageParam ?? "1", 10)
    : 1;
  const page = Math.min(requestedPage, totalPages);

  useEffect(() => {
    const canonicalPageParam = page === 1 ? null : String(page);
    if (rawPageParam === canonicalPageParam) {
      return;
    }
    const next = new URLSearchParams(searchParams);
    if (page === 1) {
      next.delete(pageParam);
    } else {
      next.set(pageParam, String(page));
    }
    setSearchParams(next, { replace: true });
  }, [page, pageParam, rawPageParam, searchParams, setSearchParams]);

  const setPage = useCallback(
    (nextPage: number) => {
      const clamped = Math.min(Math.max(1, nextPage), totalPages);
      const next = new URLSearchParams(searchParams);
      if (clamped === 1) {
        next.delete(pageParam);
      } else {
        next.set(pageParam, String(clamped));
      }
      setSearchParams(next);
    },
    [pageParam, searchParams, setSearchParams, totalPages],
  );

  return { page, setPage, totalPages };
}

function paginationWindow(
  page: number,
  totalPages: number,
): Array<number | "ellipsis"> {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }
  const candidates = new Set([1, totalPages, page - 1, page, page + 1]);
  const pages = [...candidates]
    .filter((candidate) => candidate >= 1 && candidate <= totalPages)
    .sort((left, right) => left - right);
  const result: Array<number | "ellipsis"> = [];
  pages.forEach((candidate, index) => {
    const previous = pages[index - 1];
    if (previous !== undefined && candidate - previous > 1) {
      result.push("ellipsis");
    }
    result.push(candidate);
  });
  return result;
}

function ContextChildrenPagination({
  page,
  setPage,
  totalItems,
  totalPages,
}: {
  page: number;
  setPage: (page: number) => void;
  totalItems: number;
  totalPages: number;
}) {
  const { t } = useTranslation("context");
  const statusRef = useRef<HTMLParagraphElement | null>(null);
  const previousPageRef = useRef(page);
  useEffect(() => {
    if (previousPageRef.current !== page) {
      previousPageRef.current = page;
      statusRef.current?.focus();
    }
  }, [page]);
  if (totalItems <= CONTEXT_CHILD_PAGE_SIZE) {
    return null;
  }
  const start = (page - 1) * CONTEXT_CHILD_PAGE_SIZE + 1;
  const end = Math.min(page * CONTEXT_CHILD_PAGE_SIZE, totalItems);
  return (
    <div className="context-children-pagination">
      <p
        aria-live="polite"
        className="context-children-pagination-status"
        ref={statusRef}
        tabIndex={-1}
      >
        {t("children.showingRange", {
          defaultValue: "Showing {{start}}–{{end}} of {{total}}",
          start,
          end,
          total: totalItems,
        })}
      </p>
      <nav
        aria-label={t("children.paginationLabel", {
          defaultValue: "Children pagination",
        })}
      >
        <button
          className="icon-button text-button"
          disabled={page === 1}
          onClick={() => setPage(page - 1)}
          type="button"
        >
          {t("children.previous", { defaultValue: "Previous" })}
        </button>
        {paginationWindow(page, totalPages).map((entry, index) =>
          entry === "ellipsis" ? (
            <span aria-hidden="true" key={`ellipsis-${index}`}>
              …
            </span>
          ) : (
            <button
              aria-current={entry === page ? "page" : undefined}
              aria-label={t("children.pageLabel", {
                defaultValue: "Page {{page}}",
                page: entry,
              })}
              className="numeric-pagination__page"
              key={entry}
              onClick={() => setPage(entry)}
              type="button"
            >
              {entry}
            </button>
          ),
        )}
        <button
          className="icon-button text-button"
          disabled={page === totalPages}
          onClick={() => setPage(page + 1)}
          type="button"
        >
          {t("children.next", { defaultValue: "Next" })}
        </button>
      </nav>
    </div>
  );
}

function ContextFolderSummaryCard(
  props: ComponentProps<typeof FolderSummaryCard>,
) {
  const children = useMemo(
    () => orderedContextChildren(props.nodes, props.node.id),
    [props.node.id, props.nodes],
  );
  const { page, setPage, totalPages } = useContextChildPage(
    children.length,
    CONTEXT_FOLDER_CHILD_PAGE_PARAM,
  );
  const start = (page - 1) * CONTEXT_CHILD_PAGE_SIZE;
  const visibleChildren = children.slice(
    start,
    start + CONTEXT_CHILD_PAGE_SIZE,
  );
  const folderCount = children.filter(
    (child) => child.node_kind === "folder",
  ).length;
  return (
    <FolderSummaryCard
      {...props}
      nodes={visibleChildren}
      totalChildCounts={{
        folders: folderCount,
        pages: children.length - folderCount,
      }}
      footer={
        children.length > CONTEXT_CHILD_PAGE_SIZE ? (
          <ContextChildrenPagination
            page={page}
            setPage={setPage}
            totalItems={children.length}
            totalPages={totalPages}
          />
        ) : undefined
      }
    />
  );
}

export function ContextChildrenIndex({
  node,
  nodes,
  onSelectNode,
}: {
  node: TreeNodePayload;
  nodes: TreeNodePayload[];
  onSelectNode: (node: TreeNodePayload) => void;
}) {
  const { t } = useTranslation("context");
  const children = useMemo(
    () => orderedContextChildren(nodes, node.id),
    [node.id, nodes],
  );
  const { page, setPage, totalPages } = useContextChildPage(
    children.length,
    CONTEXT_CHILD_TAB_PAGE_PARAM,
  );
  if (children.length === 0) {
    return null;
  }
  const start = (page - 1) * CONTEXT_CHILD_PAGE_SIZE;
  const visibleChildren = children.slice(
    start,
    start + CONTEXT_CHILD_PAGE_SIZE,
  );
  return (
    <section
      aria-labelledby={`context-children-${node.id}`}
      className="context-children-index detail-section"
    >
      <div className="detail-heading">
        <div>
          <p className="eyebrow">
            {t("children.eyebrow", { defaultValue: "Direct index" })}
          </p>
          <h3 id={`context-children-${node.id}`}>
            {t("children.title", { defaultValue: "Children" })}
          </h3>
        </div>
        <StatusPill
          status={t("children.count", {
            count: children.length,
            defaultValue: "{{count}} children",
          })}
        />
      </div>
      <p className="folder-summary-text">
        {t("children.description", {
          defaultValue:
            "Open a direct child page or folder from this page's index.",
        })}
      </p>
      <div className="folder-child-list">
        {visibleChildren.map((child) => (
          <button
            aria-label={child.title}
            className="folder-child-row"
            key={child.id}
            onClick={() => onSelectNode(child)}
            type="button"
          >
            {child.node_kind === "folder" ? (
              <Folder size={16} aria-hidden="true" />
            ) : (
              <FileText size={16} aria-hidden="true" />
            )}
            <div>
              <strong>{child.title}</strong>
              <p>
                {child.nav_summary ??
                  (child.node_kind === "folder"
                    ? t("children.folderChildCount", {
                        count: child.children_count,
                        defaultValue: "{{count}} direct children",
                      })
                    : [
                        child.target_meta?.page_type
                          ? domainLabel(
                              "context",
                              "pageType",
                              child.target_meta.page_type,
                            )
                          : null,
                        child.target_meta?.lifecycle_status
                          ? domainLabel(
                              "context",
                              "lifecycleStatus",
                              child.target_meta.lifecycle_status,
                            )
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · "))}
              </p>
            </div>
            <StatusPill
              compact
              label={domainLabel(
                "context",
                "nodeKind",
                child.target_kind ?? child.node_kind,
              )}
              status={child.target_kind ?? child.node_kind}
            />
          </button>
        ))}
      </div>
      {children.length > CONTEXT_CHILD_PAGE_SIZE ? (
        <footer className="folder-summary-footer">
          <ContextChildrenPagination
            page={page}
            setPage={setPage}
            totalItems={children.length}
            totalPages={totalPages}
          />
        </footer>
      ) : null}
    </section>
  );
}

export function ContextHistory({
  canEdit,
  comparePair,
  compareStateError,
  detail,
  history,
  historyLoadError,
  historyLoadingOlder,
  highlightedVersionId,
  onCloseCompare,
  onCompare,
  onComparePair,
  onLoadOlder,
  onReconcile,
  onVersionRestored,
  workspaceId,
}: {
  canEdit: boolean;
  comparePair: ComparePair | null;
  compareStateError: string | null;
  detail: ContextPageDetail;
  history: ContextChangeHistory | null;
  historyLoadError: string | null;
  historyLoadingOlder: boolean;
  highlightedVersionId?: string | null;
  onCloseCompare: () => void;
  onCompare: (entry: VersionEntry) => void;
  onComparePair: (candidate: CompareCandidate) => void;
  onLoadOlder: () => void;
  onReconcile: (versionId: string) => void;
  onVersionRestored: () => Promise<void> | void;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("context");
  const { t: tShell } = useTranslation("shell");
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [restoreMessage, setRestoreMessage] = useState<string | null>(null);
  const [restoreVersionId, setRestoreVersionId] = useState<string | null>(null);
  // One restore at a time across the whole list, not just per row.
  const restoring = restoreVersionId !== null;
  const [discardVersionId, setDiscardVersionId] = useState<string | null>(null);
  const [discardCandidate, setDiscardCandidate] = useState<VersionEntry | null>(
    null,
  );
  const [discardReason, setDiscardReason] = useState("");
  const versions = useMemo(() => history?.versions ?? [], [history?.versions]);
  const versionNodes = useMemo(() => contextHistoryNodes(versions), [versions]);
  const historyComplete = !history?.next_cursor;
  const historyGraph = useMemo(
    () =>
      buildHistoryGraph(versionNodes, {
        // Page-level ids, not row flags: the trunk has to be known before the
        // page holding its head is loaded, or Load older would re-seat rows.
        headCandidateIds: [
          detail.latest_draft_version_id,
          detail.current_published_version_id,
          detail.visible_version_id,
        ],
        complete: historyComplete,
      }),
    [
      detail.current_published_version_id,
      detail.latest_draft_version_id,
      detail.visible_version_id,
      historyComplete,
      versionNodes,
    ],
  );
  const loadDiff = useCallback(
    (pair: ComparePair, signal: AbortSignal) =>
      workspaceId
        ? diffPageVersions(workspaceId, detail.id, pair.from, pair.to, {
            signal,
          })
        : Promise.reject(new Error("No workspace")),
    [detail.id, workspaceId],
  );
  const {
    diff,
    error: diffError,
    loadingVersionId: diffLoadingVersionId,
  } = useVersionPairDiff(
    workspaceId ? comparePair : null,
    loadDiff,
    tShell("history.failedChanges", {
      defaultValue: "Failed to load changes",
    }),
  );
  const newestVersion = versionNodes.reduce<VersionEntry | null>(
    (newest, node) =>
      newest === null || node.versionNo > newest.version_no
        ? node.entry
        : newest,
    null,
  );
  const comparedVersion =
    versions.find((entry) => entry.version_id === comparePair?.to) ?? null;
  const publishedVersion =
    versions.find(
      (entry) =>
        versionBooleanField(entry, "is_published_head") ||
        versionBooleanField(entry, "is_current_published"),
    ) ?? null;
  const visibleHeadVersionId = versions.find((entry) =>
    versionBooleanField(entry, "is_visible_head"),
  )?.version_id;
  const crossesLineage = comparePairCrossesLineage(versions, comparePair);
  useEffect(() => {
    setRestoreError(null);
    setRestoreMessage(null);
    setRestoreVersionId(null);
    setDiscardVersionId(null);
    setDiscardCandidate(null);
    setDiscardReason("");
  }, [detail.id]);
  useEffect(() => {
    setRestoreError(null);
  }, [comparePair]);

  function closeCompareVersion() {
    setRestoreError(null);
    onCloseCompare();
  }

  async function restoreVersion(entry: VersionEntry) {
    // Restoring the version you are already on just stacks an identical one.
    if (entry.is_visible_head || !workspaceId || restoring) {
      return;
    }
    const ok = window.confirm(
      t("history.restoreConfirm", {
        version: entry.version_no,
        defaultValue:
          "Restore v{{version}} as a new draft? The published version stays unchanged.",
      }),
    );
    if (!ok) {
      return;
    }
    setRestoreError(null);
    setRestoreMessage(null);
    setRestoreVersionId(entry.version_id);
    try {
      const restored = await restorePageVersion(
        workspaceId,
        detail.id,
        entry.version_id,
      );
      setRestoreMessage(
        t("history.restored", {
          version: entry.version_no,
          draft: restored.version_id.slice(0, 8),
          defaultValue: "v{{version}} was restored as draft v{{draft}}.",
        }),
      );
      onCloseCompare();
      await onVersionRestored();
    } catch (exc) {
      setRestoreError(
        apiErrorMessage(
          exc,
          t("history.failedRestore", {
            defaultValue: "Failed to restore version",
          }),
        ),
      );
    } finally {
      setRestoreVersionId(null);
    }
  }

  async function discardAlternative(entry: VersionEntry) {
    if (entry.history_status !== "alternative_draft" || !workspaceId) {
      return;
    }
    if (discardReason.trim().length === 0) {
      return;
    }
    setRestoreError(null);
    setRestoreMessage(null);
    setDiscardVersionId(entry.version_id);
    try {
      await discardAlternativeDraft(workspaceId, detail.id, entry.version_id, {
        reason: discardReason.trim(),
      });
      if (comparePair?.to === entry.version_id) {
        closeCompareVersion();
      }
      setRestoreMessage(
        t("history.discarded", {
          version: entry.version_no,
          defaultValue: "Alternative v{{version}} was discarded.",
        }),
      );
      await onVersionRestored();
      setDiscardCandidate(null);
      setDiscardReason("");
    } catch (exc) {
      setRestoreError(
        apiErrorMessage(
          exc,
          t("history.failedDiscard", {
            defaultValue: "Failed to discard alternative",
          }),
        ),
      );
    } finally {
      setDiscardVersionId(null);
    }
  }

  function renderCard(row: HistoryGraphRow<VersionEntry>) {
    const { entry } = row;
    const historyStatus = entry.history_status ?? "state_inconsistent";
    const presentation = contextHistoryPresentation(historyStatus);
    const comparing = comparePair?.to === entry.version_id;
    const isVisibleHead = entry.version_id === visibleHeadVersionId;
    const isCurrent = versionBooleanField(entry, "is_current");
    const isLatestDraft =
      versionBooleanField(entry, "is_draft_head") ||
      versionBooleanField(entry, "is_latest_draft");
    const reviewStatus = versionStringField(entry, "review_status");
    const priorRelation = row.node.parentRelation;
    return (
      <HistoryCard
        actions={
          <>
            {canEdit &&
            !comparing &&
            !isCurrent &&
            !isVisibleHead &&
            !isLatestDraft &&
            entry.reconciliation_available !== true ? (
              <HistoryActionButton
                accessibleLabel={t("history.restore", {
                  defaultValue: "Restore this version",
                })}
                busy={restoreVersionId === entry.version_id}
                disabled={restoring}
                icon={RotateCcw}
                label={
                  restoreVersionId === entry.version_id
                    ? t("history.restoring", { defaultValue: "Restoring" })
                    : tShell("history.restoreShort", { defaultValue: "Restore" })
                }
                onClick={() => void restoreVersion(entry)}
              />
            ) : null}
            {canEdit &&
            entry.history_status === "alternative_draft" &&
            entry.reconciliation_available === true ? (
              <HistoryActionButton
                accessibleLabel={t("reconciliation.open", {
                  defaultValue: "Reconcile",
                })}
                icon={GitMerge}
                label={t("reconciliation.open", { defaultValue: "Reconcile" })}
                onClick={() => onReconcile(entry.version_id)}
              />
            ) : null}
            {/* Offered whenever the parent is visible, even when it is on an
                older page not loaded yet: opening it loads that page. */}
            {priorRelation === "visible" && entry.prior_version_id ? (
              <HistoryChangesButton
                loading={diffLoadingVersionId === entry.version_id}
                onToggle={() =>
                  comparing ? closeCompareVersion() : onCompare(entry)
                }
                open={comparing}
                row={row}
              />
            ) : null}
            {/* Last in source order and pushed to the right edge, so the
                buttons every row shares keep the same place whether or not
                this row can be discarded, and focus order matches the eye. */}
            {canEdit && entry.history_status === "alternative_draft" ? (
              <HistoryActionButton
                accessibleLabel={t("history.discard", {
                  defaultValue: "Discard",
                })}
                busy={discardVersionId === entry.version_id}
                icon={Trash2}
                label={
                  discardVersionId === entry.version_id
                    ? t("history.discarding", { defaultValue: "Discarding" })
                    : t("history.discard", { defaultValue: "Discard" })
                }
                onClick={() => {
                  setDiscardCandidate(entry);
                  setDiscardReason("");
                }}
                tone="danger"
              />
            ) : null}
          </>
        }
        author={
          <>
            <AvatarWithTimestamp
              actorType={entry.actor_type}
              name={entry.actor_display_name ?? null}
              timestamp={entry.created_at ?? null}
            />
            {reviewStatus ? (
              <span className="context-history-review">
                <span className="context-history-review-label">
                  {t("history.reviewLabel", { defaultValue: "Review" })}
                </span>
                <StatusPill status={reviewStatus} compact />
              </span>
            ) : null}
          </>
        }
        badges={
          <>
            {highlightedVersionId === entry.version_id ? (
              <span className="history-reader-visible">
                {t("history.readerVisibleVersion", {
                  defaultValue: "Readers see this version",
                })}
              </span>
            ) : null}
            <HistoryLineDelta
              added={entry.lines_added}
              removed={entry.lines_removed}
            />
          </>
        }
        description={t(presentation.descriptionKey)}
        notices={
          priorRelation === "hidden" || priorRelation === "missing" ? (
            <HistoryChangesUnavailable />
          ) : null
        }
        row={row}
        status={
          <StatusPill
            compact
            label={t(presentation.labelKey)}
            status={historyStatus}
            tone={contextHistoryPillTone(historyStatus)}
          />
        }
      />
    );
  }

  const olderHistoryControls = (
    <>
      {historyLoadError ? (
        <StatusBanner tone="warning">{historyLoadError}</StatusBanner>
      ) : null}
      {history?.next_cursor ? (
        <div className="history-compare-toolbar">
          <button
            aria-busy={historyLoadingOlder}
            className="text-button"
            disabled={historyLoadingOlder}
            onClick={onLoadOlder}
            type="button"
          >
            {historyLoadingOlder
              ? t("history.loadingOlder", {
                  defaultValue: "Loading older versions",
                })
              : t("history.loadOlder", {
                  defaultValue: "Load older versions",
                })}
          </button>
        </div>
      ) : null}
    </>
  );
  if (versions.length === 0) {
    return (
      <>
        <div className="wiki-empty">{t("inspector.noVersionHistory")}</div>
        {olderHistoryControls}
      </>
    );
  }
  return (
    <>
      <div>
        <div className="history-help">
          {t("history.help", {
            defaultValue:
              "Review versions newest first. Changes shows what a version changed from the version it was based on; Compare two versions diffs any pair.",
          })}
        </div>
        <div className="history-compare-toolbar">
          <button
            aria-disabled={versions.length < 2}
            className="text-button"
            disabled={versions.length < 2}
            onClick={() => {
              if (newestVersion) {
                onCompare(newestVersion);
              }
            }}
            type="button"
          >
            {tShell("history.compareTwo", {
              defaultValue: "Compare two versions",
            })}
          </button>
        </div>
        {compareStateError ? (
          <StatusBanner tone="warning">{compareStateError}</StatusBanner>
        ) : null}
        {restoreMessage ? (
          <StatusBanner tone="info">{restoreMessage}</StatusBanner>
        ) : null}
        {restoreError ? (
          <StatusBanner tone="warning">{restoreError}</StatusBanner>
        ) : null}
        <HistoryGraphList
          accent="context"
          graph={historyGraph}
          renderCard={renderCard}
          rowView={(row) => {
            const highlighted = highlightedVersionId === row.entry.version_id;
            return {
              ariaCurrent:
                row.entry.version_id === visibleHeadVersionId
                  ? "page"
                  : undefined,
              className: highlighted ? "history-card-highlighted" : undefined,
              dataAttributes: {
                "data-context-version-id": row.entry.version_id,
              },
              tabIndex: highlighted ? -1 : undefined,
              tone: contextHistoryPresentation(
                row.entry.history_status ?? "state_inconsistent",
              ).tone,
            };
          }}
        />
        {olderHistoryControls}
      </div>
      {discardCandidate ? (
        <Dialog
          className="context-history-discard-dialog"
          dismissible={discardVersionId === null}
          labelledBy="context-history-discard-title"
          onClose={() => {
            if (!discardVersionId) {
              setDiscardCandidate(null);
              setDiscardReason("");
            }
          }}
        >
          <div className="dialog-form">
            <h2 id="context-history-discard-title">
              {t("history.discardConfirm", {
                version: discardCandidate.version_no,
                defaultValue:
                  "Discard alternative v{{version}}? This cannot be undone.",
              })}
            </h2>
            <label>
              <span>
                {t("history.discardReason", { defaultValue: "Discard reason" })}
              </span>
              <textarea
                autoFocus
                required
                value={discardReason}
                onChange={(event) => setDiscardReason(event.currentTarget.value)}
              />
            </label>
            <div className="dialog-actions">
              <button
                className="icon-button text-button"
                disabled={discardVersionId !== null}
                onClick={() => setDiscardCandidate(null)}
                type="button"
              >
                {t("history.cancelDiscard", { defaultValue: "Cancel" })}
              </button>
              <button
                className="danger"
                disabled={
                  discardVersionId !== null || discardReason.trim().length === 0
                }
                onClick={() => void discardAlternative(discardCandidate)}
                type="button"
              >
                {discardVersionId
                  ? t("history.discarding", { defaultValue: "Discarding" })
                  : t("history.confirmDiscard", {
                      defaultValue: "Discard branch permanently",
                    })}
              </button>
            </div>
          </div>
        </Dialog>
      ) : null}
      {comparedVersion && comparePair ? (
        <HistoryDiffDialog
          diff={diff}
          error={diffError}
          loading={diffLoadingVersionId === comparedVersion.version_id}
          onClose={closeCompareVersion}
          onPairChange={(pair) => onComparePair({ ...pair, origin: "history" })}
          pair={comparePair}
          publishedEndpoint={
            publishedVersion?.version_id === comparePair.from
              ? "from"
              : publishedVersion?.version_id === comparePair.to
                ? "to"
                : null
          }
          publishedVersionLabel={
            publishedVersion
              ? tShell("diff.currentPublishedVersion", {
                  version: publishedVersion.version_no,
                  defaultValue:
                    "Current published v{{version}} · visible to readers",
                })
              : undefined
          }
          versions={versionNodes}
          warning={
            crossesLineage
              ? t("history.crossLineageWarning", {
                  defaultValue:
                    "These versions are on different lineages. This diff compares snapshots across a branch, not a single progression.",
                })
              : null
          }
        />
      ) : null}
    </>
  );
}

// Map deleted-source tombstones to the render-time marks MarkdownView swaps in
// for dangling `@cite{...}` tokens. The citation key is recomputed from the
// source's id/title/uri with the same algorithm the editor used to emit it.
function buildDeletedSourceMarks(
  tombstones: LibraryItemTombstone[],
  t: (key: string, options?: Record<string, unknown>) => string,
): Array<{ citationKey: string; label: string }> {
  const marks: Array<{ citationKey: string; label: string }> = [];
  for (const tombstone of tombstones) {
    const citationKey = citationKeyForSource({
      id: tombstone.library_item_id,
      title: tombstone.title,
      uri: tombstone.uri,
    });
    if (!citationKey) {
      continue;
    }
    const title = tombstone.title?.trim() || tombstone.uri;
    const version =
      tombstone.last_version_no != null
        ? `v${tombstone.last_version_no}`
        : null;
    const deletedAt = tombstone.deleted_at
      ? formatVersionTimestamp(tombstone.deleted_at)
      : null;
    const detail = [version, deletedAt].filter(Boolean).join(", ");
    const label = detail
      ? t("reader.deletedSource", {
          defaultValue: "{{title}} (deleted - {{detail}})",
          title,
          detail,
        })
      : t("reader.deletedSourceShort", {
          defaultValue: "{{title}} (deleted)",
          title,
        });
    marks.push({ citationKey, label });
  }
  return marks;
}

function versionBooleanField(entry: VersionEntry, field: string): boolean {
  return Reflect.get(entry as object, field) === true;
}

function versionStringField(entry: VersionEntry, field: string): string | null {
  const value = Reflect.get(entry as object, field);
  return typeof value === "string" && value.length > 0 ? value : null;
}

function ContextInspector({
  detail,
  workspaceId,
}: {
  detail: ContextPageDetail | null;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("context");
  const {
    data: neighbors,
    error: neighborsError,
    loading: neighborsLoading,
  } = useAsyncLoad<GraphNeighborsPayload | null>(
    () => workspaceId && detail
      ? getContextPageNeighbors(workspaceId, detail.id)
      : Promise.resolve(null),
    [workspaceId, detail],
    { errorMessage: t("neighbors.loadError"), fallbackData: null },
  );
  // Inspector tab state lives here (not in WorkspaceLayout) so it survives the
  // panel re-render and resets when a different page is selected.
  const [tab, setTab] = useState<ContextInspectorTab>("summary");
  useEffect(() => {
    setTab("summary");
  }, [detail?.id]);

  if (!detail) {
    return (
      <div className="inspector-empty">
        <h2>{t("inspector.emptyTitle", { defaultValue: "Context detail" })}</h2>
        <p>
          {t("inspector.emptyDescription", {
            defaultValue:
              "Select a Context page to inspect its details and citations.",
          })}
        </p>
      </div>
    );
  }

  const backlinkCount =
    detail.backlink_count ?? neighbors?.backlinks?.length ?? 0;
  return (
    <TabbedInspector
      activeKey={tab}
      ariaLabel={t("inspector.ariaLabel", { defaultValue: "Context detail" })}
      className="context-inspector"
      onActiveKeyChange={(key) => setTab(key as ContextInspectorTab)}
      tabs={[
        {
          key: "summary",
          label: t("inspector.summary", { defaultValue: "Summary" }),
          badge:
            detail.warnings.length > 0 ? detail.warnings.length : undefined,
          content: <ContextSummaryPanel detail={detail} />,
        },
        {
          key: "properties",
          label: t("inspector.properties", { defaultValue: "Properties" }),
          content: <ContextPropertiesPanel detail={detail} />,
        },
        {
          key: "backlinks",
          label: t("inspector.backlinks", { defaultValue: "Backlinks" }),
          badge: backlinkCount > 0 ? backlinkCount : undefined,
          content: (
            <ContextBacklinksPanel
              detail={detail}
              neighbors={neighbors}
              neighborsFailed={Boolean(neighborsError)}
              neighborsLoading={neighborsLoading}
              workspaceId={workspaceId}
            />
          ),
        },
      ]}
    />
  );
}

type ContextInspectorTab = "summary" | "properties" | "backlinks";

function ContextSummaryPanel({ detail }: { detail: ContextPageDetail }) {
  const { t } = useTranslation("context");
  const hasWarnings = detail.warnings.length > 0;
  return (
    <ContentSummaryPanel
      badge={
        hasWarnings
          ? detail.warnings.length
          : t("inspector.ok", { defaultValue: "OK" })
      }
      badgeTone={hasWarnings ? "warning" : "ok"}
      description={t("inspector.warningCount", {
        count: detail.warnings.length,
        defaultValue: "{{count}} warnings",
      })}
      heading={t("inspector.citationHealth", {
        defaultValue: "CITATION HEALTH",
      })}
      title={
        hasWarnings
          ? t("inspector.warnings", { defaultValue: "Warnings" })
          : t("inspector.noWarnings", { defaultValue: "No warnings" })
      }
      warnings={detail.warnings}
    />
  );
}

function ContextPropertiesPanel({ detail }: { detail: ContextPageDetail }) {
  const { t } = useTranslation("context");
  const version = detail.version_info;
  const versionPresentation = version
    ? contextHistoryPresentation(version.history_status)
    : null;
  return (
    <ContentPropertiesPanel
      ariaLabel={t("inspector.contextProperties", {
        defaultValue: "Context properties",
      })}
      heading={t("inspector.properties", { defaultValue: "Properties" })}
      properties={[
        {
          label: t("inspector.type", { defaultValue: "Type" }),
          value: domainLabel("context", "pageType", detail.page_type),
        },
        {
          label: t("inspector.slug", { defaultValue: "Slug" }),
          value: detail.slug,
        },
        {
          label: t("inspector.status", { defaultValue: "Status" }),
          value: versionPresentation
            ? t(versionPresentation.labelKey)
            : humanizeEnum(detail.lifecycle_status),
        },
        {
          label: t("inspector.version", { defaultValue: "Version" }),
          value: version?.version_no
            ? `v${version.version_no}`
            : t("inspector.none", { defaultValue: "none" }),
        },
      ]}
    />
  );
}

function ContextBacklinksPanel({
  detail,
  neighbors,
  neighborsFailed,
  neighborsLoading,
  workspaceId,
}: {
  detail: ContextPageDetail;
  neighbors: GraphNeighborsPayload | null;
  neighborsFailed: boolean;
  neighborsLoading: boolean;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("context");
  if (neighborsLoading) {
    return (
      <p role="status">
        {t("neighbors.graphLoading", { defaultValue: "Loading graph..." })}
      </p>
    );
  }
  const backlinks = neighbors?.backlinks ?? [];
  const citations = neighbors?.citations ?? [];
  const coAccess = neighbors?.co_access ?? [];
  const outboundCount = neighbors?.outbound?.length ?? 0;
  const neighborsBounded =
    neighbors?.truncated === true || Boolean(neighbors?.warnings?.length);
  return (
    <div className="context-backlinks-sections inspector-stack">
      {neighborsFailed ? (
        <StatusBanner tone="warning">
          {t("neighbors.loadError", {
            defaultValue:
              "Neighbor details could not be loaded. Try again later.",
          })}
        </StatusBanner>
      ) : null}
      {!neighborsFailed && neighborsBounded ? (
        <StatusBanner tone="warning">
          {t("neighbors.boundedWarning", {
            defaultValue:
              "Some relationship results were limited. The lists below may be incomplete.",
          })}
        </StatusBanner>
      ) : null}
      <ContentBacklinksPanel
        ariaLabel={t("neighbors.backlinksHeading", {
          defaultValue: "Backlinks",
        })}
        backlinks={backlinks.map((entry) => graphNeighborItem(entry, workspaceId ?? detail.workspace_id))}
        count={detail.backlink_count ?? backlinks.length}
        emptyLabel={
          neighborsFailed
            ? t("neighbors.unavailable", {
                defaultValue: "Neighbor list unavailable.",
              })
            : t("neighbors.emptyBacklinks", {
                defaultValue: "No visible pages link here yet.",
              })
        }
        heading={t("neighbors.backlinksHeading", { defaultValue: "BACKLINKS" })}
        metrics={[
          {
            label: t("neighbors.outbound", { defaultValue: "Outbound links" }),
            value: outboundCount,
          },
          {
            label: t("inspector.childPages", { defaultValue: "Child pages" }),
            value: detail.children_count,
          },
        ]}
      />
      <ContentBacklinksPanel
        ariaLabel={t("neighbors.citationsHeading", {
          defaultValue: "Citations",
        })}
        backlinks={citations.map((entry) => graphNeighborItem(entry, workspaceId ?? detail.workspace_id))}
        emptyLabel={
          neighborsFailed
            ? t("neighbors.unavailable", {
                defaultValue: "Neighbor list unavailable.",
              })
            : t("neighbors.emptyCitations", {
                defaultValue: "No visible citations yet.",
              })
        }
        heading={t("neighbors.citationsHeading", { defaultValue: "CITATIONS" })}
      />
      <ContentBacklinksPanel
        ariaLabel={t("neighbors.coAccessHeading", {
          defaultValue: "Often read together",
        })}
        backlinks={coAccess.map((entry) => graphNeighborItem(entry, workspaceId ?? detail.workspace_id))}
        emptyLabel={t("neighbors.emptyCoAccess", {
          defaultValue: "No co-access trail has been compiled yet.",
        })}
        heading={t("neighbors.coAccessHeading", {
          defaultValue: "OFTEN READ TOGETHER",
        })}
      />
    </div>
  );
}

function graphNeighborItem(
  entry: GraphNeighborEntry,
  workspaceId: string,
) {
  return {
    id: `${entry.kind}:${entry.id}:${entry.edge_kind}:${entry.direction}`,
    href:
      searchResultHref(
        {
          id: entry.id,
          type: entry.kind,
          concept_path: entry.path,
          source_ref: { kind: entry.kind, id: entry.id },
        },
        workspaceId,
      ) ?? undefined,
    title: entry.title,
    meta: entry.path ? displayConceptPath(entry.path) : humanizeEnum(entry.edge_kind),
  };
}

function workspaceGraphFocusId(graphNode: GraphNode) {
  if (
    graphNode.entity_kind === "context_page" ||
    graphNode.entity_kind === "library_item"
  ) {
    return graphNode.entity_id;
  }
  if (
    graphNode.entity_kind !== "library_artifact" &&
    graphNode.entity_kind !== "library_item_version"
  ) {
    return null;
  }
  // Local Graph artifacts and versions are version-grain nodes, whereas the
  // Workspace Knowledge Map has one canonical node per Library item. Its
  // public canonical path is the safe bridge between those projections.
  const itemMatch = /^\/library\/([^/?#]+)\.md$/u.exec(graphNode.path ?? "");
  if (!itemMatch) {
    return null;
  }
  try {
    const itemId = decodeURIComponent(itemMatch[1]);
    return itemId && !itemId.includes("/") ? itemId : null;
  } catch {
    return null;
  }
}

function resolveRequestedNodeId(
  nodes: TreeNodePayload[],
  _pages: ContextPageSummary[],
  requestedTargetId: string | null,
) {
  if (!requestedTargetId) {
    return null;
  }
  if (nodes.some((node) => node.id === requestedTargetId)) {
    return requestedTargetId;
  }
  // The target may be a page id past the flat-list cap; the tree is unbounded,
  // so resolve the owning node from the tree rather than the capped list.
  return nodeIdForPage(nodes, requestedTargetId);
}


function selectedPageIdForNode(
  nodes: TreeNodePayload[],
  _pages: ContextPageSummary[],
  nodeId: string | null,
) {
  // Resolve from the node alone; the flat list is just a cache, not the source
  // of truth. Detail is then fetched by id in the loader, so a target_id past
  // the list cap still loads instead of being a silent no-op.
  const node = nodes.find((candidate) => candidate.id === nodeId);
  return node?.target_kind === "context_page" && node.target_id
    ? node.target_id
    : null;
}

function selectableMoveTargets(
  nodes: TreeNodePayload[],
  movingNode: TreeNodePayload,
) {
  return nodes.filter(
    (node) =>
      node.can_have_children &&
      node.id !== movingNode.id &&
      node.section === movingNode.section &&
      !isTreeDescendant(nodes, node.id, movingNode.id),
  );
}

function collectSubtreeNodeIds(nodes: TreeNodePayload[], rootNodeId: string) {
  const nodeIds = new Set([rootNodeId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of nodes) {
      if (
        node.parent_id &&
        nodeIds.has(node.parent_id) &&
        !nodeIds.has(node.id)
      ) {
        nodeIds.add(node.id);
        changed = true;
      }
    }
  }
  return nodeIds;
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

function expandNodeIds(current: string[], nodeId: string | null) {
  if (!nodeId || current.includes(nodeId)) {
    return current;
  }
  return [...current, nodeId];
}
