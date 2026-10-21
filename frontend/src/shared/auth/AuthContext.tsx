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
  AUTH_EXPIRED_EVENT,
  ApiError,
  apiErrorMessage,
  setApiErrorViewerIsServerAdmin,
} from "../api/client";
import {
  getAuthCapabilities,
  getMe,
  logout as logoutRequest,
  type AuthSession,
  type AuthUser,
} from "../api/auth";
import { clearPersistedWorkspaceId, invalidateWorkspaceListCache } from "../api/workspace";
import { setTreeFavoriteActor } from "../api/treeFavorites";
import { isAbortError } from "../lib/abortError";
import { clearReviewDrafts } from "../lib/reviewDraftStorage";

type AuthStatus = "checking" | "authenticated" | "anonymous" | "transient_error";
type PasswordRecoveryMode = "email" | "admin_assisted" | "disabled";

type AuthContextValue = {
  status: AuthStatus;
  user: AuthUser | null;
  sessionChecked: boolean;
  /** Transient error from the most recent session check (e.g. network
   * outage, 5xx). Distinct from `status: "anonymous"`, which means the
   * server returned 401. UI can show a banner without forcing logout. */
  sessionError: string | null;
  capabilitiesChecked: boolean;
  signupEnabled: boolean;
  passwordRecoveryMode: PasswordRecoveryMode;
  passwordRecoveryAdminAssisted: boolean;
  capabilitiesError: string | null;
  beginAuthentication: () => void;
  refreshCapabilities: () => Promise<boolean | null>;
  refreshSession: () => Promise<AuthUser | null>;
  setAuthenticatedSession: (session: AuthSession) => void;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({
  children,
  loadInstanceCapabilities = false,
  skipInitialSessionCheck = false,
}: {
  children: ReactNode;
  loadInstanceCapabilities?: boolean;
  skipInitialSessionCheck?: boolean;
}) {
  const [status, setStatus] = useState<AuthStatus>(
    skipInitialSessionCheck ? "anonymous" : "checking",
  );
  const [user, setUser] = useState<AuthUser | null>(null);
  const [sessionChecked, setSessionChecked] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [capabilitiesChecked, setCapabilitiesChecked] = useState(false);
  const [signupEnabled, setSignupEnabled] = useState(false);
  const [passwordRecoveryMode, setPasswordRecoveryMode] =
    useState<PasswordRecoveryMode>("disabled");
  const [passwordRecoveryAdminAssisted, setPasswordRecoveryAdminAssisted] =
    useState(false);
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(null);
  const automaticCapabilitiesLoadStarted = useRef(false);
  const sessionProbeEpochRef = useRef(0);
  const activeSessionProbeRef = useRef<AbortController | null>(null);
  const sessionActorRef = useRef<string | null>(null);

  const commitUser = useCallback((nextUser: AuthUser | null, workspaceAlreadyCleared = false) => {
    const nextActorId = nextUser?.user_id ?? null;
    const previousActorId = sessionActorRef.current;
    // A cookie can change in another tab while this provider remains mounted.
    // Fence personal reads/writes synchronously, before rendering that actor.
    setTreeFavoriteActor(nextActorId);
    if (previousActorId !== nextActorId) {
      if (!workspaceAlreadyCleared) {
        if (previousActorId !== null) clearPersistedWorkspaceId();
        else invalidateWorkspaceListCache();
      }
      sessionActorRef.current = nextActorId;
    }
    setUser(nextUser);
  }, []);

  const invalidateSessionProbes = useCallback(() => {
    sessionProbeEpochRef.current += 1;
    activeSessionProbeRef.current?.abort();
    activeSessionProbeRef.current = null;
  }, []);

  useEffect(() => {
    setApiErrorViewerIsServerAdmin(user?.is_server_admin === true);
    return () => setApiErrorViewerIsServerAdmin(false);
  }, [user]);

  const refreshSession = useCallback(async () => {
    const requestEpoch = ++sessionProbeEpochRef.current;
    activeSessionProbeRef.current?.abort();
    const controller = new AbortController();
    activeSessionProbeRef.current = controller;
    const isCurrentProbe = () =>
      !controller.signal.aborted &&
      sessionProbeEpochRef.current === requestEpoch &&
      activeSessionProbeRef.current === controller;

    setStatus((current) => (current === "anonymous" && !sessionChecked ? "checking" : current));
    try {
      const activeUser = await getMe(controller.signal);
      if (!isCurrentProbe()) {
        return null;
      }
      setApiErrorViewerIsServerAdmin(activeUser.is_server_admin === true);
      commitUser(activeUser);
      setStatus("authenticated");
      setSessionChecked(true);
      setSessionError(null);
      return activeUser;
    } catch (error) {
      // Abort is cleanup, not an authentication outcome. The epoch check is
      // still required because a fetch implementation may ignore abort or may
      // have already queued its completion when the request is invalidated.
      if (!isCurrentProbe() || isAbortError(error)) {
        return null;
      }
      // Only treat true 401s as "the user is signed out". Network errors,
      // 5xx responses, and other transient failures should NOT silently
      // log the user out — surface them as sessionError so the UI can show
      // a banner without redirecting to /login (review §6.3 FA-H4).
      if (error instanceof ApiError && error.status === 401) {
        clearReviewDrafts();
        setApiErrorViewerIsServerAdmin(false);
        commitUser(null);
        setStatus("anonymous");
        setSessionChecked(true);
        setSessionError(null);
        return null;
      }
      // Preserve whatever user state we had; mark the check as failed so
      // RequireAuth can keep the prior view while a banner explains the
      // transient outage.
      const message = apiErrorMessage(error, "Session check failed.");
      setSessionError(message);
      setSessionChecked(true);
      // Initial transient failures must not look like logout. Keep a distinct
      // state so protected routes can avoid redirecting to /login.
      setStatus((prev) => (prev === "checking" ? "transient_error" : prev));
      return null;
    } finally {
      if (activeSessionProbeRef.current === controller) {
        activeSessionProbeRef.current = null;
      }
    }
  }, [commitUser, sessionChecked]);

  const refreshCapabilities = useCallback(async () => {
    // Fail closed while refreshing. A stale `true` must never keep the signup
    // form visible after the server disables self-service registration.
    setCapabilitiesChecked(false);
    setSignupEnabled(false);
    setPasswordRecoveryMode("disabled");
    setPasswordRecoveryAdminAssisted(false);
    setCapabilitiesError(null);
    try {
      const capabilities = await getAuthCapabilities();
      setSignupEnabled(capabilities.signup_enabled === true);
      setPasswordRecoveryMode(
        capabilities.password_recovery?.mode ?? "disabled",
      );
      setPasswordRecoveryAdminAssisted(
        capabilities.password_recovery?.admin_assisted ?? false,
      );
      setCapabilitiesChecked(true);
      return capabilities.signup_enabled === true;
    } catch (error) {
      setSignupEnabled(false);
      setPasswordRecoveryMode("disabled");
      setPasswordRecoveryAdminAssisted(false);
      setCapabilitiesChecked(true);
      setCapabilitiesError(
        apiErrorMessage(error, "Could not load instance capabilities."),
      );
      return null;
    }
  }, []);

  useEffect(() => {
    if (skipInitialSessionCheck || sessionChecked) {
      return;
    }
    void refreshSession();
  }, [refreshSession, sessionChecked, skipInitialSessionCheck]);

  useEffect(() => {
    if (!loadInstanceCapabilities || automaticCapabilitiesLoadStarted.current) {
      return;
    }
    automaticCapabilitiesLoadStarted.current = true;
    void refreshCapabilities();
  }, [loadInstanceCapabilities, refreshCapabilities]);

  useEffect(() => {
    const handleAuthExpired = () => {
      invalidateSessionProbes();
      setApiErrorViewerIsServerAdmin(false);
      // The dispatcher (client.ts handleAuthExpired) already dropped the
      // persisted workspace id before emitting the event, and it has to: the
      // public frame has no AuthProvider to listen. Repeating it here would
      // give the teardown sequence two owners.
      commitUser(null, true);
      setStatus("anonymous");
      setSessionChecked(true);
      setSessionError(null);
    };

    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, [commitUser, invalidateSessionProbes]);

  // Cancelling in-flight probes is the provider's own teardown, not the
  // listener's. Keeping it inside the AUTH_EXPIRED effect tied request
  // cancellation to that effect's dependency list, so any future dependency
  // added there would silently start aborting live probes on re-run. Note this
  // still fires on StrictMode's simulated unmount; that is intended -- the boot
  // effect below re-issues the probe, which is the point of the drill.
  useEffect(() => invalidateSessionProbes, [invalidateSessionProbes]);

  const beginAuthentication = useCallback(() => {
    // Credential submission establishes a new auth ordering boundary. Abort
    // and fence any pre-login /auth/me request before the credential request
    // starts, even if the underlying fetch implementation ignores abort.
    invalidateSessionProbes();
  }, [invalidateSessionProbes]);

  const setAuthenticatedSession = useCallback((session: AuthSession) => {
    invalidateSessionProbes();
    setApiErrorViewerIsServerAdmin(session.user.is_server_admin === true);
    commitUser(session.user);
    setStatus("authenticated");
    setSessionChecked(true);
    setSessionError(null);
  }, [commitUser, invalidateSessionProbes]);

  const signOut = useCallback(async () => {
    // Fence the local session immediately, before waiting for the logout
    // round-trip, and again at the state transition in case another probe was
    // started while logout was in flight.
    invalidateSessionProbes();
    try {
      await logoutRequest();
    } catch {
      // Local auth state must be cleared even if the network/server logout
      // request fails. The server-side session generation revocation still
      // protects future requests once the backend is reachable again.
    } finally {
      invalidateSessionProbes();
      setApiErrorViewerIsServerAdmin(false);
      // WS-05: the same localStorage key + in-memory snapshot the 401 path
      // clears in client.ts. Without this, the next user to sign in on this
      // browser (same shared profile) briefly sees the previous user's
      // workspace title/id/role via WorkspaceRouteBoundary's stale
      // selectedWorkspace before the fresh workspace list resolves.
      clearPersistedWorkspaceId();
      clearReviewDrafts();
      commitUser(null, true);
      setStatus("anonymous");
      setSessionChecked(true);
      setSessionError(null);
    }
  }, [commitUser, invalidateSessionProbes]);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      sessionChecked,
      sessionError,
      capabilitiesChecked,
      signupEnabled,
      passwordRecoveryMode,
      passwordRecoveryAdminAssisted,
      capabilitiesError,
      beginAuthentication,
      refreshCapabilities,
      refreshSession,
      setAuthenticatedSession,
      signOut,
    }),
    [
      refreshSession,
      refreshCapabilities,
      beginAuthentication,
      capabilitiesChecked,
      capabilitiesError,
      sessionChecked,
      sessionError,
      setAuthenticatedSession,
      signOut,
      status,
      signupEnabled,
      passwordRecoveryMode,
      passwordRecoveryAdminAssisted,
      user,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useOptionalAuth() {
  return useContext(AuthContext);
}

export function useAuth() {
  const context = useOptionalAuth();
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
