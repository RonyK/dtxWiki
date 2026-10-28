import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { StatusBanner } from "../../shared/components/StatusBanner";

export const SSO_ERROR_CODES = new Set([
  "sso_transaction_invalid", "sso_assertion_invalid", "sso_provider_unavailable",
  "sso_idp_error", "account_link_required", "email_unverified", "identity_already_linked",
  "account_disabled", "signup_not_allowed", "reauthentication_mismatch", "rate_limited",
  "account_email_conflict",
]);

// Account-page flows (link, reauthenticate) are already signed in: a trusted
// address owned by another account is a conflict, not a "sign in and link" step.
const ACCOUNT_CONTEXT_CODES: Record<string, string> = {
  account_link_required: "link_email_conflict",
};

export function ExternalAuthErrorBanner() {
  const { t } = useTranslation("auth");
  const [code] = useState(() => new URLSearchParams(window.location.search).get("auth_error"));
  useEffect(() => {
    if (!code) return;
    const url = new URL(window.location.href);
    url.searchParams.delete("auth_error");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }, [code]);
  const key = code && SSO_ERROR_CODES.has(code) ? (ACCOUNT_CONTEXT_CODES[code] ?? code) : "unknown";
  return code ? <StatusBanner tone="warning">{t(`sso.errors.${key}`)}</StatusBanner> : null;
}
