import { KeyRound } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { checkPasswordResetStatus, confirmPasswordReset } from "../../shared/api/auth";
import { ApiError } from "../../shared/api/client";
import { useAuth } from "../../shared/auth/AuthContext";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useOneTimeLinkToken } from "./oneTimeLinkToken";

export function ResetPasswordScreen() {
  const { t } = useTranslation("auth");
  const {
    capabilitiesChecked,
    capabilitiesError,
    passwordRecoveryMode,
  } = useAuth();
  const navigate = useNavigate();
  const token = useOneTimeLinkToken();
  const apiTokenRevocationForced = token.startsWith("dtxr_1_");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [revokeApiTokens, setRevokeApiTokens] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linkIssue, setLinkIssue] = useState<string | null>(null);
  const resetUnavailable = !token || linkIssue !== null;
  const emailRecoveryAvailable =
    capabilitiesChecked &&
    capabilitiesError === null &&
    passwordRecoveryMode === "email";

  useEffect(() => {
    if (!token) {
      return;
    }
    let cancelled = false;
    setLinkIssue(null);
    checkPasswordResetStatus(token).catch((statusError) => {
      if (cancelled) {
        return;
      }
      if (!(statusError instanceof ApiError)) {
        // Offline/network failures leave linkIssue untouched -- the form
        // still shows, matching the existing onSubmit fallback.
        return;
      }
      if (statusError.body.error_code === "password_reset_token_expired") {
        setLinkIssue(t("reset.expired"));
      } else if (statusError.body.error_code === "password_reset_token_used") {
        setLinkIssue(t("reset.used"));
      } else if (statusError.status === 429 || statusError.status >= 500) {
        // Transient failure -- inconclusive, not evidence the link itself is
        // invalid, so fail open the same way onSubmit does for these codes.
        return;
      } else {
        setLinkIssue(t("reset.invalid"));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [t, token]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) {
      return;
    }
    if (newPassword !== confirmation) {
      setError(t("reset.mismatch"));
      return;
    }
    setSubmitting(true);
    setError(null);
    setLinkIssue(null);
    try {
      await confirmPasswordReset({
        token,
        new_password: newPassword,
        revoke_api_tokens: apiTokenRevocationForced || revokeApiTokens,
      });
      navigate("/login?reset=complete", { replace: true });
    } catch (resetError) {
      if (resetError instanceof ApiError) {
        if (resetError.body.error_code === "password_reset_token_expired") {
          setLinkIssue(t("reset.expired"));
        } else if (resetError.body.error_code === "password_reset_token_used") {
          setLinkIssue(t("reset.used"));
        } else if (resetError.status === 429) {
          setError(t("reset.rateLimited"));
        } else if (resetError.status >= 500) {
          setError(t("reset.serverError"));
        } else {
          setLinkIssue(t("reset.invalid"));
        }
      } else {
        setError(t("reset.offline"));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="workspace-screen single-column auth-screen">
      <section className="feature-main auth-layout">
        <Breadcrumbs items={[{ label: t("reset.breadcrumb") }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("reset.eyebrow")}</p>
            <h1>{resetUnavailable ? t("reset.unavailableTitle") : t("reset.title")}</h1>
          </div>
        </div>
        {resetUnavailable ? (
          <StatusBanner tone="warning">
            {/* No token: the page was opened or reloaded without the link,
                which is cleared from the address bar once it is read. */}
            {linkIssue ?? t("reset.missing")}
          </StatusBanner>
        ) : null}
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <section
          className="auth-panel"
          aria-label={
            resetUnavailable ? t("reset.unavailableAria") : t("reset.formAria")
          }
        >
          <p className="auth-recovery-note">
            {resetUnavailable ? t("reset.unavailableSubtitle") : t("reset.subtitle")}
          </p>
          {!resetUnavailable ? (
            <form className="auth-form" onSubmit={onSubmit}>
              <label className="field-stack">
                {t("reset.new")}
                <input
                  autoComplete="new-password"
                  minLength={8}
                  name="newPassword"
                  onChange={(event) => setNewPassword(event.target.value)}
                  required
                  type="password"
                  value={newPassword}
                />
              </label>
              <label className="field-stack">
                {t("reset.confirm")}
                <input
                  autoComplete="new-password"
                  minLength={8}
                  name="confirmPassword"
                  onChange={(event) => setConfirmation(event.target.value)}
                  required
                  type="password"
                  value={confirmation}
                />
              </label>
              <p className="auth-recovery-note">{t("reset.rule")}</p>
              {apiTokenRevocationForced ? (
                <StatusBanner tone="warning">
                  {t("reset.revokeApiTokensForced", {
                    defaultValue:
                      "This link was issued for suspected account compromise. All API/MCP tokens must be revoked when the password is reset.",
                  })}
                </StatusBanner>
              ) : null}
              <label className="checkbox-field auth-remember">
                <input
                  checked={apiTokenRevocationForced || revokeApiTokens}
                  disabled={apiTokenRevocationForced}
                  onChange={(event) => setRevokeApiTokens(event.target.checked)}
                  type="checkbox"
                />
                <span>{t("reset.revokeApiTokens")}</span>
              </label>
              <p className="auth-recovery-note">
                {apiTokenRevocationForced
                  ? t("reset.revokeApiTokensForcedHint", {
                      defaultValue:
                        "This protected recovery scope cannot be turned off. Connected tools will need new credentials.",
                    })
                  : t("reset.revokeApiTokensHint")}
              </p>
              <div className="auth-actions">
                <button
                  className="icon-button primary"
                  disabled={submitting}
                  type="submit"
                >
                  <KeyRound size={16} aria-hidden="true" />
                  <span>
                    {submitting ? t("reset.submitting") : t("reset.submit")}
                  </span>
                </button>
                <Link className="icon-button text-button" to="/login">
                  {t("reset.back")}
                </Link>
              </div>
            </form>
          ) : (
            <div className="auth-actions">
              {emailRecoveryAvailable ? (
                <Link className="icon-button primary" to="/forgot-password">
                  {t("reset.requestNew")}
                </Link>
              ) : null}
              <Link className="icon-button text-button" to="/login">
                {t("reset.back")}
              </Link>
            </div>
          )}
        </section>
      </section>
    </div>
  );
}
