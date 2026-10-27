import { useState } from "react";
import { useTranslation } from "react-i18next";

import { requestEmailConflictRelease } from "../../shared/api/auth";
import { ApiError } from "../../shared/api/client";
import { StatusBanner } from "../../shared/components/StatusBanner";

type RequestState = "idle" | "busy" | "pending" | "resolved" | "expired" | "failed";

// A trusted SSO address matched an existing account whose email was never
// verified. No account is created until its owner links this provider or a
// server admin releases the address.
export function EmailConflictNotice({ passwordLoginEnabled }: { passwordLoginEnabled: boolean }) {
  const { t } = useTranslation("auth");
  const [state, setState] = useState<RequestState>("idle");

  async function requestRelease() {
    setState("busy");
    try {
      setState((await requestEmailConflictRelease()).status);
    } catch (caught) {
      setState(
        caught instanceof ApiError && caught.body.error_code === "email_conflict_ticket_invalid"
          ? "expired"
          : "failed",
      );
    }
  }

  return (
    <section className="auth-email-conflict" aria-labelledby="auth-email-conflict-title">
      <h2 id="auth-email-conflict-title">{t("emailConflict.title")}</h2>
      <p>{t(passwordLoginEnabled ? "emailConflict.mine" : "emailConflict.mineWithoutPassword")}</p>
      <p>{t("emailConflict.notMineHint")}</p>
      {state === "pending" ? <StatusBanner tone="info">{t("emailConflict.pending")}</StatusBanner> : null}
      {state === "resolved" ? <StatusBanner tone="info">{t("emailConflict.resolved")}</StatusBanner> : null}
      {state === "expired" ? <StatusBanner tone="warning">{t("emailConflict.expired")}</StatusBanner> : null}
      {state === "failed" ? <StatusBanner tone="warning">{t("emailConflict.failed")}</StatusBanner> : null}
      {state === "idle" || state === "busy" || state === "failed" ? (
        <button className="icon-button text-button" disabled={state === "busy"} type="button" onClick={() => void requestRelease()}>
          {t("emailConflict.request")}
        </button>
      ) : null}
    </section>
  );
}
