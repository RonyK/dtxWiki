import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { StatusBanner } from "../../shared/components/StatusBanner";

export function LabsSection({ labelledBy, canManage, children }: {
  labelledBy: string;
  canManage: boolean;
  children: ReactNode;
}) {
  const { t } = useTranslation("settings");
  return (
    <section aria-labelledby={labelledBy} className="feature-section">
      {canManage ? children : <StatusBanner tone="info">{t("labs.ownerOnly")}</StatusBanner>}
    </section>
  );
}
