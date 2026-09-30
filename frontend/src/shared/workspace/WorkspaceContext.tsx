import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  clearPersistedWorkspaceId,
  listWorkspaces,
  resolveSelectedWorkspaceId,
  setSelectedWorkspace,
  setSelectedWorkspaceId,
  type WorkspaceSummary,
} from "../api/workspace";
import { apiErrorMessage } from "../api/client";
import { translate } from "../i18n";

type WorkspaceContextValue = {
  defaultWorkspaceId: string | null;
  personalWorkspaceId: string | null;
  error: string | null;
  loading: boolean;
  // True once the workspace list has been fetched at least once. Distinguishes
  // "not yet fetched" (loading false, workspaces empty on first render) from
  // "fetched and genuinely empty" so guards do not act on the pre-fetch window.
  initialized: boolean;
  selectedId: string;
  selectedWorkspace: WorkspaceSummary | null;
  workspaceSlug: string | null;
  workspaces: WorkspaceSummary[];
  addWorkspace: (workspace: WorkspaceSummary) => void;
  refreshWorkspaces: (
    preferredWorkspaceId?: string | null,
  ) => Promise<WorkspaceRefreshResult | null>;
  removeWorkspace: (
    workspaceId: string,
    resolution?: WorkspaceRemovalResolution,
  ) => WorkspaceSummary | null;
  selectWorkspaceId: (workspaceId: string | null) => WorkspaceSummary | null;
};

export type WorkspaceRefreshResult = {
  selectedWorkspace: WorkspaceSummary | null;
  workspaces: WorkspaceSummary[];
};

type WorkspaceRemovalResolution = {
  fallbackWorkspaceId: string | null;
};

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({
  children,
  sessionUserId = null,
}: {
  children: ReactNode;
  // WS-05: optional so existing bare `<WorkspaceProvider>` renders (tests,
  // and callers with no session identity to hand over) keep compiling and
  // behaving exactly as before — the reset effect below never fires when
  // this stays at its default `null`.
  sessionUserId?: string | null;
}) {
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [defaultWorkspaceId, setDefaultWorkspaceId] = useState<string | null>(
    null,
  );
  const [personalWorkspaceId, setPersonalWorkspaceId] = useState<string | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const workspacesRef = useRef<WorkspaceSummary[]>(workspaces);
  const defaultWorkspaceIdRef = useRef<string | null>(defaultWorkspaceId);
  const personalWorkspaceIdRef = useRef<string | null>(personalWorkspaceId);
  const refreshGenerationRef = useRef(0);
  useEffect(() => {
    workspacesRef.current = workspaces;
  }, [workspaces]);
  useEffect(() => {
    defaultWorkspaceIdRef.current = defaultWorkspaceId;
  }, [defaultWorkspaceId]);
  useEffect(() => {
    personalWorkspaceIdRef.current = personalWorkspaceId;
  }, [personalWorkspaceId]);
  const selectedWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.id === selectedId) ?? null,
    [selectedId, workspaces],
  );

  const previousSessionUserId = useRef<string | null>(sessionUserId);
  useEffect(() => {
    const previous = previousSessionUserId.current;
    previousSessionUserId.current = sessionUserId;
    // Reset only on logout (A -> null) and account switch (A -> B). The
    // initial session resolution (null -> A) must NOT reset, or it would
    // wipe out whatever Shell's mount-only refreshWorkspaces effect already
    // populated before AuthProvider finishes its first session check, and
    // nothing would refill it (WS-05).
    if (previous === null || previous === sessionUserId) {
      return;
    }
    refreshGenerationRef.current += 1;
    clearPersistedWorkspaceId();
    workspacesRef.current = [];
    defaultWorkspaceIdRef.current = null;
    personalWorkspaceIdRef.current = null;
    setWorkspaces([]);
    setSelectedId("");
    setDefaultWorkspaceId(null);
    setPersonalWorkspaceId(null);
    setLoading(false);
    setError(null);
    setInitialized(false);
  }, [sessionUserId]);

  const applyWorkspaces = useCallback((nextWorkspaces: WorkspaceSummary[]) => {
    const defaultId = defaultWorkspaceIdRef.current;
    const personalId = personalWorkspaceIdRef.current;
    const resolved = resolveSelectedWorkspaceId({
      current_workspace_id:
        defaultId &&
        nextWorkspaces.some((workspace) => workspace.id === defaultId)
          ? defaultId
          : null,
      default_workspace_id: defaultId,
      personal_workspace_id: personalId,
      workspaces: nextWorkspaces,
    });
    setWorkspaces(nextWorkspaces);
    // Keep the user's pinned default as long as it survives in the list —
    // clearing it on every local mutation would drop the "Default" badge for
    // the remaining workspaces until the next refreshWorkspaces round-trip
    // (which nothing triggers automatically after a delete).
    const nextDefaultId =
      defaultId &&
      nextWorkspaces.some((workspace) => workspace.id === defaultId)
        ? defaultId
        : null;
    const nextPersonalId =
      personalId &&
      nextWorkspaces.some((workspace) => workspace.id === personalId)
        ? personalId
        : null;
    defaultWorkspaceIdRef.current = nextDefaultId;
    personalWorkspaceIdRef.current = nextPersonalId;
    setDefaultWorkspaceId(nextDefaultId);
    setPersonalWorkspaceId(nextPersonalId);
    setSelectedId(resolved ?? "");
    return (
      nextWorkspaces.find((workspace) => workspace.id === resolved) ?? null
    );
  }, []);

  const refreshWorkspaces = useCallback(
    async (preferredWorkspaceId?: string | null) => {
      // Every refresh is a new authority boundary. If two calls overlap, only
      // the most recently started one may update context state, even when an
      // intervening mutation has already fenced the lower-level API cache.
      const refreshGeneration = ++refreshGenerationRef.current;
      setLoading(true);
      setError(null);
      try {
        const response = await listWorkspaces();
        if (refreshGeneration !== refreshGenerationRef.current) {
          return null;
        }
        const preferred = preferredWorkspaceId
          ? response.data.workspaces.find(
              (workspace) => workspace.id === preferredWorkspaceId,
            ) ?? null
          : null;
        const resolved =
          preferred?.id ?? resolveSelectedWorkspaceId(response.data);
        setWorkspaces(response.data.workspaces);
        // Reflect the user's *persisted* default (null when unset) — do NOT
        // coalesce to current_workspace_id, or the "Default" badge would mark the
        // moving landing/fallback workspace the user never pinned (backend S2/C9).
        // Selection still uses resolveSelectedWorkspaceId (current_workspace_id).
        const nextDefaultId = response.data.default_workspace_id ?? null;
        const nextPersonalId = response.data.personal_workspace_id ?? null;
        defaultWorkspaceIdRef.current = nextDefaultId;
        personalWorkspaceIdRef.current = nextPersonalId;
        setDefaultWorkspaceId(nextDefaultId);
        setPersonalWorkspaceId(nextPersonalId);
        setSelectedId(resolved ?? "");
        if (preferred) {
          setSelectedWorkspace(preferred);
        }
        return {
          selectedWorkspace:
            response.data.workspaces.find(
              (workspace) => workspace.id === resolved,
            ) ?? null,
          workspaces: response.data.workspaces,
        };
      } catch (exc) {
        if (refreshGeneration !== refreshGenerationRef.current) {
          return null;
        }
        setError(
          apiErrorMessage(
            exc,
            translate(
              "shell:workspace.loadFailed",
              "Failed to load workspaces",
            ),
          ),
        );
        return null;
      } finally {
        if (refreshGeneration === refreshGenerationRef.current) {
          setLoading(false);
          setInitialized(true);
        }
      }
    },
    [],
  );

  const selectWorkspaceId = useCallback((workspaceId: string | null) => {
    if (!workspaceId) {
      setSelectedWorkspace(null);
      setSelectedId("");
      return null;
    }
    const workspace =
      workspacesRef.current.find((candidate) => candidate.id === workspaceId) ??
      null;
    if (workspace) {
      setSelectedWorkspace(workspace);
    } else {
      setSelectedWorkspaceId(workspaceId);
    }
    setSelectedId(workspaceId);
    return workspace;
  }, []);

  const addWorkspace = useCallback((workspace: WorkspaceSummary) => {
    refreshGenerationRef.current += 1;
    setWorkspaces((current) => {
      const existing = current.some(
        (candidate) => candidate.id === workspace.id,
      );
      return existing
        ? current.map((candidate) =>
            candidate.id === workspace.id ? workspace : candidate,
          )
        : [...current, workspace];
    });
    setSelectedWorkspace(workspace);
    setSelectedId(workspace.id);
  }, []);

  const removeWorkspace = useCallback(
    (workspaceId: string, resolution?: WorkspaceRemovalResolution) => {
      // A local removal is a mutation boundary. Ignore any context refresh
      // that began before it; the caller will issue a post-mutation refresh
      // against the API cache generation established by delete/leave.
      refreshGenerationRef.current += 1;
      const remaining =
        resolution?.fallbackWorkspaceId === null
          ? []
          : workspacesRef.current.filter(
              (workspace) => workspace.id !== workspaceId,
            );
      const selected = applyWorkspaces(remaining);
      if (resolution) {
        return selectWorkspaceId(resolution.fallbackWorkspaceId);
      }
      return selected;
    },
    [applyWorkspaces, selectWorkspaceId],
  );

  const value = useMemo(
    () => ({
      addWorkspace,
      defaultWorkspaceId,
      error,
      loading,
      initialized,
      personalWorkspaceId,
      refreshWorkspaces,
      removeWorkspace,
      selectWorkspaceId,
      selectedId,
      selectedWorkspace,
      workspaceSlug: selectedWorkspace?.slug ?? null,
      workspaces,
    }),
    [
      addWorkspace,
      defaultWorkspaceId,
      error,
      loading,
      initialized,
      personalWorkspaceId,
      refreshWorkspaces,
      removeWorkspace,
      selectWorkspaceId,
      selectedId,
      selectedWorkspace,
      workspaces,
    ],
  );

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspaceContext() {
  const value = useContext(WorkspaceContext);
  if (!value) {
    throw new Error("useWorkspaceContext requires WorkspaceProvider");
  }
  return value;
}

export function useOptionalWorkspaceContext() {
  return useContext(WorkspaceContext);
}
