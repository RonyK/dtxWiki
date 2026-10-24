import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { StatusBanner } from "../../shared/components/StatusBanner";

export const SSO_ERROR_CODES = new Set([
  "sso_transaction_invalid", "sso_assertion_invalid", "sso_provider_unavailable",
  "sso_idp_error", "account_link_required", "email_unverified", "identity_already_linked",
  "account_disabled", "signup_not_allowed", "reauthentication_mismatch", "rate_limited",
]);

export function ExternalAuthErrorBanner() {
  const { t } = useTranslation("auth");
  const [code] = useState(() => new URLSearchParams(window.location.search).get("auth_error"));
  useEffect(() => {
    if (!code) return;
    const url = new URL(window.location.href);
    url.searchParams.delete("auth_error");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }, [code]);
  return code ? <StatusBanner tone="warning">{t(`sso.errors.${SSO_ERROR_CODES.has(code) ? code : "unknown"}`)}</StatusBanner> : null;
}
