import {
  Activity,
  Bot,
  ChevronDown,
  CheckCircle2,
  Command,
  FileText,
  FolderPlus,
  LayoutDashboard,
  Library,
  Languages,
  ListChecks,
  SpellCheck,
  Menu,
  MessagesSquare,
  MessageCircleQuestion,
  Plus,
  Search,
  Settings,
  Share2,
  type LucideIcon,
  UserCircle,
  Users,
  X,
} from "lucide-react";
import type { TFunction } from "i18next";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import { apiErrorMessage } from "../api/client";
import { searchWorkspace, type RankedSearchResult } from "../api/operations";
import { createWorkspace, type WorkspaceSummary } from "../api/workspace";
import {
  listPendingWorkspaceJoins,
  type WorkspaceDirectoryItem,
} from "../api/workspaceDirectory";
import { useAuth } from "../auth/AuthContext";
import { normalizeLocale, setLocale, syncLocaleFromWorkspace } from "../i18n";
import { useLabsPreferences } from "../hooks/useLabsPreferences";
import { domainLabel } from "../lib/domainLabel";
import { inputTextError, textInputLimit, trimShortText } from "../lib/inputPolicy";
import { SEARCH_QUERY_MAX_LENGTH } from "../lib/searchLimits";
import { searchResultHref } from "../lib/searchResultHref";
import { trapTabFocus } from "../lib/trapTabFocus";
import { isEditorRole } from "../workspace/permissions";
import {
  useOptionalWorkspaceContext,
  useWorkspaceContext,
} from "../workspace/WorkspaceContext";
import { PanelVisibilityIcon } from "./PanelVisibilityIcon";
import { PrismGlyph } from "./PrismGlyph";
import { RouteLoading } from "./RouteLoading";
import { StatusBanner } from "./StatusBanner";
import { useExplicitInertRef } from "./useExplicitInertRef";
import { useScrollLeftGuardRef } from "./useScrollLeftGuardRef";

type ShellProps = {
  children: ReactNode;
};

type SidebarNavItem = {
  icon: LucideIcon;
  key: NavItemKey;
  preview?: boolean;
  to: string;
};

type CommandTarget = SidebarNavItem & {
  label: string;
  group: string;
};
type CommandSearchOption =
  | { kind: "command"; item: CommandTarget }
  | { kind: "preview"; result: RankedSearchResult };

type NavItemKey =
  | "dashboard"
  | "library"
  | "context"
  | "agent"
  | "chats"
  | "ask"
  | "review"
  | "lint"
  | "activity"
  | "search"
  | "settings";
type NavGroupKey = "knowledge" | "workflow" | "workspace";
type ShellLabels = {
  groups: Record<NavGroupKey, string>;
  nav: Record<NavItemKey, string>;
  searchFormAria: string;
  searchInputAria: string;
  searchPlaceholder: string;
  jumpTo: string;
  results: string;
  searching: string;
  noPreview: string;
  openSearch: string;
  closeNavigation: string;
  mobileNavAria: string;
  mobileNavListAria: string;
  searchFor: (query: string) => string;
  create: string;
  createTitle: string;
  newContextPage: string;
  newFolder: string;
  addLibraryItem: string;
  sidebarAria: string;
  sidebarCollapse: string;
  sidebarExpand: string;
  sidebarCollapseToggle: string;
  sidebarExpandToggle: string;
  previewBadge: string;
  previewDescription: string;
};

const SIDEBAR_NAV_GROUPS: Array<{ key: NavGroupKey; items: SidebarNavItem[] }> =
  [
    {
      key: "knowledge",
      items: [
        { icon: LayoutDashboard, key: "dashboard", to: "/dashboard" },
        { icon: Library, key: "library", to: "/library" },
        { icon: FileText, key: "context", to: "/context" },
        { icon: Bot, key: "agent", preview: true, to: "/agent" },
        { icon: MessagesSquare, key: "chats", to: "/chats" },
        { icon: MessageCircleQuestion, key: "ask", preview: true, to: "/ask" },
      ],
    },
    {
      key: "workflow",
      items: [
        { icon: ListChecks, key: "review", preview: true, to: "/review" },
        // The lint queue is the other half of the review workflow, and used to be
        // reachable only from three levels inside Settings.
        { icon: SpellCheck, key: "lint", preview: true, to: "/lint" },
        { icon: Activity, key: "activity", to: "/activity" },
        { icon: Search, key: "search", to: "/search" },
      ],
    },
    {
      key: "workspace",
      items: [{ icon: Settings, key: "settings", to: "/settings" }],
    },
  ];

const WORKSPACE_PURPOSE_PRESETS = [
  {
    key: "team_knowledge",
    label: "Team Knowledge Base",
    goal: "Organize team knowledge into source-grounded pages and cited answers.",
  },
  {
    key: "research",
    label: "Research & Analysis",
    goal: "Collect references, compare evidence, and produce cited research notes.",
  },
  {
    key: "project_docs",
    label: "Project Documentation",
    goal: "Maintain project decisions, specs, and implementation context in reusable wiki pages.",
  },
  {
    key: "personal_learning",
    label: "Personal / Learning Workspace",
    goal: "Capture learning notes and ask cited questions over saved materials.",
  },
];

const WORKSPACE_NAVIGATION_INTENT_STATE_KEY =
  "dtxwikiWorkspaceNavigationIntent";

type WorkspaceNavigationIntent =
  | {
      kind: "select";
      nonce: string;
      workspaceId: string;
    }
  | {
      goal: string;
      kind: "create";
      nonce: string;
      title: string;
    };

type WorkspaceNavigationIntentResult =
  | { status: "absent" }
  | { status: "malformed" }
  | { intent: WorkspaceNavigationIntent; status: "valid" };

function workspaceNavigationState(intent: WorkspaceNavigationIntent) {
  return { [WORKSPACE_NAVIGATION_INTENT_STATE_KEY]: intent };
}

function readWorkspaceNavigationIntent(
  state: unknown,
): WorkspaceNavigationIntentResult {
  if (!state || typeof state !== "object" || Array.isArray(state)) {
    return { status: "absent" };
  }
  if (
    !Object.prototype.hasOwnProperty.call(
      state,
      WORKSPACE_NAVIGATION_INTENT_STATE_KEY,
    )
  ) {
    return { status: "absent" };
  }
  const candidate = (state as Record<string, unknown>)[
    WORKSPACE_NAVIGATION_INTENT_STATE_KEY
  ];
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
    return { status: "malformed" };
  }
  const value = candidate as Record<string, unknown>;
  if (typeof value.nonce !== "string" || !value.nonce) {
    return { status: "malformed" };
  }
  if (
    value.kind === "select" &&
    typeof value.workspaceId === "string" &&
    value.workspaceId
  ) {
    return {
      intent: {
        kind: "select",
        nonce: value.nonce,
        workspaceId: value.workspaceId,
      },
      status: "valid",
    };
  }
  if (
    value.kind === "create" &&
    typeof value.title === "string" &&
    value.title.trim() &&
    typeof value.goal === "string"
  ) {
    return {
      intent: {
        goal: value.goal,
        kind: "create",
        nonce: value.nonce,
        title: value.title.trim(),
      },
      status: "valid",
    };
  }
  return { status: "malformed" };
}

function withoutWorkspaceNavigationIntent(state: unknown): unknown {
  if (!state || typeof state !== "object" || Array.isArray(state)) {
    return null;
  }
  const nextState = { ...(state as Record<string, unknown>) };
  delete nextState[WORKSPACE_NAVIGATION_INTENT_STATE_KEY];
  return Object.keys(nextState).length > 0 ? nextState : null;
}

function shellLabelsFromT(t: TFunction<"shell">): ShellLabels {
  return {
    groups: {
      knowledge: t("groups.knowledge", { defaultValue: "Knowledge" }),
      workflow: t("groups.workflow", { defaultValue: "Workflow" }),
      workspace: t("groups.workspace", { defaultValue: "Workspace" }),
    },
    nav: {
      dashboard: t("nav.dashboard", { defaultValue: "Dashboard" }),
      library: t("nav.library", { defaultValue: "Library" }),
      context: t("nav.context", { defaultValue: "Context" }),
      agent: t("nav.agent", { defaultValue: "Agent" }),
      chats: t("nav.chats", { defaultValue: "Chats" }),
      ask: t("nav.ask", { defaultValue: "Ask" }),
      review: t("nav.review", { defaultValue: "Review queue" }),
      lint: t("nav.lint", { defaultValue: "Lint queue" }),
      activity: t("nav.activity", { defaultValue: "Activity" }),
      search: t("nav.search", { defaultValue: "Search" }),
      settings: t("nav.settings", { defaultValue: "Settings" }),
    },
    searchFormAria: t("search.formAria", { defaultValue: "Workspace search" }),
    searchInputAria: t("search.inputAria", {
      defaultValue: "Search workspace",
    }),
    searchPlaceholder: t("search.placeholder", {
      defaultValue: "Search or jump to a workspace area",
    }),
    jumpTo: t("search.jumpTo", { defaultValue: "Jump to" }),
    results: t("search.results", { defaultValue: "Results" }),
    searching: t("search.searching", { defaultValue: "Searching…" }),
    noPreview: t("search.noPreview", { defaultValue: "No preview results" }),
    openSearch: t("search.openSearch", { defaultValue: "Open Search" }),
    closeNavigation: t("topbar.closeNavigation", {
      defaultValue: "Close navigation",
    }),
    mobileNavAria: t("mobileNav.aria", { defaultValue: "Mobile navigation" }),
    mobileNavListAria: t("mobileNav.listAria", { defaultValue: "Mobile" }),
    searchFor: (query: string) =>
      t("search.searchFor", {
        defaultValue: `Search "${query}"`,
        query,
      }),
    create: t("create.label", { defaultValue: "Create" }),
    createTitle: t("create.title", {
      defaultValue: "Create workspace content",
    }),
    newContextPage: t("create.newContextPage", {
      defaultValue: "New Context page",
    }),
    newFolder: t("create.newFolder", { defaultValue: "New folder" }),
    addLibraryItem: t("create.addLibraryItem", {
      defaultValue: "Add first Library item",
    }),
    sidebarAria: t("sidebar.aria", { defaultValue: "Sidebar" }),
    sidebarCollapse: t("sidebar.collapse", { defaultValue: "Collapse" }),
    sidebarExpand: t("sidebar.expand", { defaultValue: "Expand" }),
    sidebarCollapseToggle: t("sidebar.collapseSidebar", {
      defaultValue: "Collapse sidebar",
    }),
    sidebarExpandToggle: t("sidebar.expandSidebar", {
      defaultValue: "Expand sidebar",
    }),
    previewBadge: t("preview.badge", { defaultValue: "Preview" }),
    previewDescription: t("preview.description", {
      defaultValue: "Preview: this area is still being refined.",
    }),
  };
}

function navGroupsForLabels(labels: ShellLabels) {
  return SIDEBAR_NAV_GROUPS.map((group) => ({
    label: labels.groups[group.key],
    items: group.items.map((item) => ({
      ...item,
      label: labels.nav[item.key],
    })),
  }));
}

function readStoredBoolean(key: string, fallback: boolean) {
  if (typeof window === "undefined") {
    return fallback;
  }
  const value = window.localStorage.getItem(key);
  if (value === null) {
    return fallback;
  }
  return value === "true";
}

function writeStoredBoolean(key: string, value: boolean) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(key, String(value));
}

export function Shell({ children }: ShellProps) {
  const { t, i18n } = useTranslation("shell");
  const {
    capabilitiesChecked,
    passwordRecoveryMode,
    refreshCapabilities,
    status,
    user,
  } = useAuth();
  const location = useLocation();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() =>
    readStoredBoolean("dtxwiki:sidebar-collapsed", false),
  );
  const [workspaceRouteVersion, setWorkspaceRouteVersion] = useState(0);
  const mobileNavButtonRef = useRef<HTMLButtonElement>(null);
  const restoreMobileNavFocusRef = useRef(false);
  // Applied to every sibling the mobile nav drawer overlays (skip-link,
  // topbar, main) so none of them stays operable/announced behind it while
  // it's open. The same callback ref works on all three: React invokes it
  // once per mounted node, independently. <Sidebar> can't take this ref
  // directly (separate component), so it gets its own via an inertActive prop.
  const backgroundInertRef = useExplicitInertRef<HTMLElement>(null, mobileNavOpen);
  // Guards the shell's layout containers against a stray horizontal scroll
  // offset -- the Galaxy Fold "everything shifts left" failure (260911 Fold
  // report). It goes on `.app-shell` itself, not on `<main>`: a `scroll`
  // event targeting `.app-shell` never reaches a listener on one of its own
  // descendants. On a browser where the CSS clip path is unavailable (including
  // legacy Chromium without dynamic viewport units), `.app-shell` remains a
  // scroll container and is exactly the element that can hold the offset.
  // Full reasoning lives on the hook.
  const shellScrollGuardRef = useScrollLeftGuardRef<HTMLDivElement>();
  const isAuthenticated = status === "authenticated" && Boolean(user);
  const needsCapabilitiesForNotice =
    isAuthenticated && user?.email_verified_at === null;
  // M-14 (260908 UI audit): Account already shows its own "Email not
  // verified · Verify email" control inline, in context with the rest of the
  // account's connected-tools/security surface. Repeating the same CTA in the
  // global banner right above it doubled the ask on the one screen where it's
  // least needed.
  const showEmailVerificationBanner =
    needsCapabilitiesForNotice && location.pathname !== "/account";
  // Instance capabilities (which gate the email-verification banner below)
  // are only preloaded on auth-bootstrap routes (login/signup/etc, see
  // AppFrame). An authenticated user with an unverified email can land on
  // any other route, so fetch capabilities here, on demand, the first time
  // that combination shows up -- otherwise passwordRecoveryMode would stay
  // stuck at its "disabled" default and the banner could never appear.
  useEffect(() => {
    if (needsCapabilitiesForNotice && !capabilitiesChecked) {
      void refreshCapabilities();
    }
  }, [capabilitiesChecked, needsCapabilitiesForNotice, refreshCapabilities]);
  const shellClassName = isAuthenticated
    ? "app-shell"
    : "app-shell app-shell-public";
  const shellLabels = useMemo(() => shellLabelsFromT(t), [t]);
  const workspaceContext = useOptionalWorkspaceContext();
  const { preferences: labsPreferences } = useLabsPreferences();
  const selectedRole = workspaceContext?.selectedWorkspace?.role;
  const canWriteSelectedWorkspace = isEditorRole(selectedRole);
  // /chats is editor-gated end to end (backend + RequireEditor). Keep this
  // fail-closed until the selected role resolves so a Viewer never sees a
  // transient write-only destination during workspace hydration.
  const hideChatsNav = !canWriteSelectedWorkspace;
  const activeLocale = normalizeLocale(i18n.language);
  const nextLocale = activeLocale === "ko" ? "en" : "ko";
  const nextLocaleLabel = t(`topbar.language.${nextLocale}`);
  const languageToggleLabel = t("topbar.switchLanguage", {
    language: nextLocaleLabel,
  });
  const navGroups = useMemo(() => {
    const groups = navGroupsForLabels(shellLabels);
    return groups.map((group) => ({
      ...group,
      items: group.items.filter((item) =>
        (item.key !== "chats" || !hideChatsNav) &&
        (item.key !== "agent" || labsPreferences.agent) &&
        (item.key !== "ask" || labsPreferences.ask),
      ),
    }));
  }, [hideChatsNav, labsPreferences.agent, labsPreferences.ask, shellLabels]);

  function closeMobileNav() {
    restoreMobileNavFocusRef.current = true;
    setMobileNavOpen(false);
  }

  function closeMobileNavForNavigation() {
    restoreMobileNavFocusRef.current = false;
    setMobileNavOpen(false);
  }

  // The trigger's topbar is inert while the drawer is open. Restore focus
  // only after the closed render removes that inert state; focusing it during
  // the close event is rejected by browsers and leaves focus on <body>.
  useEffect(() => {
    if (mobileNavOpen || !restoreMobileNavFocusRef.current) {
      return;
    }
    restoreMobileNavFocusRef.current = false;
    const trigger = mobileNavButtonRef.current;
    if (trigger?.isConnected && !trigger.closest("[inert]")) {
      trigger.focus();
    }
  }, [mobileNavOpen]);

  useEffect(() => {
    writeStoredBoolean("dtxwiki:sidebar-collapsed", sidebarCollapsed);
  }, [sidebarCollapsed]);

  useEffect(() => {
    if (!mobileNavOpen) {
      return;
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        closeMobileNav();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [mobileNavOpen]);

  return (
    <div
      className={shellClassName}
      data-sidebar-collapsed={
        isAuthenticated && sidebarCollapsed ? "true" : "false"
      }
      ref={shellScrollGuardRef}
    >
      <a className="skip-link" href="#main-content" ref={backgroundInertRef}>
        {t("topbar.skip", { defaultValue: "Skip to main content" })}
      </a>
      <header className="topbar" ref={backgroundInertRef}>
        <div className="topbar-brand">
          {isAuthenticated ? (
            <button
              aria-expanded={mobileNavOpen}
              aria-label={t("topbar.openNavigation", {
                defaultValue: "Open navigation",
              })}
              className="icon-button mobile-nav-toggle"
              ref={mobileNavButtonRef}
              onClick={() => setMobileNavOpen(true)}
              type="button"
            >
              <Menu size={16} aria-hidden="true" />
            </button>
          ) : null}
          {isAuthenticated ? (
            // The current workspace's dashboard, not a plain "home" -- there
            // is no workspace-agnostic surface to send an authenticated
            // reader to. Signed out, the glyph+name stay plain: /dashboard
            // would only bounce them straight to /login.
            <Link className="topbar-brand-link" to="/dashboard">
              <PrismGlyph />
              <span className="brand-name">dtxWiki</span>
            </Link>
          ) : (
            <>
              <PrismGlyph />
              <span className="brand-name">dtxWiki</span>
            </>
          )}
          {isAuthenticated ? (
            <WorkspaceControls
              onWorkspaceLocaleChange={syncLocaleFromWorkspace}
              onWorkspaceRouteRefresh={() =>
                setWorkspaceRouteVersion((current) => current + 1)
              }
            />
          ) : null}
        </div>
        {isAuthenticated ? (
          <CommandSearchBox labels={shellLabels} navGroups={navGroups} />
        ) : (
          /* The topbar is a grid and this is its MIDDLE child, so it has to
             carry the same class the real search box does. Signed in,
             `.topbar-search` is `display: none` below 1024px, leaving two
             in-flow children for the two-track mobile rule. Signed out, this
             placeholder had no class, stayed `display: block`, and kept
             generating a column -- so `.topbar-actions` wrapped onto an
             implicit SECOND ROW and hung 9-10px below the 56px bar on every
             signed-out screen. Measured at 320/360/390/414/640/768/900/1024:
             `grid-template-rows` read "21px 44px" signed out against "55px"
             signed in. Do not drop this class to "simplify" the placeholder.

             `.topbar-search` is only hidden below 1024px (02-shell.css:677),
             so above that this placeholder painted as a live 907x34px search
             box -- border, background, hover state, `pointer-events: auto` --
             with 0 children and 0 text, on every signed-out desktop screen
             (260912 review, measured on /login at 1280x800). The second class
             below strips just the paint, so the grid track and the <1024px
             hide rule -- both load-bearing, see above -- are inherited rather
             than duplicated onto a second selector that could drift from the
             first. */
          <div
            className="topbar-search topbar-search--placeholder"
            aria-hidden="true"
          />
        )}
        <div className="topbar-actions">
          {isAuthenticated ? (
            <>
              <NavLink
                aria-label={t("topbar.openSearch", {
                  defaultValue: "Open Search",
                })}
                className="icon-button mobile-search-link"
                to="/search"
                title={t("topbar.searchTitle", { defaultValue: "Search" })}
              >
                <Search size={16} aria-hidden="true" />
              </NavLink>
              <NavLink
                aria-label={t("topbar.settings", { defaultValue: "Settings" })}
                className="icon-button"
                to="/settings"
                title={t("topbar.settings", { defaultValue: "Settings" })}
              >
                <Settings size={16} aria-hidden="true" />
              </NavLink>
              <NavLink
                aria-label={t("topbar.myPage", { defaultValue: "My page" })}
                className="icon-button"
                to="/account"
                title={t("topbar.myPage", { defaultValue: "My page" })}
              >
                <UserCircle size={16} aria-hidden="true" />
              </NavLink>
              {/* Signed-in users had no way to change their own UI language:
                  this control only existed while logged out, and the only other
                  language field is the workspace-wide default. */}
              <button
                aria-label={languageToggleLabel}
                className="icon-button text-button language-toggle"
                onClick={() => setLocale(nextLocale)}
                title={languageToggleLabel}
                type="button"
              >
                <Languages size={16} aria-hidden="true" />
                <span>{nextLocaleLabel}</span>
              </button>
              {canWriteSelectedWorkspace ? (
                <CreateMenu labels={shellLabels} />
              ) : null}
            </>
          ) : (
            <>
              <button
                aria-label={languageToggleLabel}
                className="icon-button text-button language-toggle"
                onClick={() => setLocale(nextLocale)}
                title={languageToggleLabel}
                type="button"
              >
                <Languages size={16} aria-hidden="true" />
                <span>{nextLocaleLabel}</span>
              </button>
              {location.pathname === "/login" ? null : (
                <>
                  <Link className="icon-button text-button" to="/login">
                    {t("topbar.signIn", { defaultValue: "Sign in" })}
                  </Link>
                  <Link
                    className="icon-button text-button primary"
                    to="/login?mode=signup"
                  >
                    {t("topbar.signUp", { defaultValue: "Sign up" })}
                  </Link>
                </>
              )}
            </>
          )}
        </div>
      </header>
      {isAuthenticated && mobileNavOpen ? (
        <MobileNavDrawer
          labels={shellLabels}
          navGroups={navGroups}
          onClose={closeMobileNav}
          onNavigate={closeMobileNavForNavigation}
        />
      ) : null}
      {isAuthenticated ? (
        <Sidebar
          collapsed={sidebarCollapsed}
          inertActive={mobileNavOpen}
          labels={shellLabels}
          navGroups={navGroups}
          onToggleCollapsed={() => setSidebarCollapsed((current) => !current)}
        />
      ) : null}
      <main
        className={
          showEmailVerificationBanner && passwordRecoveryMode === "email"
            ? "main-surface main-surface--account-notice"
            : "main-surface"
        }
        id="main-content"
        key={workspaceRouteVersion}
        ref={backgroundInertRef}
        tabIndex={-1}
      >
        {showEmailVerificationBanner && passwordRecoveryMode === "email" ? (
          <div className="shell-account-notice">
            <StatusBanner
              tone="info"
              action={
                <Link className="icon-button text-button" to="/account">
                  {t("emailVerification.action", {
                    defaultValue: "Verify email",
                  })}
                </Link>
              }
            >
              {t("emailVerification.notice", {
                defaultValue:
                  "Verify your email to enable self-service account recovery.",
              })}
            </StatusBanner>
          </div>
        ) : null}
        {isAuthenticated ? (
          <WorkspaceRouteBoundary>{children}</WorkspaceRouteBoundary>
        ) : (
          children
        )}
      </main>
    </div>
  );
}

function WorkspaceRouteBoundary({ children }: { children: ReactNode }) {
  const { t } = useTranslation("shell");
  const location = useLocation();
  const {
    error,
    initialized,
    loading,
    personalWorkspaceId,
    refreshWorkspaces,
    selectedWorkspace,
    selectWorkspaceId,
    workspaces,
  } = useWorkspaceContext();
  const normalizedPathname = location.pathname.replace(/\/+$/, "") || "/";
  const workspaceIndependentRoute =
    normalizedPathname === "/account" ||
    normalizedPathname === "/account/password" ||
    normalizedPathname === "/workspaces/find";

  if (workspaceIndependentRoute || selectedWorkspace) {
    return children;
  }
  if (loading || !initialized) {
    return (
      <RouteLoading
        label={t("workspaceResolution.loading", {
          defaultValue: "Loading workspaces",
        })}
      />
    );
  }
  if (error) {
    return (
      <section
        aria-labelledby="workspace-resolution-error-title"
        className="workspace-resolution-card"
      >
        <p className="eyebrow">
          {t("workspaceResolution.eyebrow", {
            defaultValue: "Workspace access",
          })}
        </p>
        <h1 id="workspace-resolution-error-title">
          {t("workspaceResolution.loadErrorTitle", {
            defaultValue: "Could not load your workspaces",
          })}
        </h1>
        <StatusBanner tone="warning">{error}</StatusBanner>
        <button
          className="icon-button primary"
          data-testid="workspace-resolution-retry"
          onClick={() => void refreshWorkspaces()}
          type="button"
        >
          {t("workspaceResolution.retry", { defaultValue: "Retry" })}
        </button>
      </section>
    );
  }
  if (workspaces.length === 0) {
    return (
      <section
        aria-labelledby="workspace-resolution-empty-title"
        className="workspace-resolution-card"
      >
        <p className="eyebrow">
          {t("workspaceResolution.eyebrow", {
            defaultValue: "Workspace access",
          })}
        </p>
        <h1 id="workspace-resolution-empty-title">
          {t("workspaceResolution.emptyTitle", {
            defaultValue: "Find or create a workspace",
          })}
        </h1>
        <p>
          {t("workspaceResolution.emptyDescription", {
            defaultValue:
              "This account has no workspace membership. Find a workspace to join, or create one from the workspace switcher.",
          })}
        </p>
        <p className="meta-line">
          {t("workspaceResolution.emptySupport", {
            defaultValue:
              "If access was just granted, retry to refresh your workspace list.",
          })}
        </p>
        <div className="workspace-resolution-actions">
          <button
            className="icon-button"
            onClick={() => void refreshWorkspaces()}
            type="button"
          >
            {t("workspaceResolution.retry", { defaultValue: "Retry" })}
          </button>
          <Link className="icon-button primary" to="/workspaces/find">
            {t("workspaceResolution.find", {
              defaultValue: "Find workspaces",
            })}
          </Link>
        </div>
      </section>
    );
  }

  function chooseWorkspace(workspace: WorkspaceSummary) {
    selectWorkspaceId(workspace.id);
    syncLocaleFromWorkspace(workspace.default_language);
  }

  return (
    <section
      aria-labelledby="workspace-resolution-picker-title"
      className="workspace-resolution-card"
    >
      <p className="eyebrow">
        {t("workspaceResolution.eyebrow", {
          defaultValue: "Workspace access",
        })}
      </p>
      <h1 id="workspace-resolution-picker-title">
        {t("workspaceResolution.chooseTitle", {
          defaultValue: "Choose a workspace",
        })}
      </h1>
      <p>
        {t("workspaceResolution.chooseDescription", {
          defaultValue:
            "This account has more than one membership and no saved workspace preference. Choose where to continue.",
        })}
      </p>
      <ul className="workspace-resolution-list">
        {workspaces.map((workspace) => {
          const isPersonal = workspace.id === personalWorkspaceId;
          return (
            <li key={workspace.id}>
              <button
                className="workspace-resolution-option"
                onClick={() => chooseWorkspace(workspace)}
                type="button"
              >
                <span className="workspace-initials">
                  {workspaceInitials(workspace.title)}
                </span>
                <span>
                  <strong>{workspace.title}</strong>
                  <small>
                    {t(`status.${workspace.role ?? "member"}`, {
                      defaultValue: workspace.role ?? "member",
                    })}
                    {" · "}
                    {t(`status.${workspace.visibility}`, {
                      defaultValue: workspace.visibility,
                    })}
                    {!isPersonal && workspace.owner_display_name ? (
                      <>
                        {" · "}
                        {t("workspace.ownedBy", {
                          defaultValue: "Owned by {{name}}",
                          name: workspace.owner_display_name,
                        })}
                      </>
                    ) : null}
                  </small>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function CommandSearchBox({
  labels,
  navGroups,
}: {
  labels: ShellLabels;
  navGroups: ReturnType<typeof navGroupsForLabels>;
}) {
  const navigate = useNavigate();
  const { t: tSearch } = useTranslation("search");
  const { selectedId } = useWorkspaceContext();
  const containerRef = useRef<HTMLFormElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const isApple = useMemo(() => isApplePlatform(), []);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(0);
  const [previewResults, setPreviewResults] = useState<RankedSearchResult[]>(
    [],
  );
  const [previewLoading, setPreviewLoading] = useState(false);
  const trimmedQuery = query.trim();
  const commandItems = useMemo(() => {
    const commandTargets: CommandTarget[] = navGroups.flatMap((group) =>
      group.items.map((item) => ({ ...item, group: group.label })),
    );
    const normalized = trimmedQuery.toLowerCase();
    const matches = commandTargets.filter((item) =>
      [item.label, item.group, item.to]
        .join(" ")
        .toLowerCase()
        .includes(normalized),
    );
    return [
      ...matches,
      {
        icon: Search,
        key: "search" as const,
        label: trimmedQuery
          ? labels.searchFor(trimmedQuery)
          : labels.openSearch,
        to: trimmedQuery
          ? `/search?q=${encodeURIComponent(trimmedQuery)}`
          : "/search",
        group: labels.nav.search,
      },
    ];
  }, [labels, navGroups, trimmedQuery]);
  const previewOptions = useMemo(
    () =>
      previewResults
        .filter((result) => Boolean(searchResultHref(result)))
        .map((result) => ({ kind: "preview" as const, result })),
    [previewResults],
  );
  const selectableOptions = useMemo<CommandSearchOption[]>(
    () => [
      ...previewOptions,
      ...commandItems.map((item) => ({ kind: "command" as const, item })),
    ],
    [commandItems, previewOptions],
  );

  useEffect(() => {
    setHighlightedIndex(0);
  }, [query]);

  useEffect(() => {
    setHighlightedIndex((current) =>
      Math.min(current, Math.max(selectableOptions.length - 1, 0)),
    );
  }, [selectableOptions.length]);

  useEffect(() => {
    if (!open || trimmedQuery.length < 2 || !selectedId) {
      setPreviewResults([]);
      setPreviewLoading(false);
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setPreviewLoading(true);
      searchWorkspace(
        selectedId,
        { query: trimmedQuery, scope: "all", limit: 5 },
        { signal: controller.signal },
      )
        .then((payload) => {
          if (!controller.signal.aborted) {
            setPreviewResults(payload.ranked_results.slice(0, 5));
          }
        })
        .catch((caught) => {
          if (
            controller.signal.aborted ||
            (caught instanceof DOMException && caught.name === "AbortError")
          ) {
            return;
          }
          setPreviewResults([]);
        })
        .finally(() => {
          if (!controller.signal.aborted) {
            setPreviewLoading(false);
          }
        });
    }, 200);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [open, selectedId, trimmedQuery]);

  useEffect(() => {
    function handleGlobalKeyDown(event: globalThis.KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        // Editable surfaces own Ctrl/Cmd+K (e.g. the page editor's link
        // shortcut), so never steal focus from them.
        if (event.defaultPrevented || isEditableEventTarget(event.target)) {
          return;
        }
        event.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
        setOpen(true);
      }
      if (event.key === "Escape") {
        setOpen(false);
      }
    }
    window.addEventListener("keydown", handleGlobalKeyDown);
    return () => window.removeEventListener("keydown", handleGlobalKeyDown);
  }, []);

  useEffect(() => {
    if (!open) {
      return;
    }
    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && containerRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!trimmedQuery) {
      activate(commandItems.at(-1));
      return;
    }
    if (open && selectableOptions.length > 0) {
      activateSearchOption(
        selectableOptions[highlightedIndex] ?? selectableOptions.at(-1),
      );
      return;
    }
    submitToSearch();
  }

  function submitToSearch() {
    navigate(`/search?q=${encodeURIComponent(trimmedQuery)}`);
    setOpen(false);
    setQuery("");
    setPreviewResults([]);
  }

  function activate(item: CommandTarget | undefined) {
    if (!item) {
      return;
    }
    navigate(item.to);
    setOpen(false);
    setQuery("");
    setPreviewResults([]);
  }

  function openPreviewResult(result: RankedSearchResult) {
    const href = searchResultHref(result);
    if (!href) {
      return;
    }
    navigate(href);
    setOpen(false);
    setQuery("");
    setPreviewResults([]);
  }

  function activateSearchOption(option: CommandSearchOption | undefined) {
    if (!option) {
      submitToSearch();
      return;
    }
    if (option.kind === "preview") {
      openPreviewResult(option.result);
      return;
    }
    activate(option.item);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setHighlightedIndex((current) =>
        Math.min(current + 1, selectableOptions.length - 1),
      );
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlightedIndex((current) => Math.max(current - 1, 0));
    }
    if (event.key === "Escape") {
      setOpen(false);
    }
  }
  const activeOptionId = open
    ? commandSearchOptionId(
        selectableOptions[highlightedIndex],
        highlightedIndex,
      )
    : undefined;

  return (
    <form
      aria-label={labels.searchFormAria}
      className={`topbar-search command-search ${open ? "is-open" : ""}`}
      onSubmit={submit}
      ref={containerRef}
      role="search"
    >
      <Search size={16} aria-hidden="true" />
      <input
        aria-autocomplete="list"
        aria-activedescendant={activeOptionId}
        aria-controls="command-search-results"
        aria-expanded={open}
        aria-label={labels.searchInputAria}
        autoComplete="off"
        ref={inputRef}
        maxLength={SEARCH_QUERY_MAX_LENGTH}
        onChange={(event) =>
          setQuery(event.target.value.slice(0, SEARCH_QUERY_MAX_LENGTH))
        }
        onFocus={() => setOpen(true)}
        onKeyDown={handleKeyDown}
        placeholder={labels.searchPlaceholder}
        role="combobox"
        type="search"
        value={query}
      />
      <kbd>
        {isApple ? (
          <>
            <Command size={11} aria-hidden="true" />K
          </>
        ) : (
          "Ctrl K"
        )}
      </kbd>
      {open ? (
        <div
          className="command-dropdown"
          id="command-search-results"
          role="listbox"
        >
          {trimmedQuery ? (
            <>
              <div className="command-section-label">{labels.results}</div>
              {previewLoading ? (
                <div className="command-empty">{labels.searching}</div>
              ) : previewResults.length > 0 ? (
                previewResults.map((result) => {
                  const href = searchResultHref(result);
                  const optionIndex = selectableOptions.findIndex(
                    (option) =>
                      option.kind === "preview" && option.result === result,
                  );
                  const active = optionIndex === highlightedIndex;
                  return (
                    <button
                      aria-disabled={href ? undefined : "true"}
                      aria-selected={active}
                      className={`command-item command-preview-item ${active ? "active" : ""}`}
                      disabled={!href}
                      id={
                        optionIndex >= 0
                          ? commandSearchOptionId(
                              selectableOptions[optionIndex],
                              optionIndex,
                            )
                          : undefined
                      }
                      key={`${result.type}-${result.id}`}
                      onMouseDown={(event) => event.preventDefault()}
                      onMouseEnter={() => {
                        if (optionIndex >= 0) {
                          setHighlightedIndex(optionIndex);
                        }
                      }}
                      onClick={() => openPreviewResult(result)}
                      role="option"
                      type="button"
                    >
                      <FileText size={15} aria-hidden="true" />
                      <span>
                        <strong>{result.title}</strong>
                        <small>{result.snippet}</small>
                      </span>
                      <small>
                        {domainLabel("search", "types", result.type, tSearch)}
                      </small>
                    </button>
                  );
                })
              ) : (
                <div className="command-empty">{labels.noPreview}</div>
              )}
            </>
          ) : null}
          <div className="command-section-label">{labels.jumpTo}</div>
          {commandItems.map((item, index) => {
            const Icon = item.icon;
            const optionIndex = previewOptions.length + index;
            const active = optionIndex === highlightedIndex;
            return (
              <button
                aria-selected={active}
                className={`command-item ${active ? "active" : ""}`}
                id={commandSearchOptionId(
                  selectableOptions[optionIndex],
                  optionIndex,
                )}
                key={`${item.group}-${item.to}-${item.label}`}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setHighlightedIndex(optionIndex)}
                onClick={() => activate(item)}
                role="option"
                type="button"
              >
                <Icon size={15} aria-hidden="true" />
                <span>{item.label}</span>
                <small>{item.group}</small>
              </button>
            );
          })}
        </div>
      ) : null}
    </form>
  );
}

function isEditableEventTarget(target: EventTarget | null) {
  if (!(target instanceof Element)) {
    return false;
  }
  return Boolean(
    target.closest(
      'input, textarea, select, [contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"], .cm-content',
    ),
  );
}

function isApplePlatform() {
  if (typeof navigator === "undefined") {
    return false;
  }
  const platform =
    (navigator as Navigator & { userAgentData?: { platform?: string } })
      .userAgentData?.platform ??
    navigator.platform ??
    "";
  return /mac|iphone|ipad|ipod/i.test(platform);
}

function commandSearchOptionId(
  option: CommandSearchOption | undefined,
  index: number,
) {
  return option ? `command-search-option-${index}` : undefined;
}

const CREATE_MENU_TRIGGER_ID = "shell-create-menu-trigger";
const CREATE_MENU_ID = "shell-create-menu";

function CreateMenu({ labels }: { labels: ShellLabels }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const initialFocusRef = useRef<"first" | "last" | null>(null);

  function menuItems() {
    return Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ??
        [],
    );
  }

  function openMenu(initialFocus: "first" | "last") {
    initialFocusRef.current = initialFocus;
    setOpen(true);
  }

  function closeMenu(restoreTriggerFocus = false) {
    initialFocusRef.current = null;
    setOpen(false);
    if (restoreTriggerFocus) {
      triggerRef.current?.focus();
    }
  }

  useEffect(() => {
    if (!open || !initialFocusRef.current) {
      return;
    }
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ??
        [],
    );
    const target =
      initialFocusRef.current === "last" ? items.at(-1) : items.at(0);
    initialFocusRef.current = null;
    target?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    function handlePointerDown(event: PointerEvent) {
      if (!menuRef.current?.contains(event.target as Node)) {
        initialFocusRef.current = null;
        setOpen(false);
      }
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        initialFocusRef.current = null;
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  function handleTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Tab" && open) {
      closeMenu();
      return;
    }
    if (
      event.key === "Enter" ||
      event.key === " " ||
      event.key === "ArrowDown" ||
      event.key === "ArrowUp"
    ) {
      event.preventDefault();
      openMenu(event.key === "ArrowUp" ? "last" : "first");
    }
  }

  function handleMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Tab") {
      closeMenu();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeMenu(true);
      return;
    }

    const items = menuItems();
    if (items.length === 0) {
      return;
    }
    const activeIndex = items.indexOf(document.activeElement as HTMLElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") {
      nextIndex = activeIndex < 0 ? 0 : (activeIndex + 1) % items.length;
    } else if (event.key === "ArrowUp") {
      nextIndex =
        activeIndex < 0
          ? items.length - 1
          : (activeIndex - 1 + items.length) % items.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = items.length - 1;
    }

    if (nextIndex !== null) {
      event.preventDefault();
      items[nextIndex]?.focus();
    }
  }

  return (
    <div className="create-menu" ref={menuRef}>
      <button
        aria-controls={CREATE_MENU_ID}
        aria-haspopup="menu"
        aria-expanded={open}
        className="icon-button text-button primary create-menu-trigger"
        id={CREATE_MENU_TRIGGER_ID}
        onClick={() => (open ? closeMenu() : openMenu("first"))}
        onKeyDown={handleTriggerKeyDown}
        ref={triggerRef}
        title={labels.createTitle}
        type="button"
      >
        <Plus size={16} aria-hidden="true" />
        <span>{labels.create}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {open ? (
        <div
          aria-labelledby={CREATE_MENU_TRIGGER_ID}
          className="create-menu-panel"
          id={CREATE_MENU_ID}
          onKeyDown={handleMenuKeyDown}
          role="menu"
          tabIndex={-1}
        >
          <Link
            className="create-menu-item"
            onClick={() => closeMenu()}
            role="menuitem"
            tabIndex={-1}
            to="/context/new?from=context"
          >
            <FileText size={15} aria-hidden="true" />
            <span>{labels.newContextPage}</span>
          </Link>
          <Link
            className="create-menu-item"
            onClick={() => closeMenu()}
            role="menuitem"
            tabIndex={-1}
            to="/context?newFolder=1"
          >
            <FolderPlus size={15} aria-hidden="true" />
            <span>{labels.newFolder}</span>
          </Link>
          <Link
            className="create-menu-item"
            onClick={() => closeMenu()}
            role="menuitem"
            tabIndex={-1}
            to="/setup"
          >
            <Library size={15} aria-hidden="true" />
            <span>{labels.addLibraryItem}</span>
          </Link>
        </div>
      ) : null}
    </div>
  );
}

function WorkspaceControls({
  onWorkspaceLocaleChange,
  onWorkspaceRouteRefresh,
}: {
  onWorkspaceLocaleChange: (locale: string | null | undefined) => void;
  onWorkspaceRouteRefresh: () => void;
}) {
  const { t } = useTranslation("shell");
  const workspaceContext = useWorkspaceContext();
  const location = useLocation();
  const navigate = useNavigate();
  const {
    addWorkspace,
    error: workspaceError,
    initialized,
    loading,
    refreshWorkspaces,
    selectWorkspaceId,
    defaultWorkspaceId,
    personalWorkspaceId,
    selectedId,
    selectedWorkspace,
    workspaces,
  } = workspaceContext;
  const [open, setOpen] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [newWorkspaceTitle, setNewWorkspaceTitle] = useState("");
  const [workspacePurpose, setWorkspacePurpose] = useState(
    WORKSPACE_PURPOSE_PRESETS[0].key,
  );
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [filterQuery, setFilterQuery] = useState("");
  const [pendingJoinWorkspaces, setPendingJoinWorkspaces] = useState<
    WorkspaceDirectoryItem[]
  >([]);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mountedRef = useRef(true);
  const navigationIntentSequenceRef = useRef(0);
  const consumedNavigationIntentRef = useRef<string | null>(null);
  const createRequestGenerationRef = useRef(0);
  const visibleStatusMessage = statusMessage ?? workspaceError;

  function nextNavigationIntentNonce() {
    navigationIntentSequenceRef.current += 1;
    return `${location.key}:${navigationIntentSequenceRef.current}`;
  }

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    void refreshWorkspaces();
  }, [refreshWorkspaces]);

  // Dismiss the switcher on outside pointerdown / Escape — same behavior as the
  // command search popover (item 14-a).
  useEffect(() => {
    if (!open) {
      return;
    }
    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && containerRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  useEffect(() => {
    onWorkspaceLocaleChange(selectedWorkspace?.default_language);
  }, [onWorkspaceLocaleChange, selectedWorkspace?.default_language]);

  useEffect(() => {
    const result = readWorkspaceNavigationIntent(location.state);
    if (result.status === "absent") {
      consumedNavigationIntentRef.current = null;
      return;
    }
    if (result.status === "malformed") {
      const malformedIntentKey = `${location.key}:malformed`;
      if (consumedNavigationIntentRef.current === malformedIntentKey) {
        return;
      }
      consumedNavigationIntentRef.current = malformedIntentKey;
      navigate(`${location.pathname}${location.search}${location.hash}`, {
        replace: true,
        state: withoutWorkspaceNavigationIntent(location.state),
      });
      return;
    }
    // Keep the committed intent in history until the authoritative workspace
    // list has settled. Consuming it against an empty pre-hydration list would
    // discard a valid selection, while creating concurrently with hydration
    // lets the late list response overwrite addWorkspace's selection.
    if (!initialized || loading) {
      return;
    }
    const { intent } = result;
    const intentKey = `${location.key}:${intent.nonce}`;
    if (consumedNavigationIntentRef.current === intentKey) {
      return;
    }
    consumedNavigationIntentRef.current = intentKey;

    // Reaching this effect proves React Router committed the navigation. Strip
    // the private marker immediately so refresh/back and StrictMode effect
    // replay cannot repeat a workspace creation request.
    navigate(`${location.pathname}${location.search}${location.hash}`, {
      replace: true,
      state: withoutWorkspaceNavigationIntent(location.state),
    });
    const requestGeneration = ++createRequestGenerationRef.current;

    if (intent.kind === "select") {
      const workspace =
        workspaces.find((candidate) => candidate.id === intent.workspaceId) ??
        null;
      if (!workspace) {
        setStatusMessage(
          t("workspace.chooseRequired", {
            defaultValue: "Choose a workspace before continuing.",
          }),
        );
        return;
      }
      setStatusMessage(null);
      pushWorkspaceRecent(intent.workspaceId);
      selectWorkspaceId(intent.workspaceId);
      onWorkspaceLocaleChange(workspace?.default_language);
      setFilterQuery("");
      setOpen(false);
      onWorkspaceRouteRefresh();
      return;
    }

    setStatusMessage(null);
    void createWorkspace({ title: intent.title, goal: intent.goal })
      .then((created) => {
        if (
          !mountedRef.current ||
          requestGeneration !== createRequestGenerationRef.current
        ) {
          return;
        }
        addWorkspace(created);
        setNewWorkspaceTitle("");
        setWorkspacePurpose(WORKSPACE_PURPOSE_PRESETS[0].key);
        onWorkspaceLocaleChange(created.default_language);
        setIsCreating(false);
        setOpen(false);
        onWorkspaceRouteRefresh();
      })
      .catch((error) => {
        if (
          mountedRef.current &&
          requestGeneration === createRequestGenerationRef.current
        ) {
          setStatusMessage(errorMessage(error));
        }
      });
  }, [
    addWorkspace,
    initialized,
    location.hash,
    location.key,
    location.pathname,
    location.search,
    location.state,
    loading,
    navigate,
    onWorkspaceLocaleChange,
    onWorkspaceRouteRefresh,
    selectWorkspaceId,
    t,
    workspaces,
  ]);

  useEffect(() => {
    if (!open) {
      return;
    }
    // Approval happens in the owner's session, not this one: nothing pushes
    // the requester's own workspace list to reflect it. Opening the switcher
    // is the moment the user is looking right at "my workspaces", so refresh
    // here rather than relying on the mount-only effect above (WS-01/WS-11).
    void refreshWorkspaces();
    const controller = new AbortController();
    void listPendingWorkspaceJoins(controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) {
          setPendingJoinWorkspaces(items);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          // The switcher remains useful when directory discovery is unavailable.
          // Do not invent pending requests from stale client state.
          setPendingJoinWorkspaces([]);
        }
      });
    return () => controller.abort();
  }, [open, refreshWorkspaces]);

  function selectWorkspace(workspaceId: string) {
    if (workspaceId === selectedId) {
      setFilterQuery("");
      setOpen(false);
      return;
    }
    // Routes often identify a Context or Library item that exists only in the
    // previous workspace.  A workspace switch must therefore start from a
    // workspace-neutral landing page instead of retaining a stale deep link.
    // Selection is intentionally deferred until that navigation commits: a
    // dirty editor can cancel the transition without any workspace side effect.
    navigate("/dashboard", {
      replace: true,
      state: workspaceNavigationState({
        kind: "select",
        nonce: nextNavigationIntentNonce(),
        workspaceId,
      }),
    });
  }

  // Pin the default workspace to the top, then most-recently-used, then the
  // rest (item 14-b). Recents persist client-side — no server write load.
  const recents = readWorkspaceRecents();
  const orderedWorkspaces = [...workspaces].sort(
    (a, b) =>
      workspaceRank(a, defaultWorkspaceId, recents) -
      workspaceRank(b, defaultWorkspaceId, recents),
  );
  const normalizedFilter = filterQuery.trim().toLowerCase();
  const visibleWorkspaces = normalizedFilter
    ? orderedWorkspaces.filter((workspace) =>
        workspace.title.toLowerCase().includes(normalizedFilter),
      )
    : orderedWorkspaces;
  const visiblePendingJoinWorkspaces = normalizedFilter
    ? pendingJoinWorkspaces.filter((workspace) =>
        workspace.title.toLowerCase().includes(normalizedFilter),
      )
    : pendingJoinWorkspaces;
  const personalWorkspace = personalWorkspaceId
    ? (visibleWorkspaces.find(
        (workspace) => workspace.id === personalWorkspaceId,
      ) ?? null)
    : null;
  const workspaceGroups = [
    {
      key: "personal",
      label: t("workspace.personalGroup", {
        defaultValue: "Personal workspace",
      }),
      workspaces: personalWorkspace ? [personalWorkspace] : [],
    },
    {
      key: "member",
      label: t("workspace.memberGroup", { defaultValue: "My workspaces" }),
      workspaces: visibleWorkspaces.filter(
        (workspace) => workspace.id !== personalWorkspace?.id,
      ),
    },
  ].filter((group) => group.workspaces.length > 0);
  const showWorkspaceFilter =
    workspaces.length + pendingJoinWorkspaces.length > 7;

  function workspaceRouteNavigationState(
    workspaceId: string,
    destination: string,
  ) {
    return workspaceNavigationState({
      kind: "select",
      nonce: `${location.key}:${destination}:${workspaceId}`,
      workspaceId,
    });
  }

  function submitWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const title = trimShortText(newWorkspaceTitle);
    const error = inputTextError(newWorkspaceTitle, 128);
    if (error) { event.currentTarget.querySelector("input")?.reportValidity(); return; }
    if (!title) {
      return;
    }
    const purpose =
      WORKSPACE_PURPOSE_PRESETS.find(
        (preset) => preset.key === workspacePurpose,
      ) ?? WORKSPACE_PURPOSE_PRESETS[0];
    // Creating is also a post-commit effect. Otherwise cancelling a dirty-page
    // blocker would still leave a newly-created server workspace behind.
    navigate("/dashboard", {
      state: workspaceNavigationState({
        goal: purpose.goal,
        kind: "create",
        nonce: nextNavigationIntentNonce(),
        title,
      }),
    });
  }

  return (
    <div className="workspace-controls" ref={containerRef}>
      <button
        aria-expanded={open}
        aria-label={t("workspace.switchCurrent", {
          defaultValue: "Switch workspace, current: {{title}}",
          title:
            selectedWorkspace?.title ??
            t("workspace.loading", { defaultValue: "Loading" }),
        })}
        className="workspace-switcher-button"
        onClick={() => setOpen((current) => !current)}
        title={
          selectedWorkspace?.title ??
          t("workspace.loadingTitle", { defaultValue: "Workspace is loading" })
        }
        type="button"
      >
        <span className="workspace-initials">
          {workspaceInitials(selectedWorkspace?.title)}
        </span>
        <span className="workspace-switcher-label">
          {selectedWorkspace?.title ??
            t("workspace.fallbackTitle", { defaultValue: "Workspace" })}
        </span>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {open ? (
        <div
          className="workspace-popover"
          role="dialog"
          aria-label={t("workspace.switcherAria", {
            defaultValue: "Workspace switcher",
          })}
        >
          <div className="workspace-current">
            <span>
              {t("workspace.current", { defaultValue: "Current workspace" })}
            </span>
            <strong>
              {selectedWorkspace?.title ??
                t("workspace.loadingTitle", {
                  defaultValue: "Workspace is loading",
                })}
            </strong>
          </div>
          {showWorkspaceFilter ? (
            <input
              aria-label={t("workspace.filterAria", {
                defaultValue: "Filter workspaces",
              })}
              className="workspace-filter-input"
              onChange={(event) => setFilterQuery(event.target.value)}
              placeholder={t("workspace.filterAria", {
                defaultValue: "Filter workspaces",
              })}
              value={filterQuery}
            />
          ) : null}
          {visiblePendingJoinWorkspaces.length > 0 ? (
            <div
              aria-label={t("workspace.pendingGroup", {
                defaultValue: "Pending join requests",
              })}
              className="workspace-menu-group workspace-menu-pending-group"
              role="group"
            >
              <p aria-hidden="true" className="workspace-menu-group-label">
                {t("workspace.pendingGroup", {
                  defaultValue: "Pending join requests",
                })}
              </p>
              {visiblePendingJoinWorkspaces.map((workspace) => (
                <Link
                  className="workspace-pending-item"
                  key={workspace.workspace_id}
                  onClick={() => setOpen(false)}
                  to="/workspaces/find"
                >
                  <span className="workspace-initials">
                    {workspaceInitials(workspace.title)}
                  </span>
                  <span className="workspace-menu-item-copy">
                    <strong>{workspace.title}</strong>
                    <small>
                      {t("workspace.pendingBadge", {
                        defaultValue: "Request pending",
                      })}
                    </small>
                  </span>
                </Link>
              ))}
            </div>
          ) : null}
          {visibleWorkspaces.length === 0 &&
          visiblePendingJoinWorkspaces.length === 0 ? (
            <p className="workspace-menu-empty">
              {t("workspace.noMatches", {
                defaultValue: "No workspaces match.",
              })}
            </p>
          ) : null}
          {visibleWorkspaces.length > 0 ? (
            <div
              aria-label={t("workspace.listAria", {
                defaultValue: "Workspaces",
              })}
              className="workspace-menu-list"
            >
              {workspaceGroups.map((group) => (
                <div
                  aria-label={group.label}
                  className="workspace-menu-group"
                  key={group.key}
                  role="group"
                >
                  <p aria-hidden="true" className="workspace-menu-group-label">
                    {group.label}
                  </p>
                  <div role="list">
                    {group.workspaces.map((workspace) => {
                      const isPersonal = workspace.id === personalWorkspaceId;
                      return (
                        <div
                          className="workspace-menu-row"
                          key={workspace.id}
                          role="listitem"
                        >
                          <button
                            aria-pressed={workspace.id === selectedId}
                            className={`workspace-menu-item ${
                              workspace.id === selectedId ? "active" : ""
                            }`}
                            onClick={() => selectWorkspace(workspace.id)}
                            type="button"
                          >
                            <span className="workspace-initials">
                              {workspaceInitials(workspace.title)}
                            </span>
                            <span className="workspace-menu-item-copy">
                              <strong>{workspace.title}</strong>
                              <small className="workspace-menu-meta">
                                {isPersonal ? (
                                  <span className="workspace-mini-badge">
                                    {t("workspace.personalBadge", {
                                      defaultValue: "Personal",
                                    })}
                                  </span>
                                ) : null}
                                {!isPersonal && workspace.owner_display_name ? (
                                  <span>
                                    {t("workspace.ownedBy", {
                                      defaultValue: "Owned by {{name}}",
                                      name: workspace.owner_display_name,
                                    })}
                                  </span>
                                ) : null}
                                <span>
                                  {t(`status.${workspace.role ?? "member"}`, {
                                    defaultValue: workspace.role ?? "member",
                                  })}
                                </span>
                                <span>
                                  {t(`status.${workspace.visibility}`, {
                                    defaultValue: workspace.visibility,
                                  })}
                                </span>
                                {(workspace.member_count ?? 0) >= 2 ? (
                                  <span>
                                    {t("workspace.memberCount", {
                                      count: workspace.member_count,
                                      defaultValue:
                                        "Shared · {{count}} members",
                                    })}
                                  </span>
                                ) : null}
                                {workspace.id === defaultWorkspaceId ? (
                                  <span className="workspace-default-mini-badge">
                                    <CheckCircle2
                                      size={10}
                                      aria-hidden="true"
                                    />
                                    {t("workspace.defaultBadge", {
                                      defaultValue: "Default",
                                    })}
                                  </span>
                                ) : null}
                              </small>
                            </span>
                          </button>
                          <div
                            aria-label={t("workspace.rowActions", {
                              defaultValue: "Actions for {{title}}",
                              title: workspace.title,
                            })}
                            className="workspace-menu-item-actions"
                            role="group"
                          >
                            <Link
                              state={workspaceRouteNavigationState(
                                workspace.id,
                                "settings",
                              )}
                              to="/settings?section=workspace"
                            >
                              <Settings aria-hidden="true" size={12} />
                              {t("workspace.settingsAction", {
                                defaultValue: "Settings",
                              })}
                            </Link>
                            {workspace.role === "owner" ? (
                              <Link
                                state={workspaceRouteNavigationState(
                                  workspace.id,
                                  "sharing",
                                )}
                                to="/settings?section=access-sharing"
                              >
                                <Share2 aria-hidden="true" size={12} />
                                {t("workspace.shareAction", {
                                  defaultValue: "Share",
                                })}
                              </Link>
                            ) : null}
                            <Link
                              state={workspaceRouteNavigationState(
                                workspace.id,
                                "members",
                              )}
                              to="/settings?section=members"
                            >
                              <Users aria-hidden="true" size={12} />
                              {t("workspace.membersAction", {
                                defaultValue: "Members",
                              })}
                            </Link>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
          <div className="workspace-menu-actions">
            <button
              aria-expanded={isCreating}
              className="icon-button text-button"
              onClick={() => setIsCreating((current) => !current)}
              type="button"
            >
              <Plus size={14} aria-hidden="true" />
              <span>
                {t("workspace.create", { defaultValue: "Create workspace" })}
              </span>
            </button>
            <Link
              className="icon-button text-button"
              onClick={() => setOpen(false)}
              to="/workspaces/find"
            >
              <Search size={14} aria-hidden="true" />
              <span>
                {t("workspace.find", { defaultValue: "Find workspaces" })}
              </span>
            </Link>
          </div>
          {isCreating ? (
            <form className="workspace-create-form" onSubmit={submitWorkspace}>
              <label className="field-stack">
                {t("workspace.nameLabel", { defaultValue: "Workspace name" })}
                <input
                  {...textInputLimit(128)}
                  onChange={(event) => setNewWorkspaceTitle(event.target.value)}
                  placeholder={t("workspace.namePlaceholder", {
                    defaultValue: "Research workspace",
                  })}
                  required
                  value={newWorkspaceTitle}
                />
              </label>
              <label className="field-stack">
                {t("workspace.purposeLabel", { defaultValue: "Purpose" })}
                <select
                  value={workspacePurpose}
                  onChange={(event) => setWorkspacePurpose(event.target.value)}
                >
                  {WORKSPACE_PURPOSE_PRESETS.map((preset) => (
                    <option key={preset.key} value={preset.key}>
                      {t(`workspace.purpose.${preset.key}`, {
                        defaultValue: preset.label,
                      })}
                    </option>
                  ))}
                </select>
              </label>
              <button className="icon-button primary" type="submit">
                {t("workspace.createSubmit", { defaultValue: "Create" })}
              </button>
            </form>
          ) : null}
          {visibleStatusMessage ? (
            <p className="workspace-control-error">{visibleStatusMessage}</p>
          ) : null}
        </div>
      ) : visibleStatusMessage ? (
        // Surface workspace errors even when the popover is closed, without a
        // stuck-open popover that outside-click can't dismiss.
        <p className="workspace-control-error workspace-control-error-standalone">
          {visibleStatusMessage}
        </p>
      ) : null}
    </div>
  );
}

const WORKSPACE_RECENTS_KEY = "dtxwiki.workspaceRecents";
const WORKSPACE_RECENTS_MAX = 8;

function readWorkspaceRecents(): string[] {
  if (typeof window === "undefined") {
    return [];
  }
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(WORKSPACE_RECENTS_KEY) ?? "[]",
    );
    return Array.isArray(parsed)
      ? parsed.filter((id): id is string => typeof id === "string")
      : [];
  } catch {
    return [];
  }
}

function pushWorkspaceRecent(workspaceId: string) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    const next = [
      workspaceId,
      ...readWorkspaceRecents().filter((id) => id !== workspaceId),
    ].slice(0, WORKSPACE_RECENTS_MAX);
    window.localStorage.setItem(WORKSPACE_RECENTS_KEY, JSON.stringify(next));
  } catch {
    // Ignore storage failures (private mode / quota).
  }
}

// Default workspace first, then most-recently-used (client-side), then the rest
// in their existing (creation) order. No server timestamp / write load (Q13).
function workspaceRank(
  workspace: WorkspaceSummary,
  defaultWorkspaceId: string | null,
  recents: string[],
): number {
  if (workspace.id === defaultWorkspaceId) {
    return -1;
  }
  const recentIndex = recents.indexOf(workspace.id);
  return recentIndex >= 0 ? recentIndex : Number.MAX_SAFE_INTEGER;
}

function MobileNavDrawer({
  labels,
  navGroups,
  onClose,
  onNavigate,
}: {
  labels: ShellLabels;
  navGroups: ReturnType<typeof navGroupsForLabels>;
  onClose: () => void;
  onNavigate: () => void;
}) {
  const drawerRef = useRef<HTMLElement | null>(null);

  // This component only mounts while the drawer is open (see its call site),
  // so a mount effect is the "on open" hook -- no open/closed prop needed.
  useEffect(() => {
    drawerRef.current?.focus();
  }, []);

  // Initial focus alone doesn't stop Tab from walking out of the drawer into
  // the topbar/sidebar behind it; trap it explicitly, same as Dialog.tsx.
  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Tab") {
        trapTabFocus(event, drawerRef.current);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  return (
    <div className="mobile-nav-backdrop">
      <button
        aria-label={labels.closeNavigation}
        className="mobile-nav-scrim"
        onClick={onClose}
        type="button"
      />
      <aside
        aria-label={labels.mobileNavAria}
        aria-modal="true"
        className="mobile-nav-drawer"
        ref={drawerRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="mobile-nav-header">
          <div className="topbar-brand compact-brand">
            {/* This drawer only mounts while authenticated (see its call
                site), so the dashboard link is unconditional here, unlike
                the topbar's own brand. onNavigate, not onClose: it closes
                the drawer as a side effect of leaving, the same as every
                other link in it. */}
            <Link
              className="topbar-brand-link"
              onClick={onNavigate}
              to="/dashboard"
            >
              <PrismGlyph />
              <span className="brand-name">dtxWiki</span>
            </Link>
          </div>
          <button
            aria-label={labels.closeNavigation}
            className="icon-button"
            onClick={onClose}
            type="button"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <nav
          className="nav-section sidebar-nav"
          aria-label={labels.mobileNavListAria}
        >
          {navGroups.map((group) => (
            <div className="nav-group" key={group.label}>
              <p className="eyebrow nav-group-label">{group.label}</p>
              <div className="nav-section nav-group-items">
                {group.items.map((item) => {
                  const title = navItemTitle(item, labels);
                  return (
                    <NavLink
                      className={({ isActive }) =>
                        `nav-item${isActive ? " active" : ""}`
                      }
                      key={item.to}
                      onClick={onNavigate}
                      title={title}
                      to={item.to}
                    >
                      <NavItemContent item={item} labels={labels} />
                    </NavLink>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>
      </aside>
    </div>
  );
}

function Sidebar({
  collapsed,
  inertActive,
  labels,
  navGroups,
  onToggleCollapsed,
}: {
  collapsed: boolean;
  inertActive: boolean;
  labels: ShellLabels;
  navGroups: ReturnType<typeof navGroupsForLabels>;
  onToggleCollapsed: () => void;
}) {
  const inertRef = useExplicitInertRef<HTMLElement>(null, inertActive);
  return (
    <aside
      className="sidebar"
      data-collapsed={collapsed ? "true" : "false"}
      ref={inertRef}
    >
      <nav className="nav-section sidebar-nav" aria-label={labels.sidebarAria}>
        {navGroups.map((group) => {
          const groupId = `sidebar-nav-${group.label.toLowerCase().replace(/\s+/g, "-")}`;
          return (
            <div
              aria-labelledby={groupId}
              className="nav-group"
              key={group.label}
              role="group"
            >
              <p className="eyebrow nav-group-label" id={groupId}>
                {group.label}
              </p>
              <div className="nav-section nav-group-items">
                {group.items.map((item) => {
                  const title = navItemTitle(item, labels);
                  return (
                    <NavLink
                      aria-label={collapsed ? title : undefined}
                      className={({ isActive }) =>
                        `nav-item${isActive ? " active" : ""}`
                      }
                      key={item.to}
                      title={title}
                      to={item.to}
                    >
                      <NavItemContent item={item} labels={labels} />
                    </NavLink>
                  );
                })}
              </div>
            </div>
          );
        })}
      </nav>
      <div className="sidebar-footer">
        <button
          aria-expanded={!collapsed}
          aria-label={
            collapsed
              ? labels.sidebarExpandToggle
              : labels.sidebarCollapseToggle
          }
          className="icon-button desktop-sidebar-toggle"
          onClick={onToggleCollapsed}
          title={
            collapsed
              ? labels.sidebarExpandToggle
              : labels.sidebarCollapseToggle
          }
          type="button"
        >
          <PanelVisibilityIcon
            side="left"
            action={collapsed ? "show" : "hide"}
            size={16}
          />
          <span className="sidebar-toggle-label">
            {collapsed ? labels.sidebarExpand : labels.sidebarCollapse}
          </span>
        </button>
      </div>
    </aside>
  );
}

function NavItemContent({
  item,
  labels,
}: {
  item: ReturnType<typeof navGroupsForLabels>[number]["items"][number];
  labels: ShellLabels;
}) {
  const Icon = item.icon;
  return (
    <>
      <Icon size={16} aria-hidden="true" />
      <span className="nav-item-label">{item.label}</span>
      {item.preview ? (
        <span className="nav-preview-badge">{labels.previewBadge}</span>
      ) : null}
    </>
  );
}

function navItemTitle(
  item: ReturnType<typeof navGroupsForLabels>[number]["items"][number],
  labels: ShellLabels,
) {
  return item.preview
    ? `${item.label} - ${labels.previewDescription}`
    : item.label;
}

function workspaceInitials(title: string | null | undefined) {
  if (!title) {
    return "WS";
  }
  return (
    title
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase() ?? "")
      .join("") || "WS"
  );
}

function errorMessage(error: unknown) {
  return apiErrorMessage(error, "Workspace request failed");
}
