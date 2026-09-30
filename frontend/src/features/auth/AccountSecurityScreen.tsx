import {
  BriefcaseBusiness,
  KeyRound,
  Link as LinkIcon,
  LogOut,
  MailCheck,
  UserRound,
} from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import {
  changePassword,
  listApiTokens,
  requestEmailVerification,
  type ApiToken,
} from "../../shared/api/auth";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
  listOAuthConsents,
  type OAuthConsent,
} from "../../shared/api/oauth";
import {
  getSelectedWorkspaceId,
  listWorkspaces,
  setDefaultWorkspace,
  type WorkspaceListData,
} from "../../shared/api/workspace";
import { useAuth } from "../../shared/auth/AuthContext";
import {
  clearStoredUiLocale,
  hasStoredUiLocale,
} from "../../shared/i18n";
import { useOptionalWorkspaceContext } from "../../shared/workspace/WorkspaceContext";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { CountChip } from "../../shared/components/CountChip";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";

type AccountOverview = {
  workspaces: WorkspaceListData | null;
  tokens: ApiToken[];
  consents: OAuthConsent[];
};

const PASSWORD_UPDATE_REDIRECT_MS = 1500;

export function AccountSecurityScreen() {
  const { i18n, t } = useTranslation("auth");
  const navigate = useNavigate();
  const {
    capabilitiesChecked,
    passwordRecoveryMode,
    refreshCapabilities,
    refreshSession,
    signOut,
    status: authStatus,
    user,
  } = useAuth();
  // "email" is the only recovery mode that means a mail provider is actually
  // configured, so it is also the only one under which email verification is
  // a thing this server does.
  const emailVerificationOffered = passwordRecoveryMode === "email";
  const [overview, setOverview] = useState<AccountOverview>({
    workspaces: null,
    tokens: [],
    consents: [],
  });
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [verificationBusy, setVerificationBusy] = useState(false);
  const [verificationStatus, setVerificationStatus] = useState<string | null>(null);
  const workspaceContext = useOptionalWorkspaceContext();
  const [defaultBusyId, setDefaultBusyId] = useState<string | null>(null);
  const [defaultStatus, setDefaultStatus] = useState<string | null>(null);
  const defaultWorkspaceId =
    workspaceContext?.defaultWorkspaceId ??
    overview.workspaces?.default_workspace_id ??
    null;
  const [languageOverride, setLanguageOverride] = useState(() => hasStoredUiLocale());
  useEffect(() => {
    const sync = () => setLanguageOverride(hasStoredUiLocale());
    window.addEventListener("dtxwiki:ui-language-changed", sync);
    return () => window.removeEventListener("dtxwiki:ui-language-changed", sync);
  }, []);
  const workspaceLanguage =
    workspaceContext?.selectedWorkspace?.default_language ?? null;
  const uiLanguageLabel = t(`account.language.${i18n.language === "ko" ? "ko" : "en"}`, {
    defaultValue: i18n.language === "ko" ? "Korean" : "English",
  });

  useEffect(() => {
    // Whether verification is even possible (passwordRecoveryMode) gates the
    // button below. Shell preloads this once its banner needs it, but a user
    // can land here directly (deep link, or before Shell's own gate has
    // fired), so fetch it on demand rather than assume it is warm.
    if (!user || user.email_verified_at || capabilitiesChecked) {
      return;
    }
    void refreshCapabilities();
  }, [capabilitiesChecked, refreshCapabilities, user]);

  useEffect(() => {
    if (!user?.user_id) {
      return;
    }
    let cancelled = false;
    async function loadOverview() {
      setOverviewError(null);
      // Load the three sections independently so one optional endpoint being
      // unavailable (e.g. OAuth consents) degrades that card to empty instead
      // of blanking the whole Account page — including the workspace list.
      const [workspaceResult, tokenResult, consentResult] =
        await Promise.allSettled([
          listWorkspaces(),
          listApiTokens(),
          listOAuthConsents(),
        ]);
      if (cancelled) {
        return;
      }
      setOverview({
        workspaces:
          workspaceResult.status === "fulfilled"
            ? workspaceResult.value.data
            : null,
        tokens: tokenResult.status === "fulfilled" ? tokenResult.value : [],
        consents:
          consentResult.status === "fulfilled" ? consentResult.value : [],
      });
      // Only surface an error if the workspace list itself failed (the rest of
      // the page can still render usefully without tokens/consents).
      setOverviewError(
        workspaceResult.status === "rejected"
          ? apiErrorMessage(workspaceResult.reason)
          : null,
      );
    }
    void loadOverview();
    return () => {
      cancelled = true;
    };
  }, [t, user?.user_id]);

  async function onSetDefaultWorkspace(workspaceId: string) {
    setDefaultBusyId(workspaceId);
    setDefaultStatus(null);
    setError(null);
    try {
      await setDefaultWorkspace(workspaceId);
      await workspaceContext?.refreshWorkspaces();
      const workspace = overview.workspaces?.workspaces.find(
        (candidate) => candidate.id === workspaceId,
      );
      setDefaultStatus(
        t("account.defaultChanged", {
          defaultValue: "{{title}} is now your default workspace.",
          title:
            workspace?.title ??
            t("account.defaultWorkspaceFallback", { defaultValue: "Workspace" }),
        }),
      );
    } catch (caught) {
      setError(apiErrorMessage(caught));
    } finally {
      setDefaultBusyId(null);
    }
  }

  async function onLogout() {
    setSigningOut(true);
    setError(null);
    try {
      await signOut();
      navigate("/login");
    } catch (caught) {
      setError(apiErrorMessage(caught));
    } finally {
      setSigningOut(false);
    }
  }

  async function onRequestEmailVerification() {
    setVerificationBusy(true);
    setVerificationStatus(null);
    setError(null);
    try {
      const result = await requestEmailVerification();
      if (result.verified) {
        await refreshSession();
        setVerificationStatus(
          t("account.emailAlreadyVerified", {
            defaultValue: "Your email is already verified.",
          }),
        );
      } else {
        setVerificationStatus(
          t("account.emailVerificationSent", {
            defaultValue: "Verification instructions were sent to your email.",
          }),
        );
      }
    } catch (caught) {
      setError(
        isApiError(caught, 422, "password_recovery_unavailable")
          ? t("account.emailVerificationUnavailable", {
              defaultValue:
                "Email verification is not available on this server. Contact the server operator.",
            })
          : apiErrorMessage(caught),
      );
    } finally {
      setVerificationBusy(false);
    }
  }

  if (authStatus === "checking") {
    return (
      <RouteLoading
        label={t("account.loading", { defaultValue: "Loading Account" })}
      />
    );
  }

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main account-layout">
        <Breadcrumbs
          items={[
            { label: t("account.breadcrumb", { defaultValue: "Account" }) },
          ]}
        />
        <div className="feature-header">
          <div>
            <p className="eyebrow">
              {t("account.eyebrow", { defaultValue: "My page" })}
            </p>
            <h1>{t("account.title", { defaultValue: "Account" })}</h1>
            <p className="feature-subtitle">
              {t("account.subtitle", {
                defaultValue:
                  "Review the account details and workspace access available to this signed-in browser.",
              })}
            </p>
          </div>
        </div>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {overviewError ? (
          <StatusBanner tone="warning">{overviewError}</StatusBanner>
        ) : null}
        {user ? (
          <div className="account-grid">
            <section
              className="auth-panel account-profile-panel"
              aria-label={t("account.profileAria", { defaultValue: "Profile" })}
            >
              <div className="auth-summary">
                <UserRound size={18} aria-hidden="true" />
                <div>
                  <strong>{user.display_name}</strong>
                  <span>{user.email}</span>
                </div>
              </div>
              {/* A server with no mail provider has no email verification at
                  all: nothing sends the message, and nothing depends on the
                  flag (it only gates email-based password recovery, which is
                  equally unavailable). Showing "Email not verified" there
                  warns about a state the person cannot leave and does not
                  need to -- so on those servers the verification row is not
                  rendered at all, rather than rendered as an unfixable
                  warning plus an apology. Verified accounts still show their
                  pill, since that is a fact and not a demand -- and the
                  comment above only became true when this gate stopped
                  dropping the whole row. `emailVerificationOffered` is
                  derived from `passwordRecoveryMode`, which is only loaded on
                  auth-bootstrap routes, so for an already-verified account it
                  stays at its "disabled" default and the pill vanished on
                  every server, not just no-mail ones. */}
              {emailVerificationOffered || user.email_verified_at ? (
              <div className="account-email-verification">
                <StatusPill
                  compact
                  label={
                    user.email_verified_at
                      ? t("account.emailVerified", { defaultValue: "Email verified" })
                      : t("account.emailUnverified", { defaultValue: "Email not verified" })
                  }
                  status={user.email_verified_at ? "verified" : "unverified"}
                  tone={user.email_verified_at ? "ok" : "warn"}
                />
                {!user.email_verified_at && passwordRecoveryMode === "email" ? (
                  <button
                    className="icon-button text-button"
                    disabled={verificationBusy}
                    onClick={() => void onRequestEmailVerification()}
                    type="button"
                  >
                    <MailCheck size={16} aria-hidden="true" />
                    <span>
                      {verificationBusy
                        ? t("account.emailVerificationSending", {
                            defaultValue: "Sending…",
                          })
                        : t("account.verifyEmail", {
                            defaultValue: "Verify email",
                          })}
                    </span>
                  </button>
                ) : null}
              </div>
              ) : null}
              {verificationStatus ? (
                <StatusBanner tone="info">{verificationStatus}</StatusBanner>
              ) : null}
              <div
                className="account-actions"
                aria-label={t("account.actionsAria", {
                  defaultValue: "Account actions",
                })}
              >
                <Link className="icon-button" to="/account/password">
                  <KeyRound size={16} aria-hidden="true" />
                  <span>
                    {t("account.updatePassword", {
                      defaultValue: "Update password",
                    })}
                  </span>
                </Link>
                <button
                  className="icon-button"
                  disabled={signingOut}
                  onClick={onLogout}
                  type="button"
                >
                  <LogOut size={16} aria-hidden="true" />
                  <span>{t("account.signOut", { defaultValue: "Sign out" })}</span>
                </button>
              </div>
              {/* A personal language choice outranks the workspace default on
                  every load, so the only place it can be seen — and undone — is
                  next to the account it belongs to. */}
              {languageOverride ? (
                <p className="account-language-note">
                  {t("account.languageOverride", {
                    defaultValue:
                      "This browser is set to {{language}}, overriding the workspace default.",
                    language: uiLanguageLabel,
                  })}{" "}
                  <button
                    className="link-button"
                    onClick={() => {
                      clearStoredUiLocale(workspaceLanguage);
                      setLanguageOverride(false);
                    }}
                    type="button"
                  >
                    {t("account.useWorkspaceLanguage", {
                      defaultValue: "Use the workspace default",
                    })}
                  </button>
                </p>
              ) : null}
            </section>

            <section
              className="auth-panel workspace-access-panel"
              aria-label={t("account.workspaceAccessAria", {
                defaultValue: "Workspace access",
              })}
            >
              <div className="panel-header">
                <div className="panel-title-with-count">
                  <span>
                    {t("account.workspaceAccess", {
                      defaultValue: "Workspace access",
                    })}
                  </span>
                  <CountChip
                    value={overview.workspaces?.workspaces.length ?? 0}
                    label={t("account.workspaceUnit", {
                      count: overview.workspaces?.workspaces.length ?? 0,
                    })}
                    compact
                  />
                </div>
              </div>
              <p className="account-panel-hint">
                {t("account.workspaceHint", {
                  defaultValue:
                    "Choose which workspace opens first after sign-in. Top-bar switching only changes the current browser session.",
                })}
              </p>
              {defaultStatus ? (
                <StatusBanner tone="info">{defaultStatus}</StatusBanner>
              ) : null}
              <WorkspaceAccessList
                busyWorkspaceId={defaultBusyId}
                data={overview.workspaces}
                defaultWorkspaceId={defaultWorkspaceId}
                onSetDefault={onSetDefaultWorkspace}
              />
            </section>

            <section
              className="auth-panel account-tools-panel"
              aria-label={t("account.connectedToolsAria", {
                defaultValue: "Connected tools",
              })}
            >
              <div className="panel-header">
                <span>
                  {t("account.connectedTools", {
                    defaultValue: "Connected tools",
                  })}
                </span>
              </div>
              <div className="account-quick-grid">
                <Link
                  className="metric-card linked"
                  to="/settings/integrations#integrations"
                >
                  <div className="metric-card-title">
                    <KeyRound size={16} aria-hidden="true" />
                    <span>{t("account.mcpKeys", { defaultValue: "MCP keys" })}</span>
                  </div>
                  <strong>{activeTokenCount(overview.tokens)}</strong>
                  <p>
                    {t("account.mcpKeysDesc", {
                      defaultValue: "Active personal access keys for MCP clients.",
                    })}
                  </p>
                </Link>
                <Link
                  className="metric-card linked"
                  to="/settings/integrations#oauth-consents"
                >
                  <div className="metric-card-title">
                    <LinkIcon size={16} aria-hidden="true" />
                    <span>
                      {t("account.oauthApps", { defaultValue: "OAuth apps" })}
                    </span>
                  </div>
                  <strong>{overview.consents.length}</strong>
                  <p>
                    {t("account.oauthAppsDesc", {
                      defaultValue: "Apps authorized to connect to dtxWiki.",
                    })}
                  </p>
                </Link>
              </div>
            </section>
          </div>
        ) : (
          <section
            className="auth-panel"
            aria-label={t("account.signedOutAria", { defaultValue: "Signed out" })}
          >
            <div className="empty-state">
              {t("account.noSession", { defaultValue: "No active session" })}
            </div>
            <Link className="icon-button primary auth-inline-link" to="/login">
              <KeyRound size={16} aria-hidden="true" />
              <span>{t("account.signIn", { defaultValue: "Sign in" })}</span>
            </Link>
          </section>
        )}
      </section>
    </div>
  );
}

export function AccountPasswordScreen() {
  const { t } = useTranslation("auth");
  const navigate = useNavigate();
  const { setAuthenticatedSession, status: authStatus, user } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!status) {
      return undefined;
    }
    const timer = window.setTimeout(() => {
      navigate("/account", { replace: true });
    }, PASSWORD_UPDATE_REDIRECT_MS);
    return () => window.clearTimeout(timer);
  }, [navigate, status]);

  async function onPasswordSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setStatus(null);
    if (newPassword !== confirmPassword) {
      setError(
        t("password.mismatch", { defaultValue: "New passwords do not match." }),
      );
      return;
    }
    setSubmitting(true);
    try {
      const session = await changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setAuthenticatedSession(session);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setStatus(
        t("password.updated", {
          defaultValue: "Password updated. Returning to account…",
        }),
      );
    } catch (caught) {
      setError(
        errorMessage(
          caught,
          t("account.requestFailed", { defaultValue: "Account request failed" }),
        ),
      );
    } finally {
      setSubmitting(false);
    }
  }

  if (authStatus === "checking") {
    return (
      <RouteLoading
        label={t("password.loading", {
          defaultValue: "Loading Account security",
        })}
      />
    );
  }

  return (
    <div className="workspace-screen single-column">
      <section className="feature-main auth-layout">
        <Breadcrumbs
          items={[
            {
              label: t("account.breadcrumb", { defaultValue: "Account" }),
              href: "/account",
            },
            { label: t("password.breadcrumb", { defaultValue: "Password" }) },
          ]}
        />
        <div className="feature-header">
          <div>
            <p className="eyebrow">
              {t("password.eyebrow", { defaultValue: "Account security" })}
            </p>
            <h1>{t("password.title", { defaultValue: "Update password" })}</h1>
            <p className="feature-subtitle">
              {t("password.subtitle", {
                defaultValue: "Choose a new password for {{email}}.",
                email:
                  user?.email ??
                  t("password.emailFallback", { defaultValue: "this account" }),
              })}
            </p>
          </div>
        </div>
        {status ? <StatusBanner tone="info">{status}</StatusBanner> : null}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        {user ? (
          <section
            className="auth-panel"
            aria-label={t("password.formAria", { defaultValue: "Update password" })}
          >
            <form className="auth-form" onSubmit={onPasswordSubmit}>
              <label className="field-stack">
                {t("password.current", { defaultValue: "Current password" })}
                <input
                  autoComplete="current-password"
                  name="currentPassword"
                  onChange={(event) => setCurrentPassword(event.target.value)}
                  required
                  type="password"
                  value={currentPassword}
                />
              </label>
              <label className="field-stack">
                {t("password.new", { defaultValue: "New password" })}
                <input
                  autoComplete="new-password"
                  minLength={8}
                  name="newPassword"
                  onChange={(event) => setNewPassword(event.target.value)}
                  required
                  aria-describedby="new-password-rule"
                  type="password"
                  value={newPassword}
                />
              </label>
              <p className="field-hint field-hint--attached" id="new-password-rule">
                {t("password.rule", { defaultValue: "At least 8 characters." })}
              </p>
              <label className="field-stack">
                {t("password.confirm", { defaultValue: "Confirm new password" })}
                <input
                  autoComplete="new-password"
                  minLength={8}
                  name="confirmPassword"
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  required
                  type="password"
                  value={confirmPassword}
                />
              </label>
              <div className="auth-actions">
                <button className="icon-button primary" disabled={submitting} type="submit">
                  <KeyRound size={16} aria-hidden="true" />
                  <span>
                    {t("password.submit", { defaultValue: "Update password" })}
                  </span>
                </button>
                <Link className="icon-button text-button" to="/account">
                  {t("password.back", { defaultValue: "Back to account" })}
                </Link>
              </div>
            </form>
          </section>
        ) : (
          <section
            className="auth-panel"
            aria-label={t("account.signedOutAria", { defaultValue: "Signed out" })}
          >
            <div className="empty-state">
              {t("account.noSession", { defaultValue: "No active session" })}
            </div>
            <Link className="icon-button primary auth-inline-link" to="/login">
              <KeyRound size={16} aria-hidden="true" />
              <span>{t("account.signIn", { defaultValue: "Sign in" })}</span>
            </Link>
          </section>
        )}
      </section>
    </div>
  );
}

function WorkspaceAccessList({
  data,
  defaultWorkspaceId,
  busyWorkspaceId,
  onSetDefault,
}: {
  data: WorkspaceListData | null;
  defaultWorkspaceId: string | null;
  busyWorkspaceId: string | null;
  onSetDefault: (workspaceId: string) => void;
}) {
  const { t } = useTranslation("auth");
  if (!data) {
    return (
      <div className="empty-state">
        {t("account.loadingWorkspaceAccess", {
          defaultValue: "Loading workspace access…",
        })}
      </div>
    );
  }
  if (data.workspaces.length === 0) {
    return (
      <div className="empty-state">
        {t("account.noWorkspaceAccess", { defaultValue: "No workspace access" })}
      </div>
    );
  }
  const storedWorkspaceId = getSelectedWorkspaceId();
  const selectedWorkspaceId =
    storedWorkspaceId && data.workspaces.some((workspace) => workspace.id === storedWorkspaceId)
      ? storedWorkspaceId
      : data.current_workspace_id ?? data.workspaces[0]?.id ?? null;
  return (
    <div className="operations-list workspace-access-list">
      {data.workspaces.map((workspace) => {
        const isDefault = workspace.id === defaultWorkspaceId;
        return (
          <div className="operations-row compact workspace-access-row" key={workspace.id}>
            <BriefcaseBusiness size={16} aria-hidden="true" />
            <div>
              <strong>{workspace.title}</strong>
              <p>
                {t("account.workspaceMeta", {
                  defaultValue: "{{language}} · {{visibility}}",
                  // L-15 (260908 UI audit): this rendered the raw locale code
                  // ("EN") while Settings' language picker and the topbar
                  // toggle both spell it out ("English"/"한국어") -- reuse
                  // that same shared naming instead of a third format.
                  language: t(`topbar.language.${workspace.default_language}`, {
                    ns: "shell",
                    defaultValue: workspace.default_language.toUpperCase(),
                  }),
                  visibility: t(`status.${workspace.visibility}`, {
                    ns: "shell",
                    defaultValue: workspace.visibility,
                  }),
                })}
              </p>
            </div>
            <div className="workspace-access-row-actions">
              {/* Show the user's role on every row. On the current workspace,
                  add a "current" marker beside it so the role is still visible
                  (L-f: the "current" pill used to replace the role entirely). */}
              {workspace.id === selectedWorkspaceId ? (
                <StatusPill status="current" compact />
              ) : null}
              <StatusPill status={workspace.role ?? "member"} compact />
              {isDefault ? (
                // A bespoke green badge between two StatusPills read as a
                // third chip language in one row; it is a state, so it wears
                // the same pill as the two beside it.
                <StatusPill
                  compact
                  label={t("account.defaultBadge", { defaultValue: "Default" })}
                  status="active"
                  tone="ok"
                />
              ) : (
                <button
                  className="text-button compact-button"
                  disabled={busyWorkspaceId !== null}
                  onClick={() => onSetDefault(workspace.id)}
                  type="button"
                >
                  {busyWorkspaceId === workspace.id
                    ? t("account.updating", { defaultValue: "Updating…" })
                    : t("account.setAsDefault", { defaultValue: "Set as default" })}
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function activeTokenCount(tokens: ApiToken[]) {
  return tokens.filter((token) => !token.revoked_at).length;
}

function errorMessage(error: unknown, fallback: string) {
  return apiErrorMessage(error, fallback);
}

// apiErrorMessage's fallback argument is only ever reached for a throw that
// is neither an ApiError nor an Error -- an HTTP failure always returns the
// server's own (untranslated) message instead. Callers that need a
// localized message for a *specific known* server error must check for it
// explicitly, the same way SettingsScreen.tsx does.
function isApiError(error: unknown, status: number, code: string) {
  return (
    error instanceof ApiError &&
    error.status === status &&
    error.body.error_code === code
  );
}
