import { type FormEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  confirmEmailReauthentication,
  getIdentityVerification,
  reauthenticate,
  requestEmailReauthentication,
  type IdentityVerification,
  type VerificationScope,
} from "../../shared/api/auth";
import { apiErrorMessage } from "../../shared/api/client";
import { useOptionalAuth } from "../../shared/auth/AuthContext";
import { preventCompositionSubmit, validateAccountPassword } from "../../shared/auth/identityPolicy";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { ExternalReauthenticationButtons } from "./ExternalReauthenticationButtons";

export function IdentityVerificationPanel({ scope, returnTo, token = "", onVerified }: {
  scope: VerificationScope;
  returnTo: string;
  token?: string;
  onVerified?: (until: string | null, returnTo?: string, mode?: IdentityVerification["mode"]) => void;
}) {
  const { t } = useTranslation("auth");
  const auth = useOptionalAuth();
  const [verification, setVerification] = useState<IdentityVerification | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const verifiedCallback = useRef(onVerified);
  useEffect(() => { verifiedCallback.current = onVerified; }, [onVerified]);

  useEffect(() => {
    let active = true;
    setVerification(null);
    setError(null);
    void getIdentityVerification(scope, returnTo).then((data) => {
      if (!active) return;
      setVerification(data);
      if (data.verified && !token) verifiedCallback.current?.(data.reauthenticated_until ?? null, undefined, data.mode);
    }).catch((caught) => {
      if (active) setError(apiErrorMessage(caught, t("verification.failed")));
    });
    return () => { active = false; };
  }, [scope, returnTo, revision, t, token]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try { await action(); }
    catch (caught) { setError(apiErrorMessage(caught, t("verification.failed"))); }
    finally { setBusy(false); }
  }

  function markVerified(until: string | null, target?: string) {
    setVerification((current) => current ? { ...current, verified: true, reauthenticated_until: until } : current);
    verifiedCallback.current?.(until, target, verification?.mode);
  }

  async function verifyPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try { validateAccountPassword(password); }
    catch (caught) { setError(t((caught as Error).message)); return; }
    await run(async () => {
      const result = await reauthenticate({ current_password: password });
      setPassword("");
      markVerified(result.reauthenticated_until);
    });
  }

  return (
    <div className="auth-form" aria-label={t("verification.panel")}>
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      {!verification ? error ? (
        <>
          <p>{t("verification.restartHint")}</p>
          <div className="auth-actions">
            <button type="button" className="icon-button" onClick={() => setRevision((value) => value + 1)}>{t("verification.retry")}</button>
            <a href="/login">{t("verification.restart")}</a>
          </div>
        </>
      ) : <p role="status">{t("verification.loading")}</p> : token ? (
        <>
          <p>{t("verification.confirmHint")}</p>
          <button type="button" className="icon-button primary" disabled={busy} onClick={() => void run(async () => {
            const result = await confirmEmailReauthentication(token, scope);
            markVerified(result.reauthenticated_until, result.return_to);
          })}>{t("verification.confirm")}</button>
        </>
      ) : verification.verified ? (
        <StatusBanner tone="info">{t(verification.mode === "session" ? "verification.sessionAccepted" : "verification.verified")}</StatusBanner>
      ) : <>
        {verification.allowed_methods.includes("sso") ? (
          <ExternalReauthenticationButtons methods={verification.providers} returnTo={returnTo} onError={setError} />
        ) : null}
        {verification.allowed_methods.includes("email") ? (
          <>
            <p>{t("verification.emailHint", { email: auth?.user?.email ?? t("verification.registeredEmail") })}</p>
            {requested ? <StatusBanner tone="info">{t("verification.sent")}</StatusBanner> : null}
            <button type="button" className="icon-button" disabled={busy} onClick={() => void run(async () => {
              await requestEmailReauthentication(scope, returnTo);
              setRequested(true);
            })}>{t(requested ? "verification.resend" : "verification.send")}</button>
          </>
        ) : null}
        {verification.allowed_methods.includes("password") ? (
        <form className="auth-form" onSubmit={(event) => void verifyPassword(event)}>
          <label className="field-stack">
            {t("password.current")}
            <input type="password" autoComplete="current-password" required value={password}
              onKeyDown={preventCompositionSubmit} onChange={(event) => setPassword(event.target.value)} />
          </label>
          <button type="submit" className="icon-button" disabled={busy}>{t("loginMethods.verifyPassword")}</button>
        </form>
        ) : null}
      </>}
    </div>
  );
}
