import { lazy, Suspense, useEffect, type ReactNode } from "react";
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
const ContextEditPermalinkResolver = lazy(() =>
  import("../features/context/ContextEditPermalinkResolver").then((module) => ({
    default: module.ContextEditPermalinkResolver,
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
const WorkspaceScopedSettingsScreen = lazy(() =>
  import("../features/settings/WorkspaceScopedSettingsScreen").then((module) => ({
    default: module.WorkspaceScopedSettingsScreen,
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
export const appRoutes = createRoutesFromElements(
    <Route errorElement={<RootRouteError />}>
      {/* Public, unauthenticated published-page reader (T16 27-a). Lives OUTSIDE
          RequireAuth and OUTSIDE AppFrame (no AuthProvider/WorkspaceProvider/Shell)
          so an anonymous visitor renders the page without any redirect to /login. */}
      <Route element={<PublicFrame />}>
        <Route path="/w/:workspaceSlug/pages/:slug" element={<PublishedPageReader />} />
      </Route>
      <Route element={<ScopedSettingsFrame />}>
        <Route path="/workspaces/:workspaceId/settings" element={<RequireAuth><WorkspaceScopedSettingsScreen /></RequireAuth>} />
      </Route>
      <Route element={<AppFrame />}>
      <Route
        path="/"
        element={
          <RequireAuth>
            <Navigate to="/dashboard" replace />
          </RequireAuth>
        }
      />
      <Route path="/login" element={<AuthScreen />} />
      <Route path="/forgot-password" element={<ForgotPasswordScreen />} />
      <Route path="/reset-password" element={<ResetPasswordScreen />} />
      <Route path="/verify-email" element={<VerifyEmailScreen />} />
      <Route path="/auth/verify" element={<IdentityVerificationScreen />} />
      <Route
        path="/account"
        element={
          <RequireAuth>
            <AccountSecurityScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/account/password"
        element={
          <RequireAuth>
            <AccountPasswordScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/workspaces/find"
        element={
          <RequireAuth>
            <WorkspaceDirectoryScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/library"
        element={
          <RequireAuth>
            <LibraryScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/context"
        element={
          <RequireAuth>
            <ContextScreen />
          </RequireAuth>
        }
      />
      {/* P10.1: canonical Context page permalink — the only workspace-in-path
          URL in this SPA. Mounts the same ContextScreen; its requestedTargetId
          derivation reads :pageId as a fallback under the query-param target. */}
      <Route
        path="/workspaces/:workspaceId/context/pages/:pageId"
        element={
          <RequireAuth>
            <ContextScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/p/:pageId"
        element={
          <RequireAuth>
            <PagePermalinkRedirect />
          </RequireAuth>
        }
      />
      <Route
        path="/agent"
        element={
          <RequireAuth>
            <AgentScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/ask"
        element={
          <RequireAuth>
            <RequireAskEnabled>
              <AskScreen />
            </RequireAskEnabled>
          </RequireAuth>
        }
      />
      {/* Every mcp-chats endpoint (including reads) requires editor+, so the
          Chats routes are editor-gated too — a viewer would only see a 403
          banner plus a misleading empty state. */}
      <Route
        path="/chats"
        element={
          <RequireAuth>
            <RequireEditor>
              <ChatsListScreen />
            </RequireEditor>
          </RequireAuth>
        }
      />
      <Route
        path="/chats/:chatId"
        element={
          <RequireAuth>
            <RequireEditor>
              <ChatsDetailScreen />
            </RequireEditor>
          </RequireAuth>
        }
      />
      <Route
        path="/agent/new"
        element={
          <RequireAuth>
            <RequireEditor>
              <AgentScreen />
            </RequireEditor>
          </RequireAuth>
        }
      />
      <Route
        path="/activity"
        element={
          <RequireAuth>
            <ActivityScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/activity/:lane"
        element={
          <RequireAuth>
            <ActivityLanePage />
          </RequireAuth>
        }
      />
      <Route
        path="/review"
        element={
          <RequireAuth>
            <ReviewScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/settings"
        element={
          <RequireAuth>
            <SettingsScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/settings/integrations"
        element={
          <RequireAuth>
            <SettingsScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/dashboard"
        element={
          <RequireAuth>
            <DashboardScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/search"
        element={
          <RequireAuth>
            <SearchScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/lint"
        element={
          <RequireAuth>
            <LintScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/setup"
        element={
          <RequireAuth>
            <SetupScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/context/new"
        element={
          <RequireAuth>
            <RequireEditor>
              <RoutedPageEditor mode="create" />
            </RequireEditor>
          </RequireAuth>
        }
      />
      {/* Still slug-routed: PageEditor's own data-fetch (readPageBySlug), its
          autosave-to-edit address-bar transition, and its draft-recovery keys
          are all slug-keyed end to end. Rewriting it to be id-native is out of
          scope for P10.1 (see the handoff report) — this stays the actual
          editor implementation route, not a public entry point; new links use
          the canonical id-based route below, which hands off here. */}
      <Route
        path="/context/:slug/edit"
        element={
          <RequireAuth>
            <RequireEditor>
              <RoutedPageEditor mode="edit" />
            </RequireEditor>
          </RequireAuth>
        }
      />
      {/* P10.1: canonical edit permalink. Resolves :pageId to a slug, then
          hands off to /context/:slug/edit above. */}
      <Route
        path="/workspaces/:workspaceId/context/pages/:pageId/edit"
        element={
          <RequireAuth>
            <RequireEditor>
              <ContextEditPermalinkResolver />
            </RequireEditor>
          </RequireAuth>
        }
      />
      <Route
        path="*"
        element={
          <RequireAuth>
            <NotFoundScreen />
          </RequireAuth>
        }
      />
      </Route>
    </Route>,
);

const appRouter = createBrowserRouter(appRoutes);

export function App() {
  return <RouterProvider router={appRouter} />;
}

export function RootRouteError() {
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
          <Link className="icon-button text-button" to="/dashboard">
            {t("routeError.dashboard", {
              defaultValue: "Go to Dashboard",
            })}
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
  const next = encodeURIComponent(`${location.pathname}${location.search}`);
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

function ScopedSettingsFrame() {
  return (
    <AuthProvider>
      <Suspense fallback={<RouteLoading />}><Outlet /></Suspense>
    </AuthProvider>
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
  // Account switches can preserve the workspace ID. Remount the authenticated
  // subtree so loaded personal flags and permission-dependent content cannot
  // cross that identity boundary while the new actor's requests are pending.
  return (
    <WorkspaceProvider
      key={user?.user_id ?? "anonymous"}
      sessionUserId={user?.user_id ?? null}
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
  if (loading || !initialized) {
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
    const fallback = location.pathname.startsWith("/agent")
      ? "/agent"
      : "/dashboard";
    if (location.pathname === fallback) {
      // Already at the bounce target (only possible while RequireEditor stays
      // mounted across the redirect); navigating again would loop.
      return null;
    }
    return <Navigate to={fallback} replace state={{ editorRequired: true }} />;
  }
  return children;
}
