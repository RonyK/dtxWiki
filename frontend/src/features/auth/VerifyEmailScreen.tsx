import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { confirmEmailVerification } from "../../shared/api/auth";
import { ApiError } from "../../shared/api/client";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { useOneTimeLinkToken } from "./oneTimeLinkToken";

type VerificationState =
  | "checking"
  | "verified"
  | "invalid"
  | "rateLimited"
  | "serverError"
  | "offline";

export function VerifyEmailScreen() {
  const { t } = useTranslation("auth");
  const token = useOneTimeLinkToken();
  const [state, setState] = useState<VerificationState>(
    token ? "checking" : "invalid",
  );
  // The token the automatic verification already ran for, so a re-render
  // (or StrictMode's effect replay) does not submit a one-time token twice.
  const verifiedToken = useRef<string | null>(null);

  const verify = useCallback(async () => {
    setState("checking");
    try {
      await confirmEmailVerification({ token });
      setState("verified");
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.status === 429) {
          setState("rateLimited");
        } else if (error.status >= 500) {
          setState("serverError");
        } else {
          setState("invalid");
        }
      } else {
        setState("offline");
      }
    }
  }, [token]);

  useEffect(() => {
    if (!token || verifiedToken.current === token) {
      return;
    }
    verifiedToken.current = token;
    void verify();
  }, [token, verify]);

  const transient = ["rateLimited", "serverError", "offline"].includes(state);

  return (
    <div className="workspace-screen single-column auth-screen">
      <section className="feature-main auth-layout">
        <Breadcrumbs items={[{ label: t("verifyEmail.breadcrumb") }]} />
        <div className="feature-header">
          <div>
            <p className="eyebrow">{t("verifyEmail.eyebrow")}</p>
            <h1>{t("verifyEmail.title")}</h1>
          </div>
        </div>
        <section className="auth-panel" aria-label={t("verifyEmail.panelAria")}>
          <StatusBanner
            action={
              transient ? (
                <button
                  className="icon-button text-button"
                  onClick={() => void verify()}
                  type="button"
                >
                  {t("verifyEmail.retry")}
                </button>
              ) : undefined
            }
            tone={
              state === "verified" || state === "checking" ? "info" : "warning"
            }
          >
            {t(`verifyEmail.${state}`)}
          </StatusBanner>
          {state !== "checking" ? (
            <div className="auth-actions">
              {state === "invalid" ? (
                // Expired/already-used tokens are the most common failure and
                // have no retry affordance above (the banner action is
                // transient-only). Reissue requires a session (see
                // requestEmailVerification), so this points at /account
                // rather than pretending to reissue while signed out.
                <Link className="icon-button text-button" to="/account">
                  {t("verifyEmail.requestNew")}
                </Link>
              ) : null}
              <Link className="icon-button text-button" to="/login">
                {t("verifyEmail.back")}
              </Link>
            </div>
          ) : null}
        </section>
      </section>
    </div>
  );
}
