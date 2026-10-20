import { inputTextError, textInputLimit } from "../../shared/lib/inputPolicy";
import { validateAccountPassword, preventCompositionSubmit } from "../../shared/auth/identityPolicy";
import {
  CheckCircle2,
  Copy,
  Link2,
  Mail,
  Search,
  ShieldAlert,
} from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { createPortal } from "react-dom";

import {
  issueAdminPasswordReset,
  searchAdminUsers,
  type AdminPasswordResetDelivery,
  type AdminUser,
} from "../../shared/api/adminUsers";
import { getAuthCapabilities, reauthenticate } from "../../shared/api/auth";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";
import {
  SettingsEmptyState,
  SettingsPaneHeader,
} from "./SettingsPaneParts";
import { canWriteToClipboard } from "../../shared/lib/clipboard";
import { formatVersionTimestamp } from "../../shared/lib/datetime";

type IssuedLink = {
  email: string;
  expiresAt: string;
  resetUrl: string;
  revokeApiTokens: boolean;
};

type RecoveryMode = "ordinary" | "compromise";

type RecoveryCapabilityState = {
  emailReady: boolean;
  linkReady: boolean;
  status: "loading" | "ready" | "error";
};

export function AdminUserRecoverySection() {
  const { t } = useTranslation("settings");
  const [directoryUnlocked, setDirectoryUnlocked] = useState(false);
  const [reauthenticatedUntil, setReauthenticatedUntil] = useState<
    string | null
  >(null);
  const [unlockPassword, setUnlockPassword] = useState("");
  const [unlocking, setUnlocking] = useState(false);
  const [unlockError, setUnlockError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [searched, setSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchValidation, setSearchValidation] = useState<string | null>(null);
  const [target, setTarget] = useState<AdminUser | null>(null);
  const [delivery, setDelivery] = useState<AdminPasswordResetDelivery>("email");
  const [recoveryMode, setRecoveryMode] = useState<RecoveryMode>("ordinary");
  const [currentPassword, setCurrentPassword] = useState("");
  const [revokeApiTokens, setRevokeApiTokens] = useState(false);
  const [issuing, setIssuing] = useState(false);
  const [issueError, setIssueError] = useState<string | null>(null);
  const [issuedLink, setIssuedLink] = useState<IssuedLink | null>(null);
  const [successNotice, setSuccessNotice] = useState<string | null>(null);
  const [recoveryCapability, setRecoveryCapability] =
    useState<RecoveryCapabilityState>({
      emailReady: false,
      linkReady: false,
      status: "loading",
    });

  useEffect(() => {
    let active = true;
    void getAuthCapabilities()
      .then((capabilities) => {
        if (!active) {
          return;
        }
        setRecoveryCapability({
          emailReady: capabilities.password_recovery.mode === "email",
          linkReady: capabilities.password_recovery.admin_assisted,
          status: "ready",
        });
      })
      .catch(() => {
        if (active) {
          setRecoveryCapability({
            emailReady: false,
            linkReady: false,
            status: "error",
          });
        }
      });
    return () => {
      active = false;
    };
  }, []);

  async function onUnlockDirectory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!unlockPassword) {
      return;
    }
    try { validateAccountPassword(unlockPassword); }
    catch (caught) { setUnlockError(t((caught as Error).message, {ns: "auth"})); return; }
    setUnlocking(true);
    setUnlockError(null);
    try {
      const result = await reauthenticate({ current_password: unlockPassword });
      setDirectoryUnlocked(true);
      setReauthenticatedUntil(result.reauthenticated_until);
      setUnlockPassword("");
    } catch (caught) {
      const fallback = t("userRecovery.unlock.error", {
        defaultValue: "Your password was not accepted. Try again.",
      });
      setUnlockError(
        caught instanceof ApiError
          ? caught.body.error_code === "invalid_credentials"
            ? fallback
            : apiErrorMessage(caught, fallback)
          : fallback,
      );
      setUnlockPassword("");
    } finally {
      setUnlocking(false);
    }
  }

  function lockDirectory(message: string) {
    setDirectoryUnlocked(false);
    setReauthenticatedUntil(null);
    setUnlockError(message);
    setQuery("");
    setUsers([]);
    setTotal(0);
    setOffset(0);
    setSubmittedQuery("");
    setSearched(false);
    setSearchError(null);
    setSearchValidation(null);
    setTarget(null);
  }

  async function onSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const error = inputTextError(query, 320);
    if (error) { setSearchValidation(error); return; }
    const normalizedQuery = query.trim();
    if (!normalizedQuery) {
      setSearchValidation(
        t("userRecovery.searchRequired", {
          defaultValue: "Enter an email, display name, or workspace.",
        }),
      );
      return;
    }
    setSearchValidation(null);
    setSuccessNotice(null);
    setSubmittedQuery(normalizedQuery);
    await loadUsers(normalizedQuery, 0);
  }

  async function loadUsers(searchQuery: string, nextOffset: number) {
    setSearching(true);
    setSearchError(null);
    try {
      const result = await searchAdminUsers(searchQuery, {
        offset: nextOffset,
      });
      setUsers(result.items);
      setTotal(result.total);
      setOffset(result.offset);
      setSearched(true);
    } catch (caught) {
      if (
        caught instanceof ApiError &&
        caught.body.error_code === "recent_reauthentication_required"
      ) {
        lockDirectory(
          t("userRecovery.unlock.expired", {
            defaultValue:
              "Your recent verification expired. Re-enter your password to search users again.",
          }),
        );
        return;
      }
      const fallback = t("userRecovery.searchError", {
        defaultValue: "User search failed. Try again.",
      });
      setSearchError(
        caught instanceof ApiError
          ? apiErrorMessage(caught, fallback)
          : fallback,
      );
    } finally {
      setSearching(false);
    }
  }

  function openRecovery(
    user: AdminUser,
    selectedDelivery: AdminPasswordResetDelivery,
  ) {
    if (
      user.status !== "active" ||
      (selectedDelivery === "email" &&
        (!user.email_verified_at || !recoveryCapability.emailReady)) ||
      (selectedDelivery === "link" && !recoveryCapability.linkReady)
    ) {
      return;
    }
    setTarget(user);
    setDelivery(selectedDelivery);
    setRecoveryMode("ordinary");
    setCurrentPassword("");
    setRevokeApiTokens(false);
    setIssueError(null);
    setIssuedLink(null);
    setSuccessNotice(null);
  }

  function changeRecoveryMode(mode: RecoveryMode) {
    setRecoveryMode(mode);
    setRevokeApiTokens(mode === "compromise");
  }

  function closeRecovery() {
    if (issuing) {
      return;
    }
    setTarget(null);
    setCurrentPassword("");
    setRecoveryMode("ordinary");
    setRevokeApiTokens(false);
    setIssueError(null);
  }

  async function onIssue(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!target || !currentPassword) {
      return;
    }
    try { validateAccountPassword(currentPassword); }
    catch (caught) { setIssueError(t((caught as Error).message, {ns: "auth"})); return; }
    setIssuing(true);
    setIssueError(null);
    try {
      await reauthenticate({ current_password: currentPassword });
      const result = await issueAdminPasswordReset(target.user_id, {
        delivery,
        revoke_api_tokens: revokeApiTokens,
      });
      setCurrentPassword("");
      if (result.delivery === "link") {
        if (!result.reset_url) {
          setIssueError(
            t("userRecovery.linkMissing", {
              defaultValue:
                "The server did not return a recovery link. Issue a new reset.",
            }),
          );
          return;
        }
        setIssuedLink({
          email: target.email,
          expiresAt: result.expires_at,
          resetUrl: result.reset_url,
          revokeApiTokens: result.revoke_api_tokens,
        });
        setTarget(null);
        return;
      }
      setSuccessNotice(
        t("userRecovery.emailQueued", {
          defaultValue: "Recovery email queued for {{email}}.",
          email: target.email,
        }),
      );
      setTarget(null);
      setRevokeApiTokens(false);
    } catch (caught) {
      setCurrentPassword("");
      setIssueError(adminRecoveryErrorMessage(caught, t));
    } finally {
      setIssuing(false);
    }
  }

  function dismissIssuedLink() {
    setIssuedLink(null);
    setSuccessNotice(
      t("userRecovery.linkDismissed", {
        defaultValue:
          "The one-time link was hidden. Issue a new reset if another link is needed.",
      }),
    );
  }

  return (
    <section
      aria-labelledby="admin-user-recovery-heading"
      className="feature-section admin-user-recovery"
    >
      {/* No eyebrow: the page header two lines above already says "System
          administration", and this said the same thing in different words. The
          destination was being named three times -- nav "User accounts", h1
          "Users", h2 "Account recovery" -- under two eyebrows. The h1 is the
          destination now; this h2 names the card's job, which is a different
          thing and may stay. */}
      <SettingsPaneHeader
        action={
          <StatusPill
            compact
            label={t("userRecovery.serverAdminOnly", {
              defaultValue: "Server admin only",
            })}
            status="restricted"
            tone="warn"
          />
        }
        description={t("userRecovery.description", {
          defaultValue:
            "Find an instance user and start a one-time recovery flow. You never choose or see the user's new password.",
        })}
        headingId="admin-user-recovery-heading"
        title={t("userRecovery.title", { defaultValue: "Account recovery" })}
      />

      <StatusBanner tone="info">
        {t("userRecovery.boundary", {
          defaultValue:
            "A successful reset signs the user out of every browser session. PAT/MCP tokens are separate and are revoked only when selected below.",
        })}
      </StatusBanner>
      {recoveryCapability.status === "loading" ? (
        <StatusBanner tone="info">
          {t("userRecovery.capabilityLoading", {
            defaultValue: "Checking server recovery availability…",
          })}
        </StatusBanner>
      ) : recoveryCapability.status === "error" ? (
        <StatusBanner tone="warning">
          {t("userRecovery.capabilityError", {
            defaultValue:
              "Recovery availability could not be confirmed. Reload before issuing a reset.",
          })}
        </StatusBanner>
      ) : !recoveryCapability.linkReady ? (
        <StatusBanner tone="warning">
          {t("userRecovery.adminRecoveryUnavailable", {
            defaultValue:
              "Admin-assisted recovery is unavailable until the canonical HTTPS public URL is ready.",
          })}
        </StatusBanner>
      ) : null}
      {successNotice ? (
        <StatusBanner tone="info">
          <span className="admin-recovery-notice">
            <CheckCircle2 aria-hidden="true" size={16} />
            {successNotice}
          </span>
        </StatusBanner>
      ) : null}
      {!directoryUnlocked ? (
        <form className="admin-user-reauth" onSubmit={onUnlockDirectory}>
          <div>
            <strong>
              {t("userRecovery.unlock.title", {
                defaultValue: "Unlock protected user search",
              })}
            </strong>
            <p className="meta-line">
              {t("userRecovery.unlock.description", {
                defaultValue:
                  "Re-enter your server-admin password before viewing the instance user directory.",
              })}
            </p>
          </div>
          {unlockError ? (
            <StatusBanner tone="warning">{unlockError}</StatusBanner>
          ) : null}
          <label
            className="field-stack"
            htmlFor="admin-user-directory-password"
          >
            <span>
              {t("userRecovery.unlock.password", {
                defaultValue: "Your current password",
              })}
            </span>
            <input
              autoComplete="current-password"
              onKeyDown={preventCompositionSubmit}
              disabled={unlocking}
              id="admin-user-directory-password"
              onChange={(event) => {
                setUnlockPassword(event.target.value);
                setUnlockError(null);
              }}
              required
              type="password"
              value={unlockPassword}
            />
          </label>
          <button
            className="icon-button primary"
            disabled={unlocking || !unlockPassword}
            type="submit"
          >
            {unlocking
              ? t("userRecovery.unlock.unlocking", {
                  defaultValue: "Verifying…",
                })
              : t("userRecovery.unlock.action", {
                  defaultValue: "Unlock user search",
                })}
          </button>
        </form>
      ) : (
        <>
          <StatusBanner tone="info">
            {t("userRecovery.unlock.unlocked", {
              defaultValue: "Protected user search is unlocked until {{date}}.",
              date: reauthenticatedUntil
                ? formatVersionTimestamp(reauthenticatedUntil)
                : t("userRecovery.unlock.shortTime", {
                    defaultValue: "the short verification window ends",
                  }),
            })}
          </StatusBanner>
          {searchError ? (
            <StatusBanner tone="warning">{searchError}</StatusBanner>
          ) : null}

          <form className="admin-user-search" onSubmit={onSearch}>
            <label className="field-stack" htmlFor="admin-user-search-query">
              <span>
                {t("userRecovery.searchLabel", {
                  defaultValue: "Search users",
                })}
              </span>
              <span className="meta-line">
                {t("userRecovery.searchHint", {
                  defaultValue: "Email, display name, or workspace name",
                })}
              </span>
              <input
                aria-describedby={
                  searchValidation ? "admin-user-search-error" : undefined
                }
                aria-invalid={Boolean(searchValidation)}
                autoComplete="off"
                id="admin-user-search-query"
                {...textInputLimit(320)}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setSearchValidation(null);
                }}
                placeholder={t("userRecovery.searchPlaceholder", {
                  defaultValue: "e.g. alex@example.com or Research",
                })}
                type="search"
                value={query}
              />
            </label>
            <button
              className="icon-button primary admin-user-search-action"
              disabled={searching}
              type="submit"
            >
              <Search aria-hidden="true" size={16} />
              {searching
                ? t("userRecovery.searching", { defaultValue: "Searching…" })
                : t("userRecovery.searchAction", { defaultValue: "Search" })}
            </button>
          </form>
          {searchValidation ? (
            <p className="form-error" id="admin-user-search-error" role="alert">
              {searchValidation}
            </p>
          ) : null}

          {searched ? (
            <div className="admin-user-search-results">
              <p aria-live="polite" className="meta-line">
                {t("userRecovery.resultCount", {
                  count: total,
                  defaultValue: "{{count}} users found",
                })}
              </p>
              {users.length === 0 ? (
                <SettingsEmptyState
                  description={t("userRecovery.noResultsHint", {
                    defaultValue: "Check the search term and try again.",
                  })}
                  title={t("userRecovery.noResults", {
                    defaultValue: "No users found",
                  })}
                />
              ) : (
                <ul className="admin-user-list">
                  {users.map((user) => (
                    <li className="admin-user-card" key={user.user_id}>
                      <div className="admin-user-card-main">
                        <div className="admin-user-identity">
                          <strong>{user.display_name || user.email}</strong>
                          <span>{user.email}</span>
                        </div>
                        <div className="admin-user-statuses">
                          <StatusPill
                            compact
                            label={
                              user.status === "active"
                                ? t("userRecovery.status.active", {
                                    defaultValue: "Active",
                                  })
                                : t("userRecovery.status.inactive", {
                                    defaultValue: "Inactive",
                                  })
                            }
                            status={user.status}
                            tone={user.status === "active" ? "ok" : "muted"}
                          />
                          <StatusPill
                            compact
                            label={
                              user.email_verified_at
                                ? t("userRecovery.emailVerified", {
                                    defaultValue: "Email verified",
                                  })
                                : t("userRecovery.emailUnverified", {
                                    defaultValue: "Email unverified",
                                  })
                            }
                            status={
                              user.email_verified_at ? "verified" : "unverified"
                            }
                            tone={user.email_verified_at ? "ok" : "warn"}
                          />
                        </div>
                        <p className="admin-user-workspaces">
                          {user.workspaces.length
                            ? user.workspaces
                                .map(
                                  (workspace) =>
                                    `${workspace.title} (${t(
                                      `userRecovery.roles.${workspace.role}`,
                                      { defaultValue: workspace.role },
                                    )})`,
                                )
                                .join(" · ")
                            : t("userRecovery.noWorkspaces", {
                                defaultValue: "No workspace memberships",
                              })}
                        </p>
                        {user.status !== "active" ? (
                          <p className="meta-line">
                            {t("userRecovery.inactiveHint", {
                              defaultValue:
                                "Recovery is available only for active accounts.",
                            })}
                          </p>
                        ) : null}
                      </div>
                      <div className="admin-user-recovery-actions">
                        <button
                          className="icon-button primary admin-user-recovery-action"
                          disabled={
                            user.status !== "active" ||
                            !user.email_verified_at ||
                            !recoveryCapability.emailReady
                          }
                          onClick={() => openRecovery(user, "email")}
                          type="button"
                        >
                          <Mail aria-hidden="true" size={16} />
                          {t("userRecovery.emailAction", {
                            defaultValue: "Send recovery email",
                          })}
                        </button>
                        <button
                          className="icon-button primary admin-user-recovery-action"
                          disabled={
                            user.status !== "active" ||
                            !recoveryCapability.linkReady
                          }
                          onClick={() => openRecovery(user, "link")}
                          type="button"
                        >
                          <Link2 aria-hidden="true" size={16} />
                          {t("userRecovery.linkAction", {
                            defaultValue: "Issue one-time link",
                          })}
                        </button>
                        {user.status === "active" && !user.email_verified_at ? (
                          <p className="meta-line">
                            {t("userRecovery.emailUnverifiedActionHint", {
                              defaultValue:
                                "Email delivery is unavailable because this address is not verified.",
                            })}
                          </p>
                        ) : user.status === "active" &&
                          recoveryCapability.status === "ready" &&
                          !recoveryCapability.emailReady ? (
                          <p className="meta-line">
                            {t("userRecovery.mailUnavailableActionHint", {
                              defaultValue:
                                "Email delivery is unavailable because server mail recovery is not ready.",
                            })}
                          </p>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              {total > 25 ? (
                <nav
                  aria-label={t("userRecovery.pagination.label", {
                    defaultValue: "User search pages",
                  })}
                  className="admin-user-pagination"
                >
                  <button
                    className="icon-button"
                    disabled={searching || offset === 0}
                    onClick={() =>
                      void loadUsers(submittedQuery, Math.max(0, offset - 25))
                    }
                    type="button"
                  >
                    {t("userRecovery.pagination.previous", {
                      defaultValue: "Previous",
                    })}
                  </button>
                  <span className="meta-line">
                    {t("userRecovery.pagination.position", {
                      defaultValue: "{{start}}–{{end}} of {{total}}",
                      start: offset + 1,
                      end: Math.min(offset + users.length, total),
                      total,
                    })}
                  </span>
                  <button
                    className="icon-button"
                    disabled={searching || offset + users.length >= total}
                    onClick={() => void loadUsers(submittedQuery, offset + 25)}
                    type="button"
                  >
                    {t("userRecovery.pagination.next", {
                      defaultValue: "Next",
                    })}
                  </button>
                </nav>
              ) : null}
            </div>
          ) : null}
        </>
      )}

      {target ? (
        <RecoveryConfirmationDialog
          busy={issuing}
          currentPassword={currentPassword}
          delivery={delivery}
          error={issueError}
          onCancel={closeRecovery}
          onCurrentPasswordChange={setCurrentPassword}
          onRecoveryModeChange={changeRecoveryMode}
          onRevokeApiTokensChange={setRevokeApiTokens}
          onSubmit={onIssue}
          recoveryMode={recoveryMode}
          revokeApiTokens={revokeApiTokens}
          target={target}
        />
      ) : null}
      {issuedLink ? (
        <OneTimeLinkDialog link={issuedLink} onClose={dismissIssuedLink} />
      ) : null}
    </section>
  );
}

function RecoveryConfirmationDialog({
  busy,
  currentPassword,
  delivery,
  error,
  onCancel,
  onCurrentPasswordChange,
  onRecoveryModeChange,
  onRevokeApiTokensChange,
  onSubmit,
  recoveryMode,
  revokeApiTokens,
  target,
}: {
  busy: boolean;
  currentPassword: string;
  delivery: AdminPasswordResetDelivery;
  error: string | null;
  onCancel: () => void;
  onCurrentPasswordChange: (value: string) => void;
  onRecoveryModeChange: (value: RecoveryMode) => void;
  onRevokeApiTokensChange: (value: boolean) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  recoveryMode: RecoveryMode;
  revokeApiTokens: boolean;
  target: AdminUser;
}) {
  const { t } = useTranslation("settings");
  const currentPasswordRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (error) {
      currentPasswordRef.current?.focus();
    }
  }, [error]);
  return (
    <AccessibleDialog
      busy={busy}
      labelledBy="admin-recovery-confirm-title"
      onCancel={onCancel}
    >
      <form className="modal-panel admin-recovery-modal" onSubmit={onSubmit}>
        <div>
          <p className="eyebrow">
            {t("userRecovery.dialog.eyebrow", {
              defaultValue: "Confirm privileged action",
            })}
          </p>
          <h2 id="admin-recovery-confirm-title">
            {t("userRecovery.dialog.title", {
              defaultValue: "Issue a password reset?",
            })}
          </h2>
        </div>
        <p>
          {t("userRecovery.dialog.description", {
            defaultValue:
              "This starts recovery for {{email}}. The user chooses the new password; you cannot view it.",
            email: target.email,
          })}
        </p>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <div className="admin-recovery-selected-delivery">
          {delivery === "email" ? (
            <Mail aria-hidden="true" size={18} />
          ) : (
            <Link2 aria-hidden="true" size={18} />
          )}
          <span>
            <strong>
              {delivery === "email"
                ? t("userRecovery.emailAction", {
                    defaultValue: "Send recovery email",
                  })
                : t("userRecovery.linkAction", {
                    defaultValue: "Issue one-time link",
                  })}
            </strong>
            <small>
              {delivery === "email"
                ? t("userRecovery.dialog.emailHint", {
                    defaultValue:
                      "Queues mail to the user's verified email address.",
                  })
                : t("userRecovery.dialog.linkHint", {
                    defaultValue:
                      "Shown once after confirmation. Send it through a separate secure channel.",
                  })}
            </small>
          </span>
        </div>
        <fieldset className="admin-recovery-mode">
          <legend>
            {t("userRecovery.dialog.mode", {
              defaultValue: "Recovery mode",
            })}
          </legend>
          <label htmlFor="admin-recovery-mode-ordinary">
            <input
              aria-label={t("userRecovery.dialog.ordinary", {
                defaultValue: "Ordinary recovery",
              })}
              checked={recoveryMode === "ordinary"}
              disabled={busy}
              id="admin-recovery-mode-ordinary"
              name="admin-recovery-mode"
              onChange={() => onRecoveryModeChange("ordinary")}
              type="radio"
              value="ordinary"
            />
            <span>
              <strong>
                {t("userRecovery.dialog.ordinary", {
                  defaultValue: "Ordinary recovery",
                })}
              </strong>
              <small>
                {t("userRecovery.dialog.ordinaryHint", {
                  defaultValue:
                    "Browser sessions are revoked after reset; PAT/MCP tokens stay active by default.",
                })}
              </small>
            </span>
          </label>
          <label htmlFor="admin-recovery-mode-compromise">
            <input
              aria-label={t("userRecovery.dialog.compromise", {
                defaultValue: "Suspected account compromise",
              })}
              checked={recoveryMode === "compromise"}
              disabled={busy}
              id="admin-recovery-mode-compromise"
              name="admin-recovery-mode"
              onChange={() => onRecoveryModeChange("compromise")}
              type="radio"
              value="compromise"
            />
            <span>
              <strong>
                <ShieldAlert aria-hidden="true" size={15} />
                {t("userRecovery.dialog.compromise", {
                  defaultValue: "Suspected account compromise",
                })}
              </strong>
              <small>
                {t("userRecovery.dialog.compromiseHint", {
                  defaultValue:
                    "Defaults to revoking every PAT/MCP token when the user completes the reset.",
                })}
              </small>
            </span>
          </label>
        </fieldset>
        <label
          className="field-stack"
          htmlFor="admin-recovery-current-password"
        >
          <span>
            {t("userRecovery.dialog.currentPassword", {
              defaultValue: "Your current password",
            })}
          </span>
          <span className="meta-line">
            {t("userRecovery.dialog.currentPasswordHint", {
              defaultValue:
                "Re-enter your server-admin password to authorize this request.",
            })}
          </span>
          <input
            autoComplete="current-password"
            onKeyDown={preventCompositionSubmit}
            disabled={busy}
            id="admin-recovery-current-password"
            onChange={(event) => onCurrentPasswordChange(event.target.value)}
            ref={currentPasswordRef}
            required
            type="password"
            value={currentPassword}
          />
        </label>
        <label
          className="admin-recovery-token-option"
          htmlFor="admin-recovery-revoke-tokens"
        >
          <input
            aria-label={t("userRecovery.dialog.revokeTokens", {
              defaultValue: "Also revoke all PAT/MCP tokens",
            })}
            checked={revokeApiTokens}
            disabled={busy}
            id="admin-recovery-revoke-tokens"
            onChange={(event) => onRevokeApiTokensChange(event.target.checked)}
            type="checkbox"
          />
          <span>
            <strong>
              {t("userRecovery.dialog.revokeTokens", {
                defaultValue: "Also revoke all PAT/MCP tokens",
              })}
            </strong>
            <small>
              {t("userRecovery.dialog.revokeTokensHint", {
                defaultValue:
                  "Use this for suspected account takeover. Browser sessions are always revoked after reset.",
              })}
            </small>
          </span>
        </label>
        <div className="modal-actions">
          <button
            className="icon-button"
            disabled={busy}
            onClick={onCancel}
            type="button"
          >
            {t("userRecovery.dialog.cancel", { defaultValue: "Cancel" })}
          </button>
          <button
            className="icon-button primary"
            disabled={busy || !currentPassword}
            type="submit"
          >
            {busy
              ? t("userRecovery.dialog.issuing", {
                  defaultValue: "Issuing reset…",
                })
              : t("userRecovery.dialog.confirm", {
                  defaultValue: "Issue password reset",
                })}
          </button>
        </div>
      </form>
    </AccessibleDialog>
  );
}

/** Stands in for a query-string value the screen must not carry. */
const REDACTED_QUERY_VALUE = "\u2026";

/**
 * The one-time recovery URL as it is safe to put on a screen.
 *
 * The full URL is a live credential: whoever reads it owns the account until
 * the link is used or expires. Rendering it into an input publishes it much
 * further than the room the admin is sitting in. Playwright generates an
 * `error-context.md` for every failed test whose accessibility snapshot prints
 * form-control *values* verbatim, and the HTML reporter copies that file into
 * `playwright-report/data/`, so one failed assertion taken while this dialog
 * is open ships the token as plaintext into a CI artifact -- with screenshots
 * and tracing both switched off. A recorded or screen-shared admin session
 * leaks it the same way.
 *
 * What the operator needs to read off the screen is that the link points at
 * the expected host and page; the secret only ever needs to reach the
 * clipboard. So keep the origin and path, redact every query value, and redact
 * any fragment. Redacting every value rather than a `token` denylist means a
 * parameter added later inherits the protection instead of leaking until
 * someone remembers this function.
 */
function maskRecoveryUrl(rawUrl: string): string {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    // Not something we can take apart, so we cannot say which half is safe.
    return REDACTED_QUERY_VALUE;
  }
  const visible = `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
  const names = Array.from(new Set(parsed.searchParams.keys()));
  const query = names
    .map((name) => `${name}=${REDACTED_QUERY_VALUE}`)
    .join("&");
  const fragment = parsed.hash ? `#${REDACTED_QUERY_VALUE}` : "";
  return `${visible}${query ? `?${query}` : ""}${fragment}`;
}

/**
 * Copy `text` without ever rendering it.
 *
 * `navigator.clipboard` is gated on a secure context, and the deployment that
 * lacks one -- self-hosted dtxWiki behind plain HTTP -- is exactly where an
 * admin still has to hand a recovery link to a locked-out user. `execCommand`
 * is deprecated but is the only copy path those browsers have.
 *
 * The carrier is created, read and removed inside a single synchronous turn,
 * so it is never painted, never part of a React commit, and cannot be observed
 * by a snapshot taken between renders. That is the whole point: the fallback
 * must not undo the masking above.
 */
function copyThroughTransientNode(text: string): boolean {
  if (
    typeof document === "undefined" ||
    typeof document.execCommand !== "function"
  ) {
    return false;
  }
  const carrier = document.createElement("textarea");
  carrier.value = text;
  carrier.readOnly = true;
  carrier.tabIndex = -1;
  carrier.setAttribute("aria-hidden", "true");
  carrier.style.position = "fixed";
  carrier.style.top = "0";
  carrier.style.opacity = "0";
  carrier.style.pointerEvents = "none";
  const previouslyFocused =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  document.body.append(carrier);
  try {
    carrier.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    carrier.remove();
    previouslyFocused?.focus();
  }
}

function OneTimeLinkDialog({
  link,
  onClose,
}: {
  link: IssuedLink;
  onClose: () => void;
}) {
  const { t } = useTranslation("settings");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(false);
  const maskedUrl = maskRecoveryUrl(link.resetUrl);

  // Last resort only: both programmatic copy paths failed, so the operator has
  // to select the link by hand or leave a locked-out user locked out. Focus and
  // native selection have to wait for the render that swaps the masked value
  // for the real one, which is why this is an effect rather than part of the
  // click handler.
  useEffect(() => {
    if (!revealed) {
      return;
    }
    const field = inputRef.current;
    field?.focus();
    field?.select();
    setCopyStatus(
      field
        ? t("userRecovery.linkDialog.selected", {
            defaultValue: "Link selected. Press Ctrl+C to copy.",
          })
        : t("userRecovery.linkDialog.copyUnavailable", {
            defaultValue: "Copy unavailable. Select the link manually.",
          }),
    );
  }, [revealed, t]);

  // Ordered by how much of the credential each rung exposes: the Clipboard API
  // and the transient carrier below never render it, revealing it does.
  async function copyLink() {
    if (canWriteToClipboard()) {
      try {
        await navigator.clipboard.writeText(link.resetUrl);
        setCopyStatus(
          t("userRecovery.linkDialog.copied", {
            defaultValue: "Recovery link copied.",
          }),
        );
        return;
      } catch {
        // Permission denied or a transient failure: try the older path before
        // falling back to putting the secret on screen.
      }
    }
    if (copyThroughTransientNode(link.resetUrl)) {
      setCopyStatus(
        t("userRecovery.linkDialog.copied", {
          defaultValue: "Recovery link copied.",
        }),
      );
      return;
    }
    // Already on screen from an earlier attempt: the operator has most likely
    // lost the selection by now, so hand it back instead of doing nothing.
    if (revealed) {
      inputRef.current?.focus();
      inputRef.current?.select();
      return;
    }
    setRevealed(true);
  }

  return (
    <AccessibleDialog
      busy={false}
      dismissible={false}
      labelledBy="admin-recovery-link-title"
      onCancel={onClose}
    >
      <div className="modal-panel admin-recovery-modal">
        <div>
          <p className="eyebrow">
            {t("userRecovery.linkDialog.eyebrow", {
              defaultValue: "One-time reveal",
            })}
          </p>
          <h2 id="admin-recovery-link-title">
            {t("userRecovery.linkDialog.title", {
              defaultValue: "Copy this recovery link now",
            })}
          </h2>
        </div>
        <StatusBanner tone="warning">
          {t("userRecovery.linkDialog.warning", {
            defaultValue:
              "This link is shown once. Closing this dialog permanently hides it; it cannot be reopened.",
          })}
        </StatusBanner>
        <p>
          {t("userRecovery.linkDialog.recipient", {
            defaultValue:
              "Send the link to {{email}} through a separate secure channel.",
            email: link.email,
          })}
        </p>
        <label className="field-stack" htmlFor="admin-recovery-issued-link">
          <span>
            {t("userRecovery.linkDialog.linkLabel", {
              defaultValue: "Recovery link",
            })}
          </span>
          <input
            id="admin-recovery-issued-link"
            onFocus={(event) => event.currentTarget.select()}
            readOnly
            ref={inputRef}
            type="text"
            value={revealed ? link.resetUrl : maskedUrl}
          />
        </label>
        <p className="meta-line">
          {t("userRecovery.linkDialog.expires", {
            defaultValue: "Expires {{date}}",
            date: formatVersionTimestamp(link.expiresAt),
          })}
        </p>
        {link.revokeApiTokens ? (
          <p className="meta-line">
            {t("userRecovery.linkDialog.tokensWillRevoke", {
              defaultValue:
                "All PAT/MCP tokens will be revoked when this reset is completed.",
            })}
          </p>
        ) : null}
        <p aria-live="polite" className="meta-line">
          {copyStatus}
        </p>
        <div className="modal-actions">
          <button
            className="icon-button"
            onClick={() => void copyLink()}
            type="button"
          >
            <Copy aria-hidden="true" size={16} />
            {t("userRecovery.linkDialog.copy", { defaultValue: "Copy link" })}
          </button>
          <button
            className="icon-button primary"
            onClick={onClose}
            type="button"
          >
            {t("userRecovery.linkDialog.close", {
              defaultValue: "Close and hide link",
            })}
          </button>
        </div>
      </div>
    </AccessibleDialog>
  );
}

function AccessibleDialog({
  busy,
  children,
  dismissible = true,
  labelledBy,
  onCancel,
}: {
  busy: boolean;
  children: ReactNode;
  dismissible?: boolean;
  labelledBy: string;
  onCancel: () => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const focusable = focusableElements(containerRef.current);
    focusable[0]?.focus();
    return () => {
      const previous = previousFocusRef.current;
      if (previous && document.contains(previous)) {
        previous.focus();
      }
    };
  }, []);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (dismissible && !busy) {
        onCancel();
      }
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const focusable = focusableElements(containerRef.current);
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const dialog = (
    // Keyboard handling is scoped to the focus-trapped modal boundary.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div
      aria-labelledby={labelledBy}
      aria-modal="true"
      className="modal-backdrop"
      onKeyDown={onKeyDown}
      onMouseDown={(event) => {
        if (
          event.target === event.currentTarget &&
          dismissible &&
          !busy
        ) {
          onCancel();
        }
      }}
      ref={containerRef}
      role="dialog"
      tabIndex={-1}
    >
      {children}
    </div>
  );

  return typeof document === "undefined"
    ? dialog
    : createPortal(dialog, document.body);
}

function focusableElements(root: HTMLElement | null) {
  if (!root) {
    return [];
  }
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute("hidden"));
}

function adminRecoveryErrorMessage(error: unknown, t: TFunction<"settings">) {
  if (error instanceof ApiError) {
    if (
      error.body.error_code === "invalid_credentials" ||
      error.body.error_code === "recent_reauthentication_required"
    ) {
      return t("userRecovery.errors.reauthentication", {
        defaultValue: "Your current password is incorrect. Try again.",
      });
    }
    if (error.body.error_code === "password_recovery_unavailable") {
      return t("userRecovery.errors.unavailable", {
        defaultValue:
          "That delivery method is unavailable. Close this dialog and explicitly choose an available recovery action.",
      });
    }
  }
  const fallback = t("userRecovery.errors.generic", {
    defaultValue: "Password reset could not be issued. Try again.",
  });
  return error instanceof ApiError
    ? apiErrorMessage(error, fallback)
    : fallback;
}
