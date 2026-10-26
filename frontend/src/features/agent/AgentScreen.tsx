import { inputTextError, textInputLimit } from "../../shared/lib/inputPolicy";
import type { TFunction } from "i18next";
import {
  Bot,
  Eye,
  EyeOff,
  FileText,
  Power,
  RefreshCw,
  RotateCcw,
  Settings2,
  ShieldAlert,
  X,
} from "lucide-react";
import type { FormEvent } from "react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Link,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  activateAgentPage,
  createAgentPageDraft,
  diffAgentPageVersions,
  listAgentChanges,
  listAgentCategories,
  listAgentPageBundles,
  listAgentPages,
  readAgentPage,
  restoreAgentPageVersion,
  updateAgentPageDraft,
} from "../../shared/api/agent";
import type {
  AgentChangeHistory,
  AgentCategoryEntry,
  AgentPageDraftPayload,
  AgentPageDraftResult,
  AgentPageDetail,
  AgentPageBundleSummary,
  AgentPageSummary,
  AgentVersionEntry,
} from "../../shared/api/agent";
import {
  createTreeFolder,
  deleteTreeNode,
  fetchTreeNodeIndexPreview,
  listTreeData,
  readTreeNode,
  moveTreeNode,
  reorderTreeNodeAt,
  updateTreeNode,
} from "../../shared/api/workspace";
import {
  FolderSummaryCard,
  nodeIdForPage,
} from "../../shared/components/FolderSummaryCard";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import type {
  TreeNodePayload,
  TreeSectionPolicy,
} from "../../shared/api/workspace";
import { isEditorRole } from "../../shared/workspace/permissions";
import { useOptionalAuth } from "../../shared/auth/AuthContext";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { AdvancedSection } from "../../shared/components/AdvancedSection";
import { AvatarWithTimestamp } from "../../shared/components/Avatar";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { ListPanelHeader } from "../../shared/components/ListPanelHeader";
import { Dialog } from "../../shared/components/Dialog";
import {
  HistoryActionButton,
  HistoryCard,
  HistoryChangesButton,
  HistoryChangesUnavailable,
  HistoryLineDelta,
} from "../../shared/components/HistoryCard";
import { HistoryDiffDialog } from "../../shared/components/HistoryDiffDialog";
import { HistoryGraphList } from "../../shared/components/HistoryGraphList";
import { useVersionPairDiff } from "../../shared/hooks/useVersionPairDiff";
import {
  buildHistoryGraph,
  type HistoryGraphRow,
} from "../../shared/lib/historyGraph";
import {
  changesPairFor,
  normalizeVersionPair,
  type VersionPair,
} from "../../shared/lib/historyVersionPair";
import { agentHistoryNodes } from "./agentHistory";
import { NewItemDialog } from "../../shared/components/NewItemDialog";
import type { NewItemType } from "../../shared/components/NewItemDialog";
import { ReaderTabs } from "../../shared/components/ReaderTabs";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import {
  DeleteTreeNodeDialog,
  MoveTreeNodeDialog,
  RenameTreeNodeDialog,
} from "../../shared/components/TreeNodeActionDialogs";
import { TreeRail } from "../../shared/components/TreeRail";
import { WorkspaceLayout } from "../../shared/components/WorkspaceLayout";
import { targetBreadcrumbs } from "../../shared/components/treeBreadcrumbs";
import {
  resolveExpandedNodeIds,
  TREE_EXPANDED_PARAM,
  TREE_VERSION_PARAM,
  updateTreeSearchParams,
  writeExpandedNodeIdsToStorage,
} from "../../shared/components/treeRouteState";
import { useAsyncLoad } from "../../shared/hooks/useAsyncLoad";
import { NotFoundScreen } from "../errors/NotFoundScreen";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { domainLabel, humanizeEnum } from "../../shared/lib/domainLabel";
import { normalizeSlugInput, slugify } from "../../shared/text/slug";
import { ArtifactChatsSection } from "../mcp-chats/ArtifactChatsSection";
import { MarkdownView } from "../pages/MarkdownView";
import {
  AGENT_TEMPLATES,
  BLANK_TEMPLATE_ID,
  findAgentTemplate,
} from "./agentTemplates";
import { useRouteWorkspaceId, useWorkspaceRoute, workspacePath, workspaceTargetHref, withWorkspaceQuery } from "../../shared/lib/workspaceRoutes";

const VISIBLE_CATEGORIES = ["agent_guide", "persona", "skill", "memory"];
const ADVANCED_CATEGORIES: string[] = [];
const SENSITIVE_CATEGORIES = new Set(["agent_guide", "memory"]);
const AGENT_PAGE_CATEGORIES = [...VISIBLE_CATEGORIES, ...ADVANCED_CATEGORIES];
const SENSITIVITY_OPTIONS: AgentPageDraftPayload["sensitivity"][] = [
  "workspace",
  "private",
  "public",
  "secret_candidate",
];

class AgentRouteTargetNotFoundError extends Error {}

async function readAgentPageForDisplay(
  workspaceId: string,
  agentPageId: string,
  version: string,
): Promise<AgentPageDetail> {
  const detail = await readAgentPage(workspaceId, agentPageId, version);
  if (
    version === "active" &&
    !detail.version_info &&
    detail.latest_draft_version_id
  ) {
    return readAgentPage(workspaceId, agentPageId, "draft");
  }
  return detail;
}

type AgentState = {
  workspaceId: string;
  pages: AgentPageSummary[];
  pagesCursor: string | null;
  pagesHasMore: boolean;
  categories: AgentCategoryEntry[];
  tree: TreeNodePayload[];
  treePolicies: Record<string, TreeSectionPolicy>;
  selectedNodeId: string | null;
  detail: AgentPageDetail | null;
  history: AgentChangeHistory | null;
  // set when a selected page's body is owner-only (sensitive category) and
  // the detail fetch was denied — the tile stays visible, the body does not.
  detailRestricted: boolean;
};

export function AgentScreen() {
  const { t } = useTranslation("agent");
  const routeWorkspaceId = useRouteWorkspaceId();
  const route = useWorkspaceRoute();
  // Viewer write-gate: the Agent read routes remain available to viewers, so
  // gate every write affordance here (including the typed edit route) so a viewer is never dropped into an editor that only
  // fails on save (mirrors ContextScreen). This hook MUST run before the early
  // `if (!state) return` below (Rules of Hooks). Backend RBAC still re-enforces.
  const auth = useOptionalAuth();
  const workspaceContext = useOptionalWorkspaceContext();
  const workspaceRole = workspaceContext?.selectedWorkspace?.role ?? null;
  const canEdit = isEditorRole(workspaceRole);
  const [expandedNodeIds, setExpandedNodeIds] = useState<string[]>([]);
  const [newItemDialog, setNewItemDialog] = useState<{
    initialItemType: NewItemType;
    parentNodeId: string | null;
  } | null>(null);
  const [renameNode, setRenameNode] = useState<TreeNodePayload | null>(null);
  const [moveNode, setMoveNode] = useState<TreeNodePayload | null>(null);
  const [deleteNode, setDeleteNode] = useState<TreeNodePayload | null>(null);
  const [treeRefreshing, setTreeRefreshing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const routeIdentity = `${routeWorkspaceId}:${location.pathname}`;
  const routeIdentityRef = useRef("");
  useLayoutEffect(() => {
    routeIdentityRef.current = routeIdentity;
    return () => {
      if (routeIdentityRef.current === routeIdentity) routeIdentityRef.current = "";
    };
  }, [routeIdentity]);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTargetId = route?.targetId ?? null;
  const selectedVersionId = searchParams.get(TREE_VERSION_PARAM);
  const previousSearchRef = useRef(location.search);
  const searchChangedRef = useRef(false);
  const suppressRouteSyncRef = useRef(false);
  // Reveal a deep-linked target once. After that, the user's expansion state
  // is authoritative, including an intentional collapse of the active path.
  const revealedRouteTargetRef = useRef<string | null>(null);
  const [routeNotFoundKey, setRouteNotFoundKey] = useState<string | null>(null);
  useEffect(() => {
    setRouteNotFoundKey(null);
  }, [routeWorkspaceId, route?.suffix, route?.targetId]);
  // After a detail load failure the route params and `state` are unchanged, so
  // re-clicking the same tree node would never re-run the route-sync effect.
  // Record the failed target and bump a retry token from selectPage instead.
  const failedDetailTargetRef = useRef<string | null>(null);
  const [detailRetryToken, setDetailRetryToken] = useState(0);
  const [loadingMoreAgentPages, setLoadingMoreAgentPages] = useState(false);
  const [activatingAgentPageId, setActivatingAgentPageId] = useState<
    string | null
  >(null);
  const {
    data: state,
    error,
    setData: setState,
    setError,
  } = useAsyncLoad<AgentState>(
    async () => {
      const workspaceId = routeWorkspaceId;
      const [categories, pagesResult, treeData] = await Promise.all([
        listAgentCategories(workspaceId),
        listAgentPages(workspaceId),
        listTreeData(workspaceId, "agent"),
      ]);
      const pages = pagesResult.data;
      const tree = treeData.nodes;
      if (route?.targetKind === "folder" && route.targetId) {
        let folder = tree.find((node) => node.id === route.targetId);
        if (!folder) {
          try {
            folder = await readTreeNode(workspaceId, route.targetId);
          } catch (exc) {
            if (exc instanceof ApiError && exc.status === 404) {
              throw new AgentRouteTargetNotFoundError();
            }
            throw exc;
          }
        }
        if (folder.section !== "agent" || folder.node_kind !== "folder") {
          throw new AgentRouteTargetNotFoundError();
        }
      }
      const routePageId = route?.targetKind === "page" ? route.targetId : null;
      const selectedNodeId = routePageId
        ? nodeIdForPage(tree, routePageId) ?? routePageId
        : resolveRequestedNodeId(tree, pages, requestedTargetId);
      const selectedPageId = routePageId ?? selectedPageIdForNode(tree, pages, selectedNodeId);
      let detail: AgentPageDetail | null = null;
      let history: AgentChangeHistory | null = null;
      let detailRestricted = false;
      if (selectedPageId) {
        // The page may be past the flat-list cap; readAgentPage defaults to
        // "active" and the version hint is only a refinement, so look up the
        // cached summary when present but do not gate the fetch on membership.
        const cached = pages.find((page) => page.id === selectedPageId);
        const version =
          cached && !cached.current_active_version_id ? "draft" : "active";
        [detail, history] = await Promise.all([
          // A sensitive AgentPage body (agent_guide/memory) is owner-only:
          // the fetch 403s for non-owners. Degrade to a restricted notice
          // instead of failing the whole screen.
          readAgentPageForDisplay(workspaceId, selectedPageId, version).catch(
            (exc) => {
              if (exc instanceof ApiError && exc.status === 403) {
                detailRestricted = true;
                return null;
              }
              throw exc;
            },
          ),
          listAgentChanges(workspaceId, selectedPageId),
        ]);
      }
      return {
        workspaceId,
        pages,
        pagesCursor: pagesResult.next_cursor,
        pagesHasMore: pagesResult.has_more,
        categories,
        tree,
        treePolicies: treeData.policies,
        selectedNodeId,
        detail,
        history,
        detailRestricted,
      };
    },
    [routeWorkspaceId],
    {
      errorMessage: "Failed to load Agent",
      formatError: (exc, fallback) => {
        if (exc instanceof AgentRouteTargetNotFoundError) {
          setRouteNotFoundKey(`${routeWorkspaceId}:${route?.suffix ?? "agent"}`);
          return "Agent target not found";
        }
        return apiErrorMessage(exc, fallback);
      },
      fallbackData: {
        workspaceId: "",
        pages: [],
        pagesCursor: null,
        pagesHasMore: false,
        categories: [],
        tree: [],
        treePolicies: {},
        selectedNodeId: null,
        detail: null,
        history: null,
        detailRestricted: false,
      },
      onSuccess: (nextState) => {
        revealedRouteTargetRef.current = nextState.selectedNodeId
          ? `${nextState.workspaceId}:${nextState.selectedNodeId}`
          : null;
        setExpandedNodeIds(
          resolveExpandedNodeIds(
            nextState.tree,
            searchParams.get(TREE_EXPANDED_PARAM),
            nextState.selectedNodeId ?? nextState.detail?.id ?? null,
            { defaultMode: "none", storageKey: "agent", workspaceId: routeWorkspaceId },
          ),
        );
      },
    },
  );

  // A8: append the next keyset page of agent pages to the overview. Guarded so a
  // reload (create/draft/restore) that replaced the list mid-flight discards the
  // stale page (mirrors the Ask/overview cursor guards).
  async function loadMoreAgentPages() {
    if (
      !state ||
      !state.pagesHasMore ||
      state.pagesCursor === null ||
      loadingMoreAgentPages
    ) {
      return;
    }
    const { workspaceId, pagesCursor } = state;
    setLoadingMoreAgentPages(true);
    setError(null);
    try {
      const next = await listAgentPages(workspaceId, { cursor: pagesCursor });
      setState((current) => {
        if (!current || current.pagesCursor !== pagesCursor) {
          return current;
        }
        return {
          ...current,
          pages: [...current.pages, ...next.data],
          pagesCursor: next.next_cursor,
          pagesHasMore: next.has_more,
        };
      });
    } catch (exc) {
      setError(apiErrorMessage(exc, t("errors.loadMore")));
    } finally {
      setLoadingMoreAgentPages(false);
    }
  }

  function selectPage(pageId: string) {
    if (!state) {
      return;
    }
    setError(null);
    if (failedDetailTargetRef.current) {
      failedDetailTargetRef.current = null;
      setDetailRetryToken((token) => token + 1);
    }
    navigate(withWorkspaceQuery(workspaceTargetHref(routeWorkspaceId, "agent", pageId, "page"), searchParams));
  }

  function selectNode(node: TreeNodePayload) {
    if (node.target_kind === "agent_page" && node.target_id) {
      selectPage(node.target_id);
      return;
    }
    navigate(withWorkspaceQuery(workspaceTargetHref(routeWorkspaceId, "agent", node.id, "folder"), searchParams));
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
    if (input.parentNodeId && !state.tree.some((node) => node.id === input.parentNodeId && node.section === "agent" && node.can_have_children)) {
      setError(t("errors.folderParentUnavailable", { defaultValue: "The selected folder is unavailable" }));
      return;
    }
    setError(null);
    const folder = await createTreeFolder(state.workspaceId, {
      parent_id: input.parentNodeId,
      section: "agent",
      slug: input.slug,
      title: input.title,
    });
    if (routeIdentityRef.current !== originRouteIdentity) return;
    const nextExpandedNodeIds = expandNodeIds(
      expandedNodeIds,
      folder.parent_id,
    );
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    navigate(withWorkspaceQuery(workspaceTargetHref(routeWorkspaceId, "agent", folder.id, "folder"), searchParams), { replace: true });
    setState((current) =>
      current
        ? {
            ...current,
            tree: [...current.tree, folder],
            selectedNodeId: folder.id,
            detail: null,
            history: null,
            detailRestricted: false,
          }
        : current,
    );
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
    const [treeData, pagesResult] = await Promise.all([
      listTreeData(state.workspaceId, "agent"),
      options.refreshOverview
        ? listAgentPages(state.workspaceId)
        : Promise.resolve(null),
    ]);
    const selectedNodeId = options.clearSelection
      ? null
      : (options.targetNodeId ?? state.selectedNodeId);
    const validNodeIds = new Set(treeData.nodes.map((node) => node.id));
    const nextExpandedNodeIds =
      options.expandedNodeIds ??
      expandedNodeIds.filter((nodeId) => validNodeIds.has(nodeId));
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    setState((current) =>
      current
        ? {
            ...current,
            pages: pagesResult ? pagesResult.data : current.pages,
            pagesCursor: pagesResult
              ? pagesResult.next_cursor
              : current.pagesCursor,
            pagesHasMore: pagesResult
              ? pagesResult.has_more
              : current.pagesHasMore,
            tree: treeData.nodes,
            treePolicies: treeData.policies,
            selectedNodeId,
            detail: options.clearSelection ? null : current.detail,
            history: options.clearSelection ? null : current.history,
            detailRestricted: options.clearSelection
              ? false
              : current.detailRestricted,
          }
        : current,
    );
    const selectedNode = treeData.nodes.find((node) => node.id === selectedNodeId);
    navigate(selectedNode
      ? withWorkspaceQuery(workspaceTargetHref(routeWorkspaceId, "agent", selectedNode.target_id ?? selectedNode.id, selectedNode.node_kind === "folder" ? "folder" : "page"), searchParams)
      : withWorkspaceQuery(workspacePath(routeWorkspaceId, "agent"), searchParams), { replace: true });
  }

  async function refreshTreeFromRail() {
    if (treeRefreshing) {
      return;
    }
    setTreeRefreshing(true);
    setError(null);
    try {
      await refreshTree();
    } catch (exc) {
      setError(apiErrorMessage(exc, t("errors.refreshTree")));
    } finally {
      setTreeRefreshing(false);
    }
  }

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

  async function saveNavSummary(
    node: TreeNodePayload,
    navSummary: string | null,
  ) {
    if (!state) {
      return;
    }
    setError(null);
    await updateTreeNode(state.workspaceId, node.id, {
      // The card renders the relabelled category node, so echo the stored title
      // back instead of the displayed one — a nav-summary save must not rename.
      display_name:
        state.tree.find((stored) => stored.id === node.id)?.title ?? node.title,
      nav_summary: navSummary,
    });
    await refreshTree({ targetNodeId: node.id });
  }

  const folderChildMeta = (child: TreeNodePayload) => {
    const listedPage = child.target_id
      ? state?.pages.find((candidate) => candidate.id === child.target_id)
      : null;
    const category = child.target_meta?.category ?? listedPage?.category;
    const injectionPolicy =
      child.target_meta?.injection_policy ?? listedPage?.injection_policy;
    return {
      subtitle: category
        ? `${enumLabel(t, "category", category)} - ${enumLabel(t, "injectionPolicy", injectionPolicy ?? "")}`
        : "",
      pillStatus:
        child.target_meta?.lifecycle_status ??
        listedPage?.lifecycle_status ??
        child.node_kind,
    };
  };

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
      reason,
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
        "Drag-and-drop move in Agent explorer",
      );
    } catch (exc) {
      setError(apiErrorMessage(exc, t("errors.moveTreeNode")));
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
      setError(apiErrorMessage(exc, t("errors.reorderTreeNode")));
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
    await deleteTreeNode(state.workspaceId, node.id);
    await refreshTree({
      clearSelection: state.selectedNodeId
        ? deletedNodeIds.has(state.selectedNodeId)
        : false,
      refreshOverview: true,
    });
  }

  function copyTreeNodeLink(node: TreeNodePayload) {
    const path = workspaceTargetHref(routeWorkspaceId, "agent", node.target_id ?? node.id, node.node_kind === "folder" ? "folder" : "page");
    const href =
      typeof window === "undefined" ? path : `${window.location.origin}${path}`;
    void navigator.clipboard?.writeText(href);
  }

  function setStoredExpandedNodeIds(nodeIds: string[]) {
    setExpandedNodeIds(nodeIds);
    writeExpandedNodeIdsToStorage("agent", nodeIds, routeWorkspaceId);
  }

  useEffect(() => {
    searchChangedRef.current = previousSearchRef.current !== location.search;
    previousSearchRef.current = location.search;
  }, [location.search]);

  useEffect(() => {
    if (route?.targetKind !== "folder" || !route.targetId || !state?.workspaceId) return;
    const existing = state.tree.find((node) => node.id === route.targetId);
    if (existing) {
      if (existing.section !== "agent" || existing.node_kind !== "folder") {
        setRouteNotFoundKey(`${routeWorkspaceId}:${route.suffix}`);
        setError("Agent folder not found");
      }
      return;
    }
    let cancelled = false;
    void readTreeNode(routeWorkspaceId, route.targetId).then((node) => {
      if (!cancelled && (node.section !== "agent" || node.node_kind !== "folder")) {
        setRouteNotFoundKey(`${routeWorkspaceId}:${route.suffix}`);
        setError("Agent folder not found");
      }
    }).catch((exc) => {
      if (!cancelled && exc instanceof ApiError && exc.status === 404) {
        setRouteNotFoundKey(`${routeWorkspaceId}:${route.suffix}`);
        setError("Agent folder not found");
      } else if (!cancelled) {
        setError(apiErrorMessage(exc, t("errors.folderParentUnavailable", { defaultValue: "The selected folder is unavailable" })));
      }
    });
    return () => { cancelled = true; };
  }, [route?.suffix, route?.targetId, route?.targetKind, routeWorkspaceId, setError, state?.tree, state?.workspaceId, t]);

  useEffect(() => {
    if (!state?.workspaceId) {
      return;
    }
    const routePageId = route?.targetKind === "page" ? route.targetId : null;
    const targetNodeId = routePageId
      ? nodeIdForPage(state.tree, routePageId) ?? routePageId
      : resolveRequestedNodeId(state.tree, state.pages, requestedTargetId);
    if (!targetNodeId) {
      revealedRouteTargetRef.current = null;
      if (suppressRouteSyncRef.current) {
        return;
      }
      if (
        (requestedTargetId || searchChangedRef.current) &&
        (state.selectedNodeId || state.detail || state.history)
      ) {
        setState((current) =>
          current
            ? {
                ...current,
                selectedNodeId: null,
                detail: null,
                history: null,
                detailRestricted: false,
              }
            : current,
        );
      }
      return;
    }
    suppressRouteSyncRef.current = false;

    const revealKey = `${state.workspaceId}:${targetNodeId}`;
    if (revealedRouteTargetRef.current !== revealKey) {
      revealedRouteTargetRef.current = revealKey;
      setExpandedNodeIds((current) => {
        const next = resolveExpandedNodeIds(
          state.tree,
          current.join(","),
          targetNodeId,
          { defaultMode: "none" },
        );
        if (
          next.length === current.length &&
          next.every((nodeId, index) => nodeId === current[index])
        ) {
          return current;
        }
        writeExpandedNodeIdsToStorage("agent", next, routeWorkspaceId);
        return next;
      });
    }

    const selectedPageId = routePageId ?? selectedPageIdForNode(state.tree, state.pages, targetNodeId);
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
                detailRestricted: false,
              }
            : current,
        );
      }
      return;
    }
    // The page may be past the flat-list cap; key the fetch off the node's
    // target_id, not flat-list membership, and use the cached summary only to
    // refine the version selector (defaulting to "active" when absent).
    const cached = state.pages.find((page) => page.id === selectedPageId);
    const defaultVersionId =
      cached?.current_active_version_id ??
      cached?.latest_draft_version_id ??
      null;
    const desiredVersionId = selectedVersionId ?? defaultVersionId;
    if (
      state.selectedNodeId === targetNodeId &&
      state.detail?.id === selectedPageId &&
      (!desiredVersionId ||
        state.detail.version_info?.version_id === desiredVersionId)
    ) {
      return;
    }

    const defaultVersion =
      cached && !cached.current_active_version_id ? "draft" : "active";
    let cancelled = false;
    let detailRestricted = false;
    setError(null);
    const historyPromise = listAgentChanges(state.workspaceId, selectedPageId);
    const detailPromise = historyPromise.then((history) => {
      const selectedVersion = selectedVersionId
        ? history.versions.find(
            (version) => version.version_id === selectedVersionId,
          )
        : null;
      if (selectedVersionId && !selectedVersion) {
        throw new Error(
          t("history.versionUnavailable", {
            defaultValue: "The selected Agent version is unavailable.",
          }),
        );
      }
      const selector = selectedVersion
        ? String(selectedVersion.version_no)
        : defaultVersion;
      return readAgentPageForDisplay(
        state.workspaceId,
        selectedPageId,
        selector,
      ).catch((exc) => {
        if (exc instanceof ApiError && exc.status === 403) {
          detailRestricted = true;
          return null;
        }
        throw exc;
      });
    });
    void Promise.all([detailPromise, historyPromise])
      .then(([detail, history]) => {
        if (cancelled) {
          return;
        }
        failedDetailTargetRef.current = null;
        setState((current) =>
          current
            ? {
                ...current,
                selectedNodeId: targetNodeId,
                detail,
                history,
                detailRestricted,
              }
            : current,
        );
      })
      .catch((exc) => {
        if (cancelled) {
          return;
        }
        if (exc instanceof ApiError && exc.status === 404) {
          failedDetailTargetRef.current = null;
          setState((current) =>
            current
              ? {
                  ...current,
                  pages: current.pages.filter(
                    (page) => page.id !== selectedPageId,
                  ),
                  tree: current.tree.filter(
                    (node) => node.target_id !== selectedPageId,
                  ),
                  selectedNodeId: null,
                  detail: null,
                  history: null,
                  detailRestricted: false,
                }
              : current,
          );
          setRouteNotFoundKey(`${routeWorkspaceId}:${route?.suffix ?? "agent"}`);
          return;
        }
        failedDetailTargetRef.current = targetNodeId;
        setError(
          apiErrorMessage(
            exc,
            t("errors.loadDetail", {
              defaultValue: "Failed to load Agent detail",
            }),
          ),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [
    detailRetryToken,
    location.pathname,
    navigate,
    requestedTargetId,
    route?.suffix,
    route?.targetId,
    route?.targetKind,
    routeWorkspaceId,
    searchParams,
    selectedVersionId,
    setError,
    setSearchParams,
    setState,
    state,
    t,
  ]);

  const editAgentPageId = canEdit && route?.edit ? route.targetId : null;
  const readerEditDetail =
    state?.detail?.id === editAgentPageId ? state.detail : null;
  // The reader loads the ACTIVE body when an active version exists, but the
  // edit submit bases the write on latest_draft_version_id. When a pending
  // draft exists, fetch it so the modal edits the same body it submits against
  // (otherwise the pending draft's changes are silently overwritten).
  const editNeedsDraftDetail = Boolean(
    readerEditDetail?.latest_draft_version_id &&
    readerEditDetail.version_info?.version_id !==
      readerEditDetail.latest_draft_version_id,
  );
  const [editDraftDetail, setEditDraftDetail] =
    useState<AgentPageDetail | null>(null);
  const [editDraftLoading, setEditDraftLoading] = useState(false);
  const [editDraftError, setEditDraftError] = useState<string | null>(null);
  const [editDraftRetryToken, setEditDraftRetryToken] = useState(0);
  useEffect(() => {
    if (!editNeedsDraftDetail || !editAgentPageId || !state?.workspaceId) {
      setEditDraftDetail(null);
      setEditDraftLoading(false);
      setEditDraftError(null);
      return;
    }
    let cancelled = false;
    setEditDraftDetail(null);
    setEditDraftLoading(true);
    setEditDraftError(null);
    readAgentPage(state.workspaceId, editAgentPageId, "draft")
      .then((draftDetail) => {
        if (!cancelled) {
          setEditDraftDetail(draftDetail);
        }
      })
      .catch((exc) => {
        if (cancelled) {
          return;
        }
        setEditDraftError(
          apiErrorMessage(
            exc,
            t("errors.loadDraft", {
              defaultValue: "Failed to load the pending Agent draft",
            }),
          ),
        );
      })
      .finally(() => {
        if (!cancelled) {
          setEditDraftLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    editAgentPageId,
    editDraftRetryToken,
    editNeedsDraftDetail,
    state?.workspaceId,
    t,
  ]);

  // Single relabelling seam for the category folders: the tree rail, the
  // breadcrumb and the folder card all read from here, so they agree with the
  // cards, table and dialogs instead of echoing the seeded Title Case name.
  const loadedTree = state?.tree;
  const treeNodes = useMemo(
    () => (loadedTree ? withCategoryLabels(loadedTree, t) : []),
    [loadedTree, t],
  );

  if (routeNotFoundKey === `${routeWorkspaceId}:${route?.suffix ?? "agent"}`) {
    return <NotFoundScreen />;
  }
  if (!state) {
    return (
      <RouteLoading label={t("loading", { defaultValue: "Loading Agent" })} />
    );
  }

  const categoryMap = new Map(
    state.categories.map((category) => [category.category, category]),
  );
  const routePageId = route?.targetKind === "page" ? route.targetId : null;
  const routeFolderNodeId = route?.targetKind === "folder" ? route.targetId : null;
  const routeNodeId = routePageId
    ? nodeIdForPage(state.tree, routePageId) ?? routePageId
    : routeFolderNodeId;
  const selectedNode = routeNodeId
    ? (treeNodes.find((node) => node.id === routeNodeId) ?? null)
    : null;
  const visiblePages = state.pages.filter((page) =>
    VISIBLE_CATEGORIES.includes(page.category),
  );
  const advancedPages = state.pages.filter((page) =>
    ADVANCED_CATEGORIES.includes(page.category),
  );
  const agentPolicy = state.treePolicies.agent;
  const folderCreateRoute = Boolean(route?.create && route.suffix === "agent/folders/new");
  const routeFolderParentId = folderCreateRoute ? searchParams.get("parent") : null;
  const routeFolderParent = routeFolderParentId
    ? treeNodes.find((node) => node.id === routeFolderParentId && node.section === "agent" && node.can_have_children)
    : null;
  const activeNewItemDialog = newItemDialog ?? (folderCreateRoute && (!routeFolderParentId || routeFolderParent)
    ? { initialItemType: "folder" as const, parentNodeId: routeFolderParentId }
    : null);
  const itemParent = activeNewItemDialog?.parentNodeId
    ? (treeNodes.find((node) => node.id === activeNewItemDialog.parentNodeId) ?? null)
    : null;
  // F-5: folder destinations the new-folder dialog can target.
  const agentFolderNodes = treeNodes.filter((node) => node.can_have_children);
  const newItemParentNodeId = selectedNode?.can_have_children
    ? selectedNode.id
    : (selectedNode?.parent_id ?? null);
  const moveTargetNodes = moveNode
    ? selectableMoveTargets(treeNodes, moveNode)
    : [];
  const resolvedRoutePageId = routePageId ?? selectedPageIdForNode(state.tree, state.pages, routeNodeId);
  const visibleDetailRestricted = state.detailRestricted &&
    !routeFolderNodeId && state.selectedNodeId === routeNodeId;
  const visibleDetail =
    state.selectedNodeId === routeNodeId &&
    state.detail?.id === resolvedRoutePageId &&
    (!selectedVersionId ||
      state.detail.version_info?.version_id === selectedVersionId)
      ? state.detail
      : null;
  const detailRouteLoading = Boolean(
    resolvedRoutePageId && !visibleDetail && !state.detailRestricted && !error,
  );
  const creationCategory = normalizeAgentCategory(searchParams.get("category"));
  const creationParentNodeId = searchParams.get("parent");
  const creationParentValid = !creationParentNodeId || treeNodes.some((node) => node.id === creationParentNodeId && node.section === "agent" && node.can_have_children);
  const creationOpen = Boolean(route?.create && route.suffix === "agent/pages/new" && creationParentValid);
  const editDetail = editNeedsDraftDetail
    ? editDraftDetail?.id === editAgentPageId
      ? editDraftDetail
      : null
    : readerEditDetail;

  function selectVersion(versionId: string | null) {
    setSearchParams(updateTreeSearchParams(searchParams, { versionId }), {
      replace: false,
    });
  }

  function openCreateAgentPage(category: string, parentNodeId?: string | null) {
    const query = new URLSearchParams({ category });
    if (parentNodeId) query.set("parent", parentNodeId);
    navigate(`${workspacePath(routeWorkspaceId, "agent/pages/new")}?${query.toString()}`);
  }

  function openCreateFolder(parentNodeId?: string | null) {
    const query = parentNodeId ? `?parent=${encodeURIComponent(parentNodeId)}` : "";
    navigate(`${workspacePath(routeWorkspaceId, "agent/folders/new")}${query}`);
  }

  function closeCreateFolder(reason: "cancel" | "success") {
    if (reason === "success") {
      setNewItemDialog(null);
    } else if (folderCreateRoute) {
      navigate(routeFolderParentId
        ? workspaceTargetHref(routeWorkspaceId, "agent", routeFolderParentId, "folder")
        : workspacePath(routeWorkspaceId, "agent"), { replace: true });
    } else setNewItemDialog(null);
  }

  function closeCreateAgentPage() {
    navigate(workspacePath(routeWorkspaceId, "agent"), { replace: true });
  }

  function closeEditAgentPage() {
    if (editAgentPageId) navigate(workspaceTargetHref(routeWorkspaceId, "agent", editAgentPageId, "page"), { replace: true });
    else navigate(workspacePath(routeWorkspaceId, "agent"), { replace: true });
  }

  async function createAgentPage(input: AgentPageDraftPayload) {
    if (!state) {
      return;
    }
    const originRouteIdentity = routeIdentity;
    setError(null);
    setNotice(null);
    const result = await createAgentPageDraft(state.workspaceId, input);
    const [pagesResult, treeData, detail, history] = await Promise.all([
      listAgentPages(state.workspaceId),
      listTreeData(state.workspaceId, "agent"),
      readAgentPage(state.workspaceId, result.agent_page_id, "draft"),
      listAgentChanges(state.workspaceId, result.agent_page_id),
    ]);
    if (routeIdentityRef.current !== originRouteIdentity) return;
    const pages = pagesResult.data;
    const targetNodeId =
      nodeIdForPage(treeData.nodes, result.agent_page_id) ??
      result.agent_page_id;
    const nextExpandedNodeIds = resolveExpandedNodeIds(
      treeData.nodes,
      searchParams.get(TREE_EXPANDED_PARAM),
      result.agent_page_id,
      { defaultMode: "active-path", storageKey: "agent", workspaceId: routeWorkspaceId },
    );
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    suppressRouteSyncRef.current = true;
    navigate(workspaceTargetHref(routeWorkspaceId, "agent", result.agent_page_id, "page"), { replace: true });
    setState((current) =>
      current
        ? {
            ...current,
            pages,
            pagesCursor: pagesResult.next_cursor,
            pagesHasMore: pagesResult.has_more,
            tree: treeData.nodes,
            treePolicies: treeData.policies,
            selectedNodeId: targetNodeId,
            detail,
            history,
          }
        : current,
    );
    setNotice(agentDraftNotice(result, t));
  }

  async function updateAgentPage(input: AgentPageDraftPayload) {
    if (!state || !editDetail) {
      return;
    }
    const originRouteIdentity = routeIdentity;
    setError(null);
    setNotice(null);
    const result = await updateAgentPageDraft(
      state.workspaceId,
      editDetail.id,
      input,
    );
    const [pagesResult, treeData, detail, history] = await Promise.all([
      listAgentPages(state.workspaceId),
      listTreeData(state.workspaceId, "agent"),
      readAgentPage(state.workspaceId, result.agent_page_id, "draft"),
      listAgentChanges(state.workspaceId, result.agent_page_id),
    ]);
    if (routeIdentityRef.current !== originRouteIdentity) return;
    const pages = pagesResult.data;
    const targetNodeId =
      nodeIdForPage(treeData.nodes, result.agent_page_id) ??
      result.agent_page_id;
    const nextExpandedNodeIds = resolveExpandedNodeIds(
      treeData.nodes,
      searchParams.get(TREE_EXPANDED_PARAM),
      result.agent_page_id,
      { defaultMode: "active-path", storageKey: "agent", workspaceId: routeWorkspaceId },
    );
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    suppressRouteSyncRef.current = true;
    navigate(`${workspaceTargetHref(routeWorkspaceId, "agent", result.agent_page_id, "page")}?version=${encodeURIComponent(result.draft_version_id)}`, { replace: true });
    setState((current) =>
      current
        ? {
            ...current,
            pages,
            pagesCursor: pagesResult.next_cursor,
            pagesHasMore: pagesResult.has_more,
            tree: treeData.nodes,
            treePolicies: treeData.policies,
            selectedNodeId: targetNodeId,
            detail,
            history,
          }
        : current,
    );
    setNotice(agentDraftNotice(result, t));
  }

  async function reloadAgentAfterRestore(
    agentPageId: string,
    draftVersionId: string,
  ) {
    if (!state) {
      return;
    }
    setError(null);
    const [pagesResult, treeData, detail, history] = await Promise.all([
      listAgentPages(state.workspaceId),
      listTreeData(state.workspaceId, "agent"),
      readAgentPage(state.workspaceId, agentPageId, "draft"),
      listAgentChanges(state.workspaceId, agentPageId),
    ]);
    const pages = pagesResult.data;
    const targetNodeId =
      nodeIdForPage(treeData.nodes, agentPageId) ?? agentPageId;
    const nextExpandedNodeIds = resolveExpandedNodeIds(
      treeData.nodes,
      searchParams.get(TREE_EXPANDED_PARAM),
      agentPageId,
      { defaultMode: "none", storageKey: "agent", workspaceId: routeWorkspaceId },
    );
    setStoredExpandedNodeIds(nextExpandedNodeIds);
    setState((current) =>
      current
        ? {
            ...current,
            pages,
            pagesCursor: pagesResult.next_cursor,
            pagesHasMore: pagesResult.has_more,
            tree: treeData.nodes,
            treePolicies: treeData.policies,
            selectedNodeId: targetNodeId,
            detail,
            history,
          }
        : current,
    );
    setSearchParams(
      updateTreeSearchParams(searchParams, { versionId: draftVersionId }),
      { replace: false },
    );
  }

  async function activateDraft(detail: AgentPageDetail) {
    if (!state || !detail.latest_draft_version_id || activatingAgentPageId) {
      return;
    }
    const confirmed = window.confirm(
      t("reader.activateConfirm", {
        title: detail.title,
        defaultValue: "Activate the pending draft for {{title}}?",
      }),
    );
    if (!confirmed) {
      return;
    }
    setError(null);
    setNotice(null);
    setActivatingAgentPageId(detail.id);
    try {
      const activeDetail = await activateAgentPage(
        state.workspaceId,
        detail.id,
        detail.latest_draft_version_id,
        t("reader.activateReason", {
          defaultValue: "Activate Agent draft from the Agent reader",
        }),
      );
      const [pagesResult, treeData, history] = await Promise.all([
        listAgentPages(state.workspaceId),
        listTreeData(state.workspaceId, "agent"),
        listAgentChanges(state.workspaceId, detail.id),
      ]);
      const targetNodeId =
        nodeIdForPage(treeData.nodes, detail.id) ??
        state.selectedNodeId ??
        detail.id;
      setState((current) =>
        current
          ? {
              ...current,
              pages: pagesResult.data,
              pagesCursor: pagesResult.next_cursor,
              pagesHasMore: pagesResult.has_more,
              tree: treeData.nodes,
              treePolicies: treeData.policies,
              selectedNodeId: targetNodeId,
              detail: activeDetail,
              history,
            }
          : current,
      );
      navigate(workspaceTargetHref(routeWorkspaceId, "agent", detail.id, "page"), { replace: true });
      setNotice(
        t("notice.activated", {
          title: activeDetail.title,
          defaultValue: "{{title}} is now active.",
        }),
      );
    } catch (exc) {
      setError(
        apiErrorMessage(
          exc,
          t("errors.activate", {
            defaultValue: "Failed to activate Agent draft",
          }),
        ),
      );
    } finally {
      setActivatingAgentPageId(null);
    }
  }

  return (
    <>
      <WorkspaceLayout
        defaultInspectorOpen={false}
        defaultLeftOpen
        inspectorLabel={t("layout.details", { defaultValue: "Details" })}
        layoutKey="agent"
        leftLabel={t("layout.explorer", { defaultValue: "Explorer" })}
        mobileAction={
          canEdit && agentPolicy?.allow_user_folder_create === true
            ? {
                ariaLabel: t("layout.createFolder", {
                  defaultValue: "Create Agent folder",
                }),
                label: t("layout.newFolder", { defaultValue: "New folder" }),
                onClick: () => {
                  openCreateFolder(newItemParentNodeId);
                },
                title: t("layout.createFolder", {
                  defaultValue: "Create Agent folder",
                }),
              }
            : undefined
        }
        left={({
          expandWidthLabel,
          expandWidthAtMax,
          onClose,
          onExpandWidth,
        }) => (
          <TreeRail
            favoriteScope={auth?.user && state.workspaceId ? { actorId: auth.user.user_id, workspaceId: state.workspaceId } : undefined}
            title={t("title", { defaultValue: "Agent" })}
            accent="agent"
            nodes={treeNodes}
            activeTargetId={routePageId ?? (!route?.targetKind ? state.detail?.id : null)}
            activeNodeId={routeNodeId}
            expandedNodeIds={expandedNodeIds}
            forceActivePathExpanded={false}
            emptyLabel={t("empty.noTreeNodes", {
              defaultValue: "No Agent tree nodes",
            })}
            hideSystemRootNodes
            expandWidthLabel={expandWidthLabel}
            expandWidthAtMax={expandWidthAtMax}
            canCreateFolder={
              canEdit && agentPolicy?.allow_user_folder_create === true
            }
            canCreatePage={canEdit}
            onCreateItem={
              canEdit
                ? ({ itemType, parentNodeId }) => {
                    // F-11: an Agent page opens the dedicated "New Agent page"
                    // editor directly; the rail dialog is folder-only.
                    if (itemType === "page" && state) {
                      const parentNode = parentNodeId
                        ? (state.tree.find(
                            (node) => node.id === parentNodeId,
                          ) ?? null)
                        : null;
                      openCreateAgentPage(
                        categoryForTreeParent(parentNode, state.pages),
                        parentNodeId,
                      );
                      return;
                    }
                    openCreateFolder(parentNodeId);
                  }
                : undefined
            }
            onCopyNodeLink={copyTreeNodeLink}
            onDeleteNode={canEdit ? setDeleteNode : undefined}
            onDropNode={canEdit ? moveTreeNodeByDrop : undefined}
            onClose={onClose}
            onExpandWidth={onExpandWidth}
            onExpandedNodeIdsChange={(nodeIds) => {
              setStoredExpandedNodeIds(nodeIds);
            }}
            onMoveNode={canEdit ? setMoveNode : undefined}
            onReorderNodeAt={canEdit ? reorderTreeNodeToAnchor : undefined}
            onRefresh={refreshTreeFromRail}
            refreshing={treeRefreshing}
            onRenameNode={canEdit ? setRenameNode : undefined}
            onSelectTarget={(node) => {
              selectNode(node);
            }}
          />
        )}
        main={
          <section className="feature-main feature-main--reader agent-main">
            <Breadcrumbs
              items={targetBreadcrumbs({
                workspaceId: routeWorkspaceId,
                basePath: workspacePath(routeWorkspaceId, "agent"),
                rootLabel: t("title", { defaultValue: "Agent" }),
                nodes: treeNodes,
                targetId: visibleDetail?.id,
                targetNodeId: routeNodeId,
                fallbackLabel: visibleDetail?.title,
              })}
            />
            {route?.create && route.suffix === "agent/pages/new" && !creationParentValid ? (
              <StatusBanner tone="warning">{t("errors.folderParentUnavailable", { defaultValue: "The selected folder is unavailable" })}</StatusBanner>
            ) : null}
            {folderCreateRoute && routeFolderParentId && !routeFolderParent ? (
              <StatusBanner tone="warning">{t("errors.folderParentUnavailable", { defaultValue: "The selected folder is unavailable" })}</StatusBanner>
            ) : null}
            {!visibleDetail &&
            !detailRouteLoading &&
            !state.detailRestricted ? (
              <div className="feature-header">
                <div>
                  <p className="eyebrow">
                    {t("header.eyebrow", { defaultValue: "Behavior layer" })}
                  </p>
                  <h1>{t("header.title", { defaultValue: "Agent pages" })}</h1>
                  <p className="feature-subtitle">
                    {t("header.description", {
                      defaultValue:
                        "Agent pages store instructions, skills, and memory for Ask and MCP clients. MCP clients must read an Agent page or build a bundle; pages are not injected into prompts automatically.",
                    })}
                  </p>
                </div>
                <div className="feature-header-actions">
                  {state.pages.length > 0 ? (
                    <CountChip
                      value={state.pages.length}
                      label={t("header.configuredUnit", {
                        count: state.pages.length,
                        defaultValue: "configured",
                      })}
                      compact
                    />
                  ) : null}
                  <Link
                    className="icon-button text-button"
                    to={`${workspacePath(routeWorkspaceId, "settings")}?section=agent-injection`}
                  >
                    <Settings2 size={16} aria-hidden="true" />
                    <span>
                      {t("header.injectionSettings", {
                        defaultValue: "Injection settings",
                      })}
                    </span>
                  </Link>
                </div>
              </div>
            ) : null}
            {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
            {notice ? <StatusBanner tone="info">{notice}</StatusBanner> : null}
            {detailRouteLoading ? (
              <RouteLoading
                label={t("reader.loadingDetail", {
                  defaultValue: "Loading Agent detail",
                })}
              />
            ) : visibleDetail ? (
              <AgentReader
                activating={activatingAgentPageId === visibleDetail.id}
                canActivate={
                  Boolean(visibleDetail.latest_draft_version_id) &&
                  (workspaceRole === "owner" ||
                    (workspaceRole === "admin" &&
                      !SENSITIVE_CATEGORIES.has(visibleDetail.category)))
                }
                canEdit={canEdit}
                detail={visibleDetail}
                history={state.history}
                onActivate={() => activateDraft(visibleDetail)}
                onVersionRestored={reloadAgentAfterRestore}
                onSelectVersion={selectVersion}
                selectedVersionId={selectedVersionId}
                workspaceId={state.workspaceId}
              />
            ) : visibleDetailRestricted ? (
              <section className="feature-section detail-section">
                <div className="empty-state">
                  <ShieldAlert size={18} aria-hidden="true" />
                  <p>
                    {t("reader.sensitiveOwnerOnly", {
                      defaultValue:
                        "This is a sensitive Agent page. Only workspace " +
                        "owners can view its contents.",
                    })}
                  </p>
                </div>
              </section>
            ) : selectedNode ? (
              <FolderSummaryCard
                canEdit={canEdit}
                childMeta={folderChildMeta}
                emptyLabel={t("folder.emptyCategory", {
                  defaultValue: "This Agent category is empty",
                })}
                eyebrow={selectedNode.node_kind.replace(/_/g, " ")}
                fallbackSummary={
                  (selectedNode.slug
                    ? categoryMap.get(selectedNode.slug)?.description
                    : null) ??
                  selectedNode.nav_summary_auto ??
                  null
                }
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
                nodes={treeNodes}
                onSaveNavSummary={(navSummary) =>
                  saveNavSummary(selectedNode, navSummary)
                }
                onSelectNode={selectNode}
                renderMarkdown={(markdown) => (
                  <MarkdownView body={markdown} wikilinkMode="plain" />
                )}
              />
            ) : (
              <>
                {state.pages.length > 0 ? <SelectAgentPageHint /> : null}
                <CategoryGrid
                  title={t("categories.visible", { defaultValue: "Visible" })}
                  categories={VISIBLE_CATEGORIES}
                  categoryMap={categoryMap}
                  onCreate={canEdit ? openCreateAgentPage : undefined}
                  pages={visiblePages}
                />
                {ADVANCED_CATEGORIES.length > 0 ? (
                  <details
                    className="advanced-section"
                    open={advancedPages.length > 0}
                  >
                    <summary>
                      {t("categories.advancedCategories", {
                        defaultValue: "Advanced categories",
                      })}
                    </summary>
                    <CategoryGrid
                      title={t("categories.advanced", {
                        defaultValue: "Advanced",
                      })}
                      categories={ADVANCED_CATEGORIES}
                      categoryMap={categoryMap}
                      onCreate={canEdit ? openCreateAgentPage : undefined}
                      pages={advancedPages}
                      compact
                    />
                  </details>
                ) : null}
                {state.pages.length > 0 ? (
                  // M-20 (260908 UI audit): the CategoryGrid header/cards above
                  // already say "0 pages" in five places once this list is
                  // empty -- a sixth "No Agent pages yet" line added nothing.
                  <AgentTable
                    activeId={null}
                    onSelect={selectPage}
                    pages={state.pages}
                  />
                ) : null}
                {state.pagesHasMore ? (
                  <div className="load-more-row">
                    <button
                      className="icon-button text-button"
                      disabled={loadingMoreAgentPages}
                      type="button"
                      onClick={() => void loadMoreAgentPages()}
                    >
                      {loadingMoreAgentPages ? t("loadingMore") : t("loadMore")}
                    </button>
                  </div>
                ) : null}
              </>
            )}
          </section>
        }
        inspector={
          visibleDetail ? (
            <AgentDetail detail={visibleDetail} history={state.history} />
          ) : undefined
        }
      />
      {canEdit && activeNewItemDialog ? (
        <NewItemDialog
          canCreateFolder={agentPolicy?.allow_user_folder_create === true}
          canCreatePage={false}
          initialItemType="folder"
          initialParentId={activeNewItemDialog.parentNodeId ?? ""}
          parentLabel={
            itemParent?.title ?? t("title", { defaultValue: "Agent" })
          }
          parentNodes={agentFolderNodes}
          rootLabel={t("title", { defaultValue: "Agent" })}
          onClose={closeCreateFolder}
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
      {creationOpen ? (
        <AgentCreationModal
          key="create"
          categories={state.categories}
          defaultCategory={creationCategory}
          mode="create"
          parentNodeId={creationParentNodeId}
          onClose={closeCreateAgentPage}
          onSubmit={createAgentPage}
        />
      ) : null}
      {editDetail ? (
        <AgentCreationModal
          key={editDetail.id}
          categories={state.categories}
          defaultCategory={editDetail.category}
          initialPage={editDetail}
          mode="edit"
          parentNodeId={editDetail.parent_node_id}
          onClose={closeEditAgentPage}
          onSubmit={updateAgentPage}
        />
      ) : null}
      {canEdit && editNeedsDraftDetail && !editDetail ? (
        <Dialog
          className="agent-editor-dialog agent-editor-loading-dialog"
          labelledBy="agent-edit-loading-title"
          onClose={closeEditAgentPage}
        >
          <div className="dialog-form">
            <header className="dialog-header">
              <div>
                <p className="eyebrow">
                  {t("dialog.existingPage", {
                    defaultValue: "Existing Agent page",
                  })}
                </p>
                <h2 id="agent-edit-loading-title">
                  {t("dialog.loadingDraftTitle", {
                    defaultValue: "Loading pending draft",
                  })}
                </h2>
              </div>
              <button
                aria-label={t("dialog.closeAria", {
                  defaultValue: "Close agent dialog",
                })}
                className="icon-button"
                onClick={closeEditAgentPage}
                type="button"
              >
                <X size={16} aria-hidden="true" />
              </button>
            </header>
            {editDraftError ? (
              <>
                <StatusBanner tone="warning">{editDraftError}</StatusBanner>
                <div className="dialog-actions">
                  <button
                    className="icon-button text-button"
                    onClick={() => setEditDraftRetryToken((value) => value + 1)}
                    type="button"
                  >
                    <RefreshCw size={16} aria-hidden="true" />
                    <span>{t("dialog.retry", { defaultValue: "Retry" })}</span>
                  </button>
                </div>
              </>
            ) : (
              <p aria-busy={editDraftLoading} aria-live="polite">
                {t("dialog.loadingDraft", {
                  defaultValue: "Loading the latest pending draft…",
                })}
              </p>
            )}
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function CategoryGrid({
  title,
  categories,
  categoryMap,
  onCreate,
  pages,
  compact = false,
}: {
  title: string;
  categories: string[];
  categoryMap: Map<string, AgentCategoryEntry>;
  // Undefined for viewers: the card renders as a static count tile (no create
  // affordance) rather than a button that would only fail on save.
  onCreate?: (category: string) => void;
  pages: AgentPageSummary[];
  compact?: boolean;
}) {
  const { t } = useTranslation("agent");
  const counts = useMemo(() => countByCategory(pages), [pages]);
  return (
    <section className={`feature-section ${compact ? "compact" : ""}`}>
      <ListPanelHeader
        title={title}
        count={pages.length}
        unit={t(pages.length === 1 ? "units.item_one" : "units.item_other", {
          defaultValue: pages.length === 1 ? "item" : "items",
        })}
      />
      <div className="metric-grid">
        {categories.map((category) => {
          const body = (
            <>
              <div className="metric-card-title">
                <Bot size={16} aria-hidden="true" />
                <span>{enumLabel(t, "category", category)}</span>
              </div>
              <strong>
                {t("categories.pageCount", {
                  count: counts.get(category) ?? 0,
                  defaultValue: "{{count}} pages",
                })}
              </strong>
              <p
                title={t(
                  `categories.injectionPolicyHint.${categoryMap.get(category)?.default_injection_policy}`,
                  { defaultValue: "" },
                )}
              >
                {enumLabel(
                  t,
                  "injectionPolicy",
                  categoryMap.get(category)?.default_injection_policy,
                )}
              </p>
              {SENSITIVE_CATEGORIES.has(category) ? (
                <span
                  className="sensitive-note"
                  title={t("categories.sensitiveHint", {
                    defaultValue:
                      "Sensitive category: only workspace owners can activate a draft here.",
                  })}
                >
                  <ShieldAlert size={14} aria-hidden="true" />
                  {t("categories.sensitive", { defaultValue: "Sensitive" })}
                </span>
              ) : null}
              {/* UI-05: explicit create affordance, separated from the count so
                  the number reads as "existing pages" rather than the action. */}
              {onCreate ? (
                <span className="metric-card-cta">
                  {t("categories.newAction", { defaultValue: "+ New" })}
                </span>
              ) : null}
            </>
          );
          return onCreate ? (
            <button
              aria-label={t("categories.createPage", {
                category: enumLabel(t, "category", category),
                defaultValue: "Create {{category}} Agent page",
              })}
              className="metric-card metric-card-button"
              key={category}
              onClick={() => onCreate(category)}
              type="button"
            >
              {body}
            </button>
          ) : (
            <div className="metric-card" key={category}>
              {body}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function AgentCreationModal({
  categories,
  defaultCategory,
  initialPage,
  mode = "create",
  parentNodeId,
  onClose,
  onSubmit,
}: {
  categories: AgentCategoryEntry[];
  defaultCategory: string;
  initialPage?: AgentPageDetail | null;
  mode?: "create" | "edit";
  parentNodeId?: string | null;
  onClose: () => void;
  onSubmit: (input: AgentPageDraftPayload) => Promise<void>;
}) {
  const { t } = useTranslation("agent");
  const isEdit = mode === "edit";
  const [category, setCategory] = useState(
    initialPage?.category ?? defaultCategory,
  );
  const [title, setTitle] = useState(initialPage?.title ?? "");
  const [customSlug, setCustomSlug] = useState(initialPage?.slug ?? "");
  const [showAdvanced, setShowAdvanced] = useState(false);
  // F-10: a NEW agent page whose category has a bundled template defaults to it
  // (the "default system prompt" / agent_guide category ships the Karpathy
  // guidelines), instead of the minimal heading. Edits keep the page's own body.
  const defaultNewTemplate = !isEdit
    ? AGENT_TEMPLATES.find((template) =>
        template.categories.includes(defaultCategory),
      )
    : undefined;
  const defaultNewBody = () =>
    initialPage?.body_markdown ??
    defaultNewTemplate?.body ??
    defaultAgentBody(defaultCategory, "");
  const [body, setBody] = useState(defaultNewBody);
  const [templateId, setTemplateId] = useState(
    defaultNewTemplate ? defaultNewTemplate.id : BLANK_TEMPLATE_ID,
  );
  // The value the form last auto-filled into `body` (a category heading or a
  // template). The body is "pristine" — safe to auto-replace — while it still
  // equals this; once the user types, it diverges and we stop touching it.
  const [appliedDefault, setAppliedDefault] = useState(defaultNewBody);
  const [reason, setReason] = useState(
    isEdit
      ? t("dialog.defaultUpdateReasonWithTitle", {
          title:
            initialPage?.title ??
            t("dialog.agentPage", { defaultValue: "Agent page" }),
          defaultValue: "Update {{title}} draft",
        })
      : t("dialog.defaultCreateReason", {
          defaultValue: "Create Agent page draft",
        }),
  );
  const [sensitivity, setSensitivity] = useState<
    AgentPageDraftPayload["sensitivity"]
  >(
    (initialPage?.sensitivity as
      AgentPageDraftPayload["sensitivity"] | undefined) ?? "workspace",
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const categoryOptions =
    categories.length > 0
      ? categories.map((entry) => entry.category)
      : AGENT_PAGE_CATEGORIES;
  const derivedSlug = slugify(title);
  const slug = isEdit
    ? normalizeSlugInput(initialPage?.slug ?? customSlug)
    : showAdvanced
      ? normalizeSlugInput(customSlug)
      : derivedSlug;
  const invalidTitle = title.trim().length === 0 || Boolean(inputTextError(title, 256));
  const invalidSlug = slug.length === 0;

  // The body is auto-replaceable only while the user hasn't edited it (still the
  // last heading/template we filled in). A picked template or hand-typed body
  // diverges from appliedDefault and is never clobbered by category/title edits.
  const bodyIsPristine = !body.trim() || body === appliedDefault;
  // Only templates mapped to the selected category are offered; the picker hides
  // entirely for categories with none.
  const availableTemplates = AGENT_TEMPLATES.filter((template) =>
    template.categories.includes(category),
  );
  const selectedTemplate = findAgentTemplate(templateId);

  function applyTemplate(nextTemplateId: string) {
    setTemplateId(nextTemplateId);
    const template = findAgentTemplate(nextTemplateId);
    const nextBody = template
      ? template.body
      : defaultAgentBody(category, title);
    setBody(nextBody);
    setAppliedDefault(nextBody);
  }

  function changeCategory(nextCategory: string) {
    setCategory(nextCategory);
    const templateStillOffered =
      templateId === BLANK_TEMPLATE_ID ||
      AGENT_TEMPLATES.some(
        (template) =>
          template.id === templateId &&
          template.categories.includes(nextCategory),
      );
    if (templateId === BLANK_TEMPLATE_ID && bodyIsPristine) {
      const nextBody = defaultAgentBody(nextCategory, title);
      setBody(nextBody);
      setAppliedDefault(nextBody);
    } else if (!templateStillOffered) {
      // The picked template isn't offered under the new category: drop the
      // picker to Blank but keep the body the user already has, and rebase the
      // pristine baseline so a later title edit won't wipe that kept content.
      setTemplateId(BLANK_TEMPLATE_ID);
      setAppliedDefault(defaultAgentBody(nextCategory, title));
    }
  }

  function changeTitle(nextTitle: string) {
    setTitle(nextTitle);
    if (templateId === BLANK_TEMPLATE_ID && bodyIsPristine) {
      const nextBody = defaultAgentBody(category, nextTitle);
      setBody(nextBody);
      setAppliedDefault(nextBody);
    }
    const previousDefaultReason = isEdit
      ? t("dialog.defaultUpdateReasonWithTitle", {
          title: title || t("dialog.agentPage", { defaultValue: "Agent page" }),
          defaultValue: "Update {{title}} draft",
        })
      : t("dialog.defaultCreateReasonWithTitle", {
          title,
          defaultValue: "Create {{title}} draft",
        });
    const emptyDefaultReason = isEdit
      ? t("dialog.defaultUpdateReason", {
          defaultValue: "Update Agent page draft",
        })
      : t("dialog.defaultCreateReason", {
          defaultValue: "Create Agent page draft",
        });
    if (reason === emptyDefaultReason || reason === previousDefaultReason) {
      const nextReason = isEdit
        ? t("dialog.defaultUpdateReasonWithTitle", {
            title: nextTitle.trim(),
            defaultValue: "Update {{title}} draft",
          })
        : t("dialog.defaultCreateReasonWithTitle", {
            title: nextTitle.trim(),
            defaultValue: "Create {{title}} draft",
          });
      setReason(nextTitle.trim() ? nextReason : emptyDefaultReason);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const bodyError = inputTextError(body, 1_000_000, false);
    if (bodyError) {
      setError(bodyError);
      return;
    }
    if (invalidTitle || invalidSlug || submitting) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({
        base_version_id: isEdit
          ? // Prefer the pending draft as the base when one exists: the backend
            // stale-base check expects the latest draft and only falls back to
            // the active version when there is no draft. The reader loads the
            // active detail (?version=active), so version_info.version_id is the
            // active id — submitting it while a draft is pending was rejected as
            // stale (C20).
            (initialPage?.latest_draft_version_id ??
            initialPage?.version_info?.version_id ??
            initialPage?.current_active_version_id ??
            null)
          : undefined,
        body_markdown: body,
        category,
        frontmatter: initialPage?.frontmatter ?? {},
        parent_node_id: parentNodeId ?? null,
        reason:
          reason.trim() ||
          (isEdit
            ? t("dialog.defaultUpdateReasonWithTitle", {
                title: title.trim(),
                defaultValue: "Update {{title}} draft",
              })
            : t("dialog.defaultCreateReasonWithTitle", {
                title: title.trim(),
                defaultValue: "Create {{title}} draft",
              })),
        sensitivity,
        slug,
        title: title.trim(),
      });
    } catch (exc) {
      setError(
        apiErrorMessage(
          exc,
          isEdit
            ? t("dialog.failedUpdate", {
                defaultValue: "Failed to update Agent page",
              })
            : t("dialog.failedCreate", {
                defaultValue: "Failed to create Agent page",
              }),
        ),
      );
      setSubmitting(false);
    }
  }

  function requestClose() {
    if (!submitting) {
      onClose();
    }
  }

  return (
    <Dialog
      className="agent-editor-dialog"
      dismissible={!submitting}
      labelledBy="agent-create-title"
      onClose={requestClose}
    >
      <form className="dialog-form agent-create-form" onSubmit={submit}>
        <header className="dialog-header">
          <div>
            <p className="eyebrow">
              {isEdit
                ? t("dialog.existingPage", {
                    defaultValue: "Existing Agent page",
                  })
                : t("dialog.newPage", { defaultValue: "New Agent page" })}
            </p>
            <h2 id="agent-create-title">
              {isEdit
                ? t("dialog.editTitle", { defaultValue: "Edit Agent draft" })
                : t("dialog.createTitle", {
                    defaultValue: "Create Agent draft",
                  })}
            </h2>
          </div>
          <button
            aria-label={t("dialog.closeAria", {
              defaultValue: "Close agent dialog",
            })}
            className="icon-button"
            disabled={submitting}
            onClick={requestClose}
            type="button"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </header>
        {isEdit ? (
          <div className="form-field">
            <span className="form-field-label">
              {t("dialog.metadata", { defaultValue: "Metadata" })}
            </span>
            <dl className="detail-list inline agent-edit-metadata">
              <div>
                <dt>{t("dialog.category", { defaultValue: "Category" })}</dt>
                <dd>{enumLabel(t, "category", category)}</dd>
              </div>
              <div>
                <dt>{t("dialog.title", { defaultValue: "Title" })}</dt>
                <dd>{title}</dd>
              </div>
              <div>
                <dt>{t("dialog.slug", { defaultValue: "Slug" })}</dt>
                <dd>{slug}</dd>
              </div>
              <div>
                <dt>
                  {t("dialog.sensitivity", { defaultValue: "Sensitivity" })}
                </dt>
                <dd>{enumLabel(t, "sensitivity", sensitivity)}</dd>
              </div>
            </dl>
          </div>
        ) : (
          <>
            <label className="form-field">
              <span>{t("dialog.category", { defaultValue: "Category" })}</span>
              <select
                aria-label={t("dialog.category", { defaultValue: "Category" })}
                onChange={(event) => changeCategory(event.currentTarget.value)}
                value={category}
              >
                {categoryOptions.map((option) => (
                  <option key={option} value={option}>
                    {enumLabel(t, "category", option)}
                  </option>
                ))}
              </select>
            </label>
            {availableTemplates.length > 0 ? (
              <label className="form-field">
                <span>
                  {t("templates.label", { defaultValue: "Template" })}
                </span>
                <select
                  aria-label={t("templates.label", {
                    defaultValue: "Template",
                  })}
                  onChange={(event) => applyTemplate(event.currentTarget.value)}
                  value={templateId}
                >
                  <option value={BLANK_TEMPLATE_ID}>
                    {t("templates.blank", { defaultValue: "Blank" })}
                  </option>
                  {availableTemplates.map((template) => (
                    <option key={template.id} value={template.id}>
                      {t(template.nameKey, { defaultValue: template.id })}
                    </option>
                  ))}
                </select>
                {selectedTemplate ? (
                  <small className="field-hint">
                    {t(selectedTemplate.descriptionKey, { defaultValue: "" })}
                  </small>
                ) : null}
              </label>
            ) : null}
            <label className="form-field">
              <span>{t("dialog.title", { defaultValue: "Title" })}</span>
              <input
                aria-label={t("dialog.title", { defaultValue: "Title" })}
                autoFocus
                {...textInputLimit(256)}
                onChange={(event) => changeTitle(event.currentTarget.value)}
                placeholder={t("dialog.defaultPersona", {
                  defaultValue: "Default persona",
                })}
                value={title}
              />
              <small className="field-hint">
                {invalidTitle
                  ? t("dialog.titleHintInvalid", {
                      defaultValue: "Enter 1-256 characters.",
                    })
                  : t("dialog.slugPreview", {
                      slug,
                      defaultValue: "Slug preview: {{slug}}",
                    })}
              </small>
            </label>
            <AdvancedSection
              label={t("dialog.advancedSlug", {
                defaultValue: "Advanced slug",
              })}
              open={showAdvanced}
              onOpenChange={setShowAdvanced}
            >
              <label className="form-field">
                <span>{t("dialog.slug", { defaultValue: "Slug" })}</span>
                <input
                  aria-label={t("dialog.slug", { defaultValue: "Slug" })}
                  maxLength={64}
                  onChange={(event) => setCustomSlug(event.currentTarget.value)}
                  placeholder={derivedSlug}
                  value={customSlug}
                />
              </label>
            </AdvancedSection>
          </>
        )}
        <label className="form-field">
          <span>{t("dialog.body", { defaultValue: "Body" })}</span>
          <textarea
            aria-label={t("dialog.body", { defaultValue: "Body" })}
            autoFocus={isEdit}
            onChange={(event) => setBody(event.currentTarget.value)}
            rows={7}
            value={body}
          />
        </label>
        {!isEdit ? (
          <label className="form-field">
            <span>
              {t("dialog.sensitivity", { defaultValue: "Sensitivity" })}
            </span>
            <select
              aria-label={t("dialog.sensitivity", {
                defaultValue: "Sensitivity",
              })}
              onChange={(event) =>
                setSensitivity(
                  event.currentTarget
                    .value as AgentPageDraftPayload["sensitivity"],
                )
              }
              value={sensitivity}
            >
              {SENSITIVITY_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {enumLabel(t, "sensitivity", option)}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label className="form-field">
          <span>{t("dialog.reason", { defaultValue: "Reason" })}</span>
          <input
            aria-label={t("dialog.reason", { defaultValue: "Reason" })}
            {...textInputLimit(2000, false)}
            onChange={(event) => setReason(event.currentTarget.value)}
            value={reason}
          />
        </label>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="dialog-actions">
          <button
            className="icon-button"
            disabled={submitting}
            onClick={requestClose}
            type="button"
          >
            {t("dialog.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={invalidTitle || invalidSlug || submitting}
            type="submit"
          >
            {submitButtonLabel({ isEdit, submitting, t })}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function AgentTable({
  pages,
  activeId,
  onSelect,
}: {
  pages: AgentPageSummary[];
  activeId: string | null;
  onSelect: (pageId: string) => void;
}) {
  const { t } = useTranslation("agent");
  return (
    <section className="feature-section">
      <ListPanelHeader
        title={t("panel.recentHeading", { defaultValue: "Recent updates" })}
        count={pages.length}
        unit={t(pages.length === 1 ? "units.item_one" : "units.item_other", {
          defaultValue: pages.length === 1 ? "item" : "items",
        })}
      />
      <div className="data-table">
        {/* Without this row the four values after the title are unlabelled:
            "persona", "Draft", "Default on", "Workspace" with nothing saying
            which column is which. */}
        <div className="data-row data-row-head">
          <div>{t("table.page", { defaultValue: "Page" })}</div>
          <span>{t("table.category", { defaultValue: "Category" })}</span>
          <span>{t("table.status", { defaultValue: "Status" })}</span>
          <span>{t("table.injection", { defaultValue: "Injection" })}</span>
          <span>{t("table.sensitivity", { defaultValue: "Sensitivity" })}</span>
        </div>
        {pages.map((page) => (
          <button
            aria-pressed={page.id === activeId}
            className={`data-row selectable-row ${
              page.id === activeId ? "active" : ""
            }`}
            key={page.id}
            type="button"
            onClick={() => onSelect(page.id)}
          >
            <div>
              <strong>{page.title}</strong>
              <p>{page.slug}</p>
            </div>
            {/* The row is one button, so a column header cannot label these
                cells the way a table's would — each value states its own field
                for anyone who cannot see the header. */}
            <span
              aria-label={`${t("table.category", { defaultValue: "Category" })}: ${enumLabel(t, "category", page.category)}`}
            >
              {enumLabel(t, "category", page.category)}
            </span>
            <StatusPill
              status={page.lifecycle_status}
              title={`${t("table.status", { defaultValue: "Status" })}: ${page.lifecycle_status}`}
              compact
            />
            <span
              aria-label={`${t("table.injection", { defaultValue: "Injection" })}: ${enumLabel(t, "injectionPolicy", page.injection_policy)}`}
              title={t(`categories.injectionPolicyHint.${page.injection_policy}`, {
                defaultValue: "",
              })}
            >
              {enumLabel(t, "injectionPolicy", page.injection_policy)}
            </span>
            <span
              aria-label={`${t("table.sensitivity", { defaultValue: "Sensitivity" })}: ${enumLabel(t, "sensitivity", page.sensitivity)}`}
            >
              {enumLabel(t, "sensitivity", page.sensitivity)}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

function submitButtonLabel({
  isEdit,
  submitting,
  t,
}: {
  isEdit: boolean;
  submitting: boolean;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  if (submitting) {
    return isEdit
      ? t("dialog.updating", { defaultValue: "Updating…" })
      : t("dialog.creating", { defaultValue: "Creating…" });
  }
  return isEdit
    ? t("dialog.updateDraft", { defaultValue: "Update draft" })
    : t("dialog.createDraft", { defaultValue: "Create draft" });
}

function AgentReader({
  activating,
  canActivate,
  canEdit,
  detail,
  history,
  onActivate,
  onVersionRestored,
  onSelectVersion,
  selectedVersionId,
  workspaceId,
}: {
  activating: boolean;
  canActivate: boolean;
  canEdit: boolean;
  detail: AgentPageDetail;
  history: AgentChangeHistory | null;
  onActivate: () => void;
  onVersionRestored: (
    agentPageId: string,
    draftVersionId: string,
  ) => Promise<void>;
  onSelectVersion: (versionId: string | null) => void;
  selectedVersionId: string | null;
  workspaceId: string;
}) {
  const { t } = useTranslation("agent");
  const [tab, setTab] = useState<"view" | "history" | "bundle" | "activity">(
    "view",
  );
  const [bundles, setBundles] = useState<AgentPageBundleSummary[] | null>(null);
  const [bundleError, setBundleError] = useState<string | null>(null);
  useEffect(() => {
    setTab("view");
    setBundles(null);
    setBundleError(null);
  }, [detail.id]);
  useEffect(() => {
    if (tab !== "bundle" || bundles !== null || bundleError !== null) {
      return;
    }
    let cancelled = false;
    void listAgentPageBundles(workspaceId, detail.id)
      .then((items) => {
        if (!cancelled) setBundles(items);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setBundleError(
            apiErrorMessage(
              cause,
              t("reader.bundleLoadFailed", {
                defaultValue: "Could not load bundles",
              }),
            ),
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [bundleError, bundles, detail.id, tab, t, workspaceId]);
  const displayedVersion = detail.version_info;
  const pendingVersion = detail.latest_draft_version_id
    ? (history?.versions.find(
        (version) => version.version_id === detail.latest_draft_version_id,
      ) ?? null)
    : null;
  const hasPendingDraft = Boolean(
    detail.latest_draft_version_id &&
    detail.latest_draft_version_id !== detail.current_active_version_id,
  );
  return (
    <section className="feature-section detail-section agent-reader">
      <div className="detail-heading">
        <div>
          <h2>{detail.title}</h2>
        </div>
        <div className="detail-heading-actions">
          {canActivate ? (
            <button
              aria-busy={activating}
              // Publishing a draft to every MCP client is the consequential
              // action in this row; the neighbours are an editor link and a
              // settings link. Give it the primary weight so the row reads.
              className="icon-button primary"
              disabled={activating}
              onClick={onActivate}
              type="button"
            >
              <Power size={16} aria-hidden="true" />
              <span>
                {activating
                  ? t("reader.activating", { defaultValue: "Activating…" })
                  : t("reader.activateDraft", {
                      defaultValue: "Activate draft",
                    })}
              </span>
            </button>
          ) : null}
          {canEdit ? (
            <Link
              className="icon-button text-button"
              to={`${workspaceTargetHref(workspaceId, "agent", detail.id, "page")}/edit`}
            >
              <FileText size={16} aria-hidden="true" />
              <span>
                {hasPendingDraft
                  ? t("reader.editPendingDraft", {
                      defaultValue: "Edit pending draft",
                    })
                  : t("reader.createDraft", { defaultValue: "Create draft" })}
              </span>
            </Link>
          ) : null}
          <Link
            className="icon-button text-button"
            to={`${workspacePath(workspaceId, "settings")}?section=agent-injection`}
          >
            <Settings2 size={16} aria-hidden="true" />
            <span>
              {t("reader.injectionSettings", {
                defaultValue: "Injection settings",
              })}
            </span>
          </Link>
        </div>
      </div>
      {displayedVersion ? (
        <div
          className="agent-reader-version-state"
          aria-label={t("reader.versionState", {
            defaultValue: "Agent version state",
          })}
        >
          <StatusPill
            compact
            status={displayedVersion.version_status}
            label={
              displayedVersion.version_status === "draft"
                ? t("reader.viewingDraft", {
                    version: displayedVersion.version_no,
                    defaultValue: "Viewing draft v{{version}}",
                  })
                : t("reader.viewingActive", {
                    version: displayedVersion.version_no,
                    defaultValue: "Viewing active v{{version}}",
                  })
            }
          />
          {hasPendingDraft ? (
            <StatusPill
              compact
              status="draft"
              label={t("reader.pendingDraft", {
                version: pendingVersion?.version_no ?? "?",
                defaultValue: "Pending draft v{{version}}",
              })}
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
      ) : null}
      <dl className="detail-list inline">
        <div>
          <dt>{t("reader.injection", { defaultValue: "Injection" })}</dt>
          <dd
            title={t(`categories.injectionPolicyHint.${detail.injection_policy}`, {
              defaultValue: "",
            })}
          >
            {enumLabel(t, "injectionPolicy", detail.injection_policy)}
          </dd>
        </div>
        <div>
          <dt>{t("reader.priority", { defaultValue: "Priority" })}</dt>
          <dd>{detail.priority}</dd>
        </div>
        <div>
          <dt>{t("reader.sensitivity", { defaultValue: "Sensitivity" })}</dt>
          <dd>{enumLabel(t, "sensitivity", detail.sensitivity)}</dd>
        </div>
      </dl>
      <ReaderTabs
        accent="agent"
        active={tab}
        ariaLabel={t("reader.ariaLabel", {
          defaultValue: "Agent reader views",
        })}
        idBase="agent-reader"
        onChange={setTab}
        tabs={[
          {
            key: "view",
            label: t("reader.tabs.view", { defaultValue: "View" }),
          },
          {
            key: "history",
            label: t("reader.tabs.history", { defaultValue: "History" }),
            count: history?.versions.length ?? 0,
          },
          {
            key: "bundle",
            label: t("reader.tabs.bundle", { defaultValue: "Bundle" }),
            count: bundles?.length,
          },
          {
            key: "activity",
            label: t("reader.tabs.activity", { defaultValue: "Activity" }),
          },
        ]}
      />
      {tab === "view" ? (
        <div
          id="agent-reader-panel-view"
          role="tabpanel"
          aria-labelledby="agent-reader-tab-view"
          tabIndex={0}
        >
          {detail.body_markdown ? (
            <MarkdownView
              body={detail.body_markdown}
              stripLeadingHeading={detail.title}
            />
          ) : (
            <div className="empty-state">
              {t("reader.noBody", { defaultValue: "No body" })}
            </div>
          )}
        </div>
      ) : null}
      {tab === "history" ? (
        <div
          id="agent-reader-panel-history"
          role="tabpanel"
          aria-labelledby="agent-reader-tab-history"
          tabIndex={0}
        >
          <AgentHistory
            canEdit={canEdit}
            detail={detail}
            history={history}
            onVersionRestored={onVersionRestored}
            onSelectVersion={onSelectVersion}
            selectedVersionId={selectedVersionId}
            workspaceId={workspaceId}
          />
        </div>
      ) : null}
      {tab === "bundle" ? (
        <div
          id="agent-reader-panel-bundle"
          role="tabpanel"
          aria-labelledby="agent-reader-tab-bundle"
          tabIndex={0}
        >
          {bundleError ? (
            <StatusBanner tone="warning">{bundleError}</StatusBanner>
          ) : bundles === null ? (
            <div className="empty-state">
              {t("reader.bundleLoading", {
                defaultValue: "Loading bundles…",
              })}
            </div>
          ) : bundles.length === 0 ? (
            <div className="empty-state">
              {t("reader.bundleEmpty", {
                defaultValue: "No bundles reference this Agent page yet.",
              })}
            </div>
          ) : (
            <div className="history-list">
              {bundles.map((bundle) => (
                <div className="history-row" key={bundle.bundle_id}>
                  <div>
                    <strong>
                      {t("reader.bundleName", {
                        id: bundle.bundle_id,
                        defaultValue: "Bundle {{id}}",
                      })}
                    </strong>
                    <p>
                      {bundle.included
                        ? t("reader.bundleIncluded", {
                            defaultValue: "Page included",
                          })
                        : t("reader.bundleOmitted", {
                            defaultValue: "Page omitted",
                          })}
                      {" · "}
                      {t("reader.bundleCreated", {
                        date: formatVersionTimestamp(bundle.created_at),
                        defaultValue: "Created {{date}}",
                      })}
                    </p>
                  </div>
                  <StatusPill compact status={bundle.status} />
                  <time
                    className="meta-line"
                    dateTime={bundle.valid_until}
                    title={formatVersionTimestamp(bundle.valid_until, "long")}
                  >
                    {t("reader.bundleExpires", {
                      date: formatVersionTimestamp(bundle.valid_until),
                      defaultValue: "Expires {{date}}",
                    })}
                  </time>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}
      {tab === "activity" ? (
        <div
          className="wiki-empty"
          id="agent-reader-panel-activity"
          role="tabpanel"
          aria-labelledby="agent-reader-tab-activity"
          tabIndex={0}
        >
          {t("reader.noActivity", {
            defaultValue:
              "No per-page activity stream yet. MCP chats that touched this page are listed under Linked chats below, and the workspace feed lives on the Activity page.",
          })}
        </div>
      ) : null}
    </section>
  );
}

// Agent's own diffs always have two real ends: unlike Context there is no
// empty baseline to compare a first version against.
type AgentVersionPair = { from: string; to: string };

function AgentHistory({
  canEdit,
  detail,
  history,
  onVersionRestored,
  onSelectVersion,
  selectedVersionId,
  workspaceId,
}: {
  canEdit: boolean;
  detail: AgentPageDetail;
  history: AgentChangeHistory | null;
  onVersionRestored: (
    agentPageId: string,
    draftVersionId: string,
  ) => Promise<void>;
  onSelectVersion: (versionId: string | null) => void;
  selectedVersionId: string | null;
  workspaceId: string;
}) {
  const { t } = useTranslation("agent");
  const { t: tShell } = useTranslation("shell");
  const [pair, setPair] = useState<AgentVersionPair | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [restoreMessage, setRestoreMessage] = useState<string | null>(null);
  const [restoreVersionId, setRestoreVersionId] = useState<string | null>(null);
  // One restore at a time for the whole tab, whichever version or pair is on
  // screen: every Restore, the dialog's pickers and its Close wait on this.
  const restoring = restoreVersionId !== null;
  const versions = useMemo(() => history?.versions ?? [], [history?.versions]);
  const versionNodes = useMemo(() => agentHistoryNodes(versions), [versions]);
  const historyGraph = useMemo(
    () =>
      buildHistoryGraph(versionNodes, {
        headCandidateIds: [
          detail.latest_draft_version_id,
          detail.current_active_version_id,
        ],
        complete: true,
      }),
    [
      detail.current_active_version_id,
      detail.latest_draft_version_id,
      versionNodes,
    ],
  );
  const loadDiff = useCallback(
    (next: AgentVersionPair, signal: AbortSignal) =>
      diffAgentPageVersions(workspaceId, detail.id, next.from, next.to, {
        signal,
      }),
    [detail.id, workspaceId],
  );
  const {
    diff,
    error: diffError,
    loadingVersionId,
  } = useVersionPairDiff(
    pair,
    loadDiff,
    tShell("history.failedChanges", {
      defaultValue: "Failed to load changes",
    }),
  );
  const pairedVersion =
    versions.find((version) => version.version_id === pair?.to) ?? null;
  // What "Compare two versions" opens first: the newest version's own
  // Changes, or the two newest when that parent is not visible.
  const newestRow = historyGraph.rows[0];
  const defaultPair = newestRow
    ? (changesPairFor(newestRow) ??
      (historyGraph.rows[1]
        ? { from: historyGraph.rows[1].node.id, to: newestRow.node.id }
        : null))
    : null;
  useEffect(() => {
    setPair(null);
    setRestoreError(null);
    setRestoreMessage(null);
    setRestoreVersionId(null);
  }, [detail.id]);
  useEffect(() => {
    setRestoreError(null);
  }, [pair]);

  function openPair(candidate: VersionPair) {
    if (restoring) {
      return;
    }
    const normalized = normalizeVersionPair(versionNodes, candidate);
    if (normalized?.from) {
      setPair({ from: normalized.from, to: normalized.to });
    }
  }

  async function restoreVersion(version: AgentVersionEntry) {
    // Restoring the version you are already on just stacks an identical one.
    if (version.is_current || restoring) {
      return;
    }
    const ok = window.confirm(
      t("history.restoreConfirm", {
        version: version.version_no,
        defaultValue:
          "Restore Agent v{{version}} as a new draft? The active version stays unchanged.",
      }),
    );
    if (!ok) {
      return;
    }
    setRestoreError(null);
    setRestoreMessage(null);
    setRestoreVersionId(version.version_id);
    try {
      const restored = await restoreAgentPageVersion(
        workspaceId,
        detail.id,
        version.version_id,
      );
      setRestoreMessage(
        t("history.restored", {
          version: version.version_no,
          draft: restored.draft_version_id.slice(0, 8),
          defaultValue: "Agent v{{version}} was restored as draft v{{draft}}.",
        }),
      );
      setPair(null);
      await onVersionRestored(
        restored.agent_page_id,
        restored.draft_version_id,
      );
    } catch (exc) {
      setRestoreError(
        apiErrorMessage(
          exc,
          t("history.failedRestore", {
            defaultValue: "Failed to restore Agent version",
          }),
        ),
      );
    } finally {
      setRestoreVersionId(null);
    }
  }

  function renderCard(row: HistoryGraphRow<AgentVersionEntry>) {
    const version = row.entry;
    const selected = selectedVersionId === version.version_id;
    const open = pair?.to === version.version_id;
    const changes = changesPairFor(row);
    return (
      <HistoryCard
        actions={
          <>
            <HistoryActionButton
              accessibleLabel={
                selected
                  ? t("history.clearSelection", {
                      defaultValue: "Clear selection",
                    })
                  : t("history.selectVersion", {
                      defaultValue: "Select version",
                    })
              }
              icon={selected ? EyeOff : Eye}
              label={
                selected
                  ? t("history.clearShort", { defaultValue: "Clear" })
                  : t("history.selectShort", { defaultValue: "Select" })
              }
              onClick={() =>
                onSelectVersion(selected ? null : version.version_id)
              }
            />
            {/* Restoring the version already being read would only stack an
                identical one, so the current row never offers it. */}
            {canEdit && !open && !version.is_current ? (
              <HistoryActionButton
                accessibleLabel={t("history.restore", {
                  defaultValue: "Restore this version",
                })}
                busy={restoreVersionId === version.version_id}
                disabled={restoring}
                icon={RotateCcw}
                label={
                  restoreVersionId === version.version_id
                    ? t("history.restoring", { defaultValue: "Restoring" })
                    : tShell("history.restoreShort", { defaultValue: "Restore" })
                }
                onClick={() => void restoreVersion(version)}
              />
            ) : null}
            {changes ? (
              <HistoryChangesButton
                loading={loadingVersionId === version.version_id}
                onToggle={() => (open ? setPair(null) : openPair(changes))}
                open={open}
                row={row}
              />
            ) : null}
          </>
        }
        author={
          <AvatarWithTimestamp
            actorType={version.actor_type}
            name={version.actor_display_name}
            timestamp={version.created_at}
          />
        }
        badges={
          <HistoryLineDelta
            added={version.lines_added}
            removed={version.lines_removed}
          />
        }
        description={describeAgentVersion(version, t)}
        notices={
          row.relation === "hidden" || row.relation === "missing" ? (
            <HistoryChangesUnavailable />
          ) : null
        }
        row={row}
        status={<StatusPill status={version.version_status} compact />}
      />
    );
  }

  if (versions.length === 0) {
    return <div className="wiki-empty">{t("history.noVersionHistory")}</div>;
  }
  return (
    <>
      <div>
        <div className="history-compare-toolbar">
          <button
            aria-disabled={defaultPair === null}
            className="text-button"
            disabled={defaultPair === null}
            onClick={() => {
              if (defaultPair) {
                openPair(defaultPair);
              }
            }}
            type="button"
          >
            {tShell("history.compareTwo", {
              defaultValue: "Compare two versions",
            })}
          </button>
        </div>
        {restoreMessage ? (
          <StatusBanner tone="info">{restoreMessage}</StatusBanner>
        ) : null}
        {restoreError ? (
          <StatusBanner tone="warning">{restoreError}</StatusBanner>
        ) : null}
        <HistoryGraphList
          accent="agent"
          graph={historyGraph}
          renderCard={renderCard}
          rowView={(row) => {
            const selected = selectedVersionId === row.node.id;
            return {
              ariaCurrent: selected ? "true" : undefined,
              className: selected ? "is-selected" : undefined,
              tone: versionTone(row.entry.version_status),
            };
          }}
        />
      </div>
      {pairedVersion && pair ? (
        <HistoryDiffDialog
          actionError={restoreError}
          diff={diff}
          error={diffError}
          loading={loadingVersionId === pair.to}
          onClose={() => setPair(null)}
          onPairChange={openPair}
          onRestore={
            canEdit && !pairedVersion.is_current
              ? () => void restoreVersion(pairedVersion)
              : undefined
          }
          pair={pair}
          restoreDisabled={restoring}
          versions={versionNodes}
        />
      ) : null}
    </>
  );
}

function SelectAgentPageHint() {
  const { t } = useTranslation("agent");
  return (
    <p className="select-target-hint">
      {t("empty.selectPrompt", {
        defaultValue:
          "Select an Agent page from the tree or table. Category folders organize pages and do not have reader content.",
      })}
    </p>
  );
}

function AgentDetail({
  detail,
  history,
}: {
  detail: AgentPageDetail | null;
  history: AgentChangeHistory | null;
}) {
  const { t } = useTranslation("agent");
  const routeWorkspaceId = useRouteWorkspaceId();
  if (!detail) {
    return (
      <div className="inspector-empty">
        <h2>{t("detail.title", { defaultValue: "Agent detail" })}</h2>
        <p>
          {t("detail.empty", {
            defaultValue:
              "Select an Agent page to inspect policy, versions, and MCP bundle metadata.",
          })}
        </p>
      </div>
    );
  }
  const activeVersion = versionNumberFor(
    detail.current_active_version_id,
    history?.versions ?? [],
    detail.version_info?.version_no ?? null,
  );
  return (
    <div className="inspector-stack">
      <section className="inspector-section">
        <header>
          <span>{t("detail.policy", { defaultValue: "POLICY" })}</span>
        </header>
        <div className="policy-anchor">
          <span
            className={`sensitivity-badge sensitivity-${detail.sensitivity}`}
          >
            {enumLabel(t, "sensitivity", detail.sensitivity)}
          </span>
          <div>
            <strong
              title={t(
                `categories.injectionPolicyHint.${detail.injection_policy}`,
                { defaultValue: "" },
              )}
            >
              {enumLabel(t, "injectionPolicy", detail.injection_policy)}
            </strong>
            <p>
              {t("detail.priorityLine", {
                priority: detail.priority,
                category: enumLabel(t, "category", detail.category),
                defaultValue: "priority {{priority}} - {{category}}",
              })}
            </p>
          </div>
        </div>
      </section>
      <section className="inspector-section">
        <header>
          <span>{t("detail.versions", { defaultValue: "VERSIONS" })}</span>
          <span>{history?.versions.length ?? 0}</span>
        </header>
        <dl>
          <div className="version-row">
            <dt>{t("detail.active", { defaultValue: "Active" })}</dt>
            <dd>
              {activeVersion
                ? `v${activeVersion}`
                : t("detail.none", { defaultValue: "none" })}
            </dd>
          </div>
          <div className="version-row">
            <dt>{t("detail.latestDraft", { defaultValue: "Latest draft" })}</dt>
            <dd>
              {detail.latest_draft_version_id
                ? t("detail.draft", { defaultValue: "draft" })
                : t("detail.none", { defaultValue: "none" })}
            </dd>
          </div>
          <div className="version-row">
            <dt>{t("detail.status", { defaultValue: "Status" })}</dt>
            <dd>{humanizeEnum(detail.lifecycle_status)}</dd>
          </div>
        </dl>
      </section>
      <section className="inspector-section">
        <header>
          <span>{t("detail.actions", { defaultValue: "ACTIONS" })}</span>
        </header>
        <div className="inspector-actions">
          <Link
            className="icon-button text-button"
            to={`${workspacePath(routeWorkspaceId, "settings")}?section=agent-injection`}
          >
            {t("detail.openPolicySettings", {
              defaultValue: "Open policy settings",
            })}
          </Link>
        </div>
      </section>
      <ArtifactChatsSection resourceId={detail.id} resourceKind="agent_page" />
    </div>
  );
}

function versionTone(status: string) {
  if (status === "active" || status === "active_snapshot") return "active";
  if (status === "published" || status === "published_snapshot")
    return "published";
  if (status === "alternative") return "alternative";
  if (status === "draft") return "draft";
  return "superseded";
}

function describeAgentVersion(
  version: AgentVersionEntry,
  t: (key: string, options?: Record<string, unknown>) => string,
) {
  if (
    version.version_status === "active" ||
    version.version_status === "active_snapshot"
  ) {
    return t("history.describe.active", {
      defaultValue: "Active agent version",
    });
  }
  if (version.version_status === "draft") {
    return t("history.describe.draft", { defaultValue: "Draft - not active" });
  }
  return t("history.describe.status", {
    status: humanizeEnum(version.version_status),
    defaultValue: "{{status}} version",
  });
}

function normalizeAgentCategory(value: string | null) {
  return value && AGENT_PAGE_CATEGORIES.includes(value) ? value : "persona";
}

function defaultAgentBody(category: string, title: string) {
  const heading = title.trim() || humanizeEnum(category);
  return `# ${heading}\n\n`;
}

function agentDraftNotice(
  result: AgentPageDraftResult,
  t: (key: string, options?: Record<string, unknown>) => string,
) {
  if (result.next_action === "review_required") {
    return result.review_item_id
      ? t("notice.sentToReview", {
          defaultValue: "Draft saved and sent to Review.",
        })
      : t("notice.reviewRequired", {
          defaultValue: "Draft saved. Review is required before activation.",
        });
  }
  return t("notice.ownerActivationRequired", {
    defaultValue:
      "Draft saved. Owner activation is required before it becomes active.",
  });
}

function versionNumberFor(
  versionId: string | null,
  versions: AgentVersionEntry[],
  fallback: number | null,
) {
  if (!versionId) {
    return null;
  }
  return (
    versions.find((version) => version.version_id === versionId)?.version_no ??
    fallback
  );
}

function countByCategory(pages: AgentPageSummary[]) {
  const counts = new Map<string, number>();
  for (const page of pages) {
    counts.set(page.category, (counts.get(page.category) ?? 0) + 1);
  }
  return counts;
}

// Thin shim over the shared domainLabel helper so the call sites below keep
// their existing (t, group, value) shape while the agent.json `labels.<group>`
// maps and acronym-aware humanized fallback are sourced from one place.
function enumLabel(
  t: TFunction,
  group: "category" | "injectionPolicy" | "sensitivity",
  value: string | null | undefined,
) {
  return domainLabel("agent", group, value, t);
}

/**
 * Relabel the seeded system category folders with the same enum label every
 * other Agent surface uses. The backend seed stores Title Case ("Agent Guide")
 * while the cards, table and dialogs render `labels.category.*` ("Agent
 * guide"), which split one concept across two casings on a single screen.
 * Crumb entries mirror the node titles, so they are rewritten by id too.
 */
function withCategoryLabels(
  nodes: TreeNodePayload[],
  t: TFunction,
): TreeNodePayload[] {
  const categorySlugById = new Map(
    nodes
      .filter(
        (node) =>
          node.is_system_node &&
          node.slug !== null &&
          AGENT_PAGE_CATEGORIES.includes(node.slug) &&
          // Only a folder still carrying its seeded name is relabelled. The
          // rename dialog is open on system folders, and overwriting a name the
          // workspace chose would make that rename look like it did nothing.
          node.title.toLowerCase() === node.slug.replace(/_/g, " "),
      )
      .map((node) => [node.id, node.slug as string]),
  );
  if (categorySlugById.size === 0) {
    return nodes;
  }
  return nodes.map((node) => {
    const ownSlug = categorySlugById.get(node.id);
    const renamedCrumbs = node.breadcrumb.some((entry) =>
      categorySlugById.has(entry.id),
    );
    if (!ownSlug && !renamedCrumbs) {
      return node;
    }
    return {
      ...node,
      title: ownSlug ? enumLabel(t, "category", ownSlug) : node.title,
      breadcrumb: node.breadcrumb.map((entry) => {
        const crumbSlug = categorySlugById.get(entry.id);
        return crumbSlug
          ? { ...entry, title: enumLabel(t, "category", crumbSlug) }
          : entry;
      }),
    };
  });
}

function resolveRequestedNodeId(
  nodes: TreeNodePayload[],
  pages: AgentPageSummary[],
  requestedTargetId: string | null,
) {
  if (!requestedTargetId) {
    return null;
  }
  if (nodes.some((node) => node.id === requestedTargetId)) {
    return requestedTargetId;
  }
  if (pages.some((page) => page.id === requestedTargetId)) {
    return nodeIdForPage(nodes, requestedTargetId);
  }
  return null;
}

function selectedPageIdForNode(
  nodes: TreeNodePayload[],
  _pages: AgentPageSummary[],
  nodeId: string | null,
) {
  const node = nodes.find((candidate) => candidate.id === nodeId);
  return node?.target_kind === "agent_page" && node.target_id
    ? node.target_id
    : null;
}

function categoryForTreeParent(
  parentNode: TreeNodePayload | null,
  pages: AgentPageSummary[],
) {
  if (!parentNode) {
    return "persona";
  }
  if (parentNode.target_kind === "agent_page" && parentNode.target_id) {
    const parentPage = pages.find((page) => page.id === parentNode.target_id);
    if (parentPage) {
      return parentPage.category;
    }
  }
  const candidateSlugs = [
    parentNode.slug,
    ...[...parentNode.breadcrumb].reverse().map((entry) => entry.slug),
  ];
  for (const slug of candidateSlugs) {
    if (slug && AGENT_PAGE_CATEGORIES.includes(slug)) {
      return slug;
    }
  }
  return "persona";
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
