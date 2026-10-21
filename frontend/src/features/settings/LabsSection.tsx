import { FlaskConical } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useLabsPreferences } from "../../shared/hooks/useLabsPreferences";

export function LabsSection({ labelledBy }: { labelledBy: string }) {
  const { t } = useTranslation("settings");
  const { preferences, setMenuVisible, ready } = useLabsPreferences();

  return (
    <section aria-labelledby={labelledBy} className="feature-section">
      <div className="policy-card">
        <h2 className="metric-card-title">
          <FlaskConical aria-hidden="true" size={16} />
          <span>{t("labs.title")}</span>
        </h2>
        <p>{t("labs.description")}</p>
        <label className="checkbox-field">
          <input
            checked={preferences.ask}
            disabled={!ready}
            onChange={(event) => setMenuVisible("ask", event.target.checked)}
            type="checkbox"
          />
          <span>{t("labs.showAsk")}</span>
        </label>
        <label className="checkbox-field">
          <input
            checked={preferences.agent}
            disabled={!ready}
            onChange={(event) => setMenuVisible("agent", event.target.checked)}
            type="checkbox"
          />
          <span>{t("labs.showAgent")}</span>
        </label>
        <p className="field-hint">{t("labs.savedLocally")}</p>
      </div>
    </section>
  );
}
