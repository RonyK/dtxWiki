import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  createBrowserRouter,
  createRoutesFromElements,
  Link,
  Navigate,
  Outlet,
  Route,
  RouterProvider,
  useLocation,
} from "react-router-dom";

import { WorkspaceGraphGate } from "../features/graph/WorkspaceGraphGate";
import { AuthProvider, useAuth } from "../shared/auth/AuthContext";
import { Shell } from "../shared/components/Shell";
import { RouteLoading } from "../shared/components/RouteLoading";
import { StatusBanner } from "../shared/components/StatusBanner";
import {
  useWorkspaceContext,
  WorkspaceProvider,
} from "../shared/workspace/WorkspaceContext";
import { useLabsPreferences } from "../shared/hooks/useLabsPreferences";
import { isEditorRole } from "../shared/workspace/permissions";
import { getCurrentWorkspaceId } from "../shared/api/workspace";
import { hasConflictingWorkspaceQuery, legacyWorkspaceEntry, parseWorkspaceRoute, workspacePath } from "../shared/lib/workspaceRoutes";
import { isWorkspaceUuid } from "../shared/lib/workspaceIdentity";

const RoutedPageEditor = lazy(() =>
  import("../features/pages/PageEditor").then((module) => ({
    default: module.RoutedPageEditor,
  })),
);
const NotFoundScreen = lazy(() =>
  import("../features/errors/NotFoundScreen").then((module) => ({
    default: module.NotFoundScreen,
  })),
);
const PagePermalinkRedirect = lazy(() =>
  import("../features/pages/PagePermalinkRedirect").then((module) => ({
    default: module.PagePermalinkRedirect,
  })),
);
const PublishedPageReader = lazy(() =>
  import("../features/pages/PublishedPageReader").then((module) => ({
    default: module.PublishedPageReader,
  })),
);
const AskScreen = lazy(() =>
  import("../features/ask/AskScreen").then((module) => ({ default: module.AskScreen })),
);
const AgentScreen = lazy(() =>
  import("../features/agent/AgentScreen").then((module) => ({ default: module.AgentScreen })),
);
const ActivityScreen = lazy(() =>
  import("../features/activity/ActivityScreen").then((module) => ({
    default: module.ActivityScreen,
  })),
);
const ActivityLanePage = lazy(() =>
  import("../features/activity/ActivityScreen").then((module) => ({
    default: module.ActivityLanePage,
  })),
);
const AccountSecurityScreen = lazy(() =>
  import("../features/auth/AccountSecurityScreen").then((module) => ({
    default: module.AccountSecurityScreen,
  })),
);
const AccountPasswordScreen = lazy(() =>
  import("../features/auth/AccountSecurityScreen").then((module) => ({
    default: module.AccountPasswordScreen,
  })),
);
const AuthScreen = lazy(() =>
  import("../features/auth/AuthScreen").then((module) => ({ default: module.AuthScreen })),
);
const ResetPasswordScreen = lazy(() =>
  import("../features/auth/ResetPasswordScreen").then((module) => ({
    default: module.ResetPasswordScreen,
  })),
);
const ForgotPasswordScreen = lazy(() =>
  import("../features/auth/ForgotPasswordScreen").then((module) => ({
    default: module.ForgotPasswordScreen,
  })),
);
const VerifyEmailScreen = lazy(() =>
  import("../features/auth/VerifyEmailScreen").then((module) => ({
    default: module.VerifyEmailScreen,
  })),
);
const IdentityVerificationScreen = lazy(() =>
  import("../features/auth/IdentityVerificationScreen").then((module) => ({ default: module.IdentityVerificationScreen })),
);
const ContextScreen = lazy(() =>
  import("../features/context/ContextScreen").then((module) => ({ default: module.ContextScreen })),
);
const WorkspaceGraphScreen = lazy(() =>
  import("../features/graph/WorkspaceGraphScreen").then((module) => ({ default: module.WorkspaceGraphScreen })),
);
const ChatsListScreen = lazy(() =>
  import("../features/mcp-chats/ChatsListScreen").then((module) => ({
    default: module.ChatsListScreen,
  })),
);
const ChatsDetailScreen = lazy(() =>
  import("../features/mcp-chats/ChatsDetailScreen").then((module) => ({
    default: module.ChatsDetailScreen,
  })),
);
const DashboardScreen = lazy(() =>
  import("../features/dashboard/DashboardScreen").then((module) => ({
    default: module.DashboardScreen,
  })),
);
const InboxScreen = lazy(() =>
  import("../features/inbox/InboxScreen").then((module) => ({
    default: module.InboxScreen,
  })),
);
const ReviewScreen = lazy(() =>
  import("../features/review/ReviewScreen").then((module) => ({
    default: module.ReviewScreen,
  })),
);
const LintScreen = lazy(() =>
  import("../features/lint/LintScreen").then((module) => ({ default: module.LintScreen })),
);
const SearchScreen = lazy(() =>
  import("../features/search/SearchScreen").then((module) => ({
    default: module.SearchScreen,
  })),
);
const SettingsScreen = lazy(() =>
  import("../features/settings/SettingsScreen").then((module) => ({
    default: module.SettingsScreen,
  })),
);
const SetupScreen = lazy(() =>
  import("../features/setup/SetupScreen").then((module) => ({ default: module.SetupScreen })),
);
const LibraryScreen = lazy(() =>
  import("../features/library/LibraryScreen").then((module) => ({ default: module.LibraryScreen })),
);
const WorkspaceDirectoryScreen = lazy(() =>
  import("../features/workspace/WorkspaceDirectoryScreen").then((module) => ({
    default: module.WorkspaceDirectoryScreen,
  })),
);

// Exported so route-level tests can mount the REAL config (e.g. assert the
// public /w/:workspaceSlug/pages/:slug route is NOT under RequireAuth).
export function createAppRoutes() {
  return createRoutesFromElements(
  <Route errorElement={<RootRouteError />}>
    <Route element={<PublicFrame />}>
      <Route path="/w/:workspaceSlug/pages/:slug" element={<PublishedPageReader />} />
    </Route>
    <Route element={<AppFrame />}>
      <Route path="/login" element={<AuthScreen />} />
      <Route path="/forgot-password" element={<ForgotPasswordScreen />} />
      <Route path="/reset-password" element={<ResetPasswordScreen />} />
      <Route path="/verify-email" element={<VerifyEmailScreen />} />
      <Route path="/auth/verify" element={<IdentityVerificationScreen />} />
      <Route path="/account" element={<RequireAuth><AccountSecurityScreen /></RequireAuth>} />
      <Route path="/account/password" element={<RequireAuth><AccountPasswordScreen /></RequireAuth>} />
      <Route path="/account/integrations" element={<RequireAuth><SettingsScreen scope="account" /></RequireAuth>} />
      <Route path="/settings/server" element={<RequireAuth><SettingsScreen scope="server" /></RequireAuth>} />
      <Route path="/workspaces/find" element={<RequireAuth><WorkspaceDirectoryScreen /></RequireAuth>} />
      <Route path="/p/:workspaceId/:pageId" element={<RequireAuth><PagePermalinkRedirect /></RequireAuth>} />
      <Route path="/p/:pageId" element={<RequireAuth><NotFoundScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId" element={<RequireAuth><WorkspaceHomeRedirect /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/graph" element={<RequireAuth><WorkspaceGraphGate><WorkspaceGraphScreen /></WorkspaceGraphGate></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/dashboard" element={<RequireAuth><DashboardScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/context" element={<RequireAuth><ContextScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/context/folders/new" element={<RequireAuth><RequireEditor><ContextScreen /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/context/folders/:nodeId" element={<RequireAuth><ContextScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/context/pages/new" element={<RequireAuth><RequireEditor><RoutedPageEditor mode="create" /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/context/pages/:pageId" element={<RequireAuth><ContextScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/context/pages/:pageId/edit" element={<RequireAuth><RequireEditor><RoutedPageEditor mode="edit" /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/library" element={<RequireAuth><LibraryScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/library/items/new" element={<RequireAuth><RequireEditor><LibraryScreen /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/library/items/:itemId" element={<RequireAuth><LibraryScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/library/artifacts/:artifactId" element={<RequireAuth><LibraryScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/library/folders/new" element={<RequireAuth><RequireEditor><LibraryScreen /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/library/folders/:nodeId" element={<RequireAuth><LibraryScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/agent" element={<RequireAuth><AgentScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/agent/pages/new" element={<RequireAuth><RequireEditor><AgentScreen /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/agent/pages/:agentPageId" element={<RequireAuth><AgentScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/agent/pages/:agentPageId/edit" element={<RequireAuth><RequireEditor><AgentScreen /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/agent/folders/new" element={<RequireAuth><RequireEditor><AgentScreen /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/agent/folders/:nodeId" element={<RequireAuth><AgentScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/activity" element={<RequireAuth><ActivityScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/activity/:lane" element={<RequireAuth><ActivityLanePage /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/review" element={<RequireAuth><ReviewScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/review/:reviewItemId" element={<RequireAuth><ReviewScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/lint" element={<RequireAuth><LintScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/lint/findings/:findingId" element={<RequireAuth><LintScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/chats" element={<RequireAuth><RequireEditor><ChatsListScreen /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/chats/:chatId" element={<RequireAuth><RequireEditor><ChatsDetailScreen /></RequireEditor></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/ask" element={<RequireAuth><RequireAskEnabled><AskScreen /></RequireAskEnabled></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/ask/:askSessionId" element={<RequireAuth><RequireAskEnabled><AskScreen /></RequireAskEnabled></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/search" element={<RequireAuth><SearchScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/settings" element={<RequireAuth><SettingsScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/settings/integrations" element={<RequireAuth><SettingsScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/setup" element={<RequireAuth><SetupScreen /></RequireAuth>} />
      <Route path="/workspaces/:workspaceId/inbox" element={<RequireAuth><InboxScreen /></RequireAuth>} />
      <Route path="/inbox" element={<RequireAuth><InboxScreen /></RequireAuth>} />
      {[
        "/", "/dashboard", "/graph", "/context", "/library", "/agent", "/ask",
        "/chats", "/activity", "/activity/:lane", "/review", "/lint",
        "/search", "/settings", "/settings/integrations", "/setup",
      ].map((path) => (
        <Route key={path} path={path} element={<RequireAuth><LegacyWorkspaceLanding /></RequireAuth>} />
      ))}
      <Route path="*" element={<RequireAuth><NotFoundScreen /></RequireAuth>} />
    </Route>
  </Route>,
);

}

export const appRoutes = createAppRoutes();

function WorkspaceHomeRedirect() {
  const location = useLocation();
  const route = parseWorkspaceRoute(location.pathname);
  return route ? <Navigate to={`${workspacePath(route.workspaceId, "dashboard")}${location.search}${location.hash}`} replace state={location.state} /> : <NotFoundScreen />;
}

function LegacyWorkspaceLanding() {
  const location = useLocation();
  const suffix = legacyWorkspaceEntry(location.pathname, location.search);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!suffix) return;
    let active = true;
    void getCurrentWorkspaceId().then(id => { if (active) { setWorkspaceId(id); setFailed(!id); } }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [suffix]);
  if (!suffix || hasConflictingWorkspaceQuery(location.search)) return <NotFoundScreen />;
  if (failed) return <Navigate to="/workspaces/find" replace />;
  if (!workspaceId) return <RouteLoading />;
  return <Navigate to={workspacePath(workspaceId, suffix) + location.search + location.hash} replace />;
}

const appRouter = createBrowserRouter(appRoutes);

export function App() {
  return <RouterProvider router={appRouter} />;
}

export function RootRouteError() {
  const workspaceId = parseWorkspaceRoute(useLocation().pathname)?.workspaceId;
  const routeWorkspaceId = workspaceId && isWorkspaceUuid(workspaceId) ? workspaceId : null;
  const { t } = useTranslation("shell");
  return (
    <main className="route-loading">
      <section className="empty-state empty-state-rich" role="alert">
        <h1>
          {t("routeError.title", {
            defaultValue: "This page couldn't be loaded",
          })}
        </h1>
        <p>
          {t("routeError.body", {
            defaultValue:
              "dtxWiki may have been updated while this tab was open. Load the latest version and try again.",
          })}
        </p>
        <div className="empty-state-actions">
          <button
            className="icon-button text-button primary"
            data-testid="route-error-reload"
            onClick={() => window.location.reload()}
            type="button"
          >
            {t("routeError.reload", {
              defaultValue: "Load the latest version",
            })}
          </button>
          <Link className="icon-button text-button" to={routeWorkspaceId ? workspacePath(routeWorkspaceId, "dashboard") : "/workspaces/find"}>
            {routeWorkspaceId ? t("routeError.dashboard", {
              defaultValue: "Go to Dashboard",
            }) : t("workspaceResolution.find")}
          </Link>
        </div>
      </section>
    </main>
  );
}

// Minimal chrome for the public published-page route (T16 27-a). Deliberately
// has NO AuthProvider, WorkspaceProvider, or Shell sidebar/rails/inspector — an
// anonymous visitor must be able to render a public page with no session. The
// only affordance is a top-right "Sign in" link back into the app, which
// carries ?next= so the visitor returns to this public page after login
// (safeNextPath accepts /w/... paths).
function PublicFrame() {
  const { t } = useTranslation("pages");
  const location = useLocation();
  const next = encodeURIComponent(`${location.pathname}${location.search}${location.hash}`);
  return (
    <div className="public-frame">
      <header className="public-frame-bar">
        <span className="public-frame-brand">dtxWiki</span>
        <Link className="icon-button text-button" to={`/login?next=${next}`}>
          {t("publicReader.signIn")}
        </Link>
      </header>
      <Suspense fallback={<RouteLoading label={t("publicReader.loading")} />}>
        <Outlet />
      </Suspense>
    </div>
  );
}

function AppFrame() {
  const location = useLocation();
  const skipInitialSessionCheck = isAuthBootstrapPath(location.pathname);
  return (
    <AuthProvider
      loadInstanceCapabilities={skipInitialSessionCheck}
      skipInitialSessionCheck={skipInitialSessionCheck}
    >
      <WorkspaceScope>
        <Shell>
          <Suspense fallback={<RouteLoading />}>
            <Outlet />
          </Suspense>
        </Shell>
      </WorkspaceScope>
    </AuthProvider>
  );
}

// WS-05: `useAuth` only works inside AuthProvider, so this thin wrapper
// reads the session identity and hands it to WorkspaceProvider as a prop —
// WorkspaceProvider itself must stay callable without an AuthProvider
// ancestor (see WorkspaceContext.test.tsx's bare renders).
export function WorkspaceScope({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const routeWorkspaceId = parseWorkspaceRoute(useLocation().pathname)?.workspaceId ?? null;
  // Account switches can preserve the workspace ID. Remount the authenticated
  // subtree so loaded personal flags and permission-dependent content cannot
  // cross that identity boundary while the new actor's requests are pending.
  return (
    <WorkspaceProvider
      key={user?.user_id ?? "anonymous"}
      sessionUserId={user?.user_id ?? null}
      routeWorkspaceId={routeWorkspaceId}
    >
      {children}
    </WorkspaceProvider>
  );
}

export function isAuthBootstrapPath(pathname: string) {
  const normalized = pathname === "/" ? "/" : pathname.replace(/\/+$/, "");
  return [
    "/login",
    "/forgot-password",
    "/reset-password",
    "/verify-email",
    "/auth/verify",
  ].includes(normalized);
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { t } = useTranslation("shell");
  const { refreshSession, sessionChecked, sessionError, status } = useAuth();
  const location = useLocation();

  useEffect(() => {
    if (!sessionChecked && status === "anonymous") {
      void refreshSession();
    }
  }, [refreshSession, sessionChecked, status]);

  if (status === "checking" || !sessionChecked) {
    return (
      <RouteLoading
        label={t("session.checking", { defaultValue: "Checking session" })}
      />
    );
  }

  if (status === "transient_error") {
    return (
      <main className="route-loading">
        <StatusBanner
          action={
            <button
              className="icon-button text-button"
              data-testid="session-verify-retry"
              onClick={() => void refreshSession()}
              type="button"
            >
              {t("session.retry", { defaultValue: "Retry" })}
            </button>
          }
          tone="warning"
        >
          <strong>
            {t("session.verifyFailed", {
              defaultValue: "Could not verify your session.",
            })}
          </strong>
          <span>
            {sessionError ??
              t("session.checkFailed", {
                defaultValue: "Session check failed.",
              })}
          </span>
        </StatusBanner>
      </main>
    );
  }

  if (status === "anonymous") {
    const next = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }

  return children;
}

export function RequireAskEnabled({ children }: { children: ReactNode }) {
  const { preferences } = useLabsPreferences();
  return preferences.ask ? children : <NotFoundScreen />;
}

// Codex #25: write routes (page/agent create + page edit) must not render the
// editor for a workspace viewer reached by a direct URL. The backend re-enforces
// RBAC, so this is UI-only, but it stops a viewer from being dropped into an
// editor that will only fail on save. Waits for the workspace list to resolve
// before deciding so an editor is never wrongly bounced during initial load.
export function RequireEditor({ children }: { children: ReactNode }) {
  const { t } = useTranslation("shell");
  const { selectedWorkspace, loading, initialized, error, refreshWorkspaces } =
    useWorkspaceContext();
  const location = useLocation();

  // Wait for the first fetch (loading, or not yet initialized) before deciding:
  // on a full-page load to a write route the list is empty pre-fetch, and gating
  // on `loading` alone (which starts false) would bounce a legitimate editor.
  // A resolved-but-empty list (initialized, not loading) still falls through to
  // the role check and redirects rather than hanging forever.
  if (!initialized || (loading && !selectedWorkspace)) {
    return <RouteLoading label={t("accessGuard.checking")} />;
  }
  // Only surface the retry banner when an error left NO usable workspace (the
  // first-load failure). `error` is a separate flag that persists alongside a
  // still-valid cached selectedWorkspace after a later transient refresh failure
  // (refreshWorkspaces' catch keeps the prior list), so gating on `error` alone
  // would wrongly block a legitimate editor who still has a usable workspace.
  if (error && !selectedWorkspace) {
    return (
      <main className="route-loading">
        <StatusBanner
          action={
            <button
              className="icon-button text-button"
              data-testid="workspace-access-retry"
              onClick={() => void refreshWorkspaces()}
              type="button"
            >
              {t("accessGuard.retry")}
            </button>
          }
          tone="warning"
        >
          <strong>{t("accessGuard.loadFailed")}</strong>
          <span>{error}</span>
        </StatusBanner>
      </main>
    );
  }
  if (!isEditorRole(selectedWorkspace?.role)) {
    // Keep the read-only /agent fallback (a viewer can use it), but bounce the
    // page edit/create gates to /dashboard instead of a random page reader that
    // drops the editorRequired banner. The Dashboard renders the banner so the
    // reason for the redirect survives regardless of workspace contents.
    const route = parseWorkspaceRoute(location.pathname);
    const fallback = route ? workspacePath(route.workspaceId, route.section === "agent" ? "agent" : "dashboard") : "/workspaces/find";
    if (location.pathname === fallback) {
      // Already at the bounce target (only possible while RequireEditor stays
      // mounted across the redirect); navigating again would loop.
      return null;
    }
    return <Navigate to={fallback} replace state={{ editorRequired: true }} />;
  }
  return children;
}
