import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { confirmPendingSignup, getPendingSignup, submitPendingSignupEmail, type AuthSession, type PendingSignup } from "../../shared/api/auth";
import { apiErrorMessage } from "../../shared/api/client";
import { useAuth } from "../../shared/auth/AuthContext";
import { normalizeAccountEmail, preventCompositionSubmit, rejectMultilinePaste } from "../../shared/auth/identityPolicy";
import { isNonSpaAuthPath, safeNextPath } from "../../shared/auth/navigation";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useOneTimeLinkToken } from "./oneTimeLinkToken";

export function ExternalSignupScreen() {
  const { t } = useTranslation("auth");
  const { refreshSession, setAuthenticatedSession } = useAuth();
  const navigate = useNavigate();
  const token = useOneTimeLinkToken();
  const [pending, setPending] = useState<PendingSignup | null>(null);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let active = true;
    setError(null);
    void getPendingSignup().then((data) => {
      if (!active) return;
      setPending(data);
      setEmail(data.email ?? "");
    }).catch((caught) => {
      if (active) setError(apiErrorMessage(caught, t("externalSignup.failed")));
    });
    return () => { active = false; };
  }, [revision, t]);

  function finish(session: AuthSession, target: string) {
    setAuthenticatedSession(session);
    const safeTarget = safeNextPath(target);
    if (isNonSpaAuthPath(safeTarget)) window.location.assign(safeTarget);
    else {
      void navigate(safeTarget, { replace: true });
      // The session response omits authoritative /me metadata such as the
      // external method and server-admin role. Refresh after the SPA return.
      void refreshSession();
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let normalizedEmail: string;
    try { normalizedEmail = normalizeAccountEmail(email); }
    catch (caught) { setError(t((caught as Error).message)); return; }
    setBusy(true);
    setError(null);
    try {
      const result = await submitPendingSignupEmail(normalizedEmail);
      if (result.status === "complete") {
        if (!result.session) throw new Error("Missing registration session");
        finish(result.session, result.return_to);
      } else setPending((current) => current ? { ...current, email: normalizedEmail, requested: true } : current);
    } catch (caught) { setError(apiErrorMessage(caught, t("externalSignup.failed"))); }
    finally { setBusy(false); }
  }

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const result = await confirmPendingSignup(token);
      finish(result.session, result.return_to);
    } catch (caught) { setError(apiErrorMessage(caught, t("externalSignup.failed"))); }
    finally { setBusy(false); }
  }

  return (
    <div className="workspace-screen single-column auth-screen">
      <section className="feature-main auth-layout">
        <Breadcrumbs items={[{ label: t("externalSignup.title") }]} />
        <h1>{t("externalSignup.title")}</h1>
        {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
        <section className="auth-panel">
          {!pending ? error ? (
            <div className="auth-actions">
              <button type="button" className="icon-button" onClick={() => setRevision((value) => value + 1)}>{t("verification.retry")}</button>
              <Link to="/login">{t("externalSignup.restart")}</Link>
            </div>
          ) : <p role="status">{t("externalSignup.loading")}</p> : token ? (
            <div className="auth-form">
              <p>{t("externalSignup.confirmHint", { email: pending.email })}</p>
              <button type="button" className="icon-button primary" disabled={busy} onClick={() => void confirm()}>{t("externalSignup.confirm")}</button>
            </div>
          ) : (
            <form className="auth-form" onSubmit={(event) => void submit(event)}>
              <p>{t(pending.email_confirmation_required ? "externalSignup.emailHint" : "externalSignup.registerHint")}</p>
              {pending.requested ? <StatusBanner tone="info">{t("externalSignup.sent")}</StatusBanner> : null}
              <label className="field-stack">
                {t("fields.email")}
                <input type="text" inputMode="email" autoComplete="email" required value={email}
                  onKeyDown={preventCompositionSubmit} onChange={(event) => setEmail(event.target.value)}
                  onPaste={(event) => rejectMultilinePaste(event, () => setError(t("validation.email")))} />
              </label>
              <button type="submit" className="icon-button primary" disabled={busy}>{t(pending.email_confirmation_required ? "externalSignup.send" : "externalSignup.register")}</button>
            </form>
          )}
        </section>
      </section>
    </div>
  );
}
