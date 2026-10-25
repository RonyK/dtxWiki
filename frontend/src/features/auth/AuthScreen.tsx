import { KeyRound, LogIn, UserPlus } from "lucide-react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { login, signup } from "../../shared/api/auth";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { useAuth } from "../../shared/auth/AuthContext";
import { normalizeAccountEmail, normalizeAccountName, validateAccountPassword, rejectMultilinePaste, preventCompositionSubmit } from "../../shared/auth/identityPolicy";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { StatusPill } from "../../shared/components/StatusPill";

type AuthMode = "login" | "signup";

// Backend-rendered routes outside the SPA (e.g. the OAuth consent page). SPA
// navigation to these would land on the router catch-all, so they need a
// full-page load instead.
const NON_SPA_PATH_PREFIXES = ["/oauth/"];

export function AuthScreen() {
  const { t } = useTranslation("auth");
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const requestedMode = searchParams.get("mode") === "signup" ? "signup" : "login";
  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(true);
  const {
    beginAuthentication,
    setAuthenticatedSession,
    status: authStatus,
    user: currentUser,
    refreshSession,
    sessionChecked,
    capabilitiesChecked,
    signupEnabled,
    passwordRecoveryMode,
    capabilitiesError,
    refreshCapabilities,
  } = useAuth();
  const submittedAuthRef = useRef(false);
  // A rejected credential attempt pins the visitor to this screen. The catch
  // below re-probes /auth/me to keep auth state honest, and that probe can
  // legitimately succeed against a *different*, still-valid cookie (someone
  // already signed in as A trying to switch to B). Without this the automatic
  // "you already have a session" redirect would fire on that result and carry
  // the visitor into the app as A, one round trip after the "wrong password"
  // banner appeared.
  const credentialFailureRef = useRef(false);
  const [status, setStatus] = useState<string | null>(() =>
    searchParams.get("reset") === "complete" ? t("status.passwordReset") : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const canSignup =
    capabilitiesChecked && capabilitiesError === null && signupEnabled;

  useEffect(() => {
    if (requestedMode === "login") {
      setMode("login");
      return;
    }
    if (canSignup) {
      setMode("signup");
      return;
    }
    setMode("login");
    if (capabilitiesChecked) {
      navigate(loginLocationWithoutSignupMode(searchParams), { replace: true });
    }
  }, [canSignup, capabilitiesChecked, navigate, requestedMode, searchParams]);

  // A failure (or success) banner from one form must not linger over the
  // other one after the login/signup toggle.
  function switchMode(nextMode: AuthMode) {
    if (nextMode === "signup" && !canSignup) {
      return;
    }
    setMode(nextMode);
    setStatus(null);
    setError(null);
    credentialFailureRef.current = false;
  }

  // Codex #26: /login skips the initial session check (AuthProvider
  // skipInitialSessionCheck), so a visitor who already holds a valid session
  // would be shown the signed-out form. Discover the session once on mount; the
  // redirect effect below then navigates an authenticated user to ?next.
  useEffect(() => {
    if (!sessionChecked && authStatus === "anonymous" && !submittedAuthRef.current) {
      void refreshSession();
    }
  }, [sessionChecked, authStatus, refreshSession]);

  useEffect(() => {
    if (
      authStatus === "authenticated" &&
      currentUser &&
      location.pathname === "/login" &&
      !submittedAuthRef.current &&
      !credentialFailureRef.current
    ) {
      const next = safeNextPath(searchParams.get("next"));
      if (isNonSpaPath(next)) {
        window.location.assign(next);
        return;
      }
      navigate(next, { replace: true });
    }
  }, [authStatus, currentUser, location.pathname, navigate, searchParams]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let normalizedEmail: string;
    let normalizedName: string | undefined;
    try {
      normalizedEmail = normalizeAccountEmail(email);
      normalizedName = mode === "signup" ? normalizeAccountName(displayName) : undefined;
      validateAccountPassword(password, mode === "signup");
    } catch (caught) {
      setError(t((caught as Error).message));
      return;
    }
    // Freeze automatic existing-session redirects and fence the pre-login
    // /auth/me probe before the credential request starts. Failures release
    // the guard and explicitly reconcile the surviving cookie below.
    submittedAuthRef.current = true;
    credentialFailureRef.current = false;
    beginAuthentication();
    setSubmitting(true);
    setError(null);
    setStatus(null);
    try {
      const session =
        mode === "signup"
          ? await signup({
              email: normalizedEmail,
              password,
              display_name: normalizedName,
              remember_me: rememberMe,
            })
          : await login({ email: normalizedEmail, password, remember_me: rememberMe });
      setAuthenticatedSession(session);
      setStatus(mode === "signup" ? t("status.created") : t("status.signedIn"));
      const next = searchParams.get("next");
      const target =
        mode === "signup" && !next ? "/dashboard?welcome=1" : safeNextPath(next);
      if (isNonSpaPath(target)) {
        window.location.assign(target);
      } else {
        navigate(target);
      }
    } catch (caught) {
      submittedAuthRef.current = false;
      credentialFailureRef.current = true;
      // The credential request may have failed while a valid cookie still
      // exists (or after an outcome-unknown network failure). The pre-submit
      // probe was deliberately fenced, so start a fresh authoritative check.
      void refreshSession();
      if (
        mode === "signup" &&
        caught instanceof ApiError &&
        caught.status === 403 &&
        caught.body.error_code === "signup_disabled"
      ) {
        setPassword("");
        setMode("login");
        setStatus(null);
        setError(null);
        // Not a rejected credential: signup was simply turned off. Nothing is
        // pinned to this screen, so restore the existing-session redirect.
        credentialFailureRef.current = false;
        navigate(loginLocationWithoutSignupMode(searchParams), { replace: true });
        void refreshCapabilities();
        return;
      }
      setError(errorMessage(caught, t("errors.failed")));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="workspace-screen single-column auth-screen">
      <section className="feature-main auth-layout">
        <Breadcrumbs items={[{ label: t("breadcrumb") }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("eyebrow")}</p>
            <h1>{mode === "signup" ? t("title.signup") : t("title.login")}</h1>
          </div>
          <StatusPill
            status={currentUser ? currentUser.status : "signed out"}
            label={currentUser ? undefined : t("pill.signedOut")}
          />
        </div>
        {status ? <StatusBanner tone="info">{status}</StatusBanner> : null}
        {error ? <div id="auth-input-error"><StatusBanner tone="warning">{error}</StatusBanner></div> : null}
        {capabilitiesChecked && capabilitiesError ? (
          <StatusBanner tone="warning">{t("capabilitiesUnavailable")}</StatusBanner>
        ) : null}
        {capabilitiesChecked && !capabilitiesError && !signupEnabled ? (
          <StatusBanner tone="info">{t("signupDisabled")}</StatusBanner>
        ) : null}
        <section className="auth-panel" aria-label={t("panelLabel")}>
          {canSignup ? (
            <SegmentedControl
              compact
              label={t("modeLabel")}
              options={[
                {
                  key: "login",
                  label: (
                    <>
                      <LogIn size={15} aria-hidden="true" />
                      {t("mode.login")}
                    </>
                  ),
                },
                {
                  key: "signup",
                  label: (
                    <>
                      <UserPlus size={15} aria-hidden="true" />
                      {t("mode.signup")}
                    </>
                  ),
                },
              ]}
              value={mode}
              onChange={switchMode}
            />
          ) : null}
          <form className="auth-form" onSubmit={onSubmit}>
            <label className="field-stack">
              {t("fields.email")}
              <input
                  onKeyDown={preventCompositionSubmit}
                autoComplete="email"
                name="email"
                onChange={(event) => setEmail(event.target.value)}
                required
                type="text"
                inputMode="email"
                aria-invalid={Boolean(error)}
                aria-describedby={error ? "auth-input-error" : undefined}
                onPaste={(event) => rejectMultilinePaste(event, () => setError(t("validation.email")))}
                value={email}
              />
            </label>
            {mode === "signup" ? (
              <label className="field-stack">
                {t("fields.displayName")}
                <input
                  onKeyDown={preventCompositionSubmit}
                  autoComplete="name"
                  name="displayName"
                  aria-describedby={error ? "display-name-rule auth-input-error" : "display-name-rule"}
                  aria-invalid={Boolean(error)}
                  onPaste={(event) => rejectMultilinePaste(event, () => setError(t("validation.displayName")))}
                  onChange={(event) => setDisplayName(event.target.value)}
                  type="text"
                  value={displayName}
                />
              </label>
            ) : null}
            {mode === "signup" ? <p id="display-name-rule" className="field-hint field-hint--attached">{t("validation.displayNameHint")}</p> : null}
            <label className="field-stack">
              {t("fields.password")}
              <input
                  onKeyDown={preventCompositionSubmit}
                aria-describedby={mode === "signup" ? (error ? "signup-password-rule auth-input-error" : "signup-password-rule") : (error ? "auth-input-error" : undefined)}
                aria-invalid={Boolean(error)}
                autoComplete={mode === "signup" ? "new-password" : "current-password"}
                name="password"
                onChange={(event) => setPassword(event.target.value)}
                required
                type="password"
                value={password}
              />
            </label>
            {mode === "signup" ? (
              // A sibling, not nested in the label: nesting it would fold
              // this text into the input's accessible name ("Password At
              // least 8 characters." instead of "Password"). Same pattern
              // and copy as AccountSecurityScreen's new-password-rule.
              <p className="field-hint field-hint--attached" id="signup-password-rule">
                {t("password.rule", { defaultValue: "8–128 characters." })}
              </p>
            ) : null}
            <label className="checkbox-field auth-remember">
              <input
                  onKeyDown={preventCompositionSubmit}
                checked={rememberMe}
                onChange={(event) => setRememberMe(event.target.checked)}
                type="checkbox"
              />
              <span>{t("fields.remember")}</span>
            </label>
            <div className="auth-actions">
              <button className="icon-button primary" disabled={submitting} type="submit">
                <KeyRound size={16} aria-hidden="true" />
                <span>
                  {mode === "signup" ? t("actions.signup") : t("actions.login")}
                </span>
              </button>
              {currentUser ? (
                <Link className="icon-button text-button" to="/account">
                  {t("actions.accountSecurity")}
                </Link>
              ) : null}
            </div>
            {mode === "signup" ? null : (
              <p className="auth-recovery-note">
                {capabilitiesChecked &&
                capabilitiesError === null &&
                passwordRecoveryMode === "email" ? (
                  <Link to="/forgot-password">{t("forgotPasswordLink")}</Link>
                ) : (
                  t("forgotPasswordOperator")
                )}
              </p>
            )}
          </form>
        </section>
      </section>
    </div>
  );
}

function safeNextPath(next: string | null) {
  if (!next || !next.startsWith("/") || next.startsWith("//")) {
    return "/dashboard";
  }
  return next;
}

function isNonSpaPath(path: string) {
  return NON_SPA_PATH_PREFIXES.some((prefix) => path.startsWith(prefix));
}

function loginLocationWithoutSignupMode(searchParams: URLSearchParams) {
  const next = new URLSearchParams(searchParams);
  next.delete("mode");
  const query = next.toString();
  return query ? `/login?${query}` : "/login";
}

function errorMessage(error: unknown, fallback: string) {
  return apiErrorMessage(error, fallback);
}
