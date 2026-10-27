import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
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

export function AccountDeletionPanel({ email }: { email: string }) {
  const { t } = useTranslation("auth");
  const { signOut } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<UserDeletionPreview | null>(null);
  const [confirmationEmail, setConfirmationEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Reloading helps only when the details are missing or out of date.
  const [canReload, setCanReload] = useState(false);
  const [policyAttempt, setPolicyAttempt] = useState(0);
  const retentionDays = preview?.retention_days ?? null;
  const emailConfirmed = sameAccountEmail(confirmationEmail, email);

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
    if (busy || retentionDays === null || !emailConfirmed) return;
    try {
      validateAccountPassword(password);
    } catch (caught) {
      setError(t((caught as Error).message));
      return;
    }
    setBusy(true);
    setError(null);
    setCanReload(false);
    try {
      await withdrawAccount({
        confirmation_email: confirmationEmail,
        current_password: password,
        expected_retention_days: retentionDays,
      });
      setPassword("");
      await signOut();
      navigate("/login?account=deletion-pending", { replace: true });
    } catch (caught) {
      setPassword("");
      const localized = caught instanceof ApiError
        ? t(`deletion.errors.${caught.body.error_code}`, { defaultValue: "" })
        : "";
      setError(localized || apiErrorMessage(caught, t("deletion.failed")));
      setCanReload(
        caught instanceof ApiError
          && caught.body.error_code === "user_deletion_policy_changed",
      );
      setBusy(false);
    }
  }

  function close() {
    if (busy) return;
    setOpen(false);
    setConfirmationEmail("");
    setPassword("");
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
            </>
          ) : null}
          <p className="field-hint">{t("deletion.sharedContent")}</p>
          {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
          {error && canReload ? (
            <button
              className="icon-button"
              disabled={busy || loading}
              onClick={() => setPolicyAttempt((value) => value + 1)}
              type="button"
            >{t("deletion.reloadPolicy")}</button>
          ) : null}
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
          <div className="account-actions">
            <button className="icon-button" disabled={busy} onClick={close} type="button">
              {t("deletion.cancel")}
            </button>
            <button
              className="icon-button danger-action"
              disabled={busy || loading || retentionDays === null || !emailConfirmed || !password}
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
