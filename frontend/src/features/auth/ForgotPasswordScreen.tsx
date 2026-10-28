import { Mail, RotateCw } from "lucide-react";
import { FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { requestPasswordReset } from "../../shared/api/auth";
import { apiErrorMessage } from "../../shared/api/client";
import { useAuth } from "../../shared/auth/AuthContext";
import { normalizeAccountEmail, rejectMultilinePaste, preventCompositionSubmit } from "../../shared/auth/identityPolicy";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { StatusBanner } from "../../shared/components/StatusBanner";

export function ForgotPasswordScreen() {
  const { t } = useTranslation("auth");
  const {
    capabilitiesChecked,
    capabilitiesError,
    passwordRecoveryMode,
    passwordLoginEnabled,
    refreshCapabilities,
  } = useAuth();
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [complete, setComplete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let normalizedEmail: string;
    try { normalizedEmail = normalizeAccountEmail(email); }
    catch (caught) { setError(t((caught as Error).message)); return; }
    setSubmitting(true);
    setError(null);
    try {
      await requestPasswordReset({ email: normalizedEmail });
      setComplete(true);
    } catch (caught) {
      setError(apiErrorMessage(caught, t("forgot.requestFailed")));
    } finally {
      setSubmitting(false);
    }
  }

  const emailRecoveryAvailable =
    capabilitiesChecked &&
    capabilitiesError === null &&
    passwordLoginEnabled &&
    passwordRecoveryMode === "email";

  return (
    <div className="workspace-screen single-column auth-screen">
      <section className="feature-main auth-layout">
        <Breadcrumbs items={[{ label: t("forgot.breadcrumb") }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("forgot.eyebrow")}</p>
            <h1>{t("forgot.title")}</h1>
          </div>
        </div>
        {capabilitiesError ? (
          <StatusBanner
            action={
              <button
                className="icon-button text-button"
                onClick={() => void refreshCapabilities()}
                type="button"
              >
                <RotateCw size={15} aria-hidden="true" />
                {t("forgot.retry")}
              </button>
            }
            tone="warning"
          >
            {t("forgot.capabilityFailed")}
          </StatusBanner>
        ) : null}
        {error ? <div id="forgot-input-error"><StatusBanner tone="warning">{error}</StatusBanner></div> : null}
        {complete ? (
          <StatusBanner tone="info">{t("forgot.complete")}</StatusBanner>
        ) : null}
        <section className="auth-panel" aria-label={t("forgot.formAria")}>
          {!capabilitiesChecked ? (
            <p className="auth-recovery-note">{t("forgot.checking")}</p>
          ) : emailRecoveryAvailable ? (
            complete ? (
              <div className="auth-actions">
                <Link className="icon-button text-button" to="/login">
                  {t("forgot.back")}
                </Link>
              </div>
            ) : (
              <form className="auth-form" onSubmit={onSubmit}>
                <p className="auth-recovery-note">{t("forgot.subtitle")}</p>
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
                    aria-describedby={error ? "forgot-input-error" : undefined}
                    onPaste={(event) => rejectMultilinePaste(event, () => setError(t("validation.email")))}
                    value={email}
                  />
                </label>
                <div className="auth-actions">
                  <button
                    className="icon-button primary"
                    disabled={submitting}
                    type="submit"
                  >
                    <Mail size={16} aria-hidden="true" />
                    {submitting ? t("forgot.submitting") : t("forgot.submit")}
                  </button>
                  <Link className="icon-button text-button" to="/login">
                    {t("forgot.back")}
                  </Link>
                </div>
              </form>
            )
          ) : (
            <>
              <StatusBanner tone="info">{t("forgot.operatorOnly")}</StatusBanner>
              <p className="auth-recovery-note">{t("forgot.unknownEmail")}</p>
              <div className="auth-actions">
                <Link className="icon-button text-button" to="/login">
                  {t("forgot.back")}
                </Link>
              </div>
            </>
          )}
        </section>
      </section>
    </div>
  );
}
