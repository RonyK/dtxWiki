import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
  approvedDeletionWorkspaces,
  getMyDeletionPreview,
  type UserDeletionPreview,
  withdrawAccount,
} from "../../shared/api/userDeletion";
import { useAuth } from "../../shared/auth/AuthContext";
import {
  preventCompositionSubmit,
  sameAccountEmail,
  validateAccountPassword,
} from "../../shared/auth/identityPolicy";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { UserDeletionImpact } from "../../shared/components/UserDeletionImpact";
import { IdentityVerificationPanel } from "./IdentityVerificationPanel";

export function AccountDeletionPanel({ email, hasPassword = true }: {
  email: string | null;
  hasPassword?: boolean;
}) {
  const { t } = useTranslation("auth");
  const { signOut } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<UserDeletionPreview | null>(null);
  const [confirmationEmail, setConfirmationEmail] = useState("");
  const [password, setPassword] = useState("");
  // Without a password, a recent account verification confirms instead.
  const [verified, setVerified] = useState(false);
  const [verificationAttempt, setVerificationAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Reloading helps only when the details are missing or out of date.
  const [canReload, setCanReload] = useState(false);
  const [policyAttempt, setPolicyAttempt] = useState(0);
  const retentionDays = preview?.retention_days ?? null;
  // The account email is typed to confirm; an account without one skips it.
  const emailConfirmed = email === null || sameAccountEmail(confirmationEmail, email);
  const credentialConfirmed = hasPassword ? password !== "" : verified;
  // Workspaces with other active members must be deleted (or given another
  // admin) in their own Settings first; withdrawal waits for a re-check.
  const blocked = (preview?.blocking_workspaces.length ?? 0) > 0;

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setPreview(null);
    setError(null);
    setCanReload(false);
    void getMyDeletionPreview().then((loaded) => {
      if (active) setPreview(loaded);
    }).catch((caught) => {
      if (!active) return;
      setError(apiErrorMessage(caught, t("deletion.policyFailed")));
      setCanReload(true);
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [open, policyAttempt, t]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      busy || preview === null || retentionDays === null || blocked || !emailConfirmed
      || !credentialConfirmed
    ) {
      return;
    }
    if (hasPassword) {
      try {
        validateAccountPassword(password);
      } catch (caught) {
        setError(t((caught as Error).message));
        return;
      }
    }
    setBusy(true);
    setError(null);
    setCanReload(false);
    try {
      await withdrawAccount({
        confirmation_email: email === null ? undefined : confirmationEmail,
        current_password: hasPassword ? password : undefined,
        expected_retention_days: retentionDays,
        approved_workspaces: approvedDeletionWorkspaces(preview),
      });
      setPassword("");
      await signOut();
      navigate("/login?account=deletion-pending", { replace: true });
    } catch (caught) {
      setPassword("");
      if (caught instanceof ApiError && caught.body.error_code === "recent_reauthentication_required") {
        setVerified(false);
        setVerificationAttempt((value) => value + 1);
      }
      const localized = caught instanceof ApiError
        ? t(`deletion.errors.${caught.body.error_code}`, { defaultValue: "" })
        : "";
      setError(localized || apiErrorMessage(caught, t("deletion.failed")));
      setCanReload(
        caught instanceof ApiError
          && [
            "user_deletion_policy_changed",
            "user_deletion_targets_changed",
            "user_deletion_last_owner",
          ].includes(caught.body.error_code),
      );
      setBusy(false);
    }
  }

  function close() {
    if (busy) return;
    setOpen(false);
    setConfirmationEmail("");
    setPassword("");
    setVerified(false);
    setError(null);
  }

  return (
    <section
      className="auth-panel account-deletion-panel"
      aria-labelledby="account-deletion-title"
    >
      <div className="panel-header">
        <h2 id="account-deletion-title">{t("deletion.title")}</h2>
      </div>
      <p className="account-panel-hint">{t("deletion.description")}</p>
      {open ? (
        <form
          className="account-deletion-form"
          onSubmit={onSubmit}
          aria-label={t("deletion.title")}
        >
          {loading ? <p role="status">{t("deletion.loading")}</p> : null}
          {preview !== null ? (
            <>
              <StatusBanner tone="warning">
                {t("deletion.retention", { count: preview.retention_days })}
              </StatusBanner>
              <UserDeletionImpact preview={preview} />
              <button
                className="icon-button"
                disabled={busy || loading}
                onClick={() => setPolicyAttempt((value) => value + 1)}
                type="button"
              >{t("deletion.recheck")}</button>
            </>
          ) : null}
          <p className="field-hint">{t("deletion.sharedContent")}</p>
          {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
          {error && canReload && preview === null ? (
            <button
              className="icon-button"
              disabled={busy || loading}
              onClick={() => setPolicyAttempt((value) => value + 1)}
              type="button"
            >{t("deletion.reloadPolicy")}</button>
          ) : null}
          {email !== null ? (
            <label className="field-stack" htmlFor="account-deletion-email">
              <span>{t("deletion.confirmEmail", { email })}</span>
              <input
                id="account-deletion-email"
                autoComplete="off"
                disabled={busy}
                onChange={(event) => setConfirmationEmail(event.target.value)}
                onKeyDown={preventCompositionSubmit}
                required
                type="email"
                value={confirmationEmail}
              />
            </label>
          ) : null}
          {hasPassword ? (
            <label className="field-stack" htmlFor="account-deletion-password">
              <span>{t("deletion.password")}</span>
              <input
                id="account-deletion-password"
                autoComplete="current-password"
                disabled={busy}
                maxLength={128}
                onChange={(event) => setPassword(event.target.value)}
                onKeyDown={preventCompositionSubmit}
                required
                type="password"
                value={password}
              />
            </label>
          ) : (
            <div className="field-stack">
              <span>{t("deletion.verify")}</span>
              {/* Its controls are plain buttons, so they never submit this form. */}
              <IdentityVerificationPanel
                key={verificationAttempt}
                scope="account"
                returnTo="/account"
                onVerified={() => setVerified(true)}
              />
            </div>
          )}
          <div className="account-actions">
            <button className="icon-button" disabled={busy} onClick={close} type="button">
              {t("deletion.cancel")}
            </button>
            <button
              className="icon-button danger-action"
              disabled={
                busy || loading || retentionDays === null || blocked || !emailConfirmed
                || !credentialConfirmed
              }
              type="submit"
            >{t(busy ? "deletion.submitting" : "deletion.submit")}</button>
          </div>
        </form>
      ) : (
        <button
          className="icon-button danger-action"
          onClick={() => setOpen(true)}
          type="button"
          aria-expanded={false}
        >{t("deletion.open")}</button>
      )}
    </section>
  );
}
