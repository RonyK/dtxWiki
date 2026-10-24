import { useState } from "react";
import { useTranslation } from "react-i18next";

import { startIdentityFlow, type LoginMethod } from "../../shared/api/auth";
import { apiErrorMessage } from "../../shared/api/client";
import { loginMethodStyle } from "./loginMethodBranding";

export function ExternalReauthenticationButtons({ methods, returnTo, onError }: {
  methods: LoginMethod[];
  returnTo: string;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation("auth");
  const [busy, setBusy] = useState(false);
  const available = methods.filter((method) => method.supports_reauthentication);

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

  if (available.length === 0) return null;

  return (
    <div className="auth-actions">
      {available.map((method) => (
        <button
          type="button"
          className="icon-button"
          style={loginMethodStyle(method)}
          disabled={busy}
          key={method.slug}
          onClick={() => void begin(method.slug)}
        >
          {t("loginMethods.verifyProvider", { provider: method.display_name })}
        </button>
      ))}
    </div>
  );
}
