import { Check, KeyRound, Link2, Plus, ShieldCheck, Unlink } from "lucide-react";
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
  type LoginIdentity,
} from "../../shared/api/auth";
import { apiErrorMessage } from "../../shared/api/client";
import { useAuth } from "../../shared/auth/AuthContext";
import {
  preventCompositionSubmit,
  normalizeAccountEmail,
  validateAccountPassword,
} from "../../shared/auth/identityPolicy";
import { Dialog } from "../../shared/components/Dialog";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import { textInputLimit } from "../../shared/lib/inputPolicy";
import { ExternalAuthErrorBanner } from "./ExternalAuthErrorBanner";
import { IdentityVerificationPanel } from "./IdentityVerificationPanel";
import { loginMethodLabel, loginMethodStyle } from "./loginMethodBranding";
import { linkedLoginMethods } from "./loginMethods";

export function LoginMethodsSection() {
  const { t, i18n } = useTranslation("auth");
  const { user, setAuthenticatedSession, refreshSession, capabilitiesChecked, passwordLoginEnabled, emailEnrollmentEnabled, refreshCapabilities } = useAuth();
  const [data, setData] = useState<LoginIdentities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [accountEmailDraft, setAccountEmailDraft] = useState("");
  const [pendingUnlink, setPendingUnlink] = useState<LoginIdentity | null>(null);

  useEffect(() => {
    if (user && !capabilitiesChecked) void refreshCapabilities();
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
      setPendingUnlink(null);
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
    try { email = normalizeAccountEmail(accountEmailDraft); }
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
    <section className="auth-panel login-methods-panel" aria-labelledby="login-methods-title">
      <div className="login-methods-heading">
        <div>
          <h2 id="login-methods-title">{t("loginMethods.title")}</h2>
          <p className="account-panel-hint">{t("loginMethods.description")}</p>
        </div>
        <a className="icon-button" id="login-methods-add-action"
          href={data ? "#login-methods-add" : undefined} aria-disabled={!data || undefined}>
          <Plus size={16} aria-hidden="true" />
          {t("loginMethods.add")}
        </a>
      </div>
      {error && !pendingUnlink ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      <ExternalAuthErrorBanner />
      {status ? <StatusBanner tone="info">{status}</StatusBanner> : null}
      {data ? (
        <>
          <div className="login-methods-summary">
            <ShieldCheck size={18} aria-hidden="true" />
            <p>{user?.session_method
              ? t("loginMethods.currentSession", { method: currentSessionLabel })
              : t("loginMethods.connectedDescription")}</p>
          </div>
          <div className="login-methods-group">
            <h3>{t("loginMethods.connected")}</h3>
            <ul className="login-methods-list">
              {data.has_password && capabilitiesChecked && passwordLoginEnabled ? (
                <li className="login-method-card">
                  <span className="login-method-icon"><KeyRound size={20} aria-hidden="true" /></span>
                  <div className="login-method-details">
                    <strong>{t("loginMethods.emailPassword")}</strong>
                    {user?.email ? <span className="login-method-email">{user.email}</span> : null}
                    <span className="field-hint">{t("loginMethods.passwordAvailable")}</span>
                  </div>
                  <span className="login-method-badge"><Check size={12} aria-hidden="true" />{t("loginMethods.connectedBadge")}</span>
                </li>
              ) : null}
              {data.identities.map((identity) => {
                const provider = providers.find((item) => item.slug === identity.provider_slug);
                return (
                  <li className="login-method-card" key={identity.id}>
                    <span className="login-method-icon" style={provider ? loginMethodStyle(provider) : undefined}>
                      {provider?.icon_url ? <img src={provider.icon_url} width={20} height={20} alt="" /> : <ShieldCheck size={20} aria-hidden="true" />}
                    </span>
                    <div className="login-method-details">
                      <strong>{providerName(identity.provider_slug)}</strong>
                      {identity.last_email ? <span className="login-method-email">{t("loginMethods.providerProfile", { email: identity.last_email })}</span> : null}
                      {identity.last_login_at ? (
                        <span className="field-hint">{t("loginMethods.lastLogin", { date: formatVersionTimestamp(identity.last_login_at) })}</span>
                      ) : null}
                      {!provider ? <span className="field-hint">{t("loginMethods.providerUnavailable")}</span> : null}
                      {!identity.can_unlink ? <span className="field-hint">{t("loginMethods.lastMethod")}</span> : null}
                    </div>
                    <span className="login-method-badge">
                      <Check size={12} aria-hidden="true" />
                      {t(identity.provider_slug === user?.session_provider_slug ? "loginMethods.currentBadge" : "loginMethods.connectedBadge")}
                    </span>
                    <button type="button" className="icon-button login-method-unlink"
                      disabled={busy || !identity.can_unlink}
                      onClick={() => { setError(null); setPendingUnlink(identity); }}>
                      <Unlink size={14} aria-hidden="true" />{t("loginMethods.unlink")}
                    </button>
                  </li>
                );
              })}
            </ul>
            {data.identities.some((identity) => identity.last_email) ? (
              <p className="field-hint">{t("loginMethods.providerProfileHint")}</p>
            ) : null}
          </div>
          {canManage ? (
            <div className="login-methods-verification">
              <p className="account-panel-hint">{t("loginMethods.verifyHint")}</p>
              <IdentityVerificationPanel scope="account" returnTo="/account" />
            </div>
          ) : null}
          <section id="login-methods-add" className="login-methods-add" aria-labelledby="login-methods-add-title" tabIndex={-1}>
            <div className="login-methods-add-heading">
              <Plus size={18} aria-hidden="true" />
              <h3 id="login-methods-add-title">{t("loginMethods.add")}</h3>
            </div>
            <p className="account-panel-hint">{t("loginMethods.addDescription")}</p>
            {unlinkedProviders.length > 0 ? (
              <div className="login-methods-available">
                {unlinkedProviders.map((provider) => (
                  <button className="icon-button auth-provider-button" style={loginMethodStyle(provider)} type="button"
                    key={provider.slug} disabled={busy} onClick={() => void linkProvider(provider.slug)}>
                    {provider.icon_url ? <img src={provider.icon_url} width={20} height={20} alt="" /> : <ShieldCheck size={20} aria-hidden="true" />}
                    <span>{t("loginMethods.link", { provider: loginMethodLabel(provider, i18n.language) })}</span>
                    <Link2 size={16} aria-hidden="true" />
                  </button>
                ))}
              </div>
            ) : <p className="login-methods-empty">{t(providers.length > 0 ? "loginMethods.allConnected" : "loginMethods.noneAvailable")}</p>}
          {!user?.email && emailEnrollmentEnabled ? <form className="auth-form" onSubmit={(event) => void onConnectEmail(event)}>
            <p>{t("loginMethods.emailConnectionHint")}</p>
            <label className="field-stack">{t("account.accountEmail")}
              <input type="email" required autoComplete="email" {...textInputLimit(320)} value={accountEmailDraft} disabled={busy}
                onKeyDown={preventCompositionSubmit}
                onChange={(event) => setAccountEmailDraft(event.target.value)} />
            </label>
            <button type="submit" className="icon-button" disabled={busy}>{t("loginMethods.connectEmail")}</button>
          </form> : null}
          {!data.has_password && user?.email && capabilitiesChecked && passwordLoginEnabled ? (
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
            <p className="field-hint login-methods-managed-note">{t("loginMethods.managedByServer")}</p>
          </section>
        </>
      ) : !error ? (
        <p role="status">{t("loginMethods.loading")}</p>
      ) : null}
      {pendingUnlink ? (
        <Dialog labelledBy="unlink-login-method-title" className="login-method-unlink-dialog" dismissible={!busy}
          onClose={() => setPendingUnlink(null)}
          restoreFocusFallback={() => document.querySelector<HTMLElement>("#login-methods-add-action")}
          header={<h2 id="unlink-login-method-title">{t("loginMethods.unlinkTitle", { provider: providerName(pendingUnlink.provider_slug) })}</h2>}
          footer={<>
            <button type="button" className="icon-button" disabled={busy} onClick={() => setPendingUnlink(null)}>{t("loginMethods.cancel")}</button>
            <button type="button" className="icon-button login-method-unlink-confirm" disabled={busy}
              onClick={() => void unlinkIdentity(pendingUnlink.id)}>{t("loginMethods.confirmUnlink")}</button>
          </>}>
          <p>{t("loginMethods.unlinkDescription", { provider: providerName(pendingUnlink.provider_slug) })}</p>
          {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        </Dialog>
      ) : null}
    </section>
  );
}
