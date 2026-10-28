import { useTranslation } from "react-i18next";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import type { VerificationScope } from "../../shared/api/auth";
import { isNonSpaAuthPath, safeNextPath } from "../../shared/auth/navigation";
import { Breadcrumbs } from "../../shared/components/Breadcrumbs";
import { IdentityVerificationPanel } from "./IdentityVerificationPanel";
import { useOneTimeLinkToken } from "./oneTimeLinkToken";

export function IdentityVerificationScreen() {
  const { t } = useTranslation("auth");
  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const scope: VerificationScope = (params.get("scope") ?? new URLSearchParams(location.hash.slice(1)).get("scope")) === "admin" ? "admin" : "account";
  const token = useOneTimeLinkToken();
  const returnTo = safeNextPath(params.get("next") || "/account");

  function finish(_until: string | null, target = returnTo) {
    let safeTarget = safeNextPath(target);
    if (scope === "admin") {
      const url = new URL(safeTarget, window.location.origin);
      url.searchParams.set("sso_verified", "1");
      safeTarget = `${url.pathname}${url.search}${url.hash}`;
    }
    if (isNonSpaAuthPath(safeTarget)) window.location.assign(safeTarget);
    else void navigate(safeTarget, { replace: true });
  }

  return (
    <div className="workspace-screen single-column auth-screen">
      <section className="feature-main auth-layout">
        <Breadcrumbs items={[{ label: t("verification.title") }]} />
        <h1>{t("verification.title")}</h1>
        <section className="auth-panel">
          <IdentityVerificationPanel scope={scope} returnTo={returnTo} token={token} onVerified={finish} />
        </section>
      </section>
    </div>
  );
}
