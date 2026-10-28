import { useState } from "react";
import { useTranslation } from "react-i18next";

import { startIdentityFlow, type LoginMethod } from "../../shared/api/auth";
import { apiErrorMessage } from "../../shared/api/client";
import { loginMethodLabel, loginMethodStyle } from "./loginMethodBranding";

export function ExternalReauthenticationButtons({ methods, returnTo, onError }: {
  methods: LoginMethod[];
  returnTo: string;
  onError: (message: string) => void;
}) {
  const { t, i18n } = useTranslation("auth");
  const [busy, setBusy] = useState(false);

  async function begin(slug: string) {
    setBusy(true);
    try {
      const result = await startIdentityFlow(slug, "reauthenticate", returnTo);
      window.location.assign(result.redirect_url);
    } catch (error) {
      onError(apiErrorMessage(error, t("loginMethods.failed")));
    } finally {
      setBusy(false);
    }
  }

  if (methods.length === 0) return null;

  return (
    <div className="auth-actions">
      {methods.map((method) => (
        <button
          type="button"
          className="icon-button"
          style={loginMethodStyle(method)}
          disabled={busy}
          key={method.slug}
          onClick={() => void begin(method.slug)}
        >
          {t(method.supports_reauthentication ? "loginMethods.verifyProvider" : "loginMethods.confirmProvider", { provider: loginMethodLabel(method, i18n.language) })}
        </button>
      ))}
      {methods.some((method) => !method.supports_reauthentication) ? <p className="field-hint">{t("verification.ssoConfirmationHint")}</p> : null}
    </div>
  );
}
