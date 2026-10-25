import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getLoginIdentities,
  requestEmailVerification,
  setAccountPassword,
  startIdentityFlow,
  unlinkLoginIdentity,
  type AuthSession,
  type LoginIdentities,
} from "../../shared/api/auth";
import { apiErrorMessage } from "../../shared/api/client";
import { useAuth } from "../../shared/auth/AuthContext";
import {
  preventCompositionSubmit,
  normalizeAccountEmail,
  validateAccountPassword,
} from "../../shared/auth/identityPolicy";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { textInputLimit } from "../../shared/lib/inputPolicy";
import { ExternalAuthErrorBanner } from "./ExternalAuthErrorBanner";
import { IdentityVerificationPanel } from "./IdentityVerificationPanel";
import { loginMethodLabel, loginMethodStyle } from "./loginMethodBranding";
import { linkedLoginMethods } from "./loginMethods";

export function LoginMethodsSection() {
  const { t, i18n } = useTranslation("auth");
  const { user, setAuthenticatedSession, refreshSession, capabilitiesChecked, emailEnrollmentEnabled, refreshCapabilities } = useAuth();
  const [data, setData] = useState<LoginIdentities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [nativeEmail, setNativeEmail] = useState("");

  useEffect(() => {
    if (user && !user.email && !capabilitiesChecked) void refreshCapabilities();
  }, [capabilitiesChecked, refreshCapabilities, user]);

  useEffect(() => {
    let active = true;
    void getLoginIdentities()
      .then((value) => {
        if (active) setData(value);
      })
      .catch((caught) => {
        if (active) {
          setError(apiErrorMessage(caught, t("loginMethods.failed")));
        }
      });
    return () => {
      active = false;
    };
  }, [t, user?.user_id]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      await action();
    } catch (caught) {
      setError(apiErrorMessage(caught, t("loginMethods.failed")));
    } finally {
      setBusy(false);
    }
  }

  async function refreshAccount(session: AuthSession) {
    setAuthenticatedSession(session);
    setData(await getLoginIdentities());
    await refreshSession();
  }

  async function onSetPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      validateAccountPassword(newPassword, true);
    } catch (caught) {
      setError(t((caught as Error).message));
      return;
    }
    if (newPassword !== confirmPassword) {
      setError(t("password.mismatch"));
      return;
    }
    await run(async () => {
      await refreshAccount(await setAccountPassword(newPassword));
      setNewPassword("");
      setConfirmPassword("");
      setStatus(t("loginMethods.passwordSet"));
    });
  }

  async function unlinkIdentity(identityId: string) {
    await run(async () => {
      await refreshAccount(await unlinkLoginIdentity(identityId));
      setStatus(t("loginMethods.unlinked"));
    });
  }

  async function linkProvider(slug: string) {
    await run(async () => {
      const result = await startIdentityFlow(slug, "link", "/account");
      window.location.assign(result.redirect_url);
    });
  }

  async function onConnectEmail(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let email: string;
    try { email = normalizeAccountEmail(nativeEmail); }
    catch { setError(t("validation.email")); return; }
    await run(async () => {
      await requestEmailVerification(email);
      setStatus(t("loginMethods.emailConnectionSent"));
    });
  }

  const providers = data?.available_providers ?? [];
  const linkedProviders = data ? linkedLoginMethods(data) : [];
  const linkedSlugs = new Set(linkedProviders.map((provider) => provider.slug));
  const unlinkedProviders = providers.filter(
    (provider) => !linkedSlugs.has(provider.slug),
  );
  const canManage = data !== null && (
    !data.has_password || providers.length > 0 || data.identities.length > 0
  );
  const providerName = (slug: string) => {
    const provider = providers.find((item) => item.slug === slug);
    return provider ? loginMethodLabel(provider, i18n.language) : slug;
  };
  const currentSessionLabel = user?.session_provider_slug
    ? providerName(user.session_provider_slug)
    : t("loginMethods.password");

  return (
    <section className="auth-panel" aria-labelledby="login-methods-title">
      <div className="panel-header">
        <h2 id="login-methods-title">{t("loginMethods.title")}</h2>
      </div>
      <p className="account-panel-hint">{t("loginMethods.description")}</p>
      {user?.session_method ? (
        <p className="account-panel-hint">
          {t("loginMethods.currentSession", { method: currentSessionLabel })}
        </p>
      ) : null}
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      <ExternalAuthErrorBanner />
      {status ? <StatusBanner tone="info">{status}</StatusBanner> : null}
      {data ? (
        <>
          <p>
            {data.has_password
              ? t("loginMethods.passwordAvailable")
              : t("loginMethods.noPassword")}
          </p>
          {data.identities.length > 0 ? (
            <ul>
              {data.identities.map((identity) => (
                <li key={identity.id}>
                  <strong>{providerName(identity.provider_slug)}</strong>
                  {identity.last_email ? (
                    <p className="meta-line">{identity.last_email}</p>
                  ) : null}
                  {identity.last_login_at ? (
                    <p className="meta-line">
                      {t("loginMethods.lastLogin", {
                        date: formatVersionTimestamp(identity.last_login_at),
                      })}
                    </p>
                  ) : null}
                  <button
                    type="button"
                    className="icon-button"
                    disabled={busy || !identity.can_unlink}
                    onClick={() => void unlinkIdentity(identity.id)}
                  >
                    {t("loginMethods.unlink")}
                  </button>
                  {!identity.can_unlink ? (
                    <p className="field-hint">{t("loginMethods.lastMethod")}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
          {canManage ? (
            <p className="account-panel-hint">{t("loginMethods.verifyHint")}</p>
          ) : null}
          {canManage ? <IdentityVerificationPanel scope="account" returnTo="/account" /> : null}
          {unlinkedProviders.length > 0 ? (
            <div className="auth-actions">
              {unlinkedProviders.map((provider) => (
                <button
                  className="icon-button"
                  style={loginMethodStyle(provider)}
                  type="button"
                  key={provider.slug}
                  disabled={busy}
                  onClick={() => void linkProvider(provider.slug)}
                >
                  {t("loginMethods.link", { provider: loginMethodLabel(provider, i18n.language) })}
                </button>
              ))}
            </div>
          ) : null}
          {!user?.email && emailEnrollmentEnabled ? <form className="auth-form" onSubmit={(event) => void onConnectEmail(event)}>
            <p>{t("loginMethods.emailConnectionHint")}</p>
            <label className="field-stack">{t("loginMethods.nativeEmail")}
              <input type="email" required autoComplete="email" {...textInputLimit(320)} value={nativeEmail} disabled={busy}
                onKeyDown={preventCompositionSubmit}
                onChange={(event) => setNativeEmail(event.target.value)} />
            </label>
            <button type="submit" className="icon-button" disabled={busy}>{t("loginMethods.connectEmail")}</button>
          </form> : null}
          {!data.has_password && user?.email ? (
            <form
              className="auth-form"
              onSubmit={(event) => void onSetPassword(event)}
            >
              <label className="field-stack">
                {t("password.new")}
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  value={newPassword}
                  onKeyDown={preventCompositionSubmit}
                  onChange={(event) => setNewPassword(event.target.value)}
                />
              </label>
              <p className="field-hint">{t("password.rule")}</p>
              <label className="field-stack">
                {t("password.confirm")}
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  value={confirmPassword}
                  onKeyDown={preventCompositionSubmit}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                />
              </label>
              <button className="icon-button" type="submit" disabled={busy}>
                {t("loginMethods.setPassword")}
              </button>
            </form>
          ) : null}
        </>
      ) : !error ? (
        <p role="status">{t("loginMethods.loading")}</p>
      ) : null}
    </section>
  );
}
